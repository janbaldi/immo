"""Free public enrichment. Each adapter returns a dict or None, logs failures and never raises.

Endpoints are configured through env vars because none of these services publishes a stable, documented
JSON API for this use (see DECISIONS.md, Needs verification). An unconfigured adapter returns None.
"""

import csv
import io
import logging
import os
import zipfile
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from immo_agent.core.db import utcnow
from immo_agent.core.models import Enrichment, MunicipalityStat, Property
from immo_agent.sources.http import Fetcher

log = logging.getLogger(__name__)
CACHE_DAYS = 30
STATBEL_URL = "https://statbel.fgov.be/sites/default/files/files/opendata/immo/vastgoed_2010_9999.zip"


def _params(p: Property) -> dict[str, str]:
    return {"street": p.street or "", "postal_code": p.postal_code or "", "municipality": p.municipality or ""}


async def _lookup(fetcher: Fetcher, env: str, p: Property, keep: tuple[str, ...]) -> dict[str, Any] | None:
    url = os.environ.get(env)
    if not url or not p.street:
        return None
    data = await fetcher.get_json(url, **_params(p))
    if not isinstance(data, dict):
        return None
    out = {k: data[k] for k in keep if k in data}
    return out or None


async def epc(fetcher: Fetcher, p: Property) -> dict[str, Any] | None:
    """VEKA 'Zoek een EPC' by address -> label, score, certificate date."""
    return await _lookup(fetcher, "EPC_LOOKUP_URL", p, ("label", "score", "date"))


async def flood(fetcher: Fetcher, p: Property) -> dict[str, Any] | None:
    """waterinfo.be information duty -> P-score and G-score (A = low risk ... D = high risk)."""
    return await _lookup(fetcher, "FLOOD_LOOKUP_URL", p, ("p_score", "g_score"))


async def permits(fetcher: Fetcher, p: Property) -> dict[str, Any] | None:
    """Omgevingsloket reporting -> number of recent permits near the address."""
    return await _lookup(fetcher, "PERMITS_LOOKUP_URL", p, ("count", "recent"))


async def statbel(fetcher: Fetcher, p: Property, db: Session) -> dict[str, Any] | None:
    row = db.scalars(
        select(MunicipalityStat)
        .where(MunicipalityStat.municipality.ilike(p.municipality or "-"))
        .order_by(MunicipalityStat.year.desc())
    ).first()
    return {"year": row.year, "median": row.median, "q1": row.q1, "q3": row.q3, "type": row.property_type} if row else None


Adapter = Callable[[Fetcher, Property], Awaitable[dict[str, Any] | None]]
ADAPTERS: dict[str, Adapter] = {"epc": epc, "flood": flood, "permits": permits}


async def enrich(db: Session, fetcher: Fetcher, p: Property) -> dict[str, dict[str, Any] | None]:
    results: dict[str, dict[str, Any] | None] = {}
    fresh_after = utcnow() - timedelta(days=CACHE_DAYS)
    for kind, fn in [*ADAPTERS.items(), ("statbel", None)]:
        cached = db.scalars(select(Enrichment).where(Enrichment.property_id == p.id, Enrichment.kind == kind)).first()
        if cached and cached.fetched_at > fresh_after and cached.ok:
            results[kind] = cached.value
            continue
        try:
            value = await statbel(fetcher, p, db) if fn is None else await fn(fetcher, p)
        except Exception as e:  # noqa: BLE001 - enrichment must never crash the pipeline
            log.warning("enrichment %s failed for property %s: %s", kind, p.id, e)
            value = None
        if cached is None:
            cached = Enrichment(property_id=p.id, kind=kind)
            db.add(cached)
        cached.value, cached.ok, cached.fetched_at = value, value is not None, utcnow()
        results[kind] = value
    if (epc_value := results.get("epc")) and not p.epc_label:
        p.epc_label, p.epc_score = epc_value.get("label"), epc_value.get("score")
    if (f := results.get("flood")) and not p.flood_p:
        p.flood_p, p.flood_g = f.get("p_score"), f.get("g_score")
    p.enriched_at = utcnow()
    return results


def _col(header: list[str], *needles: str) -> int | None:
    for i, h in enumerate(header):
        if all(n in h.upper() for n in needles):
            return i
    return None


def _ptype(text: str) -> str:
    t = text.lower()
    return "apartment" if "appart" in t else "house" if any(w in t for w in ("huis", "huiz", "villa", "woning")) else "other"


def parse_statbel(content: bytes) -> list[MunicipalityStat]:
    """Statbel 'vastgoed' open data: one row per year x municipality x property type."""
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith((".txt", ".csv")))
            content = z.read(name)
    text = content.decode("utf-8-sig", errors="replace")
    dialect = csv.Sniffer().sniff(text[:5000], delimiters="|;,\t")
    rows = list(csv.reader(io.StringIO(text), dialect))
    header = [h.strip() for h in rows[0]]
    idx = {
        "year": _col(header, "YEAR") if _col(header, "YEAR") is not None else _col(header, "JAAR"),
        "muni": _col(header, "REFNIS", "NL") if _col(header, "REFNIS", "NL") is not None else _col(header, "GEMEENTE"),
        "type": _col(header, "TYPE", "NL"),
        "median": _col(header, "P50") if _col(header, "P50") is not None else _col(header, "MEDIAN"),
        "q1": _col(header, "P25"),
        "q3": _col(header, "P75"),
        "n": _col(header, "TRANSACTION"),
    }
    if idx["year"] is None or idx["muni"] is None or idx["median"] is None:
        raise ValueError(f"Unrecognised Statbel header: {header}")

    def num(row: list[str], key: str) -> float | None:
        i = idx[key]
        try:
            return float(row[i].replace(",", ".")) if i is not None and row[i].strip() else None
        except ValueError:
            return None

    out = []
    for row in rows[1:]:
        if len(row) < len(header):
            continue
        n = num(row, "n")
        out.append(
            MunicipalityStat(
                municipality=row[idx["muni"]].strip(),
                year=int(float(row[idx["year"]])),
                property_type=_ptype(row[idx["type"]]) if idx["type"] is not None else "house",
                median=num(row, "median"),
                q1=num(row, "q1"),
                q3=num(row, "q3"),
                n=int(n) if n is not None else None,
            )
        )
    return out


async def import_statbel(db: Session, fetcher: Fetcher, url: str | None = None) -> int:
    page = await fetcher.get(url or os.environ.get("STATBEL_URL", STATBEL_URL))
    if page.status >= 400:
        raise RuntimeError(f"Statbel download failed: HTTP {page.status}")
    stats = parse_statbel(page.content)
    db.execute(delete(MunicipalityStat))
    db.add_all(stats)
    db.commit()
    return len(stats)
