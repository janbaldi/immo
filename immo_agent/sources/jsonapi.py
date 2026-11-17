"""Biddit (notarial online auctions) and the notary listing platform.

Both are JavaScript apps backed by JSON endpoints. The endpoint and the detail-URL template live in
config/sources.yaml (source.options) so they can be corrected without code changes. Field mapping is
tolerant: it searches nested JSON for well-known key names.
"""

from datetime import datetime
from typing import Any

from immo_agent.sources.base import Connector, IndexItem, sha
from immo_agent.sources.extract import RawListing, _parse_dt, guess_status, guess_type, parse_price

KEYS = {
    "id": ("id", "saleId", "propertyId", "reference", "uuid", "code"),
    "url": ("url", "detailUrl", "link", "permalink"),
    "price": ("startingPrice", "startPrice", "initialPrice", "minimumPrice", "askingPrice", "price", "currentBid"),
    "postal": ("postalCode", "zipCode", "zip", "postcode", "postCode"),
    "city": ("city", "municipality", "locality", "cityName", "municipalityName"),
    "street": ("street", "streetName", "streetAddress"),
    "number": ("houseNumber", "number", "streetNumber"),
    "surface": ("livingArea", "habitableSurface", "netHabitableSurface", "surface", "livingSurface"),
    "bedrooms": ("bedrooms", "bedroomCount", "numberOfBedrooms"),
    "type": ("propertyType", "type", "category", "subtype", "typeName"),
    "title": ("title", "name", "description"),
    "published": ("publicationDate", "publishedAt", "creationDate", "createdAt", "startDate"),
    "epc_score": ("epcScore", "energyScore", "epc"),
    "epc_label": ("epcLabel", "energyLabel"),
    "status": ("status", "saleStatus", "state"),
    "photos": ("images", "photos", "pictures", "media"),
    "lat": ("latitude", "lat"),
    "lon": ("longitude", "lng", "lon"),
}


def deep_get(obj: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(obj, dict):
        lower = {k.lower(): v for k, v in obj.items()}
        for k in keys:
            v = lower.get(k.lower())
            if v not in (None, "", [], {}):
                return v
        for v in obj.values():
            if isinstance(v, dict):
                found = deep_get(v, keys, depth + 1)
                if found not in (None, "", [], {}):
                    return found
    return None


def find_items(data: Any) -> list[dict[str, Any]]:
    """The first list of dicts anywhere in the payload is the result list."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for k in ("results", "items", "data", "content", "sales", "properties", "hits"):
            if k in data:
                found = find_items(data[k])
                if found:
                    return found
        for v in data.values():
            found = find_items(v) if isinstance(v, list | dict) else []
            if found:
                return found
    return []


def _text(v: Any) -> str | None:
    if isinstance(v, dict):  # multilingual {"nl": "...", "fr": "..."}
        v = v.get("nl") or v.get("NL") or next(iter(v.values()), None)
    return str(v) if v not in (None, "") else None


def map_item(item: dict[str, Any], detail_url: str, auction: bool) -> RawListing:
    g = {k: deep_get(item, keys) for k, keys in KEYS.items()}
    ext_id = _text(g["id"])
    url = _text(g["url"]) or detail_url.format(id=ext_id)
    price = g["price"]
    photos = g["photos"] if isinstance(g["photos"], list) else []
    title = _text(g["title"])
    ptype = _text(g["type"])
    street = " ".join(x for x in (_text(g["street"]), _text(g["number"])) if x) or None
    published = g["published"]
    surface, bedrooms = g["surface"], g["bedrooms"]
    return RawListing(
        url=url,
        external_id=ext_id,
        title=title,
        price=int(price) if isinstance(price, int | float) else parse_price(_text(price)),
        property_type=guess_type(ptype, title) if ptype or title else None,
        street=street,
        postal_code=_text(g["postal"]),
        municipality=_text(g["city"]),
        surface=float(surface) if isinstance(surface, int | float) else None,
        bedrooms=int(bedrooms) if isinstance(bedrooms, int | float) else None,
        epc_score=float(g["epc_score"]) if isinstance(g["epc_score"], int | float) else None,
        epc_label=_text(g["epc_label"]),
        status=guess_status(_text(g["status"])),
        photos=[p if isinstance(p, str) else str(deep_get(p, ("url", "src", "large", "href")) or "") for p in photos],
        published_at=_parse_dt(published)
        if isinstance(published, str)
        else (published if isinstance(published, datetime) else None),
        lat=g["lat"] if isinstance(g["lat"], float) else None,
        lon=g["lon"] if isinstance(g["lon"], float) else None,
        is_auction=auction,
        description=_text(item.get("description")),
    )


class JsonApiConnector(Connector):
    auction = False

    async def index(self, full: bool = False) -> list[IndexItem]:
        opts = self.source_options()
        params = dict(opts.get("params", {}))
        out: list[IndexItem] = []
        for page in range(int(opts.get("max_pages", 20)) if full else 1):
            if "page_param" in opts:
                params[opts["page_param"]] = page + int(opts.get("first_page", 0))
            data = await self.fetcher.get_json(self.source.list_url, **params)
            batch = find_items(data)
            if not batch:
                break
            for item in batch:
                raw = map_item(item, opts.get("detail_url", self.source.base_url + "/{id}"), self.auction)
                out.append(IndexItem(raw.url, sha(repr(sorted(item.items(), key=lambda kv: kv[0]))), raw.external_id, raw))
            if "page_param" not in opts:
                break
        return out

    async def detail(self, item: IndexItem) -> RawListing:
        assert item.raw is not None
        return item.raw

    def source_options(self) -> dict[str, Any]:
        return dict(self.source.options or {})


class BidditConnector(JsonApiConnector):
    auction = True


class NotaryConnector(JsonApiConnector):
    pass
