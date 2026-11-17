"""Read-only portal cross-check (Immoweb, Zimmo). Disabled unless PORTAL_CROSSCHECK_ENABLED=true.

Only the newest-listings page is read. Nothing from the portal is stored except, per matched property,
portal_first_seen_at and the portal URL. Content is never republished.
"""

import json
from html import unescape

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from immo_agent.sources.base import Connector, IndexItem, sha
from immo_agent.sources.extract import RawListing, extract_listing
from immo_agent.sources.jsonapi import find_items, map_item


def summaries_from_page(html: str, url: str) -> list[RawListing]:
    tree = HTMLParser(html)
    out: list[RawListing] = []
    # Search pages often embed their result JSON in a component attribute (e.g. :results="[...]").
    for node in tree.css("*"):
        for attr, value in node.attributes.items():
            if value and attr.lstrip(":").lower() in ("results", "data-results", "data-listings") and value.strip()[:1] in "[{":
                try:
                    out += [map_item(i, url.split("?")[0] + "/{id}", False) for i in find_items(json.loads(unescape(value)))]
                except json.JSONDecodeError:
                    continue
    for node in tree.css('script[type="application/json"], script#__NEXT_DATA__'):
        try:
            out += [map_item(i, url.split("?")[0] + "/{id}", False) for i in find_items(json.loads(node.text() or ""))]
        except json.JSONDecodeError:
            continue
    if not out:
        raw = extract_listing(html, url)
        if raw.price and raw.postal_code:
            out.append(raw)
    return [r for r in out if r.price and (r.postal_code or r.municipality)]


class PortalConnector(Connector):
    async def index(self, full: bool = False) -> list[IndexItem]:
        page = await self.fetcher.get(self.source.list_url)
        if page.status >= 400:
            raise RuntimeError(f"HTTP {page.status}")
        return [IndexItem(r.url, sha(r.url), r.external_id, r) for r in summaries_from_page(page.text, page.url)]

    async def detail(self, item: IndexItem) -> RawListing:
        assert item.raw is not None
        return item.raw
