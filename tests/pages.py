"""HTML used by the tests.

These are minimal pages written to the schema.org / Open Graph conventions the generic parser relies on.
They are NOT recorded copies of real agency pages: this sandbox had no network access to Belgian sites.
Record real pages with `immo record-fixture <url> <name>`; tests/test_recorded.py then checks every
recorded page automatically. See DECISIONS.md, "Needs verification".
"""

import json


def detail(
    price: int = 349000,
    street: str = "Lange Nieuwstraat 12",
    postal: str = "2000",
    city: str = "Antwerpen",
    surface: int = 120,
    bedrooms: int = 3,
    extra_text: str = "",
    photo: str = "https://cdn.agency.test/p1.jpg",
    title: str = "Woning te koop",
    published: str = "2026-11-17T08:00:00+01:00",
    jsonld: bool = True,
) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": ["Product", "SingleFamilyResidence"],
        "name": title,
        "description": f"Mooie woning met tuin. {extra_text}",
        "offers": {"@type": "Offer", "price": price, "priceCurrency": "EUR"},
        "address": {"@type": "PostalAddress", "streetAddress": street, "postalCode": postal, "addressLocality": city},
        "floorSize": {"@type": "QuantitativeValue", "value": surface, "unitCode": "MTK"},
        "numberOfBedrooms": bedrooms,
        "image": [photo],
        "datePosted": published,
    }
    if jsonld:
        head = f'<script type="application/ld+json">{json.dumps(data)}</script>'
        body = f"<h1>{title}</h1><p>EPC-label C · P-score: A · G-score: B · {extra_text}</p>"
    else:
        shown = f"{price:,}".replace(",", ".")
        head = f'<meta property="og:title" content="{title}"><meta property="og:image" content="{photo}">'
        body = (
            f'<h1>{title}</h1><div class="price">€ {shown}</div><p>{street}, {postal} {city}</p>'
            f"<p>Bewoonbare opp.: {surface} m² · {bedrooms} slaapkamers</p>"
            f"<p>EPC: 245 kWh/m² · EPC-label C · P-score: A · G-score: B</p><p>{extra_text}</p>"
        )
    return f"<html><head><title>{title}</title>{head}</head><body>{body}</body></html>"


def listing_page(links: list[tuple[str, str]], next_url: str | None = None) -> str:
    cards = "".join(f'<div class="card"><a href="{href}">Bekijk</a><span>{text}</span></div>' for href, text in links)
    nxt = f'<a href="{next_url}">Volgende</a>' if next_url else ""
    return f"<html><body><h1>Te koop</h1>{cards}{nxt}</body></html>"
