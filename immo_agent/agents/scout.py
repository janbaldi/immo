"""Scout: polls sources, fetches details only for new or changed cards, normalises, deduplicates, stores."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from immo_agent.agents import matcher
from immo_agent.analysis import changes, dedup, valuation
from immo_agent.core import db as dbm
from immo_agent.core.config import get_settings
from immo_agent.core.db import utcnow
from immo_agent.core.models import Listing, ListingEvent, Property, Source, SourceRun
from immo_agent.sources.base import IndexItem
from immo_agent.sources.extract import RawListing
from immo_agent.sources.http import Fetcher, RobotsDisallowed
from immo_agent.sources.registry import connector_for

log = logging.getLogger(__name__)
PRICE_EVENTS = ("new", "price_drop", "price_increase", "relisted")


async def _photo_hash(fetcher: Fetcher, url: str) -> str | None:
    try:
        page = await fetcher.get(url)
        return dedup.photo_hash(page.content) if page.status < 400 else None
    except Exception as e:  # noqa: BLE001 - a missing photo must not block storing the listing
        log.info("photo hash failed for %s: %s", url, e)
        return None


async def _confirm_fuzzy(db: Session, fetcher: Fetcher, prop: Property, raw: RawListing) -> bool:
    """A fuzzy match is kept unless both first photos exist and clearly differ."""
    other = next((lst for lst in prop.listings if lst.photos), None)
    if not raw.photos or other is None:
        return True
    if prop.photo_hash is None:
        prop.photo_hash = await _photo_hash(fetcher, other.photos[0])
    new_hash = await _photo_hash(fetcher, raw.photos[0])
    if prop.photo_hash is None or new_hash is None:
        return True
    return dedup.hamming(prop.photo_hash, new_hash) <= dedup.PHASH_MAX_DISTANCE


async def store(
    db: Session, source: Source, item: IndexItem, raw: RawListing, listing: Listing | None, fetcher: Fetcher
) -> tuple[Listing, list[ListingEvent]]:
    now = utcnow()
    if listing is None:
        prop, method = dedup.find_property(db, raw) if raw.transaction == "sale" else (None, "none")
        if prop is not None and method == "fuzzy" and not await _confirm_fuzzy(db, fetcher, prop, raw):
            prop = None
        if prop is None:
            prop = Property(address_norm=dedup.normalise_address(raw.street, raw.postal_code), first_seen_anywhere_at=now)
            db.add(prop)
            db.flush()
        listing = Listing(property_id=prop.id, source_id=source.id, url=item.url, external_id=raw.external_id, first_seen_at=now)
        changes.fill(listing, raw)
        db.add(listing)
        db.flush()
        events = [changes.event(db, listing, "new", None, raw.price)]
    else:
        prop = listing.property
        events = changes.apply_update(db, listing, raw)
    listing.card_hash = item.card_hash
    changes.sync_property(prop, listing, raw)
    return listing, events


async def run_source(db: Session, source: Source, fetcher: Fetcher, full: bool | None = None) -> SourceRun:
    if source.is_portal:
        return await run_portal(db, source, fetcher)
    backfill = source.backfilled_at is None
    full = backfill if full is None else full
    run = SourceRun(source_id=source.id, full_crawl=full)
    db.add(run)
    db.flush()
    connector = connector_for(source, fetcher)
    to_value: set[int] = set()
    try:
        items = await connector.index(full)
        run.listings_found = len(items)
        existing = {lst.url: lst for lst in db.scalars(select(Listing).where(Listing.source_id == source.id))}
        seen: set[int] = set()
        for item in items:
            listing = existing.get(item.url)
            if listing and listing.card_hash == item.card_hash and listing.status != "removed":
                listing.last_seen_at = utcnow()
                seen.add(listing.id)
                continue
            try:
                raw = (await connector.detail(item)).clean()
                run.details_fetched += 1
            except RobotsDisallowed:
                continue
            except Exception as e:  # noqa: BLE001 - one bad page must not stop the run
                log.info("detail failed %s: %s", item.url, e)
                run.parse_errors += 1
                continue
            if raw.missing_critical():
                run.missing_critical += 1
            if not raw.price and not raw.municipality:
                run.parse_errors += 1
                continue
            listing, events = await store(db, source, item, raw, listing, fetcher)
            seen.add(listing.id)
            run.new_count += any(ev.type == "new" for ev in events)
            run.changed_count += any(ev.type != "new" for ev in events)
            if backfill:
                for ev in events:
                    ev.matched = True  # the initial backfill fills the feed but does not alert
            if any(ev.type in PRICE_EVENTS for ev in events):
                to_value.add(listing.property_id)
        if full and run.listings_found:
            for lst in existing.values():
                if lst.id not in seen and lst.status != "removed":
                    changes.mark_removed(db, lst)
                    lst.property.status = (
                        "removed" if all(x.status == "removed" for x in lst.property.listings) else lst.property.status
                    )
        if full:
            source.backfilled_at = source.backfilled_at or utcnow()
    except RobotsDisallowed:
        run.robots_allowed, run.error = False, "robots.txt disallows the listing page"
    except Exception as e:  # noqa: BLE001 - recorded for Ops/QA
        log.warning("source %s failed: %s", source.name, e)
        run.error = repr(e)[:500]
    db.flush()
    for pid in to_value:
        valuation.value_property(db, db.get(Property, pid))  # type: ignore[arg-type]
    run.finished_at = source.last_run_at = utcnow()
    source.next_poll_at = utcnow() + timedelta(minutes=source.poll_minutes)
    db.commit()
    if not backfill:
        matcher.process(db)
    return run


async def run_portal(db: Session, source: Source, fetcher: Fetcher) -> SourceRun:
    """Cross-check only: store portal_first_seen_at and the portal URL for properties we already know."""
    run = SourceRun(source_id=source.id)
    db.add(run)
    try:
        items = await connector_for(source, fetcher).index()
        run.listings_found = len(items)
        for item in items:
            assert item.raw is not None
            prop, _ = dedup.find_property(db, item.raw)
            if prop is not None and prop.portal_first_seen_at is None:
                prop.portal_first_seen_at, prop.portal_url = utcnow(), item.url
                run.new_count += 1
    except Exception as e:  # noqa: BLE001
        run.error = repr(e)[:500]
    run.finished_at = source.last_run_at = utcnow()
    source.next_poll_at = utcnow() + timedelta(minutes=source.poll_minutes)
    db.commit()
    return run


def due_sources(db: Session) -> list[Source]:
    now = utcnow()
    q = select(Source).where(Source.enabled.is_(True), Source.review_status == "active")
    if not get_settings().portal_crosscheck_enabled:
        q = q.where(Source.is_portal.is_(False))
    return [s for s in db.scalars(q) if s.next_poll_at is None or s.next_poll_at <= now]


async def run_many(source_ids: list[int], full: bool | None = None, concurrency: int = 20) -> list[SourceRun]:
    sem = asyncio.Semaphore(concurrency)
    async with Fetcher() as fetcher:

        async def one(sid: int) -> SourceRun:
            async with sem:
                with dbm.session() as db:
                    return await run_source(db, db.get(Source, sid), fetcher, full)  # type: ignore[arg-type]

        return list(await asyncio.gather(*(one(s) for s in source_ids)))


def tick() -> None:
    with dbm.session() as db:
        ids = [s.id for s in due_sources(db)]
    if ids:
        asyncio.run(run_many(ids))


def full_crawl() -> None:
    """Nightly: crawl full inventories so removed listings are detected."""
    with dbm.session() as db:
        ids = [
            s.id
            for s in db.scalars(
                select(Source).where(Source.enabled.is_(True), Source.review_status == "active", Source.is_portal.is_(False))
            )
        ]
    asyncio.run(run_many(ids, full=True))
