"""Data model. Property fields only: no seller/agent names, phone numbers or e-mail addresses."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from immo_agent.core.db import Base, utcnow


def _pk() -> Mapped[int]:
    return mapped_column(Integer, primary_key=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = _pk()
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    plan: Mapped[str] = mapped_column(String(20), default="home")  # home | investor (no payments in MVP)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    telegram_link_code: Mapped[str | None] = mapped_column(String(32))
    email_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    quiet_start: Mapped[int | None] = mapped_column(Integer)  # hour 0-23, Belgian local time
    quiet_end: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    profiles: Mapped[list["SearchProfile"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class SearchProfile(Base):
    __tablename__ = "search_profiles"
    id: Mapped[int] = _pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100), default="My search")
    municipalities: Mapped[list[str]] = mapped_column(JSON, default=list)
    postal_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    property_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    min_price: Mapped[int | None] = mapped_column(Integer)
    max_price: Mapped[int | None] = mapped_column(Integer)
    min_bedrooms: Mapped[int | None] = mapped_column(Integer)
    min_surface: Mapped[int | None] = mapped_column(Integer)
    epc_max: Mapped[str | None] = mapped_column(String(3))  # worst acceptable label, e.g. "C"
    flood_max: Mapped[str | None] = mapped_column(String(1))  # worst acceptable P-score, e.g. "B"
    investor: Mapped[bool] = mapped_column(Boolean, default=False)
    min_yield: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    user: Mapped[User] = relationship(back_populates="profiles")

    @property
    def segment(self) -> str:
        return "investor" if self.investor else "home"


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # zabun|whise|omnicasa|generic|biddit|notary|immoweb|zimmo
    base_url: Mapped[str] = mapped_column(String(500), unique=True)
    list_url: Mapped[str] = mapped_column(String(500))
    province: Mapped[str] = mapped_column(String(50), default="antwerpen")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # kill switch
    is_portal: Mapped[bool] = mapped_column(Boolean, default=False)
    review_status: Mapped[str] = mapped_column(String(20), default="active")  # pending|active|rejected
    poll_minutes: Mapped[int] = mapped_column(Integer, default=10)
    use_browser: Mapped[bool] = mapped_column(Boolean, default=False)
    options: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # connector settings, e.g. API params
    health: Mapped[str] = mapped_column(String(10), default="unknown")  # green|amber|red|unknown
    health_note: Mapped[str | None] = mapped_column(Text)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime)
    backfilled_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceRun(Base):
    __tablename__ = "source_runs"
    id: Mapped[int] = _pk()
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    full_crawl: Mapped[bool] = mapped_column(Boolean, default=False)
    robots_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    listings_found: Mapped[int] = mapped_column(Integer, default=0)
    details_fetched: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    changed_count: Mapped[int] = mapped_column(Integer, default=0)
    parse_errors: Mapped[int] = mapped_column(Integer, default=0)
    missing_critical: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class Property(Base):
    __tablename__ = "properties"
    id: Mapped[int] = _pk()
    address_norm: Mapped[str | None] = mapped_column(String(300), index=True)
    street: Mapped[str | None] = mapped_column(String(200))
    postal_code: Mapped[str | None] = mapped_column(String(10), index=True)
    municipality: Mapped[str | None] = mapped_column(String(100), index=True)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    property_type: Mapped[str | None] = mapped_column(String(30))
    price: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="for_sale")
    surface: Mapped[float | None] = mapped_column(Float)
    bedrooms: Mapped[int | None] = mapped_column(Integer)
    epc_label: Mapped[str | None] = mapped_column(String(3))
    epc_score: Mapped[float | None] = mapped_column(Float)
    flood_p: Mapped[str | None] = mapped_column(String(1))
    flood_g: Mapped[str | None] = mapped_column(String(1))
    is_auction: Mapped[bool] = mapped_column(Boolean, default=False)
    photo_hash: Mapped[str | None] = mapped_column(String(32))
    first_seen_anywhere_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    portal_first_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    portal_url: Mapped[str | None] = mapped_column(String(500))
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime)
    listings: Mapped[list["Listing"]] = relationship(back_populates="property")


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source_id", "url"),)
    id: Mapped[int] = _pk()
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    url: Mapped[str] = mapped_column(String(500))
    external_id: Mapped[str | None] = mapped_column(String(100))
    transaction: Mapped[str] = mapped_column(String(10), default="sale")  # sale | rent
    title: Mapped[str | None] = mapped_column(String(300))
    price: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="for_sale")  # for_sale|under_option|sold|removed
    property_type: Mapped[str | None] = mapped_column(String(30))
    street: Mapped[str | None] = mapped_column(String(200))
    postal_code: Mapped[str | None] = mapped_column(String(10))
    municipality: Mapped[str | None] = mapped_column(String(100))
    surface: Mapped[float | None] = mapped_column(Float)
    bedrooms: Mapped[int | None] = mapped_column(Integer)
    epc_label: Mapped[str | None] = mapped_column(String(3))
    epc_score: Mapped[float | None] = mapped_column(Float)
    flood_p: Mapped[str | None] = mapped_column(String(1))
    flood_g: Mapped[str | None] = mapped_column(String(1))
    description: Mapped[str | None] = mapped_column(Text)
    photos: Mapped[list[str]] = mapped_column(JSON, default=list)  # URLs only, hotlinked
    is_auction: Mapped[bool] = mapped_column(Boolean, default=False)
    card_hash: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    property: Mapped[Property] = relationship(back_populates="listings")
    source: Mapped[Source] = relationship()


class ListingEvent(Base):
    __tablename__ = "listing_events"
    id: Mapped[int] = _pk()
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    type: Mapped[str] = mapped_column(String(20))  # new|price_drop|price_increase|status_change|relisted|removed
    old_value: Mapped[str | None] = mapped_column(String(200))
    new_value: Mapped[str | None] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    matched: Mapped[bool] = mapped_column(Boolean, default=False)


class Enrichment(Base):
    __tablename__ = "enrichments"
    __table_args__ = (UniqueConstraint("property_id", "kind"),)
    id: Mapped[int] = _pk()
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # epc|flood|statbel|permits
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Valuation(Base):
    __tablename__ = "valuations"
    id: Mapped[int] = _pk()
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    asking_price: Mapped[int | None] = mapped_column(Integer)
    fair_value: Mapped[float | None] = mapped_column(Float)  # None = insufficient data
    low: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    n_comparables: Mapped[int] = mapped_column(Integer, default=0)
    rent_estimate: Mapped[float | None] = mapped_column(Float)
    gross_yield: Mapped[float | None] = mapped_column(Float)
    motivated_score: Mapped[int] = mapped_column(Integer, default=0)
    model_version: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(String(200))


class ModelRun(Base):
    __tablename__ = "model_runs"
    id: Mapped[int] = _pk()
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    version: Mapped[str] = mapped_column(String(40))
    n_train: Mapped[int] = mapped_column(Integer)
    n_test: Mapped[int] = mapped_column(Integer)
    mape: Mapped[float | None] = mapped_column(Float)
    interval_coverage: Mapped[float | None] = mapped_column(Float)


class MunicipalityStat(Base):
    """Statbel open data: median and quartile sale prices per municipality."""

    __tablename__ = "municipality_stats"
    id: Mapped[int] = _pk()
    municipality: Mapped[str] = mapped_column(String(100), index=True)
    year: Mapped[int] = mapped_column(Integer)
    property_type: Mapped[str] = mapped_column(String(30))
    median: Mapped[float | None] = mapped_column(Float)
    q1: Mapped[float | None] = mapped_column(Float)
    q3: Mapped[float | None] = mapped_column(Float)
    n: Mapped[int | None] = mapped_column(Integer)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = _pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("search_profiles.id"))
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"))
    event_type: Mapped[str] = mapped_column(String(20))
    channel: Mapped[str | None] = mapped_column(String(20))  # telegram|email|telegram+email|feed
    token: Mapped[str] = mapped_column(String(32), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime)
    listing: Mapped[Listing] = relationship()
    property: Mapped[Property] = relationship()


class ContactRequest(Base):
    __tablename__ = "contact_requests"
    id: Mapped[int] = _pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|approved|sent|failed
    body: Mapped[str] = mapped_column(Text)  # no personal data: the agency address is used at send time only
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(String(300))


class UserAction(Base):
    __tablename__ = "user_actions"
    id: Mapped[int] = _pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    action: Mapped[str] = mapped_column(String(20))  # viewed|saved|contacted|visit_booked|bid|bought
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Purchase(Base):
    __tablename__ = "purchases"
    id: Mapped[int] = _pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    price_paid: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SegmentCap(Base):
    __tablename__ = "segment_caps"
    __table_args__ = (UniqueConstraint("municipality", "segment"),)
    id: Mapped[int] = _pk()
    municipality: Mapped[str] = mapped_column(String(100))
    segment: Mapped[str] = mapped_column(String(20))  # home|investor
    max_active: Mapped[int] = mapped_column(Integer)


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id: Mapped[int] = _pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DedupReview(Base):
    __tablename__ = "dedup_reviews"
    id: Mapped[int] = _pk()
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), unique=True)
    correct: Mapped[bool] = mapped_column(Boolean)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
