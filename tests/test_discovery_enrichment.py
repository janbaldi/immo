import asyncio
import io
import time
import zipfile

import httpx
import pytest
from conftest import site
from sqlalchemy import select

from immo_agent import discovery, enrichment
from immo_agent.core.models import Enrichment, MunicipalityStat, Property, Source
from immo_agent.sources.http import Fetcher, RobotsDisallowed

HOME = """<html><head><title>Vastgoed Peeters | Uw makelaar</title><script src="https://cdn.zabun.be/x.js"></script></head>
<body><a href="/nl/te-koop">Te koop</a><a href="/nl/te-huur">Te huur</a><p>Kerkplein 1, 2930 Brasschaat</p></body></html>"""


def test_template_detection_and_list_page():
    assert discovery.detect_template(HOME) == "zabun"
    assert discovery.detect_template("<script src='https://whise.eu/w.js'>") == "whise"
    assert discovery.detect_template("<html>plain</html>") == "generic"
    assert discovery.find_list_url(HOME, "https://peeters.test/") == "https://peeters.test/nl/te-koop"
    assert discovery.agency_name(HOME, "https://peeters.test/") == "Vastgoed Peeters"
    links = '<a href="https://peeters.test">P</a><a href="https://www.facebook.com/x">f</a><a href="/intern">i</a>'
    assert discovery.candidate_sites(links, "https://dir.test/list") == ["https://peeters.test/"]


def test_discover_queues_sources_for_review(db, monkeypatch):
    monkeypatch.setattr(
        discovery,
        "load_yaml",
        lambda name: {
            "province": "antwerpen",
            "directories": ["https://dir.test/{municipality}"],
            "agency_sites": ["https://ghent.test/"],
        },
    )
    monkeypatch.setattr(
        discovery, "province_config", lambda name: {"municipalities": ["Brasschaat"], "postal_ranges": [[2000, 2999]]}
    )
    pages = {
        "https://dir.test/Brasschaat": '<a href="https://peeters.test/">Peeters</a>',
        "https://peeters.test/": HOME,
        "https://ghent.test/": HOME.replace("2930 Brasschaat", "9000 Gent"),
    }

    async def go():
        async with Fetcher(transport=site(pages)) as f:
            return await discovery.discover(db, f)

    found = asyncio.run(go())
    assert [(s.name, s.kind, s.review_status, s.enabled) for s in found] == [("Vastgoed Peeters", "zabun", "pending", False)]
    assert asyncio.run(go()) == [], "known sites are skipped"


def test_enrichment_adapters(db, monkeypatch):
    monkeypatch.setenv("EPC_LOOKUP_URL", "https://epc.test/lookup")
    monkeypatch.setenv("FLOOD_LOOKUP_URL", "https://flood.test/lookup")
    p = Property(street="Kerkstraat 1", postal_code="2000", municipality="Antwerpen")
    db.add_all([p, MunicipalityStat(municipality="Antwerpen", year=2025, property_type="house", median=350000)])
    db.commit()
    pages = {
        "https://epc.test/lookup": '{"label": "B", "score": 180, "owner": "secret"}',
        "https://flood.test/lookup": (500, "down"),
    }

    async def go():
        async with Fetcher(transport=site(pages)) as f:
            return await enrichment.enrich(db, f, p)

    out = asyncio.run(go())
    assert out["epc"] == {"label": "B", "score": 180}, "only whitelisted fields are kept"
    assert out["flood"] is None and out["permits"] is None
    assert out["statbel"]["median"] == 350000
    assert p.epc_label == "B" and p.enriched_at is not None
    assert {e.kind: e.ok for e in db.scalars(select(Enrichment))} == {
        "epc": True,
        "flood": False,
        "permits": False,
        "statbel": True,
    }


def test_statbel_parser_and_import(db):
    text = (
        "CD_YEAR|CD_TYPE_NL|CD_REFNIS|CD_REFNIS_NL|MS_TRANSACTIONS|MS_P25|MS_P50|MS_P75\n"
        "2024|Huizen met 2 of 3 gevels|11002|Antwerpen|1500|250000|310000|390000\n"
        "2024|Appartementen|11002|Antwerpen|3000|180000|225000|290000\n"
    )
    rows = enrichment.parse_statbel(text.encode())
    assert [(r.property_type, r.median, r.n) for r in rows] == [("house", 310000, 1500), ("apartment", 225000, 3000)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data.txt", text)

    async def go():
        async with Fetcher(transport=site({"https://statbel.test/f.zip": buf.getvalue()})) as f:
            return await enrichment.import_statbel(db, f, "https://statbel.test/f.zip")

    assert asyncio.run(go()) == 2
    with pytest.raises(ValueError):
        enrichment.parse_statbel(b"a;b\n1;2\n")


def test_robots_and_rate_limit():
    pages = {
        "https://x.test/robots.txt": "User-agent: *\nDisallow: /private",
        "https://x.test/a": "ok",
        "https://x.test/b": "ok",
        "https://y.test/robots.txt": (503, "busy"),
        "https://y.test/a": "ok",
    }

    async def go():
        async with Fetcher(transport=site(pages), min_interval=0.3) as f:
            with pytest.raises(RobotsDisallowed):
                await f.get("https://x.test/private/1")
            t = time.monotonic()
            await f.get("https://x.test/a")
            await f.get("https://x.test/b")
            elapsed = time.monotonic() - t
            assert elapsed >= 0.55, "at most one request per interval per domain"
            with pytest.raises(RobotsDisallowed):
                await f.get("https://y.test/a")  # robots.txt server error -> stay away
            assert f.client.headers["User-Agent"].startswith("ImmoAgentBot")

    asyncio.run(go())


def test_robots_network_error_allows():
    def boom(request):
        if request.url.path == "/robots.txt":
            raise httpx.ConnectError("down")
        return httpx.Response(200, text="ok")

    async def go():
        async with Fetcher(transport=httpx.MockTransport(boom)) as f:
            return (await f.get("https://z.test/a")).text

    assert asyncio.run(go()) == "ok"


def test_source_sync(db):
    from immo_agent.sources.registry import sync_sources

    assert sync_sources(db) >= 4
    src = db.scalars(select(Source).where(Source.kind == "biddit")).one()
    src.enabled = False
    db.commit()
    sync_sources(db)
    assert db.scalars(select(Source).where(Source.kind == "biddit")).one().enabled is False, "admin switch is kept"
    portal = db.scalars(select(Source).where(Source.kind == "immoweb")).one()
    assert portal.is_portal and not portal.enabled
