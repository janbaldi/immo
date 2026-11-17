"""Analyst: enrichment (EPC, flood, Statbel, permits), fair value, rent/yield and motivated-seller score."""

import asyncio
import logging

from sqlalchemy import select

from immo_agent import enrichment
from immo_agent.analysis import valuation
from immo_agent.core import db as dbm
from immo_agent.core.models import Property
from immo_agent.sources.http import Fetcher

log = logging.getLogger(__name__)


async def _enrich_batch(limit: int) -> int:
    with dbm.session() as db:
        props = list(
            db.scalars(
                select(Property)
                .where(Property.enriched_at.is_(None), Property.status != "removed")
                .order_by(Property.id.desc())
                .limit(limit)
            )
        )
        async with Fetcher() as fetcher:
            for p in props:
                await enrichment.enrich(db, fetcher, p)
                valuation.value_property(db, p)
                db.commit()
        return len(props)


def enrich_new(limit: int = 200) -> int:
    return asyncio.run(_enrich_batch(limit))


def nightly() -> None:
    """Retrain on real data, then refresh valuations of everything still for sale."""
    with dbm.session() as db:
        run = valuation.train(db)
        log.info("model retrained: %s", run and (run.version, run.mape, run.interval_coverage))
        for p in db.scalars(select(Property).where(Property.status.in_(("for_sale", "under_option")))):
            valuation.value_property(db, p)
        db.commit()
