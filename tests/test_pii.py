import asyncio

from conftest import site
from pages import detail, listing_page
from sqlalchemy import select

from immo_agent.agents import scout
from immo_agent.core.models import Listing
from immo_agent.core.pii import find_pii, scrub
from immo_agent.report import pii_scan
from immo_agent.sources.http import Fetcher

CONTACT = "Contact: Jan Peeters, jan.peeters@agency.be, 0477 12 34 56 of +32 3 123 45 67."


def test_scrub():
    out = scrub("Bel 03/123.45.67 of mail info@makelaar.be. Contactpersoon Els Janssens helpt u graag.")
    assert "123" not in out and "@" not in out and "Els" not in out
    assert scrub("3 slaapkamers, 120 m², bouwjaar 1975, prijs 349000") == "3 slaapkamers, 120 m², bouwjaar 1975, prijs 349000"
    assert find_pii(CONTACT)


def test_no_personal_data_is_stored(db, make_source):
    src = make_source()
    url = "https://agency.test/nl/te-koop/woning/42424"
    pages = {
        "https://agency.test/nl/te-koop": listing_page([(url, "card")]),
        url: detail(extra_text=CONTACT, title="Woning te koop - bel 0477 12 34 56"),
    }

    async def go():
        async with Fetcher(transport=site(pages)) as f:
            await scout.run_source(db, src, f)

    asyncio.run(go())
    listing = db.scalars(select(Listing)).one()
    stored = " ".join(str(getattr(listing, c.name)) for c in Listing.__table__.columns)
    for secret in ("Peeters", "jan.peeters", "0477", "123 45 67"):
        assert secret not in stored
    assert pii_scan(db) == []
