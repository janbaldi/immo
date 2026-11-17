import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from immo_agent.core.models import (
    Alert,
    DedupReview,
    Listing,
    ListingEvent,
    Property,
    Purchase,
    SearchProfile,
    SegmentCap,
    Source,
    SupportTicket,
    User,
    UserAction,
    Valuation,
)
from immo_agent.web.app import app


@pytest.fixture
def client():
    return TestClient(app)


def signup(client, email="founder@example.com", plan="home"):
    r = client.post("/signup", data={"email": email, "password": "secret123", "plan": plan}, follow_redirects=False)
    assert r.status_code == 303
    return r


def seed_property(db):
    db.add(Source(id=1, name="Agency X", kind="zabun", base_url="https://a.test", list_url="https://a.test/x"))
    p = Property(
        municipality="Berchem",
        postal_code="2600",
        property_type="house",
        price=300000,
        surface=120,
        bedrooms=3,
        epc_label="C",
        flood_p="A",
    )
    db.add(p)
    db.flush()
    lst = Listing(
        property_id=p.id, source_id=1, url="https://a.test/x/1", title="Mooie woning", photos=["https://img.test/1.jpg"]
    )
    db.add(lst)
    db.flush()
    db.add(ListingEvent(listing_id=lst.id, property_id=p.id, type="price_drop", old_value="320000", new_value="300000"))
    db.add(Valuation(property_id=p.id, fair_value=310000, low=280000, high=340000, gross_yield=0.05, rent_estimate=1250))
    db.commit()
    return p


def test_auth_flow(client, db):
    assert client.get("/feed", follow_redirects=False).headers["location"] == "/login"
    assert "Create your account" in client.get("/signup").text
    assert "8+ characters" in client.post("/signup", data={"email": "x@y.be", "password": "short"}).text
    signup(client)
    assert "already has an account" in client.post("/signup", data={"email": "founder@example.com", "password": "secret123"}).text
    user = db.scalars(select(User)).one()
    assert user.password_hash.startswith("$argon2")
    client.post("/logout")
    assert "Wrong e-mail" in client.post("/login", data={"email": "founder@example.com", "password": "nope12345"}).text
    r = client.post("/login", data={"email": "founder@example.com", "password": "secret123"}, follow_redirects=False)
    assert r.headers["location"] == "/feed"
    assert client.get("/").status_code == 200
    assert client.get("/health").json()["ok"]


def test_profiles_feed_property_and_actions(client, db):
    p = seed_property(db)
    signup(client, plan="investor")
    r = client.post(
        "/profiles",
        data={
            "name": "Berchem",
            "municipalities": ["Berchem"],
            "postal_codes": "2600, x",
            "property_types": ["house"],
            "max_price": "€ 350.000",
            "epc_max": "D",
            "flood_max": "B",
            "investor": "true",
            "min_yield": "4,5",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    prof = db.scalars(select(SearchProfile)).one()
    assert (prof.postal_codes, prof.max_price, prof.min_yield, prof.investor) == (["2600"], 350000, 0.045, True)
    assert "Berchem" in client.get("/profiles").text

    feed = client.get("/feed").text
    assert "Mooie woning" in feed and "price drop" in feed and "https://a.test/x/1" in feed and "yield 5.0%" in feed
    assert "Agency X" in client.get(f"/feed/items?profile={prof.id}").text

    page = client.get(f"/property/{p.id}").text
    assert "Fair value" in page and "€ 310.000" in page and "open original" in page and "price drop" in page
    client.post(f"/property/{p.id}/action", data={"action": "contacted"})
    client.post(f"/property/{p.id}/action", data={"action": "bought", "price": "€ 295.000"})
    assert client.post(f"/property/{p.id}/action", data={"action": "hack"}).status_code == 400
    assert db.scalars(select(Purchase)).one().price_paid == 295000
    assert {a.action for a in db.scalars(select(UserAction))} == {"viewed", "contacted", "bought"}
    assert client.post(f"/property/{p.id}/visit-request").status_code == 400, "outreach is off by default"
    assert client.get("/property/999").status_code == 404

    client.post(f"/profiles/{prof.id}/delete")
    db.expire_all()
    assert db.scalars(select(SearchProfile)).one().active is False


def test_segment_cap(client, db):
    db.add(SegmentCap(municipality="Berchem", segment="home", max_active=1))
    other = User(email="other@example.com", password_hash="x")
    db.add(other)
    db.flush()
    db.add(SearchProfile(user_id=other.id, municipalities=["Berchem"]))
    db.commit()
    signup(client)
    r = client.post("/profiles", data={"municipalities": ["Berchem"]})
    assert "is full for home clients" in r.text
    assert (
        client.post("/profiles", data={"municipalities": ["Berchem"], "investor": "true"}, follow_redirects=False).status_code
        == 303
    )


def test_edge_settings_support_and_alert_open(client, db):
    p = seed_property(db)
    signup(client)
    user = db.scalars(select(User)).one()
    db.add(
        Alert(
            user_id=user.id,
            profile_id=1,
            property_id=p.id,
            listing_id=1,
            event_type="new",
            channel="telegram",
            token="tok",
            sent_at=p.first_seen_anywhere_at,
        )
    )
    db.commit()
    edge = client.get("/edge").text
    assert "Time to alert" in edge and "median 0.0 min" in edge and "CSV" in edge
    assert client.get("/edge?days=7").status_code == 200
    csv = client.get("/edge/time_to_alert.csv")
    assert csv.headers["content-type"].startswith("text/csv") and "alert_id" in csv.text
    assert client.get("/edge/coverage.csv").status_code == 404

    r = client.get("/a/tok", follow_redirects=False)
    assert r.headers["location"] == f"/property/{p.id}"
    db.refresh(db.scalars(select(Alert)).one())
    assert db.scalars(select(Alert)).one().opened_at is not None
    assert client.get("/a/missing").status_code == 404

    client.post("/settings", data={"email_alerts": "", "quiet_start": "22", "quiet_end": "7"})
    client.post("/settings/telegram")
    db.expire_all()
    user = db.get(User, user.id)
    assert (user.email_alerts, user.quiet_start, user.quiet_end) == (False, 22, 7) and user.telegram_link_code
    assert user.telegram_link_code in client.get("/settings").text
    user.telegram_chat_id = "1"
    db.commit()
    assert "Connected" in client.get("/settings").text
    client.post("/settings/telegram/disconnect")

    assert "P-score" in client.get("/faq").text
    assert "Thanks" in client.post("/support", data={"subject": "Hi", "body": "Question"}).text
    assert db.scalars(select(SupportTicket)).one().subject == "Hi"


def test_admin_pages(client, db, monkeypatch):
    p = seed_property(db)
    db.add(Listing(property_id=p.id, source_id=1, url="https://a.test/x/2", title="Zelfde woning"))
    db.add(
        Source(
            name="Pending agency",
            kind="whise",
            base_url="https://new.test",
            list_url="https://new.test/aanbod",
            review_status="pending",
            enabled=False,
        )
    )
    db.commit()
    signup(client)
    assert client.get("/admin").status_code == 403
    user = db.scalars(select(User)).one()
    user.is_admin = True
    db.commit()

    assert "Agency X" in client.get("/admin").text
    assert "Pending agency" in client.get("/admin?status=pending").text
    pending = db.scalars(select(Source).where(Source.review_status == "pending")).one()
    client.post(f"/admin/sources/{pending.id}/approve")
    client.post("/admin/sources/1/toggle")
    client.post("/admin/sources/1/interval", data={"poll_minutes": "2"})
    db.expire_all()
    assert db.get(Source, pending.id).enabled and db.get(Source, 1).enabled is False and db.get(Source, 1).poll_minutes == 5
    ran = []
    monkeypatch.setattr("immo_agent.agents.scout.run_many", lambda ids, full=None: ran.append(ids) or _done())
    client.post("/admin/sources/1/run")
    assert ran == [[1]]
    assert client.post("/admin/sources/1/explode").status_code == 400
    assert "Run source now" in client.get("/admin/source/1").text

    for url in (
        "/admin/metrics",
        "/admin/metrics?days=14",
        "/admin/model",
        "/admin/users",
        "/admin/tickets",
        "/admin/parser-test",
    ):
        assert client.get(url).status_code == 200, url
    assert "coverage" in client.get("/admin/metrics/coverage.csv").headers["content-disposition"]

    client.post("/admin/caps", data={"municipality": "Berchem", "segment": "home", "max_active": "3"})
    assert "Berchem" in client.get("/admin/users").text
    client.post("/admin/caps", data={"municipality": "Berchem", "segment": "home", "max_active": ""})
    assert db.scalars(select(SegmentCap)).all() == []

    assert "Same home" in client.get("/admin/dedup").text
    client.post(f"/admin/dedup/{p.id}", data={"correct": "true"})
    assert db.scalars(select(DedupReview)).one().correct is True

    db.add(SupportTicket(user_id=user.id, subject="Q", body="?"))
    db.commit()
    client.post("/admin/tickets/1/close")
    db.expire_all()
    assert db.get(SupportTicket, 1).status == "closed"


def _done():
    import asyncio

    return asyncio.sleep(0, result=[])


def test_parser_test_page(client, db, monkeypatch):
    from conftest import site
    from pages import detail

    from immo_agent.web import app as webapp

    real = webapp.Fetcher
    monkeypatch.setattr(webapp, "Fetcher", lambda **kw: real(transport=site({"https://a.test/huis/1": detail()}), **kw))
    signup(client)
    db.scalars(select(User)).one().is_admin = True
    db.commit()
    r = client.post("/admin/parser-test", data={"url": "https://a.test/huis/1"})
    assert "349000" in r.text and "Missing critical fields: <b>none" in r.text
    assert "missing critical fields" in client.post("/admin/parser-test", data={"url": "https://a.test/missing"}).text.lower()
