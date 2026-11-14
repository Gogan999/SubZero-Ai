"""What every web module shares: the services object, sign-in and CSRF helpers, page rendering, and the
security middleware. web.py, web_admin.py and web_code.py all import from here, never from each other."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from . import __version__
from .ai_backend import Capabilities, capabilities_for, resolve_backend
from .approvals import ApprovalService, Outcome
from .attachments import MAX_FILES, Attachments
from .bridge_login import BridgeLogin
from .auth import SlackLogin
from .claude import ClaudeClient
from .code_runner import CodeRunner
from .config import Settings, normalize_model_id
from .connectors import Drive, GitHub
from .db import Database
from .egress_proxy import EgressProxy
from .generation import ReplyRunner
from .health import Health
from .live import Broker, Tasks
from .maintenance import Nightly
from .projects import Projects
from .roles import RECOVERY_USER, Directory
from .sandbox import SandboxManager
from .screener import Screener
from .shares import Shares
from .slack_api import SlackWebApi
from .slack_dm import SlackDMs
from .slack_handlers import SlackInteractions
from .store import Store, Viewer

log = logging.getLogger("subzero.web")
HERE = Path(__file__).parent

# How many of a person's own chats the sidebar lists before "All chats"
RECENTS = 25

SAFE_NEXT = re.compile(r"/(?:chats|recents|artifacts(?:/\d+/\d+)?|projects(?:/\d+)?|code(?:/\d+)?|profile|queue|audit|settings|about|c/\d+)")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data: https://*.slack-edge.com https://secure.gravatar.com; "
       "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


@dataclass
class Services:
    settings: Settings
    db: Database
    store: Store
    slack: Any
    claude: Any
    broker: Broker
    tasks: Tasks
    runner: ReplyRunner
    approvals: ApprovalService
    directory: Directory
    interactions: SlackInteractions
    login: SlackLogin
    attachments: Attachments
    health: Health
    nightly: Nightly
    projects: Projects
    shares: Shares
    github: GitHub
    drive: Drive
    code: CodeRunner
    screener: Screener  # the one screener: the approval window and Code requests both use it
    # A Claude subscription sign-in started on the Settings page, waiting for its code
    bridge_login: BridgeLogin = field(default_factory=BridgeLogin)
    # Explainer titles and sizes per finished reply, for the Artifacts gallery (a finished reply never changes)
    artifact_index: dict[int, list[tuple[int, str, int]]] = field(default_factory=dict)
    # Model names from the Pi gateway, by provider/model id, for labels (filled once at startup, may stay empty)
    model_names: dict[str, str] = field(default_factory=dict)
    # The gateway's full record per model id (`images`, `reasoning`, ...), for what each non-Claude model can do
    model_info: dict[str, dict] = field(default_factory=dict)
    # Model safety checks in progress: model id -> when it started (the background task itself is in `tasks`)
    model_checks_running: dict[str, str] = field(default_factory=dict)
    dms: SlackDMs | None = None  # messaging the bot in Slack (None without Slack)
    egress: EgressProxy | None = None  # the sandboxes' internet proxy (egress_proxy.py), started for Hyper-V sandboxes
    # config.env settings saved on the Settings page that apply after a restart: name -> saved value ("" for secrets)
    pending_restart: dict[str, str] = field(default_factory=dict)
    checks_running: str = ""  # when the Settings page's Run checks job started ("" when none is running)
    slack_workspace: str = ""  # the Slack workspace's name, once the Settings page has asked Slack
    slack_workspace_asked: float = 0.0  # time.monotonic() of the last time it asked
    # Clients made from tokens typed on the setup page, before they're saved (tests swap these out)
    slack_factory: Callable[[str], Any] = SlackWebApi
    claude_factory: Callable[[str], Any] = ClaudeClient
    sandboxes: SandboxManager | None = None  # where Code sessions' commands run (sandbox.py)

    def capabilities(self, model: str) -> Capabilities:
        """What this model can do with the backend in use: the one place templates and routes ask."""
        return capabilities_for(model, resolve_backend(self.settings), self.model_info.get(normalize_model_id(model)))


async def _load_model_names(s: Services) -> None:
    """Ask the Pi gateway once for model names to label the models with. Best effort: never blocks startup."""
    list_models = getattr(s.claude, "list_models", None)
    if list_models is None:
        return
    try:
        models = await list_models(list(s.settings.ai_providers))
        s.model_names.update({str(m["id"]): str(m["name"]) for m in models if m.get("id") and m.get("name")})
        s.model_info.update({str(m["id"]): dict(m) for m in models if m.get("id")})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.info("Couldn't get model names from the AI helper: %s", exc)


def short_text(text: str, limit: int = 60) -> str:
    """One line, cut at a word boundary, for titles shown before Claude names the chat."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    cut = cut.rsplit(" ", 1)[0] if " " in cut else text[:limit]
    return cut.rstrip(" ,.;:-") + "…"


# --------------------------------------------------------------------------- helpers
def services(request: Request) -> Services:
    return request.app.state.services


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


async def check_csrf(request: Request) -> bool:
    expected = request.session.get("csrf")
    if not expected:
        return False
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf")
    return bool(sent) and secrets.compare_digest(str(sent), expected)


async def current_viewer(request: Request) -> Viewer | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    s = services(request)
    row = s.store.user(uid)
    if row is None or not row["active"]:
        request.session.clear()
        return None
    if row["slack_user_id"] == RECOVERY_USER["slack_user_id"] and not is_local_request(request):
        return None  # the recovery sign-in only works on the mini PC itself
    role = await s.directory.role_for(row["slack_user_id"])
    if role != row["role"]:
        s.store.set_user_role(row["id"], role)
    return Viewer(id=row["id"], slack_user_id=row["slack_user_id"], name=row["name"], role=role,
                  avatar_url=row["avatar_url"], theme=row["theme"])


def is_local_request(request: Request) -> bool:
    """True only for a browser on the mini PC itself, never for visits through the Cloudflare Tunnel."""
    if request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for"):
        return False
    host = request.url.hostname or ""
    client = request.client.host if request.client else ""
    return host in {"localhost", "127.0.0.1", "::1", "testserver"} and client in {"127.0.0.1", "::1", "testclient"}


def dev_login_allowed(request: Request) -> bool:
    return services(request).settings.dev_login and is_local_request(request)


def _finish_login(request: Request, s: Services, user) -> Response:
    target = request.session.pop("next", None) or "/chats"
    request.session.clear()
    request.session["uid"] = user["id"]
    request.session["csrf"] = secrets.token_urlsafe(24)
    s.db.audit("signed_in", actor_id=user["id"], target_type="user", target_id=user["id"])
    # Only return to the app's own pages ("/\evil.example" would otherwise leave the site in some browsers).
    if not SAFE_NEXT.fullmatch(target):
        target = "/chats"
    return RedirectResponse(target, status_code=303)


def json_error(outcome: Outcome) -> JSONResponse:
    status = {"invalid": 400, "limit": 429, "forbidden": 403, "not_found": 404, "already_handled": 409}.get(
        outcome.code, 400)
    return JSONResponse({"ok": False, "error": outcome.message, "code": outcome.code}, status_code=status)


def page(require: str = "login"):
    """Route decorator: resolves the viewer and enforces login, mentor, or admin access."""

    def wrap(handler: Callable[[Request, Viewer], Awaitable[Response]]):
        async def endpoint(request: Request) -> Response:
            viewer = await current_viewer(request)
            wants_json = request.method != "GET" or "application/json" in request.headers.get("accept", "")
            if viewer is None:
                if wants_json:
                    return JSONResponse({"ok": False, "error": "Sign in again to continue."}, status_code=401)
                request.session["next"] = request.url.path
                return RedirectResponse("/login", status_code=303)
            if (require == "mentor" and not viewer.is_mentor) or (require == "admin" and not viewer.is_admin):
                if wants_json:
                    return JSONResponse({"ok": False, "error": "You don't have access to that."}, status_code=403)
                return error_page(request, viewer, 403, "Mentors only", "This page is for mentors and coaches.")
            if request.method == "POST" and not await check_csrf(request):
                return JSONResponse({"ok": False, "error": "Your session expired. Reload the page and try again."},
                                    status_code=403)
            return await handler(request, viewer)

        endpoint.__name__ = handler.__name__
        return endpoint

    return wrap


templates = Jinja2Templates(directory=str(HERE / "templates"))


def fmt_time(store: Store, iso: str | None) -> str:
    local = store.local_time(iso)
    if local is None:
        return ""
    now = datetime.now(store.tz)
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == now.date():
        return clock
    return f"{local.strftime('%b')} {local.day}, {clock}"


def waited(iso: str) -> str:
    minutes = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} hr" if hours < 48 else f"{hours // 24} days"


def render(request: Request, name: str, viewer: Viewer | None, status_code: int = 200, **context) -> HTMLResponse:
    from .web import conversation_list  # late import: the chat pages live in web.py, which imports this module
    s = services(request)
    base = {
        "viewer": viewer,
        "settings": s.settings,
        "csrf": csrf_token(request),
        "dev_mode": s.settings.dev_login,
        "version": __version__,
        "pending_count": (len(s.store.pending_messages()) + s.shares.count_pending() + s.code.count_waiting()
                          if viewer and viewer.is_mentor else 0),
        "setting_alert": viewer is not None and viewer.is_admin and any(
            row.get("alert") for row in _health_alerts(s)),
        "path": request.url.path,
        # The sidebar's Recents: the viewer's own chats, looked up only when a page draws the sidebar
        "recents": (lambda: conversation_list(s, viewer, "mine", "", limit=RECENTS + 1)) if viewer else list,
        "recents_shown": RECENTS,
    }
    base.update(context)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


def _health_alerts(s: Services) -> list[dict[str, str]]:
    from .web_admin import health_rows  # late import: web_admin imports this module
    return health_rows(s)


def error_page(request: Request, viewer: Viewer | None, code: int, title: str, detail: str) -> HTMLResponse:
    return render(request, "error.html", viewer, status_code=code, title=title, detail=detail)


# --------------------------------------------------------------------------- middleware
class SecurityHeaders:
    def __init__(self, app, https: bool):
        self.app = app
        self.https = https

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Explainer pages and attachments set their own policy; explainers are framed by the chat page.
                own_policy = any(name.lower() == b"content-security-policy" for name, _ in headers)
                if not own_policy:
                    headers.append((b"content-security-policy", CSP.encode()))
                headers += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"SAMEORIGIN" if own_policy else b"DENY"),
                ]
                # Share links send no referrer at all, so the link never leaks to a site a reply links to
                if not any(name.lower() == b"referrer-policy" for name, _ in headers):
                    headers.append((b"referrer-policy", b"same-origin"))
                if self.https:
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class LimitBody:
    """Refuse request bodies bigger than any real message with attachments, before reading them all."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            return await self._too_big(send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooBig()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except BodyTooBig:
            await self._too_big(send)

    @staticmethod
    async def _too_big(send):
        response = JSONResponse({"ok": False, "error": f"That's too much to send at once. Attach up to {MAX_FILES} "
                                                       "files of up to 50 MB each, and less than 90 MB in all."}, status_code=413)
        await response({"type": "http"}, None, send)


class BodyTooBig(Exception):
    pass
