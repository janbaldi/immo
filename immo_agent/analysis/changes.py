"""Change detection: hash key fields, record every price/status/description change as a listing event."""

from sqlalchemy.orm import Session

from immo_agent.core.db import utcnow
from immo_agent.core.models import Listing, ListingEvent, Property
from immo_agent.sources.base import sha
from immo_agent.sources.extract import RawListing

FIELDS = (
    "title",
    "price",
    "transaction",
    "status",
    "property_type",
    "street",
    "postal_code",
    "municipality",
    "surface",
    "bedrooms",
    "epc_label",
    "epc_score",
    "flood_p",
    "flood_g",
    "description",
    "photos",
    "is_auction",
    "published_at",
)
PROPERTY_FIELDS = (
    "street",
    "postal_code",
    "municipality",
    "property_type",
    "surface",
    "bedrooms",
    "epc_label",
    "epc_score",
    "flood_p",
    "flood_g",
    "lat",
    "lon",
)


def content_hash(raw: RawListing) -> str:
    return sha(repr((raw.price, raw.status, raw.description)))


def event(db: Session, listing: Listing, type_: str, old: object = None, new: object = None) -> ListingEvent:
    ev = ListingEvent(
        listing_id=listing.id,
        property_id=listing.property_id,
        type=type_,
        old_value=None if old is None else str(old)[:200],
        new_value=None if new is None else str(new)[:200],
    )
    db.add(ev)
    return ev


def apply_update(db: Session, listing: Listing, raw: RawListing) -> list[ListingEvent]:
    """Compare a fresh parse with the stored listing and record what changed."""
    events: list[ListingEvent] = []
    if listing.status == "removed":
        events.append(event(db, listing, "relisted", "removed", raw.status))
    new_hash = content_hash(raw)
    if new_hash != listing.content_hash:
        if raw.price and listing.price and raw.price != listing.price:
            events.append(
                event(db, listing, "price_drop" if raw.price < listing.price else "price_increase", listing.price, raw.price)
            )
        if raw.status != listing.status and listing.status != "removed":
            events.append(event(db, listing, "status_change", listing.status, raw.status))
        if (raw.description or "") != (listing.description or "") and listing.description:
            events.append(event(db, listing, "description_change"))
    fill(listing, raw)
    return events


def fill(listing: Listing, raw: RawListing) -> None:
    for f in FIELDS:
        value = getattr(raw, f)
        if value not in (None, "", []):
            setattr(listing, f, value)
    listing.status = raw.status
    listing.content_hash = content_hash(raw)
    listing.last_seen_at = utcnow()


def sync_property(prop: Property, listing: Listing, raw: RawListing) -> None:
    """Property keeps the best known value of each field (latest non-empty)."""
    for f in PROPERTY_FIELDS:
        value = getattr(raw, f)
        if value not in (None, ""):
            setattr(prop, f, value)
    if listing.transaction == "sale":
        prop.price = listing.price or prop.price
        prop.status = listing.status
    prop.is_auction = prop.is_auction or raw.is_auction


def mark_removed(db: Session, listing: Listing) -> ListingEvent:
    old = listing.status
    listing.status = "removed"
    return event(db, listing, "removed", old, "removed")
