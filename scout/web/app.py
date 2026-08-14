"""Web UI: dashboard, criteria, settings, run history.

Server-rendered Jinja2 with a sprinkle of vanilla JS. No build step, no npm, no
bundle to rebuild on a Pi over SSH - the whole UI is four templates and one
stylesheet.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import scheduler, store
from ..config import get_config
from ..db import connect, init_db, load_criteria, load_settings, save_criteria, save_settings
from ..models import AMENITIES, Criteria, Settings
from ..sources.registry import SOURCES

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["chf"] = lambda v: f"{v:,}".replace(",", "'") if v else "—"
templates.env.filters["rooms"] = lambda v: f"{v:g}" if v else "—"

security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    """Optional HTTP basic auth.

    Off by default because this is meant to sit on a home LAN. If you expose it
    any further, set SCOUT_AUTH_USER and SCOUT_AUTH_PASSWORD.
    """
    config = get_config()
    if not (config.scout_auth_user and config.scout_auth_password):
        return
    if (
        credentials is None
        or not secrets.compare_digest(credentials.username, config.scout_auth_user)
        or not secrets.compare_digest(credentials.password, config.scout_auth_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Appart-Scout", lifespan=lifespan, dependencies=[Depends(require_auth)])
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def _page(request: Request, name: str, **context: Any) -> HTMLResponse:
    with connect() as conn:
        criteria = load_criteria(conn)
        settings = load_settings(conn)
    context.setdefault("criteria", criteria)
    context.setdefault("settings", settings)
    context["running"] = scheduler.is_running()
    context["next_run"] = scheduler.next_run_time()
    context["nav"] = name
    return templates.TemplateResponse(request, f"{name}.html", context)


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, show_hidden: bool = False, limit: int = 60):
    with connect() as conn:
        items = store.ranked(conn, limit=limit, include_hidden=show_hidden)
        total = conn.execute("SELECT COUNT(*) c FROM listing WHERE active = 1").fetchone()["c"]
    return _page(
        request, "index", items=items, total=total, show_hidden=show_hidden
    )


@app.post("/listing/{listing_id}/feedback")
def feedback(listing_id: str, verdict: str = Form(...)):
    with connect() as conn:
        store.set_feedback(conn, listing_id, verdict)
    return RedirectResponse("/", status_code=303)


@app.post("/run")
async def run_now():
    await scheduler.trigger_now()
    return RedirectResponse("/runs", status_code=303)


# --------------------------------------------------------------------------
# criteria
# --------------------------------------------------------------------------


@app.get("/criteria", response_class=HTMLResponse)
def criteria_form(request: Request):
    return _page(request, "criteria", amenities=AMENITIES)


@app.post("/criteria")
async def criteria_save(request: Request):
    form = await request.form()
    with connect() as conn:
        current = load_criteria(conn).model_dump()

        for field, value in form.multi_items():
            if field not in current:
                continue
            current[field] = value

        # Multi-selects and checkbox groups need the full list, not the last value.
        for field in ("must_have", "nice_to_have", "categories", "cantons"):
            current[field] = form.getlist(field)

        current["exclude_keywords"] = [
            word.strip()
            for word in str(form.get("exclude_keywords", "")).split(",")
            if word.strip()
        ]
        for field in ("move_in_earliest", "move_in_latest"):
            current[field] = form.get(field) or None

        try:
            criteria = Criteria(**current)
        except Exception as exc:
            return _page(request, "criteria", amenities=AMENITIES, error=str(exc))
        save_criteria(conn, criteria)
    return RedirectResponse("/criteria?saved=1", status_code=303)


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


#: Credentials the form never echoes back. Submitting one blank means "keep
#: what is stored"; erasing one is an explicit button.
SECRET_FIELDS = ("openai_api_key", "smtp_password", "imap_password")

#: Fields where an empty form value means "unset, fall back to .env" rather
#: than a literal empty value. Ports and the two TLS toggles are typed
#: `int | None` / `bool | None` for exactly this.
NULLABLE_FIELDS = ("smtp_port", "imap_port", "smtp_starttls", "imap_ssl")


def _settings_context(settings: Settings) -> dict:
    """Shared context for both the GET and the re-rendered-on-error POST.

    Stored secrets are never sent to the browser — only their last four
    characters, as a placeholder, so you can tell which one is loaded.
    """
    config = get_config()
    creds = config.resolve(settings)
    return {
        "all_sources": list(SOURCES),
        "creds": creds,
        "env": config,
        "db_path": str(config.db_path),
        # Which layer each secret is coming from, so the page can say so.
        "secret_state": {field: _secret_state(settings, config, field) for field in SECRET_FIELDS},
    }


def _secret_state(settings: Settings, config, field: str) -> dict:
    stored = (getattr(settings, field, "") or "").strip()
    from_env = (getattr(config, field, "") or "").strip()
    return {
        "stored_here": bool(stored),
        "in_env": bool(from_env),
        "hint": _mask(stored or from_env, "saved" if stored else "from .env"),
    }


def _mask(secret: str, origin: str) -> str:
    """Enough of a secret to recognize it by, and where it is coming from."""
    return f"…{secret[-4:]} ({origin})" if len(secret) >= 4 else ""


@app.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request):
    with connect() as conn:
        settings = load_settings(conn)
    return _page(request, "settings", **_settings_context(settings))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    with connect() as conn:
        current = load_settings(conn).model_dump()

        skip = ("enabled_sources", "recipients", *SECRET_FIELDS)
        for field, value in form.multi_items():
            if field in current and field not in skip:
                # "" here means "not set, use .env" for these, not "the empty
                # string" — otherwise a blank port box would fail validation.
                current[field] = None if field in NULLABLE_FIELDS and value == "" else value

        # Secret fields are rendered empty because we never echo a secret back,
        # so an empty submission means "leave it alone" rather than "erase it".
        # Clearing is its own explicit button per field.
        for field in SECRET_FIELDS:
            submitted = str(form.get(field, "")).strip()
            if form.get(f"clear_{field}"):
                current[field] = ""
            elif submitted:
                current[field] = submitted

        current["enabled_sources"] = form.getlist("enabled_sources")
        current["recipients"] = [
            address.strip()
            for address in str(form.get("recipients", "")).replace("\n", ",").split(",")
            if address.strip()
        ]
        for flag in (
            "send_when_empty",
            "vision_enabled",
            "instant_alert_enabled",
        ):
            current[flag] = form.get(flag) is not None

        try:
            settings = Settings(**current)
        except Exception as exc:
            return _page(
                request,
                "settings",
                **_settings_context(load_settings(conn)),
                error=str(exc),
            )
        save_settings(conn, settings)
    scheduler.reschedule(settings.run_every_hours)
    return RedirectResponse("/settings?saved=1", status_code=303)


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request):
    with connect() as conn:
        history = store.recent_runs(conn, limit=25)
    return _page(request, "runs", runs=history)


@app.get("/healthz")
def healthz():
    return {"ok": True, "running": scheduler.is_running()}
