import hashlib
import re
from dataclasses import dataclass

from immo_agent.core.models import Source
from immo_agent.sources.extract import RawListing
from immo_agent.sources.http import Fetcher


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def external_id_from_url(url: str) -> str | None:
    nums = re.findall(r"\d{4,}", url.split("?")[0])
    return nums[-1] if nums else None


@dataclass
class IndexItem:
    url: str
    card_hash: str
    external_id: str | None = None
    raw: RawListing | None = None  # set when the index already carries all fields (JSON APIs)


class Connector:
    """One connector per template family. index() lists current items cheaply; detail() parses one item."""

    def __init__(self, source: Source, fetcher: Fetcher):
        self.source = source
        self.fetcher = fetcher

    async def index(self, full: bool = False) -> list[IndexItem]:
        raise NotImplementedError

    async def detail(self, item: IndexItem) -> RawListing:
        raise NotImplementedError
