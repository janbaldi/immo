"""Ops/QA: hourly source health and the founder's weekly summary."""

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from immo_agent import notify
from immo_agent.core.db import utcnow
from immo_agent.core.models import Listing, Property, Source, SourceRun, SupportTicket, User

log = logging.getLogger(__name__)


def check_source(db: Session, source: Source) -> tuple[str, str]:
    now = utcnow()
    runs = list(
        db.scalars(
            select(SourceRun)
            .where(SourceRun.source_id == source.id, SourceRun.started_at > now - timedelta(hours=24))
            .order_by(SourceRun.id.desc())
        )
    )
    if not runs:
        return "red", "no runs in the last 24 h"
    if not runs[0].robots_allowed:
        return "red", "robots.txt disallows crawling"
    if all(r.error for r in runs[:3]):
        return "red", f"last runs failed: {runs[0].error}"
    # Rates come from the latest run that parsed pages, so a template change shows up on the next poll.
    latest = next((r for r in runs if r.details_fetched + r.parse_errors), runs[0])
    fetched, errors = latest.details_fetched, latest.parse_errors
    parse_err_rate = errors / max(1, fetched, errors)  # unusable pages count in both fetched and errors
    missing_rate = latest.missing_critical / max(1, fetched)
    prev = db.scalars(
        select(SourceRun.listings_found)
        .where(SourceRun.source_id == source.id, SourceRun.id < runs[0].id, SourceRun.error.is_(None))
        .order_by(SourceRun.id.desc())
        .limit(5)
    ).all()
    if runs[0].listings_found == 0 and prev and max(prev) > 0:
        return "red", "listing page returns no listings any more (template change?)"
    if parse_err_rate > 0.5:
        return "red", f"parse error rate {parse_err_rate:.0%}"
    if missing_rate > 0.5:
        return "red", f"critical fields missing on {missing_rate:.0%} of pages"
    if source.is_portal:
        return "green", "ok"
    new_24h = (
        db.scalar(
            select(func.count(Listing.id)).where(
                Listing.source_id == source.id, Listing.first_seen_at > now - timedelta(hours=24)
            )
        )
        or 0
    )
    week = (
        db.scalar(
            select(func.count(Listing.id)).where(
                Listing.source_id == source.id,
                Listing.first_seen_at > now - timedelta(days=7),
                Listing.first_seen_at <= now - timedelta(hours=24),
            )
        )
        or 0
    )
    avg = week / 6
    notes = []
    if avg >= 1 and new_24h < 0.5 * avg:
        notes.append(f"{new_24h} new in 24 h vs {avg:.1f}/day average")
    if parse_err_rate > 0.2:
        notes.append(f"parse errors {parse_err_rate:.0%}")
    if missing_rate > 0.3:
        notes.append(f"missing critical fields {missing_rate:.0%}")
    return ("amber", "; ".join(notes)) if notes else ("green", "ok")


def run(db: Session) -> dict[str, int]:
    counts = {"green": 0, "amber": 0, "red": 0}
    newly_red = []
    for source in db.scalars(select(Source).where(Source.enabled.is_(True), Source.review_status == "active")):
        health, note = check_source(db, source)
        if health == "red" and source.health != "red":
            newly_red.append(f"{source.name}: {note}")
        source.health, source.health_note = health, note
        counts[health] += 1
    db.commit()
    if newly_red:
        notify.notify_admin(f"Immo Agent: {len(newly_red)} source(s) turned red", "\n".join(newly_red))
    return counts


def weekly_summary(db: Session) -> str:
    from immo_agent.metrics import compute_all

    since = utcnow() - timedelta(days=7)
    m = compute_all(db, days=7)
    lines = [
        "Immo Agent weekly summary",
        f"New properties: {db.scalar(select(func.count(Property.id)).where(Property.first_seen_anywhere_at > since))}",
        f"Active users: {db.scalar(select(func.count(User.id)).where(User.active.is_(True)))}",
        f"Open tickets: {db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.status == 'open'))}",
        "Sources: " + ", ".join(f"{k} {v}" for k, v in run(db).items()),
        "",
    ] + [f"{v['title']}: {v['headline']}" for v in m.values()]
    body = "\n".join(lines)
    notify.notify_admin("Immo Agent weekly summary", body)
    return body
