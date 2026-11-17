import asyncio
import io

from conftest import site
from pages import detail, listing_page
from PIL import Image
from sqlalchemy import select

from immo_agent.agents import scout
from immo_agent.core.models import Alert, Listing, ListingEvent, Property, SearchProfile, User
from immo_agent.sources.http import Fetcher

LIST = "https://agency.test/nl/te-koop"
D1, D2, D3 = (f"https://agency.test/nl/te-koop/woning/{n}" for n in (10001, 10002, 10003))


def run(db, source, pages, full=None):
    async def go():
        async with Fetcher(transport=site(pages)) as f:
            return await scout.run_source(db, source, f, full)

    return asyncio.run(go())


def pages_v1():
    return {
        LIST: listing_page([(D1, "€ 349.000 Antwerpen"), (D2, "€ 289.000 Berchem")], next_url=LIST + "?page=2"),
        LIST + "?page=2": listing_page([(D3, "€ 199.000 Mortsel")]),
        D1: detail(),
        D2: detail(price=289000, street="Statiestraat 5", postal="2600", city="Berchem", surface=95, bedrooms=2),
        D3: detail(price=199000, street="Kapelstraat 1", postal="2640", city="Mortsel", surface=70, bedrooms=1),
    }


def founder(db, **profile):
    u = User(email="founder@example.com", password_hash="x")
    db.add(u)
    db.flush()
    db.add(SearchProfile(user_id=u.id, municipalities=["Antwerpen", "Berchem"], **profile))
    db.commit()
    return u


def test_backfill_then_incremental_change_detection(db, make_source, monkeypatch):
    src = make_source()
    founder(db)
    sent = []
    monkeypatch.setattr("immo_agent.agents.matcher.deliver", lambda db, a: sent.append(a) or True)

    r = run(db, src, pages_v1())
    assert (r.listings_found, r.new_count, r.parse_errors, r.error) == (3, 3, 0, None)
    assert src.backfilled_at is not None
    assert db.scalar(select(Property).where(Property.postal_code == "2600")).price == 289000
    assert not db.scalars(select(Alert)).all(), "the initial backfill must not alert"

    # Incremental poll: D1 drops in price, a new listing D4 appears in Berchem.
    d4 = "https://agency.test/nl/te-koop/woning/10004"
    pages = pages_v1()
    pages[LIST] = listing_page(
        [(d4, "€ 310.000 Berchem"), (D1, "€ 329.000 Antwerpen"), (D2, "€ 289.000 Berchem")], next_url=LIST + "?page=2"
    )
    pages[D1] = detail(price=329000)
    pages[d4] = detail(price=310000, street="Kerkstraat 9", postal="2600", city="Berchem", surface=110)
    r = run(db, src, pages)
    assert r.details_fetched == 2, "unchanged cards must not be re-fetched"
    types = sorted(e.type for e in db.scalars(select(ListingEvent).where(ListingEvent.matched.is_(True))) if e.type != "new")
    assert "price_drop" in types
    assert sorted(a.event_type for a in sent) == ["new", "price_drop"]

    # Nightly full crawl without D3 -> removed event.
    del pages[D3]
    pages[LIST + "?page=2"] = listing_page([])
    run(db, src, pages, full=True)
    removed = db.scalars(select(Listing).where(Listing.url == D3)).one()
    assert removed.status == "removed" and removed.property.status == "removed"

    # It comes back -> relisted.
    pages[LIST + "?page=2"] = listing_page([(D3, "€ 199.000 Mortsel")])
    pages[D3] = detail(price=199000, street="Kapelstraat 1", postal="2640", city="Mortsel", surface=70, bedrooms=1)
    run(db, src, pages)
    assert db.scalars(select(ListingEvent).where(ListingEvent.type == "relisted")).first() is not None


def test_same_home_on_two_sources_is_one_property(db, make_source, monkeypatch):
    monkeypatch.setattr("immo_agent.agents.matcher.deliver", lambda db, a: True)
    a = make_source()
    b = make_source(name="Other", kind="whise", base_url="https://other.test", list_url="https://other.test/aanbod")
    for s in (a, b):
        s.backfilled_at = s.created_at
    db.commit()
    founder(db)
    run(db, a, {LIST: listing_page([(D1, "x")]), D1: detail()})
    other = "https://other.test/aanbod/pand/55555"
    run(db, b, {"https://other.test/aanbod": listing_page([(other, "y")]), other: detail(street="Lange Nieuwstraat 12 bus 1")})
    assert len(db.scalars(select(Property)).all()) == 1
    assert len(db.scalars(select(Listing)).all()) == 2
    assert len(db.scalars(select(Alert)).all()) == 1, "one alert per user per property"


def _jpeg(color):
    buf = io.BytesIO()
    img = Image.new("RGB", (64, 64), color)
    for x in range(32):
        for y in range(64):
            img.putpixel((x, y), (0, 0, 0) if color == "white" else (255, 255, 255))
    img.save(buf, "JPEG")
    return buf.getvalue()


def test_fuzzy_dedup_uses_photo_hash(db, make_source):
    a = make_source()
    b = make_source(name="Other", base_url="https://other.test", list_url="https://other.test/aanbod")
    # No house number -> no address match; same area/type/surface/price -> fuzzy candidate.
    common = dict(street="Lange Nieuwstraat", surface=120)
    run(
        db,
        a,
        {
            LIST: listing_page([(D1, "x")]),
            D1: detail(photo="https://cdn.test/a.jpg", **common),
            "https://cdn.test/a.jpg": _jpeg("white"),
        },
    )
    o1, o2 = "https://other.test/aanbod/pand/77777", "https://other.test/aanbod/pand/88888"
    pages = {
        "https://other.test/aanbod": listing_page([(o1, "same"), (o2, "different")]),
        o1: detail(price=351000, surface=121, photo="https://cdn.test/a.jpg", street="Lange Nieuwstraat"),
        o2: detail(price=351000, surface=121, photo="https://cdn.test/b.jpg", street="Lange Nieuwstraat"),
        "https://cdn.test/a.jpg": _jpeg("white"),
        "https://cdn.test/b.jpg": _jpeg("black"),
    }
    run(db, b, pages)
    pid = {lst.url: lst.property_id for lst in db.scalars(select(Listing))}
    assert pid[o1] == pid[D1]
    assert pid[o2] != pid[D1]


def test_source_errors_are_recorded(db, make_source):
    src = make_source()
    r = run(db, src, {LIST: (500, "boom")})
    assert r.error and "HTTP 500" in r.error
    r = run(db, src, {"https://agency.test/robots.txt": "User-agent: *\nDisallow: /", LIST: listing_page([])})
    assert r.robots_allowed is False


def test_due_sources(db, make_source):
    make_source()
    make_source(name="Portal", kind="immoweb", base_url="https://portal.test", list_url="https://portal.test/x", is_portal=True)
    make_source(name="Off", base_url="https://off.test", list_url="https://off.test/x", enabled=False)
    assert [s.name for s in scout.due_sources(db)] == ["Agency"]
