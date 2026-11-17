"""14-day acceptance report (`make report`), computed from live data only."""

from datetime import timedelta
from statistics import median

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from immo_agent import metrics
from immo_agent.core.db import utcnow
from immo_agent.core.models import (
    Alert,
    ContactRequest,
    DedupReview,
    Listing,
    ListingEvent,
    SourceRun,
    Valuation,
)
from immo_agent.core.pii import find_pii

PII_COLUMN_WORDS = ("phone", "telefoon", "gsm", "email", "mail", "seller", "verkoper", "agent_name", "contact")


def pii_scan(db: Session) -> list[str]:
    """Schema check (no personal-data columns on property tables) + content scan of free-text fields."""
    findings = []
    insp = inspect(db.get_bind())
    for table in ("properties", "listings", "listing_events", "enrichments", "valuations", "contact_requests"):
        for col in insp.get_columns(table):
            if any(w in col["name"].lower() for w in PII_COLUMN_WORDS):
                findings.append(f"column {table}.{col['name']}")
    for lid, title, desc in db.execute(select(Listing.id, Listing.title, Listing.description)):
        for hit in find_pii(title) + find_pii(desc):
            findings.append(f"listing {lid}: {hit}")
    for cid, body in db.execute(select(ContactRequest.id, ContactRequest.body)):
        findings += [f"contact_request {cid}: {hit}" for hit in find_pii(body)]
    return findings


def interval_coverage_after_outcome(db: Session) -> tuple[float | None, int]:
    """For listings that later dropped in price or sold: was the later price inside the earlier 80% interval?"""
    hits = total = 0
    for ev in db.scalars(select(ListingEvent).where(ListingEvent.type.in_(("price_drop", "status_change")))):
        if ev.type == "status_change" and ev.new_value != "sold":
            continue
        val = db.scalars(
            select(Valuation)
            .where(Valuation.property_id == ev.property_id, Valuation.fair_value.is_not(None), Valuation.at < ev.at)
            .order_by(Valuation.id.desc())
        ).first()
        if not val or val.low is None or val.high is None:
            continue
        outcome = float(ev.new_value) if ev.type == "price_drop" and ev.new_value else val.asking_price
        if outcome is None:
            continue
        total += 1
        hits += val.low <= outcome <= val.high
    return (hits / total if total else None), total


def build(db: Session, days: int = 14, dead_links: list[str] | None = None) -> tuple[str, bool]:
    since = utcnow() - timedelta(days=days)
    first_run = db.scalar(select(func.min(SourceRun.started_at)))
    live_days = (utcnow() - first_run).days if first_run else 0
    m = metrics.compute_all(db, days=days)
    checks: list[tuple[str, str, bool | None]] = []

    tta = m["time_to_alert"]
    checks.append(("Median time to alert < 15 min", tta["headline"], tta["value"] is not None and tta["value"] < 15))
    lat = m["detection_latency"]
    per_source = "; ".join(f"{k}: {v['median']:.0f} min (n={v['n']})" for k, v in lat.get("per_source", {}).items())
    checks.append(("Detection latency per source", per_source or "no source exposes publication times yet", None))
    lead = m["lead_time_vs_portals"]
    checks.append(("Share seen before the portals", lead["headline"], None))
    pii = pii_scan(db)
    checks.append(
        ("Zero personal-data fields stored", f"{len(pii)} findings" + (": " + ", ".join(pii[:5]) if pii else ""), not pii)
    )
    alerts = list(db.scalars(select(Alert).where(Alert.created_at >= since)))
    missing = [a.id for a in alerts if not (a.listing and a.listing.url.startswith("http"))]
    link_ok = not missing and not dead_links
    detail = f"{len(alerts)} alerts, {len(missing)} without source URL"
    if dead_links is not None:
        detail += f", {len(dead_links)} dead links" + (f" ({', '.join(dead_links[:3])})" if dead_links else "")
    checks.append(("Every alert links to its live source", detail, link_ok))
    reviews = list(db.scalars(select(DedupReview.correct)))
    precision = sum(reviews) / len(reviews) if reviews else None
    checks.append(
        (
            "Dedup precision ≥ 95% on 200 labelled properties",
            f"{precision:.1%} on {len(reviews)} labels" if precision is not None else "no labels yet (Admin → Dedup review)",
            precision is not None and len(reviews) >= 200 and precision >= 0.95,
        )
    )
    cov, n = interval_coverage_after_outcome(db)
    checks.append(
        (
            "Fair-value interval coverage on later price drops / sales",
            f"{cov:.0%} (n={n})" if cov is not None else "no outcomes yet",
            None,
        )
    )
    checks.append((f"Live running ≥ {days} days", f"{live_days} days since the first source run", live_days >= days))

    passed = all(ok is not False for _, _, ok in checks)
    lines = [
        f"# Immo Agent acceptance report ({utcnow():%Y-%m-%d %H:%M} UTC, last {days} days)",
        "",
        "| Check | Result | Status |",
        "|---|---|---|",
    ]
    for name, result, ok in checks:
        lines.append(f"| {name} | {result} | {'PASS' if ok else 'FAIL' if ok is False else 'info'} |")
    lines += ["", "## All metrics", ""] + [f"- **{v['title']}**: {v['headline']}" for v in m.values()]
    lines += ["", f"Overall: **{'PASS' if passed else 'NOT YET'}**"]
    if tta["value"] is not None:
        mins = sorted(r["minutes"] for r in tta["rows"])
        lines.append(f"\nTime to alert: median {median(mins)} min, p90 {mins[int(0.9 * (len(mins) - 1))]} min")
    return "\n".join(lines), passed
