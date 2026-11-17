from datetime import datetime

from pages import detail

from immo_agent.sources.extract import epc_label_from_score, extract_listing, guess_status, guess_type, links, parse_price


def test_jsonld_page():
    raw = extract_listing(detail(), "https://agency.test/nl/te-koop/woning/12345")
    assert raw.price == 349000
    assert (raw.street, raw.postal_code, raw.municipality) == ("Lange Nieuwstraat 12", "2000", "Antwerpen")
    assert raw.surface == 120 and raw.bedrooms == 3
    assert raw.property_type == "house"
    assert raw.epc_label == "C" and raw.flood_p == "A" and raw.flood_g == "B"
    assert raw.photos[0] == "https://cdn.agency.test/p1.jpg"
    assert raw.published_at == datetime(2026, 11, 17, 7, 0)
    assert raw.missing_critical() == []


def test_text_only_page():
    raw = extract_listing(
        detail(jsonld=False, price=275000, title="Appartement te koop"), "https://agency.test/nl/te-koop/app/987"
    )
    assert raw.price == 275000
    assert raw.postal_code == "2000" and raw.municipality == "Antwerpen"
    assert raw.surface == 120 and raw.bedrooms == 3
    assert raw.property_type == "apartment"
    assert raw.epc_score == 245 and raw.epc_label == "C"
    assert raw.photos == ["https://cdn.agency.test/p1.jpg"]


def test_helpers():
    assert parse_price("Vraagprijs: € 1.250.000") == 1250000
    assert parse_price("349 000 EUR") == 349000
    assert parse_price("Prijs op aanvraag") is None
    assert guess_type("Ruime villa met zwembad") == "house"
    assert guess_type("Bouwgrond voor open bebouwing") == "land"
    assert guess_status("Verkocht onder voorbehoud") == "sold"
    assert guess_status("In optie") == "under_option"
    assert guess_status("Nieuw") == "for_sale"
    assert epc_label_from_score(80, "house") == "A"
    assert epc_label_from_score(450, "house") == "E"
    assert epc_label_from_score(600, "house") == "F"
    assert epc_label_from_score(150, "apartment") == "C"


def test_links_keep_card_text():
    html = '<div><a href="/nl/te-koop/huis/111">Bekijk</a> € 300.000 Berchem</div><a href="mailto:x@y.be">mail</a>'
    out = links(html, "https://agency.test/nl/te-koop")
    assert out == [("https://agency.test/nl/te-koop/huis/111", "Bekijk", "Bekijk € 300.000 Berchem")]
