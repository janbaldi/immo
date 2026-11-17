"""Web app: user side (feed, profiles, edge dashboard, settings, support) and admin side."""

import asyncio
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from immo_agent import metrics
from immo_agent.agents import matcher, outreach, scout
from immo_agent.analysis.valuation import latest_valuation, price_history
from immo_agent.core.config import get_settings, province_config
from immo_agent.core.db import get_db, utcnow
from immo_agent.core.models import (
    Alert,
    ContactRequest,
    DedupReview,
    Enrichment,
    Listing,
    ListingEvent,
    ModelRun,
    Property,
    Purchase,
    SearchProfile,
    SegmentCap,
    Source,
    SourceRun,
    SupportTicket,
    User,
    UserAction,
)
from immo_agent.sources.extract import EPC_LABELS, extract_listing
from immo_agent.sources.http import Fetcher

app = FastAPI(title="Immo Agent")
app.add_middleware(
    SessionMiddleware, secret_key=get_settings().secret_key, same_site="lax", https_only=False, max_age=60 * 60 * 24 * 30
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
hasher = PasswordHasher()
ACTIONS = ("saved", "contacted", "visit_booked", "bid", "bought")
FAQ = [
    (
        "How fast are alerts?",
        "Sources are checked every 5–15 minutes. When a new home matches one of your searches, the "
        "alert goes out immediately by Telegram and/or e-mail. Your 'My edge' page shows the measured time.",
    ),
    (
        "Where do listings come from?",
        "From estate-agency websites, Biddit and the notary platform. Every listing links "
        "to its original page: always check details there.",
    ),
    (
        "What is the fair value?",
        "An estimate from a model trained on real asking prices in your area. The bar shows the "
        "80% range. With fewer than 30 comparable homes we show 'insufficient data' instead of guessing.",
    ),
    (
        "What do P-score and G-score mean?",
        "Flood risk scores from the Flemish information duty: A is the lowest risk, "
        "D the highest. P is for the plot, G for the building.",
    ),
    (
        "Do you store personal data of sellers?",
        "No. We store property facts only and never names, phone numbers or e-mail addresses of sellers or agents.",
    ),
]


def h(v: Any) -> str:
    return "?" if v is None else f"€ {v:,.0f}".replace(",", ".")


templates.env.filters["euro"] = h
templates.env.globals.update(definitions=metrics.DEFINITIONS, now=utcnow, epc_labels=EPC_LABELS)


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


@app.exception_handler(Redirect)
async def _redirect(_: Request, exc: Redirect) -> RedirectResponse:
    return RedirectResponse(exc.url, status_code=303)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None or not user.active:
        raise Redirect("/login")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admins only")
    return user


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"settings": get_settings(), **ctx})


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"ok": True, "properties": db.scalar(select(func.count(Property.id)))}


@app.get("/")
def index(request: Request) -> RedirectResponse:
    return RedirectResponse("/feed" if request.session.get("uid") else "/login", status_code=303)


# ---------- auth ----------


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request) -> HTMLResponse:
    return render(request, "login.html", mode="signup")


@app.post("/signup")
def signup(
    request: Request, email: str = Form(...), password: str = Form(...), plan: str = Form("home"), db: Session = Depends(get_db)
) -> Response:
    email = email.strip().lower()
    if len(password) < 8 or "@" not in email:
        return render(request, "login.html", mode="signup", error="Use a valid e-mail and a password of 8+ characters.")
    if db.scalars(select(User).where(User.email == email)).first():
        return render(request, "login.html", mode="signup", error="That e-mail already has an account.")
    user = User(email=email, password_hash=hasher.hash(password), plan="investor" if plan == "investor" else "home")
    db.add(user)
    db.commit()
    request.session["uid"] = user.id
    return RedirectResponse("/profiles", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return render(request, "login.html", mode="login")


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)) -> Response:
    user = db.scalars(select(User).where(User.email == email.strip().lower())).first()
    try:
        ok = user is not None and hasher.verify(user.password_hash, password)
    except VerifyMismatchError:
        ok = False
    if not ok or not user.active:  # type: ignore[union-attr]
        return render(request, "login.html", mode="login", error="Wrong e-mail or password.")
    request.session["uid"] = user.id  # type: ignore[union-attr]
    return RedirectResponse("/feed", status_code=303)


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- feed & property ----------


def feed_properties(db: Session, user: User, profile_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    profiles = [p for p in user.profiles if p.active and (profile_id is None or p.id == profile_id)]
    # shortcut: filters the 2000 newest properties in Python; move matching into SQL past ~50k properties.
    props = db.scalars(
        select(Property)
        .where(Property.status.in_(("for_sale", "under_option")), Property.price.is_not(None))
        .order_by(Property.first_seen_anywhere_at.desc())
        .limit(2000)
    )
    out = []
    for p in props:
        val = latest_valuation(db, p.id)
        if profiles and not any(matcher.matches(pr, p, val) for pr in profiles):
            continue
        out.append(card(db, p, val))
        if len(out) >= limit:
            break
    return out


def card(db: Session, p: Property, val: Any) -> dict[str, Any]:
    listings = sorted(p.listings, key=lambda lst: lst.first_seen_at)
    first = listings[0] if listings else None
    recent = utcnow() - timedelta(days=7)
    events = {
        e for (e,) in db.execute(select(ListingEvent.type).where(ListingEvent.property_id == p.id, ListingEvent.at > recent))
    }
    badges = [
        b
        for b, on in (
            ("new", p.first_seen_anywhere_at > utcnow() - timedelta(hours=24)),
            ("price drop", "price_drop" in events),
            ("relisted", "relisted" in events),
            ("Biddit", p.is_auction),
            ("under option", p.status == "under_option"),
        )
        if on
    ]
    pos = None
    if val and val.fair_value and val.low and val.high and p.price and val.high > val.low:
        pos = max(0, min(100, round((p.price - val.low) / (val.high - val.low) * 100)))
    return {
        "p": p,
        "val": val,
        "first": first,
        "badges": badges,
        "pos": pos,
        "minutes": int((utcnow() - p.first_seen_anywhere_at).total_seconds() // 60),
        "title": next((lst.title for lst in listings if lst.title), None),
        "photo": next((lst.photos[0] for lst in listings if lst.photos), None),
    }


@app.get("/feed", response_class=HTMLResponse)
def feed(
    request: Request, profile: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> HTMLResponse:
    return render(request, "feed.html", user=user, cards=feed_properties(db, user, profile), profile=profile)


@app.get("/feed/items", response_class=HTMLResponse)
def feed_items(
    request: Request, profile: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> HTMLResponse:
    return render(request, "_cards.html", user=user, cards=feed_properties(db, user, profile))


@app.get("/property/{pid}", response_class=HTMLResponse)
def property_page(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> HTMLResponse:
    p = db.get(Property, pid)
    if p is None:
        raise HTTPException(404)
    db.add(UserAction(user_id=user.id, property_id=pid, action="viewed"))
    db.commit()
    val = latest_valuation(db, pid)
    comps = list(
        db.scalars(
            select(Property)
            .where(
                Property.municipality == p.municipality,
                Property.property_type == p.property_type,
                Property.id != pid,
                Property.price.is_not(None),
            )
            .order_by(func.abs(func.coalesce(Property.surface, 0) - (p.surface or 0)))
            .limit(8)
        )
    )
    return render(
        request,
        "property.html",
        user=user,
        c=card(db, p, val),
        history=price_history(db, p),
        enrich=list(db.scalars(select(Enrichment).where(Enrichment.property_id == pid))),
        comps=comps,
        actions=list(
            db.scalars(
                select(UserAction)
                .where(UserAction.user_id == user.id, UserAction.property_id == pid, UserAction.action != "viewed")
                .order_by(UserAction.at)
            )
        ),
        contacts=list(
            db.scalars(select(ContactRequest).where(ContactRequest.user_id == user.id, ContactRequest.property_id == pid))
        ),
    )


@app.post("/property/{pid}/action")
def property_action(
    pid: int, action: str = Form(...), price: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)
) -> RedirectResponse:
    if action not in ACTIONS or db.get(Property, pid) is None:
        raise HTTPException(400)
    db.add(UserAction(user_id=user.id, property_id=pid, action=action))
    digits = "".join(ch for ch in price if ch.isdigit())
    if action == "bought" and digits:
        db.add(Purchase(user_id=user.id, property_id=pid, price_paid=int(digits)))
    db.commit()
    return RedirectResponse(f"/property/{pid}", status_code=303)


@app.post("/property/{pid}/visit-request")
def visit_request(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> RedirectResponse:
    p = db.get(Property, pid)
    if not get_settings().outreach_enabled or p is None or not p.listings:
        raise HTTPException(400, "Outreach is not enabled")
    listing = sorted(p.listings, key=lambda lst: lst.first_seen_at)[0]
    outreach.draft(db, user.id, pid, listing.id)
    return RedirectResponse(f"/property/{pid}", status_code=303)


@app.post("/contact/{cid}/approve")
async def approve_contact(cid: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> RedirectResponse:
    req = db.get(ContactRequest, cid)
    if req is None or (req.user_id != user.id and not user.is_admin) or req.status != "draft":
        raise HTTPException(400)
    req.status = "approved"
    db.commit()
    await outreach.send(db, req)
    return RedirectResponse(f"/property/{req.property_id}", status_code=303)


@app.get("/a/{token}")
def alert_open(token: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    alert = db.scalars(select(Alert).where(Alert.token == token)).first()
    if alert is None:
        raise HTTPException(404)
    if alert.opened_at is None:
        alert.opened_at = utcnow()
        db.commit()
    return RedirectResponse(f"/property/{alert.property_id}", status_code=303)


# ---------- search profiles ----------


def cap_violation(db: Session, user: User, municipalities: list[str], segment: str) -> str | None:
    """Edge dilution guard: max active clients per municipality and segment."""
    default = get_settings().default_segment_cap
    others = list(
        db.scalars(
            select(SearchProfile)
            .join(User)
            .where(SearchProfile.active.is_(True), User.active.is_(True), SearchProfile.user_id != user.id)
        )
    )
    for muni in municipalities:
        cap = (
            db.scalars(
                select(SegmentCap.max_active).where(
                    func.lower(SegmentCap.municipality) == muni.lower(), SegmentCap.segment == segment
                )
            ).first()
            or default
        )
        if not cap:
            continue
        users = {p.user_id for p in others if p.segment == segment and muni.lower() in (m.lower() for m in p.municipalities)}
        if len(users) >= cap:
            return f"{muni} is full for {segment} clients ({cap} active). Join the waiting list via Support."
    return None


def _int(v: str) -> int | None:
    digits = "".join(ch for ch in v if ch.isdigit())
    return int(digits) if digits else None


@app.get("/profiles", response_class=HTMLResponse)
def profiles(request: Request, user: User = Depends(current_user)) -> HTMLResponse:
    return render(request, "profiles.html", user=user, municipalities=province_config().get("municipalities", []))


@app.post("/profiles")
def create_profile(
    request: Request,
    name: str = Form("My search"),
    municipalities: list[str] = Form([]),
    postal_codes: str = Form(""),
    property_types: list[str] = Form([]),
    min_price: str = Form(""),
    max_price: str = Form(""),
    min_bedrooms: str = Form(""),
    min_surface: str = Form(""),
    epc_max: str = Form(""),
    flood_max: str = Form(""),
    investor: bool = Form(False),
    min_yield: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    segment = "investor" if investor else "home"
    error = cap_violation(db, user, municipalities, segment)
    if error:
        return render(
            request, "profiles.html", user=user, error=error, municipalities=province_config().get("municipalities", [])
        )
    y = min_yield.replace(",", ".").replace("%", "").strip()
    db.add(
        SearchProfile(
            user_id=user.id,
            name=name[:100],
            municipalities=municipalities,
            postal_codes=[pc.strip() for pc in postal_codes.replace(";", ",").split(",") if pc.strip().isdigit()],
            property_types=[t for t in property_types if t in ("house", "apartment", "land", "garage", "commercial")],
            min_price=_int(min_price),
            max_price=_int(max_price),
            min_bedrooms=_int(min_bedrooms),
            min_surface=_int(min_surface),
            epc_max=epc_max if epc_max in EPC_LABELS else None,
            flood_max=flood_max if flood_max in "ABCD" and flood_max else None,
            investor=investor,
            min_yield=float(y) / 100 if y.replace(".", "", 1).isdigit() else None,
        )
    )
    db.commit()
    return RedirectResponse("/profiles", status_code=303)


@app.post("/profiles/{pid}/delete")
def delete_profile(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> RedirectResponse:
    prof = db.get(SearchProfile, pid)
    if prof and prof.user_id == user.id:
        prof.active = False
        db.commit()
    return RedirectResponse("/profiles", status_code=303)


# ---------- my edge ----------


@app.get("/edge", response_class=HTMLResponse)
def edge(
    request: Request,
    profile: int | None = None,
    days: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    m = metrics.compute_all(db, user_id=user.id, profile_id=profile, days=days, names=metrics.USER_METRICS)
    return render(request, "metrics.html", user=user, m=m, title="My edge", csv_base="/edge", profile=profile, days=days)


@app.get("/edge/{name}.csv")
def edge_csv(name: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> PlainTextResponse:
    if name not in metrics.USER_METRICS:
        raise HTTPException(404)
    rows = metrics.METRICS[name](db, user_id=user.id)["rows"]
    return PlainTextResponse(
        metrics.to_csv(rows), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}.csv"}
    )


# ---------- settings & support ----------


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(current_user)) -> HTMLResponse:
    return render(request, "settings.html", user=user)


@app.post("/settings")
def save_settings(
    email_alerts: bool = Form(False),
    quiet_start: str = Form(""),
    quiet_end: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user.email_alerts = email_alerts
    qs, qe = _int(quiet_start), _int(quiet_end)
    user.quiet_start = qs if qs is not None and qs < 24 else None
    user.quiet_end = qe if qe is not None and qe < 24 else None
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/telegram")
def telegram_code(user: User = Depends(current_user), db: Session = Depends(get_db)) -> RedirectResponse:
    user.telegram_link_code = secrets.token_hex(6)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/telegram/disconnect")
def telegram_disconnect(user: User = Depends(current_user), db: Session = Depends(get_db)) -> RedirectResponse:
    user.telegram_chat_id = None
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.get("/faq", response_class=HTMLResponse)
def faq(request: Request, user: User = Depends(current_user)) -> HTMLResponse:
    return render(request, "support.html", user=user, faq=FAQ)


@app.post("/support")
def support(
    request: Request,
    subject: str = Form(...),
    body: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    db.add(SupportTicket(user_id=user.id, subject=subject[:200], body=body[:5000]))
    db.commit()
    return render(request, "support.html", user=user, faq=FAQ, sent=True)


# ---------- admin ----------


@app.get("/admin", response_class=HTMLResponse)
def admin_sources(
    request: Request, status: str = "active", user: User = Depends(admin_user), db: Session = Depends(get_db)
) -> HTMLResponse:
    sources = list(db.scalars(select(Source).where(Source.review_status == status).order_by(Source.health.desc(), Source.name)))
    counts = dict(db.execute(select(Source.review_status, func.count(Source.id)).group_by(Source.review_status)).all())
    listing_counts = dict(db.execute(select(Listing.source_id, func.count(Listing.id)).group_by(Listing.source_id)).all())
    return render(
        request, "admin_sources.html", user=user, sources=sources, status=status, counts=counts, listing_counts=listing_counts
    )


@app.post("/admin/sources/{sid}/{op}")
def admin_source_op(
    sid: int,
    op: str,
    background: BackgroundTasks,
    poll_minutes: str = Form(""),
    user: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    src = db.get(Source, sid)
    if src is None:
        raise HTTPException(404)
    if op == "toggle":
        src.enabled = not src.enabled
    elif op == "approve":
        src.review_status, src.enabled = "active", True
    elif op == "reject":
        src.review_status, src.enabled = "rejected", False
    elif op == "interval" and (m := _int(poll_minutes)):
        src.poll_minutes = max(5, min(m, 1440))
    elif op == "run":
        background.add_task(lambda: asyncio.run(scout.run_many([sid])))
    else:
        raise HTTPException(400)
    db.commit()
    return RedirectResponse(f"/admin/source/{sid}" if op in ("run", "interval") else "/admin", status_code=303)


@app.get("/admin/source/{sid}", response_class=HTMLResponse)
def admin_source(request: Request, sid: int, user: User = Depends(admin_user), db: Session = Depends(get_db)) -> HTMLResponse:
    src = db.get(Source, sid)
    if src is None:
        raise HTTPException(404)
    runs = list(db.scalars(select(SourceRun).where(SourceRun.source_id == sid).order_by(SourceRun.id.desc()).limit(50)))
    return render(request, "admin_source.html", user=user, s=src, runs=runs)


@app.get("/admin/parser-test", response_class=HTMLResponse)
def parser_form(request: Request, user: User = Depends(admin_user)) -> HTMLResponse:
    return render(request, "admin_parser.html", user=user)


@app.post("/admin/parser-test", response_class=HTMLResponse)
async def parser_test(request: Request, url: str = Form(...), user: User = Depends(admin_user)) -> HTMLResponse:
    try:
        async with Fetcher(min_interval=0) as f:
            page = await f.get(url)
        raw = extract_listing(page.text, page.url).clean()
        return render(request, "admin_parser.html", user=user, url=url, raw=vars(raw), missing=raw.missing_critical())
    except Exception as e:  # noqa: BLE001 - shown to the admin
        return render(request, "admin_parser.html", user=user, url=url, error=repr(e))


@app.get("/admin/metrics", response_class=HTMLResponse)
def admin_metrics(
    request: Request, days: int | None = None, user: User = Depends(admin_user), db: Session = Depends(get_db)
) -> HTMLResponse:
    return render(
        request,
        "metrics.html",
        user=user,
        m=metrics.compute_all(db, days=days),
        title="Global metrics",
        csv_base="/admin/metrics",
        days=days,
    )


@app.get("/admin/metrics/{name}.csv")
def admin_metrics_csv(name: str, user: User = Depends(admin_user), db: Session = Depends(get_db)) -> PlainTextResponse:
    if name not in metrics.METRICS:
        raise HTTPException(404)
    return PlainTextResponse(
        metrics.to_csv(metrics.METRICS[name](db)["rows"]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}.csv"},
    )


@app.get("/admin/model", response_class=HTMLResponse)
def admin_model(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)) -> HTMLResponse:
    from immo_agent.report import interval_coverage_after_outcome

    runs = list(db.scalars(select(ModelRun).order_by(ModelRun.id.desc()).limit(30)))
    return render(request, "admin_model.html", user=user, runs=runs, outcome=interval_coverage_after_outcome(db))


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)) -> HTMLResponse:
    users = list(db.scalars(select(User).order_by(User.created_at.desc())))
    usage: dict[tuple[str, str], set[int]] = {}
    for p in db.scalars(select(SearchProfile).join(User).where(SearchProfile.active.is_(True), User.active.is_(True))):
        for m in p.municipalities:
            usage.setdefault((m, p.segment), set()).add(p.user_id)
    caps = {(c.municipality, c.segment): c.max_active for c in db.scalars(select(SegmentCap))}
    rows = sorted({*usage, *caps})
    return render(
        request,
        "admin_users.html",
        user=user,
        users=users,
        caps=[(m, s, len(usage.get((m, s), ())), caps.get((m, s))) for m, s in rows],
    )


@app.post("/admin/caps")
def admin_caps(
    municipality: str = Form(...),
    segment: str = Form(...),
    max_active: str = Form(""),
    user: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    cap = db.scalars(select(SegmentCap).where(SegmentCap.municipality == municipality, SegmentCap.segment == segment)).first()
    value = _int(max_active)
    if value is None:
        if cap:
            db.delete(cap)
    elif cap:
        cap.max_active = value
    else:
        db.add(SegmentCap(municipality=municipality.strip(), segment=segment, max_active=value))
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@app.get("/admin/tickets", response_class=HTMLResponse)
def admin_tickets(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)) -> HTMLResponse:
    tickets = db.execute(
        select(SupportTicket, User.email).join(User).order_by(SupportTicket.status.desc(), SupportTicket.id.desc())
    ).all()
    drafts = list(db.scalars(select(ContactRequest).where(ContactRequest.status == "draft")))
    return render(request, "admin_tickets.html", user=user, tickets=tickets, drafts=drafts)


@app.post("/admin/tickets/{tid}/close")
def close_ticket(tid: int, user: User = Depends(admin_user), db: Session = Depends(get_db)) -> RedirectResponse:
    t = db.get(SupportTicket, tid)
    if t:
        t.status = "closed"
        db.commit()
    return RedirectResponse("/admin/tickets", status_code=303)


@app.get("/admin/dedup", response_class=HTMLResponse)
def admin_dedup(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)) -> HTMLResponse:
    """Label merged properties (2+ listings) as correct or wrong; precision feeds the acceptance report."""
    reviewed = select(DedupReview.property_id)
    multi = select(Listing.property_id).group_by(Listing.property_id).having(func.count(Listing.id) > 1)
    prop = db.scalars(
        select(Property).where(Property.id.in_(multi), Property.id.not_in(reviewed)).order_by(func.random()).limit(1)
    ).first()
    labels = list(db.scalars(select(DedupReview.correct)))
    return render(
        request, "admin_dedup.html", user=user, p=prop, n=len(labels), precision=(sum(labels) / len(labels)) if labels else None
    )


@app.post("/admin/dedup/{pid}")
def admin_dedup_label(
    pid: int, correct: bool = Form(...), user: User = Depends(admin_user), db: Session = Depends(get_db)
) -> RedirectResponse:
    if db.scalars(select(DedupReview).where(DedupReview.property_id == pid)).first() is None:
        db.add(DedupReview(property_id=pid, correct=correct))
        db.commit()
    return RedirectResponse("/admin/dedup", status_code=303)
