import asyncio
import json

from conftest import site
from sqlalchemy import select

from immo_agent.agents import scout
from immo_agent.core.config import get_settings
from immo_agent.core.models import Listing, Property
from immo_agent.sources.agency import is_detail_link
from immo_agent.sources.http import Fetcher
from immo_agent.sources.jsonapi import find_items, map_item
from immo_agent.sources.portals import summaries_from_page

ITEM = {
    "saleId": 9876,
    "title": {"nl": "Woning met tuin", "fr": "Maison"},
    "startingPrice": 185000,
    "property": {"type": "HOUSE", "livingArea": 140, "bedrooms": 3},
    "address": {"street": "Dorpstraat", "houseNumber": "4", "postalCode": "2500", "city": "Lier"},
    "images": [{"url": "https://img.test/1.jpg"}],
    "publicationDate": "2026-11-17T09:00:00Z",
    "status": "ONLINE",
    "notary": {"name": "Notaris Janssens", "email": "n@notaris.be"},
}


def test_map_item():
    raw = map_item(ITEM, "https://www.biddit.be/nl/catalog/detail/{id}", auction=True).clean()
    assert raw.url == "https://www.biddit.be/nl/catalog/detail/9876"
    assert (raw.price, raw.postal_code, raw.municipality, raw.street) == (185000, "2500", "Lier", "Dorpstraat 4")
    assert (raw.surface, raw.bedrooms, raw.property_type, raw.is_auction) == (140, 3, "house", True)
    assert raw.photos == ["https://img.test/1.jpg"] and raw.published_at.hour == 9
    assert "Janssens" not in json.dumps(raw.__dict__, default=str), "notary contact is not mapped"
    assert find_items({"data": {"results": [ITEM]}}) == [ITEM]


def test_biddit_connector_pages(db, make_source):
    src = make_source(
        name="Biddit",
        kind="biddit",
        base_url="https://www.biddit.be",
        list_url="https://www.biddit.be/api/sales",
        options={"detail_url": "https://www.biddit.be/nl/catalog/detail/{id}", "page_param": "page", "max_pages": 3},
    )
    calls = []

    def handler_pages(request):
        import httpx

        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        page = int(request.url.params.get("page", 0))
        calls.append(page)
        items = [dict(ITEM, saleId=9876 + page)] if page < 2 else []
        return httpx.Response(200, json={"content": items})

    import httpx

    async def go():
        async with Fetcher(transport=httpx.MockTransport(handler_pages)) as f:
            return await scout.run_source(db, src, f)

    run = asyncio.run(go())
    assert run.new_count == 2 and calls == [0, 1, 2]
    assert db.scalars(select(Property)).first().is_auction


def test_detail_link_rules():
    base = "https://agency.test/nl/te-koop"
    assert is_detail_link("https://agency.test/nl/te-koop/huis/12345", base, "zabun")
    assert is_detail_link("https://agency.test/detail?id=777", base, "omnicasa")
    assert not is_detail_link("https://agency.test/nl/te-koop?page=2", base, "zabun")
    assert not is_detail_link("https://other.test/nl/te-koop/huis/12345", base, "zabun")
    assert not is_detail_link("https://agency.test/nl/contact", base, "generic")


def test_portal_crosscheck_stores_only_first_seen(db, make_source):
    get_settings().portal_crosscheck_enabled = True
    try:
        agency = make_source()
        portal = make_source(
            name="Immoweb", kind="immoweb", base_url="https://portal.test", list_url="https://portal.test/search", is_portal=True
        )
        p = Property(postal_code="2500", municipality="Lier", property_type="house", surface=140, price=185000)
        db.add(p)
        db.commit()
        results = json.dumps(
            [
                {
                    "id": 1,
                    "price": 185500,
                    "property": {"type": "HOUSE", "livingArea": 141},
                    "location": {"postalCode": "2500", "city": "Lier"},
                }
            ]
        )
        html = f"<html><body><iw-search :results='{results}'></iw-search></body></html>"
        assert summaries_from_page(html, "https://portal.test/search")[0].postal_code == "2500"

        async def go():
            async with Fetcher(transport=site({"https://portal.test/search": html})) as f:
                return await scout.run_source(db, portal, f)

        run = asyncio.run(go())
        assert run.new_count == 1
        db.refresh(p)
        assert p.portal_first_seen_at is not None and p.portal_url == "https://portal.test/search/1"
        assert db.scalars(select(Listing)).all() == [], "no portal content is stored"
        assert agency.id
    finally:
        get_settings().portal_crosscheck_enabled = False
