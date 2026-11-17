"""Outreach (phase 2, off by default): visit requests to the listing agency.

The agency address is read from the live listing page at send time and never stored. Drafts need one-click
approval unless OUTREACH_MODE=auto. Stored message bodies contain no personal data.
"""

import re
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from immo_agent import notify
from immo_agent.core import llm
from immo_agent.core.config import get_settings
from immo_agent.core.db import utcnow
from immo_agent.core.models import ContactRequest, Listing, User
from immo_agent.core.pii import EMAIL_RE
from immo_agent.sources.http import Fetcher

TEMPLATE = (
    "Goeiedag,\n\nIk ben geïnteresseerd in het pand {title} ({url}) en zou het graag bezoeken. "
    "Welke momenten zijn mogelijk?\n\nMet vriendelijke groeten"
)


def draft(db: Session, user_id: int, property_id: int, listing_id: int) -> ContactRequest:
    listing = db.get(Listing, listing_id)
    assert listing is not None
    body = TEMPLATE.format(title=listing.title or listing.municipality or "", url=listing.url)
    better = llm.complete_text(
        "Rewrite this short, polite Dutch visit request to a Belgian estate agency. "
        "Keep the URL. Do not add names or contact details.\n\n" + body
    )
    status = "approved" if get_settings().outreach_mode == "auto" else "draft"
    req = ContactRequest(user_id=user_id, property_id=property_id, listing_id=listing_id, body=better or body, status=status)
    db.add(req)
    db.commit()
    return req


def agency_address(html: str, page_url: str) -> str | None:
    """Prefer an address on the agency's own domain."""
    found = re.findall(r"mailto:([^\"'?]+)", html) + EMAIL_RE.findall(html)
    host = urlsplit(page_url).netloc.removeprefix("www.")
    return next((a for a in found if a.lower().endswith(host)), found[0] if found else None)


async def send(db: Session, req: ContactRequest, fetcher: Fetcher | None = None) -> bool:
    listing, user = db.get(Listing, req.listing_id), db.get(User, req.user_id)
    assert listing is not None and user is not None
    own = fetcher is None
    fetcher = fetcher or Fetcher()
    try:
        page = await fetcher.get(listing.url)
        to = agency_address(page.text, listing.url)
    finally:
        if own:
            await fetcher.client.aclose()
    if not to:
        req.status, req.error = "failed", "no agency e-mail address on the listing page"
    elif notify.send_email(to, f"Bezoekaanvraag: {listing.title or listing.url}"[:150], req.body, reply_to=user.email):
        req.status, req.sent_at, req.error = "sent", utcnow(), None
    else:
        req.status, req.error = "failed", "SMTP send failed"
    db.commit()
    return req.status == "sent"


async def send_approved(db: Session) -> int:
    """Worker job: send drafts the user approved (or all drafts in auto mode)."""
    reqs = list(db.scalars(select(ContactRequest).where(ContactRequest.status == "approved")))
    async with Fetcher() as fetcher:
        return sum([await send(db, r, fetcher) for r in reqs])
