import asyncio

from conftest import site
from pages import detail, listing_page

from immo_agent.agents import ops_qa, scout
from immo_agent.sources.http import Fetcher

LIST = "https://agency.test/nl/te-koop"
URLS = [f"https://agency.test/nl/te-koop/woning/{n}" for n in range(20001, 20006)]


def poll(db, src, pages, full=None):
    async def go():
        async with Fetcher(transport=site(pages)) as f:
            await scout.run_source(db, src, f, full)

    asyncio.run(go())


def test_detects_broken_parser_when_fixture_html_changes(db, make_source):
    src = make_source()
    good = {LIST: listing_page([(u, f"card {i}") for i, u in enumerate(URLS)])}
    good.update({u: detail(price=300000 + i, street=f"Straat {i}") for i, u in enumerate(URLS)})
    poll(db, src, good)
    assert ops_qa.check_source(db, src)[0] == "green"

    # The agency changes its template: detail pages no longer carry structured data or prices.
    broken = {LIST: listing_page([(u, f"new card {i}") for i, u in enumerate(URLS)])}
    broken.update({u: "<html><body><div class='x'>Nieuw ontwerp</div></body></html>" for u in URLS})
    poll(db, src, broken)
    health, note = ops_qa.check_source(db, src)
    assert health == "red" and "parse error" in note

    # Or the listing page itself changes and yields no links any more.
    poll(db, src, {LIST: "<html><body>Nieuwe website</body></html>"})
    health, note = ops_qa.check_source(db, src)
    assert health == "red" and "no listings" in note


def test_run_alerts_founder_once(db, make_source, monkeypatch):
    sent = []
    monkeypatch.setattr("immo_agent.notify.notify_admin", lambda s, b: sent.append(s))
    src = make_source()
    ops_qa.run(db)  # no runs yet -> red
    ops_qa.run(db)
    assert src.health == "red" and len(sent) == 1


def test_weekly_summary(db, make_source, monkeypatch):
    sent = []
    monkeypatch.setattr("immo_agent.notify.notify_admin", lambda s, b: sent.append(b))
    make_source()
    body = ops_qa.weekly_summary(db)
    assert "Time to alert" in body and sent
