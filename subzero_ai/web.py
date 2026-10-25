"""The web app: pages, JSON actions for the chat UI, live updates, and startup/shutdown."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import __version__
from .approvals import ApprovalService, Outcome
from .attachments import ACCEPT, MAX_FILE_BYTES, MAX_FILES, AttachmentError, Attachments, Prepared, prepare, size_label
from .auth import LoginError, SlackLogin, profile_from_claims
from .claude import ClaudeClient, FakeClaude
from .config import Settings, load_settings
from .db import Database
from .generation import ReplyRunner
from .live import Broker, Tasks
from .render import EXPLAINER_CSP, explainer_download, explainers, markdown_html, plain_html, pygments_css
from .roles import DEV_USERS, Directory
from .slack_api import FakeSlack, SlackWebApi
from .slack_handlers import SlackInteractions
from .socket_mode import SocketModeClient, WebsocketsTransport
from .store import (
    APPROVED,
    AUTO,
    COMPLETE,
    EDITED,
    ERROR,
    HELD,
    PENDING,
    REJECTED,
    STREAMING,
    WITHDRAWN,
    Store,
    Viewer,
)

log = logging.getLogger("subzero.web")
HERE = Path(__file__).parent

SUBTEAMS = ["Programming", "Fab and design", "Business and outreach"]

MODEL_LABELS = {
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-opus-5-5": "Opus 5.5",
    "claude-haiku-5-5": "Haiku 5.5",
}


def model_label(model_id: str) -> str:
    return MODEL_LABELS.get(model_id, model_id)


def short_text(text: str, limit: int = 60) -> str:
    """One line, cut at a word boundary, for titles shown before Claude names the chat."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    cut = cut.rsplit(" ", 1)[0] if " " in cut else text[:limit]
    return cut.rstrip(" ,.;:-") + "…"


SAFE_NEXT = re.compile(r"/(?:chats|queue|audit|about|c/\d+)")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data: https://*.slack-edge.com https://secure.gravatar.com; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


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


def build_services(settings: Settings, *, slack=None, claude=None) -> Services:
    db = Database(settings.db_path)
    store = Store(db, timezone_name=settings.timezone, show_pending_to_team=settings.show_pending_to_team,
                  show_rejected_to_team=settings.show_rejected_to_team)
    if slack is None and settings.slack_enabled:
        slack = SlackWebApi(settings.slack_bot_token)
    if claude is None:
        claude = FakeClaude() if settings.fake_claude else ClaudeClient(
            settings.anthropic_api_key, base_url=settings.anthropic_base_url)
    broker, tasks = Broker(), Tasks()
    attachments = Attachments(db, settings.data_dir / "files")
    runner = ReplyRunner(settings=settings, db=db, store=store, claude=claude, broker=broker, tasks=tasks,
                         attachments=attachments)
    approvals = ApprovalService(settings=settings, db=db, store=store, slack=slack, broker=broker, tasks=tasks,
                                runner=runner, attachments=attachments)
    runner.on_alert = approvals.mentor_alert
    directory = Directory(settings, slack)
    interactions = SlackInteractions(settings=settings, store=store, approvals=approvals, directory=directory,
                                     slack=slack, tasks=tasks)
    return Services(settings, db, store, slack, claude, broker, tasks, runner, approvals, directory, interactions,
                    SlackLogin(settings), attachments)


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
    role = await s.directory.role_for(row["slack_user_id"])
    if role != row["role"]:
        s.store.set_user_role(row["id"], role)
    return Viewer(id=row["id"], slack_user_id=row["slack_user_id"], name=row["name"], role=role,
                  avatar_url=row["avatar_url"])


def dev_login_allowed(request: Request) -> bool:
    if not services(request).settings.dev_login:
        return False
    if request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for"):
        return False
    host = request.url.hostname or ""
    client = request.client.host if request.client else ""
    return host in {"localhost", "127.0.0.1", "::1", "testserver"} and client in {"127.0.0.1", "::1", "testclient"}


def upload_context() -> dict[str, Any]:
    return {"accept": ACCEPT, "max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES}


async def read_uploads(form, settings: Settings) -> list[Prepared]:
    """The files sent with a message, checked and converted. Raises AttachmentError."""
    try:
        uploads = [f for f in form.getlist("files") if hasattr(f, "read") and getattr(f, "filename", "")]
        if len(uploads) > MAX_FILES:
            raise AttachmentError(f"You can attach up to {MAX_FILES} files to one message.")
        prepared = []
        for upload in uploads:
            data = await upload.read(MAX_FILE_BYTES + 1)
            prepared.append(await asyncio.to_thread(prepare, upload.filename, data,
                                                    max_text_bytes=settings.max_text_file_kb * 1024))
        return prepared
    finally:
        await form.close()  # delete the temporary copies of the uploads


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
    s = services(request)
    base = {
        "viewer": viewer,
        "settings": s.settings,
        "csrf": csrf_token(request),
        "dev_mode": s.settings.dev_login,
        "version": __version__,
        "pending_count": len(s.store.pending_messages()) if viewer and viewer.is_mentor else 0,
        "path": request.url.path,
    }
    base.update(context)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


def error_page(request: Request, viewer: Viewer | None, code: int, title: str, detail: str) -> HTMLResponse:
    return render(request, "error.html", viewer, status_code=code, title=title, detail=detail)


def file_views(s: Services, message_id: int, rows=None) -> list[dict[str, Any]]:
    rows = s.attachments.for_message(message_id) if rows is None else rows
    return [{"id": f["id"], "name": f["name"], "size": size_label(f["size"]), "kind": f["kind"],
             "url": f"/files/{f['id']}"} for f in rows]


def message_view(s: Services, viewer: Viewer, conv, m, last_id: int | None = None, files=None) -> dict[str, Any]:
    """Everything a template needs to draw one message for this viewer.
    last_id is the newest message in the conversation (only the newest failed reply can be retried).
    files is the message's attachments, when the caller already has them."""
    is_author = m["author_id"] is not None and m["author_id"] == viewer.id
    view: dict[str, Any] = {
        "id": m["id"], "role": m["role"], "status": m["status"], "time": fmt_time(s.store, m["created_at"]),
        "author_name": m["author_name"], "author_avatar": m["author_avatar"],
        "author_is_mentor": m["author_role"] in ("mentor", "admin"),
        "reviewer_name": m["reviewer_name"], "note": m["review_note"], "error": m["error"],
        "reply_to": m["reply_to"], "can_review": False, "can_withdraw": False, "can_retry": False,
        "original": None, "tag": None, "tag_kind": None, "files": [], "explainer": False,
    }
    if m["role"] == "assistant":
        if m["status"] == STREAMING:
            text, seq = s.runner.live_text(m["id"])
            view["html"], view["seq"] = plain_html(text), seq
        elif m["status"] == COMPLETE:
            view["html"] = markdown_html(m["content"], explainer_base=f"/c/{conv['id']}/x/{m['id']}")
        else:
            view["html"] = plain_html(m["content"])
        view["can_retry"] = (m["status"] == ERROR and m["id"] == last_id
                             and (viewer.is_mentor or conv["owner_id"] == viewer.id))
        return view
    view["html"] = plain_html(m["content"])
    view["raw"] = m["content"]
    view["files"] = file_views(s, m["id"], files)
    view["explainer"] = bool(m["wants_explainer"])
    status = m["status"]
    reviewer = m["reviewer_name"] or "a mentor"
    if status == PENDING:
        view["tag"], view["tag_kind"] = "Waiting for a mentor", "pending"
        view["can_review"] = viewer.is_mentor and not is_author
        view["can_withdraw"] = is_author
    elif status == APPROVED:
        view["tag"], view["tag_kind"] = f"Approved by {reviewer}", "approved"
    elif status == EDITED:
        view["tag"], view["tag_kind"] = f"Edited by {reviewer}", "edited"
        if is_author or viewer.is_mentor:
            view["original"] = m["original_content"]
    elif status == REJECTED:
        view["tag"], view["tag_kind"] = f"Not approved by {reviewer}", "rejected"
    elif status == HELD:
        if viewer.is_mentor:
            view["tag"] = f"Handled privately by {reviewer}"
        else:
            view["tag"] = "A mentor will follow up with you directly"
        view["tag_kind"] = "held"
    elif status == WITHDRAWN:
        view["tag"], view["tag_kind"] = "Withdrawn", "withdrawn"
    elif status == AUTO:
        view["tag"], view["tag_kind"] = None, None
    return view


def conversation_list(s: Services, viewer: Viewer, scope: str, query: str, subteam: str | None = None,
                      owner_id: int | None = None):
    rows = s.store.list_conversations(viewer, scope=scope, query=query, subteam=subteam, owner_id=owner_id)
    items = []
    for c in rows:
        own = c["owner_id"] == viewer.id
        title = c["title"]
        if not title:
            title = short_text(c["first_text"] or "New chat") if (own or viewer.is_mentor) else "New chat"
        items.append({"id": c["id"], "title": title, "owner": c["owner_name"], "subteam": c["subteam"],
                      "private": c["visibility"] == "private", "pending": c["pending_count"] if (own or viewer.is_mentor) else 0,
                      "updated": fmt_time(s.store, c["updated_at"])})
    return items


def sidebar_context(s: Services, viewer: Viewer, request: Request | None = None) -> dict[str, Any]:
    """The chat list with the filters from the query string (search, subteam, and who started the chat)."""
    params = request.query_params if request is not None else {}
    scope = "mine" if params.get("scope") == "mine" else "team"
    query = (params.get("q") or "").strip()[:100]
    subteam = params.get("subteam") if params.get("subteam") in SUBTEAMS else None
    owner = None
    if (params.get("owner") or "").isdigit():
        row = s.store.user(int(params["owner"]))
        if row is not None:
            owner = {"id": row["id"], "name": row["name"]}
    return {
        "items": conversation_list(s, viewer, scope, query, subteam, owner["id"] if owner else None),
        "scope": scope, "query": query, "subteams": SUBTEAMS, "subteam_filter": subteam, "owner_filter": owner,
    }


# --------------------------------------------------------------------------- pages
async def home(request: Request) -> Response:
    viewer = await current_viewer(request)
    return RedirectResponse("/chats" if viewer else "/login", status_code=303)


async def login_page(request: Request) -> Response:
    viewer = await current_viewer(request)
    if viewer:
        return RedirectResponse("/chats", status_code=303)
    error = request.session.pop("login_error", None)
    s = services(request)
    return render(request, "login.html", None, error=error, slack_ready=s.settings.slack_login_enabled,
                  dev_allowed=dev_login_allowed(request))


async def ensure_team_id(s: Services) -> bool:
    """Learn the team's Slack workspace ID from the bot token if it isn't known yet."""
    if s.settings.slack_team_id:
        return True
    if s.slack is None:
        return False
    try:
        s.settings.slack_team_id = (await s.slack.auth_test()).get("team_id", "")
    except Exception:
        log.exception("Couldn't reach Slack to look up the workspace ID")
    return bool(s.settings.slack_team_id)


async def auth_start(request: Request) -> Response:
    s = services(request)
    if not s.settings.slack_login_enabled:
        request.session["login_error"] = "Slack sign-in isn't set up yet. An admin needs to finish setup."
        return RedirectResponse("/login", status_code=303)
    if not await ensure_team_id(s):
        if s.slack is None:
            request.session["login_error"] = ("Slack sign-in isn't set up yet. An admin needs to add the Slack "
                                              "bot token to config.env.")
        else:
            request.session["login_error"] = ("Sign-in can't start because the app couldn't reach Slack to confirm "
                                              "the team's workspace. Try again in a minute.")
        return RedirectResponse("/login", status_code=303)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    request.session["oidc_state"], request.session["oidc_nonce"] = state, nonce
    return RedirectResponse(s.login.authorize_url(state, nonce), status_code=303)


async def auth_callback(request: Request) -> Response:
    s = services(request)
    state = request.session.pop("oidc_state", None)
    nonce = request.session.pop("oidc_nonce", None)
    if request.query_params.get("error"):
        request.session["login_error"] = "Sign-in was cancelled."
        return RedirectResponse("/login", status_code=303)
    code = request.query_params.get("code")
    if not code or not state or not secrets.compare_digest(request.query_params.get("state", ""), state):
        request.session["login_error"] = "That sign-in link expired. Try again."
        return RedirectResponse("/login", status_code=303)
    try:
        claims = await s.login.finish(code, nonce or "")
    except LoginError as exc:
        request.session["login_error"] = str(exc)
        return RedirectResponse("/login", status_code=303)
    profile = profile_from_claims(claims)
    if not profile["avatar_url"] and s.slack is not None:
        try:
            info = await s.slack.user_info(profile["slack_user_id"])
            profile["avatar_url"] = (info.get("profile") or {}).get("image_72")
        except Exception:
            log.warning("Couldn't look up the Slack picture for %s", profile["slack_user_id"], exc_info=True)
    role = await s.directory.role_for(profile["slack_user_id"], force=True)
    user = s.store.upsert_user(slack_user_id=profile["slack_user_id"], name=profile["name"], role=role,
                               team_id=profile["team_id"], email=profile["email"], avatar_url=profile["avatar_url"],
                               login=True)
    return _finish_login(request, s, user)


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


async def dev_login(request: Request) -> Response:
    if not dev_login_allowed(request):
        return PlainTextResponse("Not found", status_code=404)
    s = services(request)
    if request.method == "GET":
        return render(request, "dev_login.html", None, users=DEV_USERS)
    form = await request.form()
    match = next((u for u in DEV_USERS if u["slack_user_id"] == form.get("user")), None)
    if match is None:
        return RedirectResponse("/auth/dev", status_code=303)
    user = s.store.upsert_user(slack_user_id=match["slack_user_id"], name=match["name"], role=match["role"],
                               login=True)
    return _finish_login(request, s, user)


async def logout(request: Request) -> Response:
    if request.session.get("uid") and await check_csrf(request):
        request.session.clear()
    return RedirectResponse("/login", status_code=303)


@page()
async def chats(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    return render(request, "chats.html", viewer, current_id=None, **upload_context(),
                  **sidebar_context(s, viewer, request))


@page()
async def conversation_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    conv = s.store.conversation(request.path_params["cid"])
    if conv is None or not s.store.can_view_conversation(viewer, conv):
        return error_page(request, viewer, 404, "Chat not found",
                          "This chat doesn't exist, or it's private to the student and mentors.")
    visible = s.store.visible_messages(viewer, conv)
    last_id = s.store.last_message_id(conv["id"])
    files = s.attachments.for_conversation(conv["id"])
    messages = [message_view(s, viewer, conv, m, last_id, files.get(m["id"], [])) for m in visible]
    long_chat = s.store.last_context_tokens(conv["id"]) >= s.settings.long_chat_tokens
    own = conv["owner_id"] == viewer.id
    needs_review = (not viewer.is_mentor) or s.settings.mentors_need_approval
    waiting = own and needs_review and s.store.conversation_has_pending(conv["id"])
    replying = s.store.conversation_is_replying(conv["id"])
    if waiting:
        hint = "Waiting for a mentor to review your last message"
    elif replying:
        hint = "Claude is replying…"
    else:
        hint = "Ask a follow-up"
    title = conv["title"]
    if not title:
        first = next((m for m in visible if m["role"] == "user"), None)
        title = short_text(first["content"]) if first and (own or viewer.is_mentor) else "New chat"
    models = [(m, model_label(m)) for m in dict.fromkeys([s.settings.model_default, s.settings.model_hard, conv["model"]])]
    return render(request, "conversation.html", viewer, conv=conv, messages=messages, own=own, title=title,
                  can_send=own, waiting=waiting, replying=replying, composer_hint=hint, needs_review=needs_review,
                  model_options=models, current_id=conv["id"], long_chat=long_chat, **upload_context(),
                  **sidebar_context(s, viewer))


@page()
async def message_partial(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    conv = s.store.conversation(request.path_params["cid"])
    msg = s.store.message(request.path_params["mid"])
    if conv is None or msg is None or msg["conversation_id"] != conv["id"] or not s.store.can_view_message(viewer, conv, msg):
        return Response(status_code=204)
    view = message_view(s, viewer, conv, msg, s.store.last_message_id(conv["id"]))
    return render(request, "_message.html", viewer, m=view, conv=conv)


@page(require="mentor")
async def set_model(request: Request, viewer: Viewer) -> Response:
    """Mentors can move a conversation to the bigger model for hard problems (it costs about twice as much)."""
    s = services(request)
    conv = s.store.conversation(request.path_params["cid"])
    if conv is None:
        return error_page(request, viewer, 404, "Chat not found", "This chat doesn't exist.")
    form = await request.form()
    model = str(form.get("model", ""))
    if model not in (s.settings.model_default, s.settings.model_hard):
        return JSONResponse({"ok": False, "error": "Pick one of the listed models."}, status_code=400)
    if model != conv["model"]:
        s.store.set_model(conv["id"], model)
        s.db.audit("model_changed", actor_id=viewer.id, target_type="conversation", target_id=conv["id"],
                   details={"from": conv["model"], "to": model})
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True})
    return RedirectResponse(f"/c/{conv['id']}", status_code=303)


@page()
async def new_chat(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    subteam = form.get("subteam") if form.get("subteam") in SUBTEAMS else None
    try:
        files = await read_uploads(form, s.settings)
    except AttachmentError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "code": "invalid"}, status_code=400)
    outcome = await s.approvals.submit(viewer, str(form.get("text", "")), subteam=subteam, files=files,
                                       explainer=form.get("explainer") == "1")
    if not outcome.ok:
        return json_error(outcome)
    return JSONResponse({"ok": True, "url": f"/c/{outcome.conversation_id}", "message_id": outcome.message_id})


@page()
async def send_message(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    try:
        files = await read_uploads(form, s.settings)
    except AttachmentError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "code": "invalid"}, status_code=400)
    outcome = await s.approvals.submit(viewer, str(form.get("text", "")), conversation_id=request.path_params["cid"],
                                       files=files, explainer=form.get("explainer") == "1")
    if not outcome.ok:
        return json_error(outcome)
    return JSONResponse({"ok": True, "message_id": outcome.message_id})


def _review_action(kind: str):
    @page()
    async def action(request: Request, viewer: Viewer) -> Response:
        s = services(request)
        mid = request.path_params["mid"]
        form = await request.form()
        if kind in ("approve", "reject", "hold"):
            # Re-check mentor status with Slack, so someone just removed from #claude-approvals can't act.
            role = await s.directory.role_for(viewer.slack_user_id, force=True)
            if role != viewer.role:
                s.store.set_user_role(viewer.id, role)
                viewer = Viewer(id=viewer.id, slack_user_id=viewer.slack_user_id, name=viewer.name, role=role,
                                avatar_url=viewer.avatar_url)
        if kind == "approve":
            text = form.get("text")
            outcome = await s.approvals.approve(viewer, mid, edited_text=str(text) if text is not None else None)
        elif kind == "reject":
            outcome = await s.approvals.reject(viewer, mid, str(form.get("reason", "")))
        elif kind == "hold":
            outcome = await s.approvals.hold(viewer, mid)
        elif kind == "withdraw":
            outcome = await s.approvals.withdraw(viewer, mid)
        else:
            outcome = await s.approvals.retry(viewer, mid)
        if not outcome.ok:
            return json_error(outcome)
        return JSONResponse({"ok": True, "conversation_id": outcome.conversation_id})

    action.__name__ = f"{kind}_action"
    return action


@page(require="mentor")
async def queue_page(request: Request, viewer: Viewer) -> Response:
    return render(request, "queue.html", viewer, items=_queue_items(services(request)))


@page(require="mentor")
async def queue_items(request: Request, viewer: Viewer) -> Response:
    return render(request, "_queue_items.html", viewer, items=_queue_items(services(request)))


def _queue_items(s: Services) -> list[dict[str, Any]]:
    items = []
    for m in s.store.pending_messages():
        prev = s.store.previous_reply(m["conversation_id"], m["id"])
        items.append({
            "id": m["id"], "conversation_id": m["conversation_id"], "student": m["author_name"],
            "avatar": m["author_avatar"], "title": m["conversation_title"] or "New chat",
            "subteam": m["conversation_subteam"], "html": plain_html(m["content"]), "raw": m["content"],
            "waited": waited(m["created_at"]), "files": file_views(s, m["id"]),
            "explainer": bool(m["wants_explainer"]),
            "previous": (prev["content"][:200] + ("…" if len(prev["content"]) > 200 else "")) if prev else None,
        })
    return items


@page()
async def explainer_page(request: Request, viewer: Viewer) -> Response:
    """One interactive explainer from a reply, shown inside the chat's sandboxed frame."""
    s = services(request)
    conv = s.store.conversation(request.path_params["cid"])
    msg = s.store.message(request.path_params["mid"])
    if (conv is None or msg is None or msg["conversation_id"] != conv["id"] or msg["role"] != "assistant"
            or msg["status"] != COMPLETE or not s.store.can_view_message(viewer, conv, msg)):
        return PlainTextResponse("Not found", status_code=404)
    pages = explainers(msg["content"])
    index = request.path_params["n"]
    if index >= len(pages):
        return PlainTextResponse("Not found", status_code=404)
    headers = {"Content-Security-Policy": EXPLAINER_CSP, "Cache-Control": "private, no-cache"}
    page_html = pages[index]
    if request.query_params.get("download"):
        page_html = explainer_download(page_html)
        name = f"explainer-{conv['id']}-{msg['id']}-{index + 1}.html"
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return HTMLResponse(page_html, headers=headers)


@page()
async def attachment_file(request: Request, viewer: Viewer) -> Response:
    """An attached file, for anyone who can see the message it belongs to."""
    s = services(request)
    row = s.attachments.get(request.path_params["aid"])
    msg = s.store.message(row["message_id"]) if row is not None else None
    conv = s.store.conversation(msg["conversation_id"]) if msg is not None else None
    if conv is None or not s.store.can_view_message(viewer, conv, msg):
        return PlainTextResponse("Not found", status_code=404)
    try:
        data = s.attachments.read(row)
    except OSError:
        return PlainTextResponse("This file is missing from the server.", status_code=404)
    media_type = row["media_type"] if row["kind"] == "image" else "text/plain; charset=utf-8"
    disposition = "attachment" if request.query_params.get("download") else "inline"
    ascii_name = row["name"].encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    headers = {
        # A file is only ever shown, never run, even if someone opens it directly.
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
        "Content-Disposition": f'{disposition}; filename="{ascii_name}"',
        "Cache-Control": "private, max-age=3600",
    }
    return Response(data, media_type=media_type, headers=headers)


@page(require="admin")
async def audit_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    entries = []
    for row in s.store.audit_entries():
        details = json.loads(row["details"] or "{}")
        link = None
        if row["target_type"] == "conversation":
            link = f"/c/{row['target_id']}"
        elif row["target_type"] == "message" and details.get("conversation"):
            link = f"/c/{details['conversation']}#m{row['target_id']}"
        entries.append({"time": fmt_time(s.store, row["created_at"]), "actor": row["actor_name"] or "System",
                        "action": row["action"].replace("_", " "),
                        "target": f"{row['target_type'] or ''} {row['target_id'] or ''}".strip(),
                        "link": link, "details": details})
    return render(request, "audit.html", viewer, entries=entries)


async def about_page(request: Request) -> Response:
    viewer = await current_viewer(request)
    return render(request, "about.html", viewer)


async def health(request: Request) -> Response:
    s = services(request)
    return JSONResponse({"ok": True, "version": __version__, "slack": bool(s.slack),
                         "socket_mode": s.settings.socket_mode_enabled})


async def code_css(request: Request) -> Response:
    return Response(pygments_css(), media_type="text/css", headers={"Cache-Control": "public, max-age=86400"})


# --------------------------------------------------------------------------- live updates
def _sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def _event_stream(request: Request, topic: str, check: Callable[[dict], str]) -> StreamingResponse:
    broker = services(request).broker
    queue = broker.subscribe(topic)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                decision = check(event)
                if decision == "gone":
                    yield _sse({"type": "gone"})
                    return
                if decision == "send":
                    yield _sse(event)
        finally:
            broker.unsubscribe(topic, queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@page()
async def conversation_events(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    cid = request.path_params["cid"]
    conv = s.store.conversation(cid)
    if conv is None or not s.store.can_view_conversation(viewer, conv):
        return Response(status_code=404)

    def check(event: dict) -> str:
        if event["type"] == "visibility":
            fresh = s.store.conversation(cid)
            return "send" if fresh and s.store.can_view_conversation(viewer, fresh) else "gone"
        return "send"

    return _event_stream(request, f"conv:{cid}", check)


@page(require="mentor")
async def queue_events(request: Request, viewer: Viewer) -> Response:
    return _event_stream(request, "queue", lambda event: "send")


# --------------------------------------------------------------------------- app
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
                    (b"referrer-policy", b"same-origin"),
                    (b"x-frame-options", b"SAMEORIGIN" if own_policy else b"DENY"),
                ]
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
                                                       "files of up to 5 MB each."}, status_code=413)
        await response({"type": "http"}, None, send)


class BodyTooBig(Exception):
    pass


async def _every(seconds: float, job: Callable[[], Awaitable[Any]], stop: asyncio.Event, first_delay: float = 5) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=first_delay)
        return
    except asyncio.TimeoutError:
        pass
    while not stop.is_set():
        try:
            await job()
        except Exception:
            log.exception("Scheduled job failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


def create_app(settings: Settings | None = None, *, slack=None, claude=None,
               socket_connect: Callable[[str], Awaitable[Any]] | None = None,
               background: bool = True) -> Starlette:
    settings = settings or load_settings()
    s = build_services(settings, slack=slack, claude=claude)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # Each streaming reply holds a worker thread, so allow plenty of them.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=32, thread_name_prefix="io"))
        s.db.execute("UPDATE messages SET status = 'error', error = 'The server restarted before Claude finished.' "
                     "WHERE status = 'streaming'")
        await ensure_team_id(s)
        stop = asyncio.Event()
        loops: list[asyncio.Task] = []
        if background:
            loops.append(asyncio.create_task(_every(60, s.approvals.send_reminders, stop)))
            loops.append(asyncio.create_task(_every(86400, s.approvals.check_accounts, stop, first_delay=600)))
            if settings.socket_mode_enabled and isinstance(s.slack, SlackWebApi | FakeSlack):
                client = SocketModeClient(
                    open_url=lambda: s.slack.open_socket_url(settings.slack_app_token),
                    connect=socket_connect or WebsocketsTransport.connect,
                    dispatch=s.interactions.dispatch)
                app.state.socket_client = client
                loops.append(asyncio.create_task(client.run_forever(stop)))
        log.info("SubZero AI %s is running at %s", __version__, settings.public_url)
        try:
            yield
        finally:
            stop.set()
            for task in loops:
                task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            await s.tasks.cancel_all()

    review_routes = [
        Route(f"/m/{{mid:int}}/{kind}", _review_action(kind), methods=["POST"])
        for kind in ("approve", "reject", "hold", "withdraw", "retry")
    ]
    routes = [
        Route("/", home),
        Route("/login", login_page),
        Route("/auth/login", auth_start),
        Route("/auth/slack/callback", auth_callback),
        Route("/auth/dev", dev_login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/chats", chats),
        Route("/chats/new", new_chat, methods=["POST"]),
        Route("/c/{cid:int}", conversation_page),
        Route("/c/{cid:int}/messages", send_message, methods=["POST"]),
        Route("/c/{cid:int}/m/{mid:int}", message_partial),
        Route("/c/{cid:int}/events", conversation_events),
        Route("/c/{cid:int}/model", set_model, methods=["POST"]),
        Route("/c/{cid:int}/x/{mid:int}/{n:int}", explainer_page),
        Route("/files/{aid:int}", attachment_file),
        *review_routes,
        Route("/queue", queue_page),
        Route("/queue/items", queue_items),
        Route("/queue/events", queue_events),
        Route("/audit", audit_page),
        Route("/about", about_page),
        Route("/healthz", health),
        Route("/assets/code.css", code_css),
        Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    middleware = [
        Middleware(SecurityHeaders, https=settings.https),
        Middleware(LimitBody, max_bytes=MAX_FILES * MAX_FILE_BYTES + 1024 * 1024),
        Middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="subzero_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=settings.https),
    ]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.services = s
    return app
