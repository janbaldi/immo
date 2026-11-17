"""The seven edge metrics: definitions, computation (global, per user, per profile) and CSV export."""

import csv
import io
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from immo_agent.core.config import get_settings
from immo_agent.core.db import utcnow
from immo_agent.core.models import (
    Alert,
    ContactRequest,
    Listing,
    ModelRun,
    Property,
    Purchase,
    Source,
    UserAction,
    Valuation,
)

DEFINITIONS: dict[str, dict[str, str]] = {
    "time_to_alert": {
        "title": "Time to alert",
        "definition": "alert.sent_at − property.first_seen_anywhere_at, in minutes, for alerts about new properties "
        "delivered by Telegram or e-mail. Target: median under 15 minutes.",
    },
    "lead_time_vs_portals": {
        "title": "Lead time vs portals",
        "definition": "portal_first_seen_at − first_seen_anywhere_at, in minutes, for properties later found on Immoweb "
        "or Zimmo. Positive = we saw it first. Only measured when the portal cross-check is enabled.",
    },
    "detection_latency": {
        "title": "Detection latency",
        "definition": "first_seen_anywhere_at − published_at, in minutes, where the source exposes a publication "
        "time. Measures how fresh polling really is. Listings from a source's initial backfill are excluded.",
    },
    "time_to_first_contact": {
        "title": "Time to first contact",
        "definition": "Earliest of (visit request sent, 'I contacted the agency' click) − alert.sent_at, in minutes.",
    },
    "price_vs_fair_value": {
        "title": "Price vs fair value",
        "definition": "(asking or paid price − fair value) / fair value. Negative = below fair value. Fair value is the "
        "model's median estimate; the 80% interval is shown next to it.",
    },
    "coverage": {
        "title": "Coverage",
        "definition": "New listings per source per day, and the share of all properties that each source found first.",
    },
    "model_quality": {
        "title": "Model quality",
        "definition": "Backtest of the fair-value model on 20% held-out real listings at each nightly retrain: MAPE "
        "(mean absolute % error of the median) and 80%-interval coverage (target ≈ 80%).",
    },
}


def _minutes(a: datetime, b: datetime) -> float:
    return round((a - b).total_seconds() / 60, 1)


def _result(
    name: str,
    values: list[float],
    rows: list[dict[str, Any]],
    headline: str | None = None,
    chart: dict[str, list[Any]] | None = None,
    unit: str = "min",
) -> dict[str, Any]:
    med = round(median(values), 1 if unit else 4) if values else None
    return {
        **DEFINITIONS[name],
        "value": med,
        "n": len(values),
        "headline": headline or (f"median {med} {unit} (n={len(values)})" if values else "no data yet"),
        "rows": rows,
        "chart": chart or {"labels": [r.get("at", "") for r in rows][-200:], "values": values[-200:]},
    }


def _alerts(db: Session, user_id: int | None, profile_id: int | None, since: datetime | None) -> list[Alert]:
    q = select(Alert).where(Alert.sent_at.is_not(None))
    if user_id:
        q = q.where(Alert.user_id == user_id)
    if profile_id:
        q = q.where(Alert.profile_id == profile_id)
    if since:
        q = q.where(Alert.sent_at >= since)
    return list(db.scalars(q.order_by(Alert.sent_at)))


def time_to_alert(
    db: Session, user_id: int | None = None, profile_id: int | None = None, since: datetime | None = None
) -> dict[str, Any]:
    rows, values = [], []
    for a in _alerts(db, user_id, profile_id, since):
        if a.event_type != "new" or a.channel in (None, "feed"):
            continue
        m = _minutes(a.sent_at, a.property.first_seen_anywhere_at)  # type: ignore[arg-type]
        values.append(m)
        rows.append(
            {
                "alert_id": a.id,
                "property_id": a.property_id,
                "at": a.sent_at.isoformat(),  # type: ignore[union-attr]
                "first_seen_anywhere_at": a.property.first_seen_anywhere_at.isoformat(),
                "minutes": m,
            }
        )
    return _result("time_to_alert", values, rows)


def lead_time_vs_portals(
    db: Session, user_id: int | None = None, profile_id: int | None = None, since: datetime | None = None
) -> dict[str, Any]:
    q = select(Property).where(Property.portal_first_seen_at.is_not(None))
    if user_id or profile_id:
        q = q.where(Property.id.in_([a.property_id for a in _alerts(db, user_id, profile_id, since)]))
    if since:
        q = q.where(Property.first_seen_anywhere_at >= since)
    props = list(db.scalars(q))
    if not props and not get_settings().portal_crosscheck_enabled:
        return _result("lead_time_vs_portals", [], [], headline="not measured (portal cross-check is off)")
    values = [_minutes(p.portal_first_seen_at, p.first_seen_anywhere_at) for p in props]  # type: ignore[arg-type]
    rows = [
        {
            "property_id": p.id,
            "at": p.first_seen_anywhere_at.isoformat(),
            "portal_url": p.portal_url,
            "portal_first_seen_at": str(p.portal_first_seen_at),
            "lead_minutes": v,
        }  # type: ignore[union-attr]
        for p, v in zip(props, values, strict=True)
    ]
    share = sum(v > 0 for v in values) / len(values) if values else 0
    res = _result("lead_time_vs_portals", values, rows)
    if values:
        res["headline"] = f"median {res['value']} min ahead; {share:.0%} seen before the portal (n={len(values)})"
    res["share_before_portal"] = share if values else None
    return res


def detection_latency(db: Session, since: datetime | None = None, **_: Any) -> dict[str, Any]:
    q = (
        select(Listing, Source)
        .join(Source)
        .where(Listing.published_at.is_not(None), Source.backfilled_at.is_not(None), Listing.first_seen_at > Source.backfilled_at)
    )
    if since:
        q = q.where(Listing.first_seen_at >= since)
    rows, values, per_source = [], [], defaultdict(list)
    for lst, src in db.execute(q):
        m = max(0.0, _minutes(lst.first_seen_at, lst.published_at))  # type: ignore[arg-type]
        values.append(m)
        per_source[src.name].append(m)
        rows.append(
            {
                "listing_id": lst.id,
                "source": src.name,
                "at": lst.first_seen_at.isoformat(),
                "published_at": str(lst.published_at),
                "minutes": m,
            }
        )
    res = _result(
        "detection_latency", values, rows, chart={"labels": list(per_source), "values": [median(v) for v in per_source.values()]}
    )
    res["per_source"] = {k: {"median": median(v), "n": len(v)} for k, v in per_source.items()}
    return res


def time_to_first_contact(
    db: Session, user_id: int | None = None, profile_id: int | None = None, since: datetime | None = None
) -> dict[str, Any]:
    rows, values = [], []
    for a in _alerts(db, user_id, profile_id, since):
        contacts = [
            c
            for c in db.scalars(
                select(ContactRequest.sent_at).where(
                    ContactRequest.user_id == a.user_id,
                    ContactRequest.property_id == a.property_id,
                    ContactRequest.sent_at.is_not(None),
                )
            )
        ]
        contacts += list(
            db.scalars(
                select(UserAction.at).where(
                    UserAction.user_id == a.user_id, UserAction.property_id == a.property_id, UserAction.action == "contacted"
                )
            )
        )
        sent = a.sent_at
        assert sent is not None
        later = [c for c in contacts if c is not None and c >= sent]
        if later:
            m = _minutes(min(later), sent)
            values.append(m)
            rows.append({"alert_id": a.id, "property_id": a.property_id, "at": sent.isoformat(), "minutes": m})
    return _result("time_to_first_contact", values, rows)


def _valuation_before(db: Session, property_id: int, when: datetime | None = None) -> Valuation | None:
    q = select(Valuation).where(Valuation.property_id == property_id, Valuation.fair_value.is_not(None))
    if when:
        q = q.where(Valuation.at <= when)
    return db.scalars(q.order_by(Valuation.id.desc()).limit(1)).first()


def price_vs_fair_value(
    db: Session, user_id: int | None = None, profile_id: int | None = None, since: datetime | None = None
) -> dict[str, Any]:
    if user_id or profile_id:
        pids = {a.property_id for a in _alerts(db, user_id, profile_id, since)}
    else:
        pids = set(db.scalars(select(Valuation.property_id).where(Valuation.fair_value.is_not(None))))
    rows, values = [], []
    for pid in sorted(pids):
        v = _valuation_before(db, pid)
        if v and v.asking_price and v.fair_value:
            gap = round((v.asking_price - v.fair_value) / v.fair_value, 4)
            values.append(gap)
            rows.append(
                {
                    "property_id": pid,
                    "kind": "asking",
                    "at": v.at.isoformat(),
                    "price": v.asking_price,
                    "fair_value": v.fair_value,
                    "low": v.low,
                    "high": v.high,
                    "gap": gap,
                }
            )
    pq = select(Purchase)
    if user_id:
        pq = pq.where(Purchase.user_id == user_id)
    paid = []
    for pur in db.scalars(pq):
        v = _valuation_before(db, pur.property_id, pur.at)
        if v and v.fair_value:
            gap = round((pur.price_paid - v.fair_value) / v.fair_value, 4)
            paid.append(gap)
            rows.append(
                {
                    "property_id": pur.property_id,
                    "kind": "paid",
                    "at": pur.at.isoformat(),
                    "price": pur.price_paid,
                    "fair_value": v.fair_value,
                    "low": v.low,
                    "high": v.high,
                    "gap": gap,
                }
            )
    res = _result("price_vs_fair_value", values, rows, unit="")
    if values:
        res["headline"] = f"median asking {median(values):+.1%} vs fair value (n={len(values)})"
    if paid:
        res["headline"] += f"; paid {median(paid):+.1%} (n={len(paid)})"
    res["paid_gap"] = median(paid) if paid else None
    return res


def coverage(db: Session, since: datetime | None = None, **_: Any) -> dict[str, Any]:
    since = since or utcnow() - timedelta(days=30)
    per_day: Counter[tuple[str, str]] = Counter()
    first: dict[int, tuple[datetime, str]] = {}
    for lst, src in db.execute(select(Listing, Source).join(Source).where(Listing.first_seen_at >= since)):
        per_day[(src.name, lst.first_seen_at.date().isoformat())] += 1
        if lst.property_id not in first or lst.first_seen_at < first[lst.property_id][0]:
            first[lst.property_id] = (lst.first_seen_at, src.name)
    firsts = Counter(name for _, name in first.values())
    total = sum(firsts.values())
    rows = [{"source": s, "day": d, "new_listings": n} for (s, d), n in sorted(per_day.items())]
    share = {s: round(n / total, 4) for s, n in firsts.most_common()} if total else {}
    res = _result(
        "coverage",
        [],
        rows,
        headline=f"{len(firsts)} sources found {total} properties first" if total else "no data yet",
        chart={"labels": list(share)[:30], "values": list(share.values())[:30]},
    )
    res["share_first"] = share
    return res


def model_quality(db: Session, **_: Any) -> dict[str, Any]:
    runs = list(db.scalars(select(ModelRun).order_by(ModelRun.id)))
    rows = [
        {
            "version": r.version,
            "at": r.at.isoformat(),
            "n_train": r.n_train,
            "n_test": r.n_test,
            "mape": r.mape,
            "interval_coverage": r.interval_coverage,
        }
        for r in runs
    ]
    if not runs:
        return _result("model_quality", [], [], headline="no model trained yet (needs ≥100 real listings)")
    last = runs[-1]
    return _result(
        "model_quality",
        [],
        rows,
        headline=f"MAPE {last.mape:.1%}, 80% interval coverage {last.interval_coverage:.0%} (test n={last.n_test})",
        chart={"labels": [r["at"] for r in rows], "values": [r["mape"] for r in rows]},
    )


METRICS: dict[str, Callable[..., dict[str, Any]]] = {
    "time_to_alert": time_to_alert,
    "lead_time_vs_portals": lead_time_vs_portals,
    "detection_latency": detection_latency,
    "time_to_first_contact": time_to_first_contact,
    "price_vs_fair_value": price_vs_fair_value,
    "coverage": coverage,
    "model_quality": model_quality,
}
USER_METRICS = ("time_to_alert", "lead_time_vs_portals", "time_to_first_contact", "price_vs_fair_value")


def compute_all(
    db: Session,
    user_id: int | None = None,
    profile_id: int | None = None,
    days: int | None = None,
    names: tuple[str, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    since = utcnow() - timedelta(days=days) if days else None
    return {n: METRICS[n](db, user_id=user_id, profile_id=profile_id, since=since) for n in (names or tuple(METRICS))}


def to_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        writer.writeheader()
        writer.writerows(rows)
    return buf.getvalue()
