"""Matcher: turns new listing events into alerts for matching search profiles, sent immediately."""

import html
import logging
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from immo_agent import notify
from immo_agent.analysis.valuation import latest_valuation
from immo_agent.core.config import get_settings
from immo_agent.core.db import utcnow
from immo_agent.core.models import Alert, Listing, ListingEvent, Property, SearchProfile, User, Valuation
from immo_agent.sources.extract import EPC_LABELS

log = logging.getLogger(__name__)
ALERT_EVENTS = ("new", "price_drop", "relisted")
FLOOD_ORDER = "ABCD"
BRUSSELS = ZoneInfo("Europe/Brussels")


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def matches(profile: SearchProfile, prop: Property, val: Valuation | None = None) -> bool:
    if prop.status in ("sold", "removed"):
        return False
    area, postal = {_norm(m) for m in profile.municipalities or []}, profile.postal_codes or []
    if (area or postal) and _norm(prop.municipality) not in area and prop.postal_code not in postal:
        return False
    if (profile.property_types or []) and prop.property_type not in (profile.property_types or []):
        return False
    if prop.price is None and (profile.min_price or profile.max_price):
        return False
    if profile.min_price and prop.price and prop.price < profile.min_price:
        return False
    if profile.max_price and prop.price and prop.price > profile.max_price:
        return False
    if profile.min_bedrooms and (prop.bedrooms or 0) < profile.min_bedrooms:
        return False
    if profile.min_surface and (prop.surface or 0) < profile.min_surface:
        return False
    # Unknown EPC or flood score does not block an alert: speed matters more; the alert shows "unknown".
    if profile.epc_max and prop.epc_label in EPC_LABELS and EPC_LABELS.index(prop.epc_label) > EPC_LABELS.index(profile.epc_max):
        return False
    if profile.flood_max and prop.flood_p and FLOOD_ORDER.index(prop.flood_p) > FLOOD_ORDER.index(profile.flood_max):
        return False
    if profile.investor and profile.min_yield and (val is None or val.gross_yield is None or val.gross_yield < profile.min_yield):
        return False
    return True


def in_quiet_hours(user: User, now: datetime | None = None) -> bool:
    if user.quiet_start is None or user.quiet_end is None or user.quiet_start == user.quiet_end:
        return False
    hour = (now or datetime.now(BRUSSELS)).hour
    if user.quiet_start < user.quiet_end:
        return user.quiet_start <= hour < user.quiet_end
    return hour >= user.quiet_start or hour < user.quiet_end


def euro(value: float | int | None) -> str:
    return "?" if value is None else f"€ {value:,.0f}".replace(",", ".")


def message(alert: Alert, val: Valuation | None) -> str:
    p, lst = alert.property, alert.listing
    label = {"new": "🆕 New", "price_drop": "📉 Price drop", "relisted": "🔁 Relisted"}.get(alert.event_type, alert.event_type)
    if p.is_auction:
        label += " · Biddit"
    mins = int((utcnow() - p.first_seen_anywhere_at).total_seconds() // 60)
    lines = [
        f"<b>{label}: {html.escape(lst.title or p.property_type or 'Property')}</b>",
        f"{euro(p.price)} · {html.escape(p.municipality or '?')} {p.postal_code or ''}",
        f"{p.bedrooms or '?'} bedrooms · {p.surface or '?'} m² · EPC {p.epc_label or 'unknown'}"
        f" · flood P {p.flood_p or 'unknown'}",
    ]
    if val and val.fair_value:
        lines.append(f"Fair value {euro(val.fair_value)} (80%: {euro(val.low)}–{euro(val.high)})")
    elif val and val.note:
        lines.append(f"Fair value: {val.note}")
    if val and val.gross_yield:
        lines.append(f"Gross yield {val.gross_yield:.1%} (rent ≈ {euro(val.rent_estimate)}/month)")
    lines.append(f"First seen {mins} min ago on {html.escape(lst.source.name)}")
    lines.append(f"Source: {lst.url}")
    lines.append(f"Details: {get_settings().base_url}/a/{alert.token}")
    return "\n".join(lines)


def deliver(db: Session, alert: Alert) -> bool:
    user = db.get(User, alert.user_id)
    assert user is not None
    if in_quiet_hours(user):
        return False
    val = latest_valuation(db, alert.property_id)
    text = message(alert, val)
    channels = []
    if user.telegram_chat_id and notify.telegram_send(user.telegram_chat_id, text):
        channels.append("telegram")
    if user.email_alerts:
        plain = text.replace("<b>", "").replace("</b>", "")
        if notify.send_email(user.email, plain.splitlines()[0][:150], html.unescape(plain)):
            channels.append("email")
    if not channels:
        return False
    alert.channel, alert.sent_at = "+".join(channels), utcnow()
    return True


def process(db: Session) -> list[Alert]:
    events = list(db.scalars(select(ListingEvent).where(ListingEvent.matched.is_(False)).order_by(ListingEvent.id)))
    profiles = list(db.scalars(select(SearchProfile).join(User).where(SearchProfile.active.is_(True), User.active.is_(True))))
    created: list[Alert] = []
    for ev in events:
        ev.matched = True
        listing = db.get(Listing, ev.listing_id)
        if ev.type not in ALERT_EVENTS or listing is None or listing.transaction != "sale":
            continue
        prop = listing.property
        val = latest_valuation(db, prop.id)
        for profile in profiles:
            if not matches(profile, prop, val):
                continue
            # One "new" alert per user per property, even when it appears on several sources.
            q = select(Alert.id).where(Alert.user_id == profile.user_id, Alert.property_id == prop.id)
            if ev.type == "new":
                if db.scalars(q).first():
                    continue
            elif db.scalars(q.where(Alert.event_type == ev.type, Alert.created_at > ev.at - timedelta(hours=1))).first():
                continue
            alert = Alert(
                user_id=profile.user_id,
                profile_id=profile.id,
                property_id=prop.id,
                listing_id=listing.id,
                event_type=ev.type,
                token=secrets.token_urlsafe(16),
            )
            db.add(alert)
            db.flush()
            created.append(alert)
    db.commit()
    for alert in created:
        deliver(db, alert)
        _maybe_outreach(db, alert)
    db.commit()
    return created


def flush_pending(db: Session, max_age_hours: int = 24) -> int:
    """Retry alerts held back by quiet hours or a failed channel."""
    pending = db.scalars(
        select(Alert).where(Alert.sent_at.is_(None), Alert.created_at > utcnow() - timedelta(hours=max_age_hours))
    )
    sent = sum(deliver(db, a) for a in pending)
    db.commit()
    return sent


def _maybe_outreach(db: Session, alert: Alert) -> None:
    if get_settings().outreach_enabled and alert.event_type == "new":
        from immo_agent.agents import outreach

        outreach.draft(db, alert.user_id, alert.property_id, alert.listing_id)
