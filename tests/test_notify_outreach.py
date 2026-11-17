import asyncio

import httpx
from conftest import site
from sqlalchemy import select

from immo_agent import notify
from immo_agent.agents import outreach
from immo_agent.core.config import get_settings
from immo_agent.core.models import ContactRequest, Listing, Property, Source, User
from immo_agent.sources.http import Fetcher


def test_telegram(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 5}]})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert notify.telegram_send("1", "hi", client) is False, "no token configured"
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "TOKEN")
    assert notify.telegram_send("1", "hi", client) is True
    assert notify.telegram_updates(None, client) == [{"update_id": 5}]
    assert "/botTOKEN/sendMessage" in str(seen[0].url)


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_email(monkeypatch):
    assert notify.send_email("a@b.be", "s", "b") is False, "no SMTP configured"
    monkeypatch.setattr(get_settings(), "smtp_host", "smtp.test")
    monkeypatch.setattr(get_settings(), "smtp_user", "u")
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    assert notify.send_email("a@b.be", "Subject", "Body", html="<b>x</b>", reply_to="r@b.be")
    msg = FakeSMTP.sent[-1]
    assert msg["To"] == "a@b.be" and msg["Reply-To"] == "r@b.be"


def test_notify_admin(db, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send_email", lambda *a, **k: calls.append("mail"))
    monkeypatch.setattr(notify, "telegram_send", lambda *a, **k: calls.append("tg"))
    db.add(User(email="f@example.com", password_hash="x", is_admin=True, telegram_chat_id="9"))
    db.commit()
    notify.notify_admin("s", "b")
    assert calls == ["mail", "tg"]


def test_outreach_send_uses_address_without_storing_it(db, monkeypatch):
    mails = []
    monkeypatch.setattr(notify, "send_email", lambda to, s, b, **k: mails.append((to, k)) or True)
    db.add_all(
        [
            Source(id=1, name="A", kind="zabun", base_url="https://agency.test", list_url="https://agency.test/x"),
            Property(id=1),
            User(id=1, email="buyer@example.com", password_hash="x"),
        ]
    )
    db.add(Listing(id=1, property_id=1, source_id=1, url="https://agency.test/nl/te-koop/huis/1", title="Huis"))
    db.commit()
    get_settings().outreach_mode = "approve"
    req = outreach.draft(db, 1, 1, 1)
    assert req.status == "draft"
    page = '<a href="mailto:info@agency.test">mail</a> or other@gmail.com'

    async def go():
        async with Fetcher(transport=site({"https://agency.test/nl/te-koop/huis/1": page})) as f:
            req.status = "approved"
            return await outreach.send(db, req, f)

    assert asyncio.run(go()) is True
    assert mails == [("info@agency.test", {"reply_to": "buyer@example.com"})]
    stored = db.scalars(select(ContactRequest)).one()
    assert stored.status == "sent" and "info@agency.test" not in (stored.body + (stored.error or ""))
    assert outreach.agency_address("<p>none</p>", "https://a.test") is None
