from datetime import timedelta

from immo_agent import metrics
from immo_agent.core.config import get_settings
from immo_agent.core.db import utcnow
from immo_agent.core.models import (
    Alert,
    ContactRequest,
    Listing,
    ModelRun,
    Property,
    Purchase,
    Source,
    User,
    UserAction,
    Valuation,
)


def setup(db):
    now = utcnow()
    src = Source(
        name="A", kind="zabun", base_url="https://a.test", list_url="https://a.test/x", backfilled_at=now - timedelta(days=5)
    )
    u = User(email="u@example.com", password_hash="x")
    db.add_all([src, u])
    db.flush()
    props = []
    for i, (delay, portal_after) in enumerate([(5, 60), (10, None), (30, -20)]):
        p = Property(
            first_seen_anywhere_at=now - timedelta(hours=2),
            price=300000 + i,
            portal_first_seen_at=None if portal_after is None else now - timedelta(hours=2) + timedelta(minutes=portal_after),
        )
        db.add(p)
        db.flush()
        lst = Listing(
            property_id=p.id,
            source_id=src.id,
            url=f"https://a.test/{i}",
            first_seen_at=p.first_seen_anywhere_at,
            published_at=p.first_seen_anywhere_at - timedelta(minutes=8),
        )
        db.add(lst)
        db.flush()
        a = Alert(
            user_id=u.id,
            profile_id=1,
            property_id=p.id,
            listing_id=lst.id,
            event_type="new",
            channel="telegram",
            token=f"t{i}",
            sent_at=p.first_seen_anywhere_at + timedelta(minutes=delay),
        )
        db.add(a)
        db.add(
            Valuation(
                property_id=p.id, at=now - timedelta(hours=1), asking_price=p.price, fair_value=320000, low=290000, high=350000
            )
        )
        props.append((p, a))
    p0, a0 = props[0]
    db.add(UserAction(user_id=u.id, property_id=p0.id, action="contacted", at=a0.sent_at + timedelta(minutes=45)))
    db.add(
        ContactRequest(
            user_id=u.id, property_id=props[1][0].id, listing_id=1, body="x", sent_at=props[1][1].sent_at + timedelta(minutes=3)
        )
    )
    db.add(Purchase(user_id=u.id, property_id=p0.id, price_paid=304000, at=now))
    db.add(ModelRun(version="v1", n_train=80, n_test=20, mape=0.11, interval_coverage=0.78))
    db.commit()
    return u


def test_all_metrics(db, monkeypatch):
    u = setup(db)
    m = metrics.compute_all(db, user_id=u.id)
    assert m["time_to_alert"]["value"] == 10 and m["time_to_alert"]["n"] == 3
    lead = m["lead_time_vs_portals"]
    assert lead["value"] == 20 and lead["share_before_portal"] == 0.5
    assert m["time_to_first_contact"]["value"] == 24  # median of 45 and 3
    pv = m["price_vs_fair_value"]
    assert round(pv["value"], 4) == round((300001 - 320000) / 320000, 4)
    assert pv["paid_gap"] == (304000 - 320000) / 320000
    g = metrics.compute_all(db)
    assert g["detection_latency"]["value"] == 8 and g["detection_latency"]["per_source"]["A"]["n"] == 3
    assert g["coverage"]["share_first"] == {"A": 1.0}
    assert "MAPE 11.0%" in g["model_quality"]["headline"]
    for r in g.values():
        assert r["definition"] and r["title"]
    csv = metrics.to_csv(m["time_to_alert"]["rows"])
    assert csv.splitlines()[0].startswith("alert_id,property_id")
    assert metrics.to_csv([]) == ""


def test_not_measured_without_portal_flag(db):
    assert metrics.lead_time_vs_portals(db)["headline"].startswith("not measured")
    get_settings().portal_crosscheck_enabled = True
    try:
        assert metrics.lead_time_vs_portals(db)["headline"] == "no data yet"
    finally:
        get_settings().portal_crosscheck_enabled = False
