"""Dashboard (Phase 6). Talks to the database ONLY - it never calls Google, Ollama or Bridge, so it can run
on another host. Anything that needs those services is queued as a job for the worker on the desktop.

Security: login (one user, scrypt hash), server-side sessions (only a token hash stored), CSRF token on
every POST, strict CSP without inline script, Host header check, no state change on GET, autoescaped
templates, mail shown as text only.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from mailassistant import __version__
from mailassistant.config import Settings, get_settings
from mailassistant.db import audit
from mailassistant.db.models import (AuditLog, Briefing, Capture, Classification, Email, EventProposal, Followup,
                                     FollowupEvent, Job, LlmCall, MailAccount, MailAction, ProposedAction,
                                     ServiceStatus, WebSession)
from mailassistant.db.session import session_scope, utcnow
from mailassistant.web.auth import LoginThrottle, new_token, same, token_hash, verify_password

HERE = Path(__file__).parent
COOKIE, LOGIN_COOKIE, FLASH_COOKIE = "ma_session", "ma_login", "ma_flash"
PUBLIC = ("/login", "/healthz", "/static/", "/api/")   # /api/ checks its own bearer token
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
CATEGORIES = ["personal", "work", "newsletter", "automated_notification", "marketing", "invitation",
              "spam_suspicious", "manual_review"]

app = FastAPI(title="Mail Assistant", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")  # autoescape on for .html
app.state.settings = None       # tests inject Settings
app.state.sessionmaker = None   # tests inject a sessionmaker bound to the test database
throttle = LoginThrottle()
api_bad_token = LoginThrottle(limit=5, window=900.0)
api_rate = LoginThrottle(limit=30, window=600.0)   # used as a request counter


def settings() -> Settings:
    return app.state.settings or get_settings()


@contextmanager
def db() -> Iterator[Session]:
    if app.state.sessionmaker is None:
        with session_scope() as s:
            yield s
        return
    s = app.state.sessionmaker()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ------------------------------------------------------------------ template helpers
def _local(value, fmt: str = "%a %d %b %H:%M") -> str:
    if not value:
        return "-"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(ZoneInfo(settings().timezone)).strftime(fmt)


templates.env.filters["local"] = _local
templates.env.globals["version"] = __version__


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    flash = request.cookies.get(FLASH_COOKIE)
    resp = templates.TemplateResponse(request, name, {
        "csrf": getattr(request.state, "csrf", ""), "flash": unquote(flash) if flash else None,
        "path": request.url.path, **ctx})
    if flash:
        resp.delete_cookie(FLASH_COOKIE, path="/")
    return resp


def redirect(url: str, flash: str | None = None) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if flash:
        resp.set_cookie(FLASH_COOKIE, quote(flash[:400]), max_age=60, httponly=True, secure=True,
                        samesite="strict", path="/")
    return resp


def _set_cookie(resp: Response, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=True, samesite="strict", path="/")


# ------------------------------------------------------------------ middleware: host, auth, csrf, headers
def _host_allowed(host_header: str, allowed: list[str]) -> bool:
    host = (host_header or "").strip().lower()
    if host.startswith("["):              # [::1]:8443
        host = host[1:host.find("]")]
    elif host.count(":") == 1:
        host = host.split(":")[0]
    return host in {a.lower() for a in allowed}


async def _form(request: Request) -> dict[str, list[str]]:
    body = (await request.body()).decode("utf-8", "replace")
    return parse_qs(body, keep_blank_values=True, max_num_fields=600)


@app.middleware("http")
async def guard(request: Request, call_next):
    s = settings()
    if not _host_allowed(request.headers.get("host", ""), s.web_allowed_hosts):
        return PlainTextResponse("unknown host name (add it to WEB_ALLOWED_HOSTS)", status_code=400)
    path = request.url.path
    if not path.startswith(PUBLIC):
        token = request.cookies.get(COOKIE)
        ws = _load_session(token) if token else None
        if ws is None:
            if request.method == "GET":
                return redirect("/login")
            return PlainTextResponse("not logged in", status_code=403)
        request.state.session_hash, request.state.csrf = ws
        if request.method == "POST":
            form = await _form(request)
            request.state.form = form
            sent = (form.get("csrf") or [""])[0] or request.headers.get("x-csrf-token", "")
            if not same(sent, request.state.csrf):
                return PlainTextResponse("invalid or missing CSRF token; reload the page", status_code=403)
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-store"
    if s.web_tls != "off":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


def _load_session(token: str) -> tuple[str, str] | None:
    s = settings()
    now = utcnow()
    with db() as d:
        ws = d.get(WebSession, token_hash(token))
        if ws is None:
            return None
        if ws.expires_at < now or ws.last_seen_at < now - timedelta(hours=s.web_session_idle_hours):
            d.delete(ws)
            return None
        if ws.last_seen_at < now - timedelta(minutes=1):
            ws.last_seen_at = now
        return ws.id_hash, ws.csrf_token


def form_value(request: Request, key: str, default: str = "") -> str:
    return ((getattr(request.state, "form", {}) or {}).get(key) or [default])[0]


def form_ids(request: Request) -> list[int]:
    return [int(x) for x in (getattr(request.state, "form", {}) or {}).get("ids", []) if x.isdigit()]


# ------------------------------------------------------------------ login / logout / health
@app.get("/healthz")
def healthz() -> dict:
    try:
        with db() as d:
            d.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="database unavailable") from e
    return {"status": "ok", "version": __version__}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    pre = new_token()
    resp = render(request, "login.html", login_csrf=pre,
                  configured=settings().web_password_hash is not None)
    _set_cookie(resp, LOGIN_COOKIE, pre, 900)
    return resp


@app.post("/login")
async def login(request: Request):
    form = await _form(request)
    who = request.client.host if request.client else "?"
    if not same((form.get("csrf") or [""])[0], request.cookies.get(LOGIN_COOKIE)):
        return redirect("/login", "The login form expired; please try again.")
    if throttle.blocked(who):
        return PlainTextResponse("too many failed logins; try again in 15 minutes", status_code=429)
    s = settings()
    stored = s.web_password_hash.get_secret_value() if s.web_password_hash else None
    if not verify_password((form.get("password") or [""])[0], stored):
        throttle.failed(who)
        with db() as d:
            audit.record(d, actor="user", action="web_login_failed", detail=f"from {who}")
        return redirect("/login", "Wrong password.")
    throttle.succeeded(who)
    token, now = new_token(), utcnow()
    with db() as d:
        d.execute(delete(WebSession).where(WebSession.expires_at < now))
        d.add(WebSession(id_hash=token_hash(token), csrf_token=new_token(), created_at=now, last_seen_at=now,
                         expires_at=now + timedelta(days=s.web_session_max_days),
                         user_agent=(request.headers.get("user-agent") or "")[:200]))
        audit.record(d, actor="user", action="web_login", detail=f"from {who}")
    resp = redirect("/")
    _set_cookie(resp, COOKIE, token, s.web_session_max_days * 86400)
    resp.delete_cookie(LOGIN_COOKIE, path="/")
    return resp


@app.post("/logout")
def logout(request: Request):
    with db() as d:
        d.execute(delete(WebSession).where(WebSession.id_hash == request.state.session_hash))
    resp = redirect("/login", "Logged out.")
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ------------------------------------------------------------------ briefing
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    from mailassistant.followups.briefing import build_briefing

    s = settings()
    today = utcnow().astimezone(ZoneInfo(s.timezone)).date()
    with db() as d:
        content = build_briefing(d, today, s.timezone, rebuild=True).content
    return render(request, "briefing.html", b=content)


# ------------------------------------------------------------------ follow-ups
FU_STATUSES = {"active": ("proposed", "open", "waiting"), "proposed": ("proposed",), "open": ("open", "waiting"),
               "closure": ("proposed", "open", "waiting"), "done": ("completed", "cancelled")}


@app.get("/followups", response_class=HTMLResponse)
def followups(request: Request, status: str = "active", direction: str = ""):
    statuses = FU_STATUSES.get(status, FU_STATUSES["active"])
    with db() as d:
        q = select(Followup, Email).join(Email, Email.id == Followup.email_id).where(Followup.status.in_(statuses))
        if status == "closure":
            q = q.where(Followup.closure_email_id.is_not(None))
        if direction in ("I_OWE", "THEY_OWE"):
            q = q.where(Followup.direction == direction)
        order = Followup.updated_at.desc() if status == "done" else Followup.remind_at
        rows = d.execute(q.order_by(order).limit(300)).all()
        counts = dict(d.execute(select(Followup.status, func.count()).group_by(Followup.status)).all())
    return render(request, "followups.html", rows=rows, status=status, direction=direction, counts=counts,
                  now=utcnow())


def _fu_action(name: str):
    from mailassistant.followups import service
    return {"confirm": service.confirm, "reject": service.reject, "done": service.mark_done,
            "close": service.accept_closure, "keep": service.dismiss_closure}.get(name)


@app.post("/followups/bulk")
def followups_bulk(request: Request):
    from mailassistant.followups.service import FollowupError

    action, ids = form_value(request, "action"), form_ids(request)
    fn = _fu_action(action)
    back = form_value(request, "back", "/followups")
    if fn is None or not ids:
        return redirect(back if back.startswith("/followups") else "/followups", "Select one or more items first.")
    ok, errors = [], []
    with db() as d:
        for fid in ids:
            try:
                with d.begin_nested():
                    fn(d, fid)
                ok.append(f"#{fid}")
            except FollowupError as e:
                errors.append(f"#{fid}: {e}")
    msg = f"{action}: {', '.join(ok) or 'nothing'}" + (f". Not possible - {'; '.join(errors)}" if errors else "")
    return redirect(back if back.startswith("/followups") else "/followups", msg)


@app.get("/followups/{fid}", response_class=HTMLResponse)
def followup_detail(request: Request, fid: int):
    with db() as d:
        f = d.get(Followup, fid)
        if f is None:
            raise HTTPException(404)
        src = d.get(Email, f.email_id)
        closure = d.get(Email, f.closure_email_id) if f.closure_email_id else None
        events = d.scalars(select(FollowupEvent).where(FollowupEvent.followup_id == fid)
                           .order_by(FollowupEvent.id)).all()
    return render(request, "followup_detail.html", f=f, src=src, closure=closure, events=events)


@app.post("/followups/{fid}/{action}")
def followup_action(request: Request, fid: int, action: str):
    from mailassistant.followups import service

    s = settings()
    try:
        with db() as d:
            if action == "snooze":
                days = form_value(request, "days", "2")
                service.snooze(d, fid, int(days) if days.isdigit() else 2, s)
            elif action == "deadline":
                try:
                    day = date.fromisoformat(form_value(request, "day"))
                except ValueError:
                    return redirect(f"/followups/{fid}", "Enter a date.")
                service.set_deadline(d, fid, day, s)
            elif action == "edit":
                service.edit_action(d, fid, form_value(request, "text"))
            elif (fn := _fu_action(action)) is not None:
                fn(d, fid)
            else:
                raise HTTPException(404)
    except service.FollowupError as e:
        return redirect(f"/followups/{fid}", f"Not possible: {e}")
    return redirect(f"/followups/{fid}", f"#{fid}: {action} done.")


# ------------------------------------------------------------------ calendar proposals
@app.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, show: str = "open"):
    from mailassistant.gcal.review import approval_pending

    with db() as d:
        q = select(EventProposal, Email).join(Email, Email.id == EventProposal.email_id)
        if show == "open":
            q = q.where(EventProposal.status.in_(("proposed", "approved", "failed")),
                        EventProposal.end_at > utcnow()).order_by(EventProposal.start_at)
        else:
            q = q.where(EventProposal.status.in_(("created", "rejected", "superseded"))) \
                .order_by(EventProposal.decided_at.desc().nulls_last()).limit(100)
        rows = d.execute(q).all()
        from mailassistant.gcal.review import calendars, private_calendar_usable, suggestion_for
        st = settings()
        suggest = {p.id: (p.target_calendar, None) if p.target_calendar else suggestion_for(d, e, st) for p, e in rows}
        cals = calendars(d) or {}
        private_ok = private_calendar_usable(d, st)
    labels = {"work": ((cals.get("work") or {}).get("name") or "work"),
              "private": ((cals.get("private") or {}).get("name") or "private")}
    pending = any(approval_pending(p) for p, _ in rows)
    return render(request, "calendar.html", rows=rows, show=show, pending=pending, approval_pending=approval_pending,
                  suggest=suggest, private_enabled=private_ok, labels=labels,
                  private_problem=bool(st.google_private_calendar) and not private_ok)


@app.post("/calendar/bulk")
def calendar_bulk(request: Request):
    from mailassistant.gcal.review import ReviewError, reject_proposal

    ids = form_ids(request)
    if form_value(request, "action") != "reject" or not ids:
        return redirect("/calendar", "Select one or more proposals first. (Approving is one at a time.)")
    ok, errors = [], []
    with db() as d:
        for pid in ids:
            try:
                with d.begin_nested():
                    reject_proposal(d, pid)
                ok.append(f"#{pid}")
            except ReviewError as e:
                errors.append(f"#{pid}: {e}")
    return redirect("/calendar", f"rejected: {', '.join(ok) or 'nothing'}" + (f". Not possible - {'; '.join(errors)}" if errors else ""))


@app.post("/calendar/{pid}/approve")
def calendar_approve(request: Request, pid: int):
    from mailassistant.gcal.review import ReviewError, request_approval

    try:
        with db() as d:
            request_approval(d, pid, accept_conflicts=form_value(request, "accept_conflicts") == "1",
                             calendar=form_value(request, "calendar") or None, settings=settings())
    except ReviewError as e:
        return redirect("/calendar", f"#{pid}: {e}")
    return redirect("/calendar", f"#{pid}: being created by the worker on the desktop…")


# ------------------------------------------------------------------ mail / classification corrections
def _mail_query(category: str, days: int):
    """The Mail page's list. Also used by "Move all shown to Trash", so it acts on exactly what was shown."""
    from mailassistant.mail.trash import hidden_clause

    q = (select(Email, Classification).join(Classification, (Classification.email_id == Email.id)
                                            & Classification.is_current.is_(True))
         .where(Email.direction == "in", func.coalesce(Email.sent_at, Email.received_at) > utcnow() - timedelta(days=days),
                ~hidden_clause()))
    if category in CATEGORIES:
        q = q.where(Classification.category == category)
    return q.order_by(func.coalesce(Email.sent_at, Email.received_at).desc()).limit(200)


def _account_labels(d: Session) -> dict[int, str]:
    """{account id: short label}, empty when there is only one account (then no tags are shown)."""
    accts = d.scalars(select(MailAccount)).all()
    if len(accts) < 2:
        return {}
    return {a.id: (a.label or ("proton" if a.kind == "proton_bridge" else a.address)) for a in accts}


@app.get("/mail", response_class=HTMLResponse)
def mail(request: Request, category: str = "manual_review", days: int = 14):
    from mailassistant.mail.trash import hidden_clause

    days = max(1, min(days, 60))
    with db() as d:
        rows = d.execute(_mail_query(category, days)).all()
        ids = [e.id for e, _ in rows]
        actions: dict[int, list[str]] = {}
        for a in d.scalars(select(ProposedAction).where(ProposedAction.email_id.in_(ids),
                                                        ProposedAction.status == "proposed")):
            actions.setdefault(a.email_id, []).append(a.action_type)
        counts = dict(d.execute(select(Classification.category, func.count()).join(Email, Email.id == Classification.email_id)
                                .where(Classification.is_current.is_(True), Email.direction == "in", ~hidden_clause(),
                                       func.coalesce(Email.sent_at, Email.received_at) > utcnow() - timedelta(days=days))
                                .group_by(Classification.category)).all())
        trash_queue = d.scalar(select(func.count()).select_from(MailAction).where(MailAction.status == "requested"))
        accounts = _account_labels(d)
    return render(request, "mail.html", rows=rows, category=category, days=days, categories=CATEGORIES,
                  counts=counts, actions=actions, trash_queue=trash_queue, accounts=accounts)


@app.post("/mail/trash", response_class=HTMLResponse)
def mail_trash_confirm(request: Request):
    """Step 1: show what would be moved (nothing changes yet)."""
    from mailassistant.mail.trash import MAX_PER_REQUEST, TrashError, select_for_trash

    category = form_value(request, "category", "manual_review")
    try:
        days = max(1, min(int(form_value(request, "days", "14")), 60))
    except ValueError:
        days = 14
    back = f"/mail?category={quote(category)}&days={days}"
    with db() as d:
        if form_value(request, "scope") == "shown":
            ids = [e.id for e, _ in d.execute(_mail_query(category, days)).all()]
        else:
            ids = form_ids(request)
        if not ids:
            return redirect(back, "Select one or more mails first.")
        try:
            sel = select_for_trash(d, ids)
        except TrashError as e:
            return redirect(back, str(e))
        accounts = _account_labels(d)
        eligible = [(e.id, e.subject, e.from_addr, e.account_id) for e in sel.eligible]
        kept = [(e.id, e.subject, e.from_addr, why) for e, why in sel.kept]
    return render(request, "mail_trash.html", eligible=eligible, kept=kept, back=back, accounts=accounts,
                  max_per_request=MAX_PER_REQUEST)


@app.post("/mail/trash/confirm")
def mail_trash_do(request: Request):
    """Step 2: the user confirmed. Record + queue; the worker moves the mails to Trash."""
    from mailassistant.mail.trash import TrashError, request_trash

    back = form_value(request, "back", "/mail")
    back = back if back.startswith("/mail") else "/mail"
    ids = form_ids(request)
    if not ids:
        return redirect(back, "Nothing to move.")
    try:
        with db() as d:
            n = request_trash(d, ids, actor="user")
    except TrashError as e:
        return redirect(back, str(e))
    return redirect(back, f"{n} mail(s) queued: the worker moves them to the Trash folder of your mailbox "
                          "(recoverable there).")


@app.post("/mail/{email_id}/category")
def mail_correct(request: Request, email_id: int):
    from mailassistant.classify.service import apply_user_correction

    cat = form_value(request, "category")
    back = form_value(request, "back", "/mail")
    if cat not in CATEGORIES:
        return redirect("/mail", "Unknown category.")
    with db() as d:
        if d.get(Email, email_id) is None:
            raise HTTPException(404)
        apply_user_correction(d, email_id, cat, note="corrected in dashboard")
    return redirect(back if back.startswith("/mail") else "/mail", f"Mail {email_id} is now '{cat}'.")


# ------------------------------------------------------------------ captures: things you add yourself
KINDS = {"auto": "Laat het bepalen", "event": "Afspraak", "followup": "Follow-up"}
CAPTURE_TOKEN_FILE = "/run/secrets/capture_token"


@app.get("/capture", response_class=HTMLResponse)
def capture_form(request: Request):
    with db() as d:
        recent = d.execute(select(Capture, Email).join(Email, Email.id == Capture.email_id)
                           .order_by(Capture.id.desc()).limit(15)).all()
    return render(request, "capture.html", recent=recent, kinds=KINDS, cap=None, email=None)


@app.post("/capture")
def capture_create(request: Request):
    from mailassistant.capture.service import CaptureError, create_capture

    try:
        with db() as d:
            cap = create_capture(d, form_value(request, "text"), form_value(request, "kind", "auto"), "dashboard",
                                 settings())
            cid = cap.id
    except CaptureError as e:
        return redirect("/capture", f"Not possible: {e}")
    return redirect(f"/capture/{cid}")


@app.get("/capture/{cid}", response_class=HTMLResponse)
def capture_detail(request: Request, cid: int):
    from mailassistant.capture.speech import speech

    with db() as d:
        cap = d.get(Capture, cid)
        if cap is None:
            raise HTTPException(404)
        email = d.get(Email, cap.email_id)
    return render(request, "capture.html", cap=cap, email=email, kinds=KINDS, recent=[],
                  summary=speech(cap.status, cap.result or {}, settings().timezone))


def _capture_token(s: Settings) -> str:
    """Read the token file on every call, so a new token works without restarting the dashboard."""
    path = Path(CAPTURE_TOKEN_FILE)
    if app.state.settings is None and path.is_file():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return s.capture_token.get_secret_value().strip() if s.capture_token else ""


@app.post("/api/capture")
async def api_capture(request: Request):
    """For Home Assistant Assist: {"text": "...", "kind": "event|followup|auto"} with a bearer token.
    Waits briefly for the worker and answers with a sentence to read back (built by code, not the model)."""
    from mailassistant.capture.service import CaptureError, create_capture
    from mailassistant.capture.speech import speech

    s = settings()
    who = request.client.host if request.client else "?"
    expected = _capture_token(s)
    if not expected:
        return JSONResponse({"error": "capture API disabled (secrets/capture_token is empty)"}, status_code=404)
    if api_bad_token.blocked(who):
        return JSONResponse({"error": "too many wrong tokens; try again later"}, status_code=429)
    sent = request.headers.get("authorization", "")
    if not same(sent.removeprefix("Bearer ").strip() if sent.startswith("Bearer ") else "", expected):
        api_bad_token.failed(who)
        with db() as d:
            audit.record(d, actor="system", action="capture_api_refused", detail=f"wrong token from {who}")
        return JSONResponse({"error": "wrong token"}, status_code=401)
    if api_rate.blocked(who):
        return JSONResponse({"error": "too many requests"}, status_code=429)
    api_rate.failed(who)
    raw = await request.body()
    if len(raw) > 16_000:
        return JSONResponse({"error": "too large"}, status_code=413)
    try:
        body = json.loads(raw or b"{}")
        text_in, kind = str(body.get("text", "")), str(body.get("kind") or "auto")
    except (ValueError, AttributeError):
        return JSONResponse({"error": "send JSON: {\"text\": ..., \"kind\": \"event|followup|auto\"}"}, status_code=400)
    try:
        with db() as d:
            cid = create_capture(d, text_in, kind, "assist", s).id
    except CaptureError as e:
        return JSONResponse({"error": str(e), "speech": "Dat lukte niet: ik hoorde geen tekst."}, status_code=400)
    deadline = asyncio.get_running_loop().time() + max(0.0, s.capture_wait_seconds)
    status, result = "queued", {}
    while True:
        with db() as d:
            cap = d.get(Capture, cid)
            status, result = cap.status, dict(cap.result or {})
        if status != "queued" or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.5)
    return JSONResponse({"id": cid, "status": status, "speech": speech(status, result, s.timezone),
                         "events": [e["id"] for e in result.get("events", [])],
                         "followups": [f["id"] for f in result.get("followups", [])]}, status_code=202)


# ------------------------------------------------------------------ edit before approving
@app.get("/calendar/{pid}/edit", response_class=HTMLResponse)
def proposal_edit_form(request: Request, pid: int):
    with db() as d:
        p = d.get(EventProposal, pid)
        if p is None:
            raise HTTPException(404)
    z = ZoneInfo(p.tz_name or settings().timezone)
    return render(request, "proposal_edit.html", p=p, day=p.start_at.astimezone(z).date().isoformat(),
                  start=p.start_at.astimezone(z).strftime("%H:%M"), end=p.end_at.astimezone(z).strftime("%H:%M"))


@app.post("/calendar/{pid}/edit")
def proposal_edit(request: Request, pid: int):
    from mailassistant.gcal.review import ReviewError, edit_proposal

    def t(v: str):
        return time.fromisoformat(v) if v else None
    try:
        day = date.fromisoformat(form_value(request, "day"))
        with db() as d:
            edit_proposal(d, pid, settings(), title=form_value(request, "title"), day=day,
                          all_day=form_value(request, "all_day") == "1", start_time=t(form_value(request, "start")),
                          end_time=t(form_value(request, "end")), location=form_value(request, "location"))
    except ValueError:
        return redirect(f"/calendar/{pid}/edit", "Check the date and times.")
    except ReviewError as e:
        return redirect(f"/calendar/{pid}/edit", f"Not possible: {e}")
    return redirect("/calendar", f"#{pid} changed. Conflicts are checked again when you approve.")


# ------------------------------------------------------------------ status (database only)
@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    now = utcnow()
    with db() as d:
        comps = {c.component: c for c in d.scalars(select(ServiceStatus))}
        jobs = d.execute(select(Job.type, Job.status, func.count()).where(
            Job.status.in_(("queued", "running")) | (Job.finished_at > now - timedelta(days=1)))
            .group_by(Job.type, Job.status).order_by(Job.type)).all()
        dead = d.execute(select(Job.type, Job.last_error, Job.finished_at).where(
            Job.status == "dead", Job.finished_at > now - timedelta(days=3)).order_by(Job.finished_at.desc()).limit(10)).all()
        llm = d.execute(select(LlmCall.task, func.count(), func.count().filter(LlmCall.parsed_ok.is_(False)),
                               func.avg(LlmCall.duration_ms)).where(LlmCall.created_at > now - timedelta(days=1))
                        .group_by(LlmCall.task)).all()
        from mailassistant.gcal.review import calendars as resolved_calendars
        cals = resolved_calendars(d)
        last_briefing = d.scalar(select(func.max(Briefing.briefing_date)))
        last_push = d.scalar(select(func.max(AuditLog.at)).where(AuditLog.action == "ha_notified"))
        trash_failed = d.scalar(select(func.count()).select_from(MailAction).where(
            MailAction.status.in_(("failed", "skipped")), MailAction.done_at > now - timedelta(days=7)))
    worker = comps.get("worker")
    worker_age = (now - worker.checked_at) if worker else None
    return render(request, "status.html", comps=comps, jobs=jobs, dead=dead, llm=llm, worker_age=worker_age,
                  stale=worker_age is None or worker_age > timedelta(minutes=5), last_briefing=last_briefing,
                  last_push=last_push, now=now, error_class=lambda e: (e or "").split("(")[0][:80], cals=cals,
                  trash_failed=trash_failed, mail_components=sorted(k for k in comps if k.startswith("mail:")))


@app.post("/status/test-notification")
def status_test_notification(request: Request):
    from mailassistant.db import queue

    with db() as d:
        queue.enqueue(d, "notify.test", {}, dedupe_key="notify.test", priority=10)
    return redirect("/status", "Test notification queued for the worker.")


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


__all__ = ["app", "is_loopback"]
