from datetime import datetime

from sqlalchemy import select

from immo_agent.agents import matcher
from immo_agent.core.models import Alert, Listing, ListingEvent, Property, SearchProfile, User, Valuation


def make(db, **prop):
    src_id = 1
    from immo_agent.core.models import Source

    if db.get(Source, 1) is None:
        db.add(Source(id=1, name="Agency X", kind="zabun", base_url="https://a.test", list_url="https://a.test/x"))
    p = Property(
        **{
            "municipality": "Berchem",
            "postal_code": "2600",
            "property_type": "house",
            "price": 300000,
            "bedrooms": 3,
            "surface": 120,
            "epc_label": "C",
            "flood_p": "A",
            **prop,
        }
    )
    db.add(p)
    db.flush()
    lst = Listing(property_id=p.id, source_id=src_id, url=f"https://a.test/x/{p.id}", title="Woning", price=p.price)
    db.add(lst)
    db.flush()
    db.add(ListingEvent(listing_id=lst.id, property_id=p.id, type="new"))
    db.commit()
    return p


def profile(db, **kw):
    u = db.scalars(select(User)).first()
    if u is None:
        u = User(email="u@example.com", password_hash="x", email_alerts=True)
        db.add(u)
        db.flush()
    pr = SearchProfile(user_id=u.id, **kw)
    db.add(pr)
    db.commit()
    return pr


def test_criteria(db):
    p = make(db)
    assert matcher.matches(SearchProfile(municipalities=["berchem"]), p)
    assert matcher.matches(SearchProfile(postal_codes=["2600"]), p)
    assert not matcher.matches(SearchProfile(municipalities=["Mortsel"]), p)
    assert not matcher.matches(SearchProfile(max_price=250000), p)
    assert not matcher.matches(SearchProfile(min_price=350000), p)
    assert not matcher.matches(SearchProfile(min_bedrooms=4), p)
    assert not matcher.matches(SearchProfile(min_surface=150), p)
    assert not matcher.matches(SearchProfile(property_types=["apartment"]), p)
    assert not matcher.matches(SearchProfile(epc_max="B"), p)
    assert matcher.matches(SearchProfile(epc_max="D", flood_max="A"), p)
    p.flood_p = "C"
    assert not matcher.matches(SearchProfile(flood_max="B"), p)
    inv = SearchProfile(investor=True, min_yield=0.05)
    assert not matcher.matches(inv, p, None)
    assert matcher.matches(inv, p, Valuation(gross_yield=0.06))
    p.status = "sold"
    assert not matcher.matches(SearchProfile(), p)


def test_quiet_hours():
    u = User(quiet_start=22, quiet_end=7)
    assert matcher.in_quiet_hours(u, datetime(2026, 1, 1, 23))
    assert matcher.in_quiet_hours(u, datetime(2026, 1, 1, 6))
    assert not matcher.in_quiet_hours(u, datetime(2026, 1, 1, 12))
    assert matcher.in_quiet_hours(User(quiet_start=1, quiet_end=5), datetime(2026, 1, 1, 3))
    assert not matcher.in_quiet_hours(User(), datetime(2026, 1, 1, 3))


def test_alert_sent_by_telegram_and_email(db, monkeypatch):
    tg, mails = [], []
    monkeypatch.setattr("immo_agent.notify.telegram_send", lambda chat, text: tg.append(text) or True)
    monkeypatch.setattr("immo_agent.notify.send_email", lambda to, s, b, **k: mails.append((to, s, b)) or True)
    profile(db, municipalities=["Berchem"])
    db.scalars(select(User)).first().telegram_chat_id = "123"
    make(db, price=310000)
    make(db, municipality="Mortsel", postal_code="2640")
    alerts = matcher.process(db)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.channel == "telegram+email" and a.sent_at is not None
    assert "https://a.test/x/" in tg[0] and "/a/" + a.token in tg[0] and "Agency X" in tg[0]
    assert mails[0][0] == "u@example.com" and "<b>" not in mails[0][2]
    assert matcher.process(db) == [], "events are processed once"


def test_failed_delivery_is_retried(db, monkeypatch):
    monkeypatch.setattr("immo_agent.notify.send_email", lambda *a, **k: False)
    profile(db)
    make(db)
    matcher.process(db)
    assert db.scalars(select(Alert)).one().sent_at is None
    monkeypatch.setattr("immo_agent.notify.send_email", lambda *a, **k: True)
    assert matcher.flush_pending(db) == 1


def test_message_shows_fair_value_and_yield(db):
    p = make(db)
    profile(db)
    db.add(Valuation(property_id=p.id, fair_value=320000, low=290000, high=350000, gross_yield=0.052, rent_estimate=1300))
    db.commit()
    a = Alert(user_id=1, profile_id=1, property_id=p.id, listing_id=p.listings[0].id, event_type="price_drop", token="t")
    db.add(a)
    db.flush()
    text = matcher.message(a, db.scalars(select(Valuation)).first())
    assert "Price drop" in text and "€ 320.000" in text and "5.2%" in text


def test_outreach_draft_when_enabled(db, monkeypatch):
    from immo_agent.core.config import get_settings
    from immo_agent.core.models import ContactRequest

    monkeypatch.setattr(get_settings(), "outreach_enabled", True)
    monkeypatch.setattr("immo_agent.notify.send_email", lambda *a, **k: True)
    profile(db)
    make(db)
    matcher.process(db)
    req = db.scalars(select(ContactRequest)).one()
    assert req.status == "draft" and "https://a.test/x/" in req.body and "@" not in req.body
