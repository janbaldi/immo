"""Agency website connectors for the Zabun, Whise and Omnicasa template families, plus a generic fallback.

The three families share one crawler; they differ in markers (used by discovery) and detail-link patterns.
shortcut: family patterns are written from public URL conventions, not recorded pages; tighten them once
`immo record-fixture` has captured real pages for each family (see DECISIONS.md, Needs verification).
"""

import re
from urllib.parse import urlsplit

from immo_agent.sources.base import Connector, IndexItem, external_id_from_url, sha
from immo_agent.sources.extract import RawListing, links, read_listing

DETAIL_WORDS = (
    r"(te-koop|kopen|koop|detail|pand|panden|property|properties|aanbod|woning|huis|appartement|estate"
    r"|for-sale|a-vendre|verkoop|te-huur|huren)"
)

FAMILIES: dict[str, dict[str, object]] = {
    "zabun": {"markers": ["zabun"], "detail_re": rf"/{DETAIL_WORDS}/.*\d"},
    "whise": {"markers": ["whise"], "detail_re": rf"/{DETAIL_WORDS}/.*\d"},
    "omnicasa": {"markers": ["omnicasa"], "detail_re": rf"(/{DETAIL_WORDS}/.*\d|[?&]id=\d+)"},
    "generic": {"markers": [], "detail_re": rf"/{DETAIL_WORDS}[/-].*\d{{3,}}"},
}
NEXT_RE = re.compile(r"(?i)(volgende|next|suivant|›|»)")
MAX_PAGES_FULL, MAX_PAGES_INCREMENTAL = 30, 2


def is_detail_link(url: str, list_url: str, kind: str) -> bool:
    if urlsplit(url).netloc != urlsplit(list_url).netloc or url.rstrip("/") == list_url.rstrip("/"):
        return False
    if re.search(r"(?i)[?&](page|p|pagina)=\d+|/page/\d+|\.(pdf|jpe?g|png)$", url):
        return False
    return re.search(str(FAMILIES[kind]["detail_re"]), url, re.I) is not None


class AgencyConnector(Connector):
    async def index(self, full: bool = False) -> list[IndexItem]:
        url: str | None = self.source.list_url
        items: dict[str, IndexItem] = {}
        seen_pages: set[str] = set()
        for _ in range(MAX_PAGES_FULL if full else MAX_PAGES_INCREMENTAL):
            if not url or url in seen_pages:
                break
            seen_pages.add(url)
            page = await self.fetcher.get(url, browser=self.source.use_browser)
            if page.status >= 400:
                raise RuntimeError(f"HTTP {page.status} for {url}")
            next_url = None
            for href, text, card in links(page.text, page.url):
                if is_detail_link(href, self.source.list_url, self.source.kind):
                    items.setdefault(href, IndexItem(href, sha(card), external_id_from_url(href)))
                elif NEXT_RE.search(text[:20]) and href not in seen_pages and urlsplit(href).netloc == urlsplit(page.url).netloc:
                    next_url = next_url or href
            url = next_url
        return list(items.values())

    async def detail(self, item: IndexItem) -> RawListing:
        page = await self.fetcher.get(item.url, browser=self.source.use_browser)
        if page.status >= 400:
            raise RuntimeError(f"HTTP {page.status} for {item.url}")
        raw = read_listing(page.text, item.url)
        raw.external_id = item.external_id
        return raw


class ZabunConnector(AgencyConnector):
    pass


class WhiseConnector(AgencyConnector):
    pass


class OmnicasaConnector(AgencyConnector):
    pass


class GenericConnector(AgencyConnector):
    pass
