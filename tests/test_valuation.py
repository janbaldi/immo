import random
from datetime import timedelta

from immo_agent.analysis import valuation
from immo_agent.core.db import utcnow
from immo_agent.core.models import Listing, ListingEvent, MunicipalityStat, Property, Source


def seed(db, n=160):
    """Numeric rows for testing the model code (not listings shown to users)."""
    rng = random.Random(1)
    for i in range(n):
        muni, ppm2 = ("Antwerpen", 3000) if i % 4 else ("Mortsel", 2600)
        surface = rng.randint(60, 200)
        db.add(
            Property(
                municipality=muni,
                property_type="house",
                surface=surface,
                bedrooms=surface // 40,
                price=int(surface * ppm2 * rng.uniform(0.9, 1.1)),
                epc_score=rng.randint(80, 500),
            )
        )
    db.add(MunicipalityStat(municipality="Antwerpen", year=2025, property_type="house", median=330000))
    db.commit()


def test_train_and_value(db):
    assert valuation.train(db) is None, "needs at least 100 rows"
    seed(db)
    run = valuation.train(db)
    assert run.n_test == 32 and run.mape < 0.15 and 0.4 <= run.interval_coverage <= 1
    p = Property(municipality="Antwerpen", property_type="house", surface=100, bedrooms=2, price=300000)
    db.add(p)
    db.flush()
    v = valuation.value_property(db, p)
    assert v.low <= v.fair_value <= v.high and 240000 < v.fair_value < 360000
    small = Property(municipality="Lille", property_type="house", surface=100, price=250000)
    db.add(small)
    db.flush()
    assert valuation.value_property(db, small).note.startswith("insufficient data")


def test_no_model_yet(db):
    p = Property(municipality="Antwerpen", property_type="house", surface=100, price=1)
    db.add(p)
    db.flush()
    assert valuation.value_property(db, p).note == "no model trained yet"


def test_rent_yield_and_signals(db):
    db.add(Source(id=1, name="A", kind="zabun", base_url="https://a.test", list_url="https://a.test/x"))
    p = Property(
        municipality="Berchem",
        property_type="apartment",
        surface=80,
        price=240000,
        first_seen_anywhere_at=utcnow() - timedelta(days=60),
    )
    db.add(p)
    db.flush()
    for i in range(10):
        db.add(
            Listing(
                property_id=p.id,
                source_id=1,
                url=f"https://a.test/r{i}",
                transaction="rent",
                municipality="Berchem",
                price=1000 + i * 20,
                surface=80,
            )
        )
    db.flush()
    lst = db.query(Listing).first()
    db.add_all([ListingEvent(listing_id=lst.id, property_id=p.id, type=t) for t in ("price_drop", "price_drop", "relisted")])
    db.commit()
    rent = valuation.rent_estimate(db, p)
    assert 1000 <= rent <= 1200
    assert valuation.gross_yield(rent, p.price) == round(rent * 12 / 240000, 4)
    assert valuation.motivated_score(db, p) == 20 + 40 + 20
    assert len(valuation.price_history(db, p)) == 3
