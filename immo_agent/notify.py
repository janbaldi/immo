"""Alert channels: Telegram bot (primary) and e-mail over SMTP."""

import logging
import smtplib
from email.message import EmailMessage

import httpx

from immo_agent.core.config import get_settings

log = logging.getLogger(__name__)
TELEGRAM_API = "https://api.telegram.org"


def telegram_send(chat_id: str, text: str, client: httpx.Client | None = None) -> bool:
    token = get_settings().telegram_bot_token
    if not token or not chat_id:
        return False
    try:
        c = client or httpx.Client(timeout=15)
        r = c.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False},
        )
        return r.status_code == 200 and r.json().get("ok", False)
    except httpx.HTTPError as e:
        log.warning("telegram send failed: %s", e)
        return False


def telegram_updates(offset: int | None = None, client: httpx.Client | None = None) -> list[dict]:
    token = get_settings().telegram_bot_token
    if not token:
        return []
    try:
        c = client or httpx.Client(timeout=15)
        r = c.get(f"{TELEGRAM_API}/bot{token}/getUpdates", params={"offset": offset, "timeout": 0})
        return r.json().get("result", []) if r.status_code == 200 else []
    except httpx.HTTPError as e:
        log.warning("telegram getUpdates failed: %s", e)
        return []


def send_email(to: str, subject: str, body: str, html: str | None = None, reply_to: str | None = None) -> bool:
    s = get_settings()
    if not s.smtp_host or not to:
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as e:
        log.warning("email to %s failed: %s", to, e)
        return False


def notify_admin(subject: str, body: str) -> None:
    """Founder alerts: e-mail to ADMIN_EMAIL plus Telegram to every admin who linked a chat."""
    from sqlalchemy import select

    from immo_agent.core.db import session
    from immo_agent.core.models import User

    send_email(get_settings().admin_email, subject, body)
    with session() as db:
        for chat in db.scalars(select(User.telegram_chat_id).where(User.is_admin.is_(True), User.telegram_chat_id.is_not(None))):
            telegram_send(chat, f"<b>{subject}</b>\n{body}")  # type: ignore[arg-type]
