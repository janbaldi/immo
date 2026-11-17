import os

# Test settings: SQLite, no real channels, no politeness delay. Set before importing the app.
os.environ.update(
    {
        "DATABASE_URL": "sqlite://",
        "SECRET_KEY": "test",
        "SMTP_HOST": "",
        "TELEGRAM_BOT_TOKEN": "",
        "ADMIN_EMAIL": "",
        "LLM_PROVIDER": "none",
        "MIN_REQUEST_INTERVAL": "0",
        "PORTAL_CROSSCHECK_ENABLED": "false",
        "OUTREACH_ENABLED": "false",
    }
)

from collections.abc import Callable, Iterator  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from immo_agent.core import db as dbm  # noqa: E402
from immo_agent.core.config import get_settings  # noqa: E402
from immo_agent.core.db import Base  # noqa: E402
from immo_agent.core.models import Source  # noqa: E402


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch) -> Iterator[None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbm, "_engine", engine)
    monkeypatch.setattr(dbm, "_SessionLocal", sessionmaker(engine, expire_on_commit=False))
    s = get_settings()
    monkeypatch.setattr(s, "model_path", str(tmp_path / "model.pkl"))
    monkeypatch.setattr(s, "min_comparables", 30)
    from immo_agent.analysis import valuation

    valuation._cache.clear()
    yield
    engine.dispose()


@pytest.fixture
def db() -> Iterator[Session]:
    with dbm.session() as s:
        yield s


def site(pages: dict[str, str | bytes | tuple[int, str]]) -> httpx.MockTransport:
    """A fake web server for tests. Unknown URLs (including robots.txt) return 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = pages.get(url, pages.get(url.split("?")[0]))
        if body is None:
            return httpx.Response(404, text="not found")
        if isinstance(body, tuple):
            return httpx.Response(body[0], text=body[1])
        if isinstance(body, bytes):
            return httpx.Response(200, content=body)
        ctype = "application/json" if body.lstrip()[:1] in "[{" else "text/html"
        return httpx.Response(200, text=body, headers={"content-type": ctype})

    return httpx.MockTransport(handler)


@pytest.fixture
def make_source(db: Session) -> Callable[..., Source]:
    def make(**kw) -> Source:
        defaults = dict(
            name="Agency",
            kind="zabun",
            base_url="https://agency.test",
            list_url="https://agency.test/nl/te-koop",
            review_status="active",
            enabled=True,
        )
        src = Source(**{**defaults, **kw})
        db.add(src)
        db.commit()
        return src

    return make
