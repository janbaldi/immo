"""Fair value with an 80% interval (10th-90th percentile) from quantile gradient boosting.

Trained only on real collected asking prices plus Statbel municipality medians. Segments with fewer
than MIN_COMPARABLES comparables get no value ("insufficient data") instead of a guess.
"""

import logging
import math
import pickle
import random
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from immo_agent.core.config import get_settings
from immo_agent.core.db import utcnow
from immo_agent.core.models import Listing, ListingEvent, ModelRun, MunicipalityStat, Property, Valuation

log = logging.getLogger(__name__)
QUANTILES = (0.1, 0.5, 0.9)
TYPES = ("house", "apartment")
MIN_RENTALS = 10


@dataclass
class Model:
    version: str
    muni_ppm2: dict[str, float]  # median log(price/m²) per municipality from training data
    global_ppm2: float
    statbel: dict[str, float]
    regressors: dict[float, Any]

    def features(self, p: Property) -> list[float]:
        muni = (p.municipality or "").lower()
        return [
            math.log(p.surface or 1),
            float(p.bedrooms) if p.bedrooms is not None else np.nan,
            float(p.epc_score) if p.epc_score is not None else np.nan,
            float(TYPES.index(p.property_type)) if p.property_type in TYPES else 2.0,
            self.muni_ppm2.get(muni, self.global_ppm2),
            math.log(self.statbel[muni]) if muni in self.statbel else np.nan,
        ]

    def predict(self, p: Property) -> tuple[float, float, float]:
        x = np.array([self.features(p)])
        lo, mid, hi = (math.exp(float(self.regressors[q].predict(x)[0])) for q in QUANTILES)
        return min(lo, mid), mid, max(hi, mid)


def training_rows(db: Session) -> list[Property]:
    return list(
        db.scalars(
            select(Property).where(
                Property.price.is_not(None),
                Property.surface > 10,
                Property.is_auction.is_(False),
                Property.property_type.in_(("house", "apartment")),
            )
        )
    )


def _fit(rows: list[Property], statbel: dict[str, float], version: str) -> Model:
    by_muni: dict[str, list[float]] = {}
    for p in rows:
        by_muni.setdefault((p.municipality or "").lower(), []).append(math.log(p.price / p.surface))  # type: ignore[operator]
    glob = median(v for vals in by_muni.values() for v in vals)
    muni = {k: median(v) for k, v in by_muni.items() if len(v) >= 5}
    model = Model(version, muni, glob, statbel, {})
    x = np.array([model.features(p) for p in rows])
    y = np.array([math.log(p.price) for p in rows])  # type: ignore[arg-type]
    for q in QUANTILES:
        model.regressors[q] = HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=200, min_samples_leaf=10).fit(
            x, y
        )
    return model


def statbel_medians(db: Session) -> dict[str, float]:
    latest = db.scalar(select(func.max(MunicipalityStat.year)))
    rows = db.scalars(select(MunicipalityStat).where(MunicipalityStat.year == latest, MunicipalityStat.median.is_not(None)))
    return {r.municipality.lower(): float(r.median) for r in rows if r.median}  # type: ignore[arg-type]


def train(db: Session, seed: int = 0) -> ModelRun | None:
    path = Path(get_settings().model_path)
    rows = training_rows(db)
    if len(rows) < 100:
        log.info("Not enough real listings to train (%d < 100)", len(rows))
        return None
    statbel = statbel_medians(db)
    random.Random(seed).shuffle(rows)
    cut = int(len(rows) * 0.8)
    version = utcnow().strftime("%Y%m%d%H%M")
    test_model = _fit(rows[:cut], statbel, version)
    errors, covered = [], 0
    for p in rows[cut:]:
        lo, mid, hi = test_model.predict(p)
        errors.append(abs(mid - p.price) / p.price)  # type: ignore[operator]
        covered += lo <= p.price <= hi  # type: ignore[operator]
    run = ModelRun(
        version=version,
        n_train=cut,
        n_test=len(rows) - cut,
        mape=float(np.mean(errors)),
        interval_coverage=covered / (len(rows) - cut),
    )
    db.add(run)
    db.commit()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(_fit(rows, statbel, version)))  # final model uses all rows
    _cache.clear()
    return run


_cache: dict[str, Model] = {}


def load_model() -> Model | None:
    path = Path(get_settings().model_path)
    if "m" not in _cache and path.exists():
        _cache["m"] = pickle.loads(path.read_bytes())  # our own file, written by train()
    return _cache.get("m")


def comparables(db: Session, p: Property) -> int:
    return (
        db.scalar(
            select(func.count(Property.id)).where(
                Property.municipality == p.municipality,
                Property.property_type == p.property_type,
                Property.price.is_not(None),
                Property.surface.is_not(None),
                Property.id != p.id,
            )
        )
        or 0
    )


def rent_estimate(db: Session, p: Property) -> float | None:
    if not p.surface or not p.municipality:
        return None
    rents = db.execute(
        select(Listing.price, Listing.surface).where(
            Listing.transaction == "rent",
            Listing.municipality == p.municipality,
            Listing.price.is_not(None),
            Listing.surface > 10,
            Listing.price < 20000,
        )
    ).all()
    if len(rents) < MIN_RENTALS:
        return None
    return round(median(price / surface for price, surface in rents) * p.surface)  # type: ignore[operator]


def gross_yield(rent: float | None, price: int | None) -> float | None:
    return round(rent * 12 / price, 4) if rent and price else None


def motivated_score(db: Session, p: Property) -> int:
    """0-100: long on market, repeated price cuts, relisting."""
    events = db.scalars(select(ListingEvent.type).where(ListingEvent.property_id == p.id)).all()
    days = (utcnow() - p.first_seen_anywhere_at).days
    cuts, relisted = events.count("price_drop"), events.count("relisted")
    return min(100, min(40, days // 3) + min(40, 20 * cuts) + min(20, 20 * relisted))


def price_history(db: Session, p: Property) -> list[tuple[Any, str, str | None, str | None]]:
    rows = db.execute(
        select(ListingEvent.at, ListingEvent.type, ListingEvent.old_value, ListingEvent.new_value)
        .where(ListingEvent.property_id == p.id)
        .order_by(ListingEvent.at)
    ).all()
    return [tuple(r) for r in rows]  # type: ignore[misc]


def value_property(db: Session, p: Property) -> Valuation:
    v = Valuation(property_id=p.id, asking_price=p.price, motivated_score=motivated_score(db, p))
    v.rent_estimate = rent_estimate(db, p)
    v.gross_yield = gross_yield(v.rent_estimate, p.price)
    v.n_comparables = comparables(db, p)
    model = load_model()
    if model is None:
        v.note = "no model trained yet"
    elif v.n_comparables < get_settings().min_comparables:
        v.note = f"insufficient data ({v.n_comparables} comparables)"
    elif not p.surface or p.property_type not in TYPES:
        v.note = "insufficient data (surface or type unknown)"
    else:
        v.low, v.fair_value, v.high = (round(x, -3) for x in model.predict(p))
        v.model_version = model.version
    db.add(v)
    return v


def latest_valuation(db: Session, property_id: int) -> Valuation | None:
    return db.scalars(
        select(Valuation).where(Valuation.property_id == property_id).order_by(Valuation.id.desc()).limit(1)
    ).first()
