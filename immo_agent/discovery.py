"""Agency discovery: find real agency websites from directory pages, detect their template, queue for review.

Seeds (directory pages and municipalities) live in config/discovery.yaml. New sources are stored with
review_status='pending' and appear in the admin source list for one-click approval.
"""

import logging
import re
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from sqlalchemy import select
from sqlalchemy.orm import Session

from immo_agent.core.config import in_province, load_yaml, province_config
from immo_agent.core.models import Source
from immo_agent.sources.extract import links
from immo_agent.sources.http import Fetcher

log = logging.getLogger(__name__)
SKIP_HOSTS = re.compile(
    r"(facebook|instagram|linkedin|twitter|x\.com|youtube|google|immoweb|zimmo|immovlan|immoscoop|"
    r"biddit|notaris|notaire|apple|microsoft|wikipedia|tiktok|pinterest)",
    re.I,
)
LIST_WORDS = re.compile(r"(?i)(te[- ]koop|ons aanbod|aanbod|kopen|panden|te-koop|for-sale|a-vendre|woningen)")
MARKERS = {"zabun": ("zabun",), "whise": ("whise",), "omnicasa": ("omnicasa",)}


def detect_template(html: str) -> str:
    text = html.lower()
    scores = {kind: sum(text.count(m) for m in marks) for kind, marks in MARKERS.items()}
    kind, score = max(scores.items(), key=lambda kv: kv[1])
    return kind if score else "generic"


def find_list_url(html: str, home: str) -> str | None:
    host = urlsplit(home).netloc
    best = None
    for href, text, _ in links(html, home):
        if urlsplit(href).netloc != host or re.search(r"(?i)huur|rent|louer", href + text[:40]):
            continue
        if LIST_WORDS.search(href) or LIST_WORDS.search(text[:40]):
            path_depth = urlsplit(href).path.strip("/").count("/")
            if best is None or path_depth < best[0]:
                best = (path_depth, href)
    return best[1] if best else None


def agency_name(html: str, home: str) -> str:
    tree = HTMLParser(html)
    title = tree.css_first("title")
    name = (title.text(strip=True) if title else "") or urlsplit(home).netloc
    return re.split(r"\s[|\-–]\s", name)[0][:200]


def candidate_sites(html: str, base: str) -> list[str]:
    homes = []
    for href, _, _ in links(html, base):
        parts = urlsplit(href)
        if (
            parts.scheme in ("http", "https")
            and parts.netloc
            and parts.netloc != urlsplit(base).netloc
            and not SKIP_HOSTS.search(parts.netloc)
        ):
            homes.append(f"{parts.scheme}://{parts.netloc}/")
    return list(dict.fromkeys(homes))


async def inspect_site(fetcher: Fetcher, home: str, province: str) -> Source | None:
    page = await fetcher.get(home)
    if page.status >= 400:
        return None
    list_url = find_list_url(page.text, page.url)
    postal_codes = re.findall(r"\b([1-9]\d{3})\s+[A-Z]", HTMLParser(page.text).text())
    if not list_url or not any(in_province(pc, province) for pc in postal_codes):
        return None
    return Source(
        name=agency_name(page.text, home),
        kind=detect_template(page.text),
        base_url=home.rstrip("/"),
        list_url=list_url,
        province=province,
        review_status="pending",
        enabled=False,
    )


async def discover(db: Session, fetcher: Fetcher, limit: int = 500) -> list[Source]:
    seeds = load_yaml("discovery.yaml") or {}
    province = seeds.get("province", "antwerpen")
    known = set(db.scalars(select(Source.base_url)))
    homes: list[str] = list(seeds.get("agency_sites", []))
    munis = province_config(province).get("municipalities", [])
    directories = [
        d.format(municipality=m) for d in seeds.get("directories", []) for m in (munis if "{municipality}" in d else [""])
    ]
    for directory in dict.fromkeys(directories):
        try:
            page = await fetcher.get(directory)
            homes += candidate_sites(page.text, page.url)
        except Exception as e:  # noqa: BLE001
            log.warning("directory %s failed: %s", directory, e)
    found = []
    for home in list(dict.fromkeys(homes))[:limit]:
        if home.rstrip("/") in known:
            continue
        try:
            src = await inspect_site(fetcher, home, province)
        except Exception as e:  # noqa: BLE001
            log.info("site %s skipped: %s", home, e)
            continue
        if src:
            db.add(src)
            db.commit()
            known.add(src.base_url)
            found.append(src)
    return found
