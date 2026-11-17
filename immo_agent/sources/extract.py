"""Generic field extraction from a listing detail page.

Order of trust: JSON-LD (schema.org) -> Open Graph / meta -> labelled text patterns -> optional LLM.
Contact details are never extracted; free text passes through pii.scrub.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from immo_agent.core.pii import scrub

CRITICAL_FIELDS = ("price", "municipality", "property_type")


@dataclass
class RawListing:
    url: str
    external_id: str | None = None
    title: str | None = None
    price: int | None = None
    transaction: str = "sale"
    status: str = "for_sale"
    property_type: str | None = None
    street: str | None = None
    postal_code: str | None = None
    municipality: str | None = None
    surface: float | None = None
    bedrooms: int | None = None
    epc_label: str | None = None
    epc_score: float | None = None
    flood_p: str | None = None
    flood_g: str | None = None
    description: str | None = None
    photos: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    is_auction: bool = False

    def missing_critical(self) -> list[str]:
        return [f for f in CRITICAL_FIELDS if getattr(self, f) in (None, "")]

    def clean(self) -> "RawListing":
        self.description = scrub(self.description)
        self.title = scrub(self.title)
        self.photos = [p for p in dict.fromkeys(self.photos) if p.startswith("http")][:30]
        if self.description and len(self.description) > 5000:
            self.description = self.description[:5000]
        return self


TYPE_WORDS = [
    ("apartment", ("appartement", "apartment", "studio", "penthouse", "duplex", "loft", "flat")),
    ("land", ("bouwgrond", "grond", "perceel", "terrain")),
    ("garage", ("garage", "parking", "staanplaats", "autostaanplaats")),
    ("commercial", ("handelspand", "kantoor", "commercieel", "bedrijfspand", "magazijn", "horeca")),
    (
        "house",
        (
            "woning",
            "huis",
            "villa",
            "rijwoning",
            "hoeve",
            "bungalow",
            "fermette",
            "herenhuis",
            "house",
            "eengezinswoning",
            "halfopen",
            "open bebouwing",
            "gesloten bebouwing",
        ),
    ),
]
EPC_LABELS = ("A+", "A", "B", "C", "D", "E", "F")


def guess_type(*texts: str | None) -> str | None:
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return None
    best: tuple[int, str] | None = None
    for ptype, words in TYPE_WORDS:
        for w in words:
            i = blob.find(w)
            if i >= 0 and (best is None or i < best[0]):
                best = (i, ptype)
    return best[1] if best else None


def parse_price(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"€\s*([\d.\s ]{3,})|([\d.\s ]{3,})\s*(?:€|eur\b|euro)", str(text), re.I)
    raw = (m.group(1) or m.group(2)) if m else str(text)
    digits = re.sub(r"[^\d]", "", raw.split(",")[0])
    if not digits:
        return None
    value = int(digits)
    return value if 100 <= value <= 50_000_000 else None


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(text))
    return float(m.group(0).replace(",", ".")) if m else None


TEXT_PATTERNS: dict[str, re.Pattern[str]] = {
    "surface": re.compile(r"(?i)(?:bewoonbare|woon)\s*(?:opp(?:ervlakte)?\.?)?\s*:?\s*(?:±|ca\.?)?\s*(\d{2,4}(?:[.,]\d+)?)\s*m"),
    "surface_any": re.compile(r"(?i)\b(\d{2,4})\s*m²"),
    "bedrooms": re.compile(r"(?i)(?:(\d{1,2})\s*slaapkamers?|slaapkamers?\s*:?\s*(\d{1,2})|(\d{1,2})\s*(?:slpk|bedrooms?))"),
    "epc_score": re.compile(r"(?i)(?:epc|energiescore|kengetal|primair energieverbruik)[^\d]{0,40}?(\d{1,4}(?:[.,]\d+)?)\s*kwh"),
    "epc_label": re.compile(r"(?i)(?:epc[- ]?label|energielabel|label)\s*:?\s*(A\+|[A-F])\b"),
    "flood_p": re.compile(r"(?i)p[- ]?score\s*:?\s*([A-D])\b"),
    "flood_g": re.compile(r"(?i)g[- ]?score\s*:?\s*([A-D])\b"),
    "postal": re.compile(r"\b([1-9]\d{3})\s+([A-Z][A-Za-zÀ-ÿ'-]+)"),  # Flemish names join words with hyphens
}
STATUS_WORDS = [
    ("sold", ("verkocht", "sold", "vendu")),
    ("under_option", ("in optie", "onder optie", "optie", "onder voorbehoud", "compromis", "sous option")),
]


def guess_status(*texts: str | None) -> str:
    blob = " ".join(t for t in texts if t).lower()
    for status, words in STATUS_WORDS:
        if any(re.search(rf"\b{re.escape(w)}\b", blob) for w in words):
            return status
    return "for_sale"


def _jsonld_objects(tree: HTMLParser) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                out.append(obj)
                stack.extend(v for k, v in obj.items() if k in ("@graph", "mainEntity", "itemOffered", "about"))
    return out


def _first(obj: Any, *keys: str) -> Any:
    for k in keys:
        if isinstance(obj, dict) and obj.get(k) not in (None, "", []):
            return obj[k]
    return None


def _apply_jsonld(raw: RawListing, objs: list[dict[str, Any]]) -> None:
    for obj in objs:
        types = obj.get("@type")
        types = " ".join(types) if isinstance(types, list) else str(types or "")
        if not any(
            t in types
            for t in (
                "Product",
                "Offer",
                "Residence",
                "House",
                "Apartment",
                "Accommodation",
                "RealEstate",
                "Place",
                "SingleFamily",
                "Event",
            )
        ):
            continue
        raw.title = raw.title or _first(obj, "name")
        raw.description = raw.description or _first(obj, "description")
        offers = _first(obj, "offers")
        offers = offers[0] if isinstance(offers, list) and offers else offers
        price = _first(offers, "price", "lowPrice") if isinstance(offers, dict) else None
        price = price or _first(obj, "price")
        if price and not raw.price:
            raw.price = parse_price(str(price)) if not isinstance(price, int | float) else int(price)
        addr = _first(obj, "address") or (_first(_first(obj, "location") or {}, "address"))
        if isinstance(addr, dict):
            raw.street = raw.street or _first(addr, "streetAddress")
            raw.postal_code = raw.postal_code or (str(_first(addr, "postalCode")) if _first(addr, "postalCode") else None)
            raw.municipality = raw.municipality or _first(addr, "addressLocality")
        geo = _first(obj, "geo")
        if isinstance(geo, dict):
            raw.lat, raw.lon = _num(geo.get("latitude")), _num(geo.get("longitude"))
        floor = _first(obj, "floorSize")
        if floor and not raw.surface:
            raw.surface = _num(floor.get("value") if isinstance(floor, dict) else floor)
        rooms = _first(obj, "numberOfBedrooms", "numberOfRooms")
        if rooms and not raw.bedrooms:
            n = _num(rooms.get("value") if isinstance(rooms, dict) else rooms)
            raw.bedrooms = int(n) if n is not None else None
        images = _first(obj, "image", "photo")
        for img in images if isinstance(images, list) else [images] if images else []:
            url = img.get("url") if isinstance(img, dict) else img
            if isinstance(url, str):
                raw.photos.append(url)
        posted = _first(obj, "datePosted", "datePublished", "validFrom")
        if posted and not raw.published_at:
            raw.published_at = _parse_dt(str(posted))
        if "Event" in types or "veiling" in str(obj.get("name", "")).lower():
            raw.is_auction = raw.is_auction or "Event" in types


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def _meta(tree: HTMLParser, *names: str) -> str | None:
    for n in names:
        node = tree.css_first(f'meta[property="{n}"]') or tree.css_first(f'meta[name="{n}"]')
        if node and node.attributes.get("content"):
            return node.attributes["content"]
    return None


def visible_text(tree: HTMLParser) -> str:
    body = tree.body
    if body is None:
        return ""
    for n in body.css("script, style, noscript, nav, footer, header"):
        n.decompose()
    return re.sub(r"\s+", " ", body.text(separator=" ")).strip()


def extract_listing(html: str, url: str) -> RawListing:
    tree = HTMLParser(html)
    raw = RawListing(url=url)
    _apply_jsonld(raw, _jsonld_objects(tree))

    h1 = tree.css_first("h1")
    raw.title = raw.title or _meta(tree, "og:title") or (h1.text(strip=True) if h1 else None)
    raw.description = raw.description or _meta(tree, "og:description", "description")
    raw.price = raw.price or parse_price(_meta(tree, "product:price:amount", "og:price:amount"))
    og_img = _meta(tree, "og:image")
    if og_img:
        raw.photos.insert(0, og_img)
    for img in tree.css("img"):
        src = img.attributes.get("data-src") or img.attributes.get("src") or ""
        if src and re.search(r"\.(jpe?g|webp|png)(\?|$)", src, re.I) and not re.search(r"logo|icon|sprite", src, re.I):
            raw.photos.append(urljoin(url, src))

    text = visible_text(tree)
    if not raw.price:
        price_node = tree.css_first('[class*="price"], [class*="prijs"], [itemprop="price"]')
        raw.price = parse_price(price_node.text() if price_node else None) or parse_price(
            (re.search(r"(?i)(?:prijs|vraagprijs|price|instelprijs|startprijs)[^€\d]{0,30}€?\s*[\d.\s]{3,}", text) or [None])[0]
        )
    _apply_text(raw, text)
    raw.property_type = raw.property_type or guess_type(raw.title, url, raw.description)
    raw.status = guess_status(
        raw.title, " ".join(n.text() for n in tree.css('[class*="status"], [class*="label"], [class*="badge"]'))
    )
    if re.search(r"(?i)te huur|/huur|/rent|for-rent|a-louer", (raw.title or "") + url):
        raw.transaction = "rent"
    return raw


def _apply_text(raw: RawListing, text: str) -> None:
    def grab(key: str) -> str | None:
        m = TEXT_PATTERNS[key].search(text)
        return next((g for g in m.groups() if g), None) if m else None

    if not raw.surface:
        raw.surface = _num(grab("surface")) or _num(grab("surface_any"))
    if not raw.bedrooms and (b := grab("bedrooms")):
        raw.bedrooms = int(b)
    if not raw.epc_score:
        raw.epc_score = _num(grab("epc_score"))
    if not raw.epc_label and (lbl := grab("epc_label")):
        raw.epc_label = lbl.upper()
    raw.flood_p = raw.flood_p or ((grab("flood_p") or "").upper() or None)
    raw.flood_g = raw.flood_g or ((grab("flood_g") or "").upper() or None)
    if not raw.postal_code:
        m = TEXT_PATTERNS["postal"].search(text)
        if m:
            raw.postal_code, raw.municipality = m.group(1), raw.municipality or m.group(2)
    if raw.epc_score and not raw.epc_label:
        raw.epc_label = epc_label_from_score(raw.epc_score, raw.property_type)


def epc_label_from_score(score: float, ptype: str | None) -> str:
    # Flemish residential EPC label bands (kWh/m²/year); apartments use tighter bands.
    bands = [0, 100, 200, 300, 400, 500] if ptype != "apartment" else [0, 75, 125, 175, 225, 275]
    labels = ["A+", "A", "B", "C", "D", "E"]
    for i in range(len(bands) - 1, -1, -1):
        if score > bands[i]:
            return labels[i + 1] if i + 1 < len(labels) else "F"
    return "A+"


def links(html: str, base: str) -> list[tuple[str, str, str]]:
    """All anchors as (absolute url, anchor text, card text). Card text (the anchor's container) is used to spot changes."""
    tree = HTMLParser(html)
    out = []
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        text = a.text(strip=True)
        card = a.parent if a.parent is not None and len(text) < 40 else a
        out.append((urljoin(base, href).split("#")[0], text, re.sub(r"\s+", " ", card.text(separator=" ")).strip()))
    return out
