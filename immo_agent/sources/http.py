"""Polite fetching: robots.txt (cached 24 h), a clear User-Agent and at most one request per domain per interval."""

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from immo_agent.core.config import get_settings

log = logging.getLogger(__name__)


class RobotsDisallowed(Exception):
    pass


@dataclass
class Page:
    url: str
    status: int
    text: str
    content: bytes = b""


class Fetcher:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, min_interval: float | None = None):
        s = get_settings()
        self.user_agent = s.user_agent
        self.min_interval = s.min_request_interval if min_interval is None else min_interval
        self.client = httpx.AsyncClient(
            transport=transport,
            timeout=s.http_timeout,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent, "Accept-Language": "nl-BE,nl;q=0.9"},
        )
        self._robots: dict[str, tuple[float, RobotFileParser]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()

    async def _wait_turn(self, domain: str) -> None:
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            delay = self._last.get(domain, 0) + self.min_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[domain] = time.monotonic()

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        domain = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(domain)
        if not cached or time.time() - cached[0] > 86400:
            rp = RobotFileParser()
            try:
                await self._wait_turn(parts.netloc)
                r = await self.client.get(domain + "/robots.txt")
                # 4xx: no robots.txt -> allowed; 5xx: be careful -> disallow everything.
                if r.status_code >= 500:
                    rp.parse(["User-agent: *", "Disallow: /"])
                else:
                    rp.parse(r.text.splitlines() if r.status_code < 400 else [])
            except httpx.HTTPError as e:
                log.warning("robots.txt fetch failed for %s: %s", domain, e)
                rp.parse([])
            cached = (time.time(), rp)
            self._robots[domain] = cached
        return cached[1].can_fetch(self.user_agent, url)

    async def get(self, url: str, browser: bool = False) -> Page:
        if not await self.allowed(url):
            raise RobotsDisallowed(url)
        await self._wait_turn(urlsplit(url).netloc)
        if browser and get_settings().enable_playwright:
            return Page(url, 200, await _render(url, self.user_agent))
        r = await self.client.get(url)
        return Page(str(r.url), r.status_code, r.text, r.content)

    async def get_json(self, url: str, **params: object) -> object:
        if not await self.allowed(url):
            raise RobotsDisallowed(url)
        await self._wait_turn(urlsplit(url).netloc)
        r = await self.client.get(url, params=params, headers={"Accept": "application/json"})  # type: ignore[arg-type]
        r.raise_for_status()
        return r.json()


async def _render(url: str, user_agent: str) -> str:  # pragma: no cover - needs a browser
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(user_agent=user_agent)
        await page.goto(url, wait_until="networkidle")
        html = await page.content()
        await browser.close()
        return html
