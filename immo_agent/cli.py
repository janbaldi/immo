"""Command line: `immo <command>`. Run `immo --help` for the list."""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select

from immo_agent.core import db as dbm
from immo_agent.core.config import ROOT, get_settings


def cmd_init(_: argparse.Namespace) -> None:
    """Create/upgrade database tables and load config/sources.yaml."""
    from alembic import command
    from alembic.config import Config

    from immo_agent.sources.registry import sync_sources

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "immo_agent" / "migrations"))
    command.upgrade(cfg, "head")
    with dbm.session() as db:
        print(f"Database ready. {sync_sources(db)} configured sources loaded.")


def cmd_web(a: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("immo_agent.web.app:app", host=a.host, port=a.port, proxy_headers=True, forwarded_allow_ips="*")


def cmd_worker(_: argparse.Namespace) -> None:
    from immo_agent.worker import main

    main()


def cmd_admin(a: argparse.Namespace) -> None:
    from immo_agent.core.models import User

    with dbm.session() as db:
        user = db.scalars(select(User).where(User.email == a.email.lower())).first()
        if user is None:
            sys.exit(f"No user {a.email}. Sign up at {get_settings().base_url}/signup first.")
        user.is_admin = a.action == "create"
        db.commit()
        print(f"{user.email} is {'now' if user.is_admin else 'no longer'} an admin.")


def cmd_discover(_: argparse.Namespace) -> None:
    from immo_agent.discovery import discover
    from immo_agent.sources.http import Fetcher

    async def go() -> None:
        with dbm.session() as db:
            async with Fetcher() as f:
                found = await discover(db, f)
        for s in found:
            print(f"+ {s.kind:9} {s.name} {s.list_url}")
        print(f"{len(found)} new agency sources queued for review (Admin → Sources).")

    asyncio.run(go())


def cmd_run(a: argparse.Namespace) -> None:
    from immo_agent.agents import scout
    from immo_agent.core.models import Source

    with dbm.session() as db:
        q = select(Source.id).where(Source.review_status == "active", Source.enabled.is_(True))
        ids = [a.source] if a.source else list(db.scalars(q.where(Source.is_portal.is_(False))))
    for run in asyncio.run(scout.run_many(ids, full=a.full or None)):
        print(
            f"source {run.source_id}: found {run.listings_found}, new {run.new_count}, changed {run.changed_count}, "
            f"errors {run.parse_errors}{' ERROR ' + run.error if run.error else ''}"
        )


def cmd_check_live(a: argparse.Namespace) -> None:
    from immo_agent import notify
    from immo_agent.agents import scout
    from immo_agent.core.models import Source, User
    from immo_agent.sources.registry import sync_sources

    with dbm.session() as db:
        sync_sources(db)
        q = select(Source).where(Source.review_status == "active", Source.enabled.is_(True))
        if not get_settings().portal_crosscheck_enabled:
            q = q.where(Source.is_portal.is_(False))
        sources = {s.id: s for s in db.scalars(q.limit(a.limit))}
    print(f"Checking {len(sources)} live sources (polite: max 1 request per {get_settings().min_request_interval}s per site)...")
    runs = asyncio.run(scout.run_many(list(sources), full=False))
    ok = 0
    print(f"{'source':40} {'kind':9} {'robots':7} {'found':>5} {'parsed':>6} {'ok%':>5} {'missing':>7}  status")
    for r in runs:
        s = sources[r.source_id]
        attempts = r.details_fetched + r.parse_errors
        rate = (r.details_fetched - r.missing_critical) / attempts if attempts else None
        good = r.robots_allowed and not r.error and r.listings_found > 0 and (rate is None or rate >= 0.8)
        ok += good
        shown_rate = "-" if rate is None else format(rate, ".0%")
        status = "OK" if good else "FAIL " + (r.error or "")[:60]
        print(
            f"{s.name[:40]:40} {s.kind:9} {'ok' if r.robots_allowed else 'BLOCKED':7} {r.listings_found:>5} "
            f"{r.details_fetched:>6} {shown_rate:>5} {r.missing_critical:>7}  {status}"
        )
    print(f"\n{ok}/{len(runs)} sources OK (need {a.min_ok}).")

    msg = "✅ Immo Agent check-live: alerts reach this channel."
    with dbm.session() as db:
        chats = list(db.scalars(select(User.telegram_chat_id).where(User.is_admin.is_(True), User.telegram_chat_id.is_not(None))))
    tg = any(notify.telegram_send(c, msg) for c in chats if c)
    mail = notify.send_email(get_settings().admin_email, "Immo Agent check-live", msg)
    print(f"Telegram alert to admin: {'OK' if tg else 'FAIL (set TELEGRAM_BOT_TOKEN and connect Telegram in Settings)'}")
    print(f"E-mail alert to ADMIN_EMAIL: {'OK' if mail else 'FAIL (set SMTP_* and ADMIN_EMAIL in .env)'}")
    sys.exit(0 if ok >= a.min_ok and tg and mail else 1)


def cmd_report(a: argparse.Namespace) -> None:
    from immo_agent.report import build

    dead = None
    if a.check_links:
        dead = asyncio.run(_dead_links(a.days))
    with dbm.session() as db:
        text, passed = build(db, a.days, dead)
    out = Path("reports")
    out.mkdir(exist_ok=True)
    path = out / f"acceptance-{dbm.utcnow():%Y%m%d}.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nSaved to {path}")
    sys.exit(0 if passed else 1)


async def _dead_links(days: int) -> list[str]:
    from datetime import timedelta

    from immo_agent.core.models import Alert, Listing
    from immo_agent.sources.http import Fetcher

    with dbm.session() as db:
        urls = set(
            db.scalars(
                select(Listing.url)
                .join(Alert, Alert.listing_id == Listing.id)
                .where(Alert.created_at >= dbm.utcnow() - timedelta(days=days))
            )
        )
    dead = []
    async with Fetcher() as f:
        for url in urls:
            try:
                if (await f.get(url)).status >= 400:
                    dead.append(url)
            except Exception:  # noqa: BLE001
                dead.append(url)
    return dead


def cmd_train(_: argparse.Namespace) -> None:
    from immo_agent.agents.analyst import nightly

    nightly()
    from immo_agent.metrics import model_quality

    with dbm.session() as db:
        print(model_quality(db)["headline"])


def cmd_statbel(a: argparse.Namespace) -> None:
    from immo_agent.enrichment import import_statbel
    from immo_agent.sources.http import Fetcher

    async def go() -> int:
        with dbm.session() as db:
            async with Fetcher() as f:
                return await import_statbel(db, f, a.url)

    print(f"Imported {asyncio.run(go())} Statbel rows.")


def cmd_parse(a: argparse.Namespace) -> None:
    from immo_agent.sources.extract import extract_listing
    from immo_agent.sources.http import Fetcher

    async def go() -> None:
        async with Fetcher() as f:
            page = await f.get(a.url)
        raw = extract_listing(page.text, page.url).clean()
        print(json.dumps(asdict(raw), indent=2, default=str, ensure_ascii=False))
        print("missing critical fields:", raw.missing_critical() or "none")

    asyncio.run(go())


def cmd_record(a: argparse.Namespace) -> None:
    """Save a live page as a test fixture (a recorded copy of a real page)."""
    from immo_agent.sources.http import Fetcher

    async def go() -> None:
        async with Fetcher() as f:
            page = await f.get(a.url)
        path = ROOT / "tests" / "fixtures" / "recorded" / f"{a.name}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"<!-- recorded from {page.url} on {dbm.utcnow():%Y-%m-%d} -->\n{page.text}", encoding="utf-8")
        print(f"Saved {path} ({len(page.text)} chars)")

    asyncio.run(go())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="immo", description="Immo Agent command line")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create/upgrade the database and load config/sources.yaml").set_defaults(fn=cmd_init)
    w = sub.add_parser("web", help="start the web app")
    w.add_argument("--host", default="0.0.0.0")
    w.add_argument("--port", type=int, default=8000)
    w.set_defaults(fn=cmd_web)
    sub.add_parser("worker", help="start the background agents").set_defaults(fn=cmd_worker)
    ad = sub.add_parser("admin", help="grant or remove admin rights")
    ad.add_argument("action", choices=["create", "remove"])
    ad.add_argument("email")
    ad.set_defaults(fn=cmd_admin)
    sub.add_parser("discover", help="find agency websites and queue them for review").set_defaults(fn=cmd_discover)
    r = sub.add_parser("run", help="poll sources once now")
    r.add_argument("--source", type=int)
    r.add_argument("--full", action="store_true", help="crawl the full inventory")
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser("check-live", help="run the pipeline once against live sources and test alerts")
    c.add_argument("--min-ok", type=int, default=100)
    c.add_argument("--limit", type=int, default=1000)
    c.set_defaults(fn=cmd_check_live)
    rp = sub.add_parser("report", help="acceptance report from live data")
    rp.add_argument("--days", type=int, default=14)
    rp.add_argument("--check-links", action="store_true", help="also fetch every alerted source URL")
    rp.set_defaults(fn=cmd_report)
    sub.add_parser("train", help="retrain the fair-value model now").set_defaults(fn=cmd_train)
    st = sub.add_parser("statbel-import", help="download Statbel municipality prices")
    st.add_argument("--url")
    st.set_defaults(fn=cmd_statbel)
    pa = sub.add_parser("parse", help="fetch one listing URL and print what the parser extracts")
    pa.add_argument("url")
    pa.set_defaults(fn=cmd_parse)
    rec = sub.add_parser("record-fixture", help="save a live page as a test fixture")
    rec.add_argument("url")
    rec.add_argument("name")
    rec.set_defaults(fn=cmd_record)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
