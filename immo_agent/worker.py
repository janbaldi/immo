"""Background worker: all agents on one APScheduler, in its own process (`immo worker`)."""

import asyncio
import logging
from collections.abc import Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import select

from immo_agent import discovery, notify
from immo_agent.agents import analyst, matcher, ops_qa, outreach, scout
from immo_agent.core import db as dbm
from immo_agent.core.models import User
from immo_agent.sources.http import Fetcher

log = logging.getLogger(__name__)
_tg_offset: dict[str, int] = {}


def link_telegram() -> int:
    """Users press Start in the bot with their personal code: '/start <code>' links their chat."""
    linked = 0
    updates = notify.telegram_updates(_tg_offset.get("o"))
    with dbm.session() as db:
        for u in updates:
            _tg_offset["o"] = u["update_id"] + 1
            msg = u.get("message") or {}
            parts = (msg.get("text") or "").split()
            if len(parts) == 2 and parts[0] == "/start":
                user = db.scalars(select(User).where(User.telegram_link_code == parts[1])).first()
                if user:
                    user.telegram_chat_id, user.telegram_link_code = str(msg["chat"]["id"]), None
                    notify.telegram_send(user.telegram_chat_id, "✅ Immo Agent is connected. Alerts will arrive here.")
                    linked += 1
        db.commit()
    return linked


def _with_db(fn: Callable) -> Callable[[], None]:
    def job() -> None:
        with dbm.session() as db:
            fn(db)

    job.__name__ = getattr(fn, "__name__", "job")
    return job


async def _discover() -> None:
    with dbm.session() as db:
        async with Fetcher() as f:
            await discovery.discover(db, f)


async def _outreach() -> None:
    with dbm.session() as db:
        await outreach.send_approved(db)


def safe(fn: Callable[[], object]) -> Callable[[], None]:
    def job() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - one failing job must not stop the worker
            log.exception("job %s failed", getattr(fn, "__name__", fn))

    return job


JOBS: list[tuple[str, Callable[[], object], dict[str, object]]] = [
    ("scout", scout.tick, {"trigger": "interval", "minutes": 1}),
    ("matcher", _with_db(matcher.process), {"trigger": "interval", "minutes": 1}),
    ("pending_alerts", _with_db(matcher.flush_pending), {"trigger": "interval", "minutes": 5}),
    ("telegram_link", link_telegram, {"trigger": "interval", "minutes": 1}),
    ("analyst", analyst.enrich_new, {"trigger": "interval", "minutes": 5}),
    ("ops_qa", _with_db(ops_qa.run), {"trigger": "interval", "hours": 1}),
    ("outreach", lambda: asyncio.run(_outreach()), {"trigger": "interval", "minutes": 1}),
    ("full_crawl", scout.full_crawl, {"trigger": "cron", "hour": 2, "minute": 7}),
    ("retrain", analyst.nightly, {"trigger": "cron", "hour": 4, "minute": 13}),
    ("weekly", _with_db(ops_qa.weekly_summary), {"trigger": "cron", "day_of_week": "mon", "hour": 7, "minute": 41}),
    ("discover", lambda: asyncio.run(_discover()), {"trigger": "cron", "day_of_week": "sun", "hour": 3, "minute": 21}),
]


def build() -> BlockingScheduler:
    s = BlockingScheduler(timezone="Europe/Brussels", job_defaults={"coalesce": True, "max_instances": 1})
    for job_id, fn, trigger in JOBS:
        s.add_job(safe(fn), id=job_id, name=job_id, **trigger)  # type: ignore[arg-type]
    return s


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("Immo Agent worker starting")
    build().start()
