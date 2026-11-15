"""The web app: sign-in, the mentor Review page and read-only chat page, live updates, and startup/shutdown."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, saved_settings, setup_flow
from .approvals import ApprovalService
from .attachments import Attachments, size_label
from .auth import LoginError, SlackLogin, profile_from_claims
from .ai_backend import make_client, resolve_backend
from .anthropic_gateway import AnthropicGateway
from .code_engine import RESTARTED
from .code_runner import CodeRunner
from .egress_proxy import EgressProxy, TokenStore
from .sandbox import SandboxManager, reap_sandboxes, watch_sandbox_disks
from .config import Settings, load_settings
from .connectors import GitHub
from .db import Database
from .generation import ReplyRunner
from .health import Health
from .live import Broker, Tasks
from .maintenance import Nightly, check_for_update
from .pricing import cost_usd, usage_line
from .render import markdown_html, plain_html, pygments_css
from .roles import DEV_USERS, Directory, is_excluded_account
from .screener import Screener
from .slack_api import FakeSlack, SlackWebApi
from .slack_dm import SlackDMs
from .slack_handlers import SlackInteractions
from .socket_mode import SocketModeClient, WebsocketsTransport
from .web_common import (
    HERE,
    SAFE_NEXT,
    LimitBody,
    SecurityHeaders,
    Services,
    _finish_login,
    _load_model_names,
    check_csrf,
    current_viewer,
    dev_login_allowed,
    error_page,
    fmt_time,
    is_local_request,
    json_error,
    page,
    render,
    services,
    short_text,
    waited,
)
from .store import (
    APPROVED,
    AUTO,
    COMPLETE,
    EDITED,
    ERROR,
    GRACE,
    HELD,
    PENDING,
    REJECTED,
    STREAMING,
    WITHDRAWN,
    THEMES,
    Store,
    Viewer,
)

log = logging.getLogger("subzero.web")

def build_services(settings: Settings, *, slack=None, claude=None) -> Services:
    db = Database(settings.db_path)
    saved_settings.load_into(settings, db)
    if not setup_flow.setup_complete(db) and setup_flow.config_is_complete(settings):
        # Set up by hand in config.env (versions before 0.4 did this): lock the setup page.
        setup_flow.mark_setup_complete(db, actor="config.env was already complete")
    store = Store(db, timezone_name=settings.timezone)
    if slack is None and settings.slack_enabled:
        slack = SlackWebApi(settings.slack_bot_token)
    if claude is None:
        claude = make_client(settings)
    broker, tasks = Broker(), Tasks()
    tokens = TokenStore()  # per-sandbox proxy logins: the proxy checks them, the sandboxes manager issues them
    egress = EgressProxy.from_settings(settings, tokens=tokens, db=db)
    sandboxes = SandboxManager(settings, db, tokens=tokens, egress=egress)
    attachments = Attachments(db, settings.data_dir / "files")
    github = GitHub(settings)
    runner = ReplyRunner(settings=settings, db=db, store=store, claude=claude, broker=broker, tasks=tasks,
                         attachments=attachments)
    screener = Screener(settings, claude)  # one screener, shared by the approval window and Code requests
    approvals = ApprovalService(settings=settings, db=db, store=store, slack=slack, broker=broker, tasks=tasks,
                                runner=runner, attachments=attachments, screener=screener)
    runner.on_alert = approvals.mentor_alert
    health = Health(settings, slack, claude)
    runner.on_result = lambda ok, error: health.record("claude", ok, error)
    directory = Directory(settings, slack, db=db)
    interactions = SlackInteractions(settings=settings, store=store, approvals=approvals, directory=directory,
                                     slack=slack, tasks=tasks)
    code = CodeRunner(settings=settings, db=db, claude=claude, github=github, tasks=tasks, sandboxes=sandboxes,
                      slack=slack)
    # The Code gateway: where the new Code engine's sandboxes reach Anthropic. Built here, started with the app only for
    # Hyper-V sandboxes and the new engine (and when a session needs it, if an admin switched the engine on later).
    gateway = AnthropicGateway(host=settings.sandbox_host_address, port=settings.sandbox_gateway_port,
                               authenticate=code.agent.authenticate, policy=code.agent.policy,
                               record=code.agent.record, credential=code.agent.credential,
                               upstream_base=settings.anthropic_base_url or "https://api.anthropic.com")
    code.agent.gateway = gateway
    svc = Services(settings, db, store, slack, claude, broker, tasks, runner, approvals, directory, interactions,
                   SlackLogin(settings), attachments, health, Nightly(settings, db, store.tz), github, code, screener,
                   sandboxes=sandboxes, gateway=gateway)
    svc.nightly.reap = lambda: reap_sandboxes(svc)
    svc.nightly.watch_disks = lambda: watch_sandbox_disks(svc)
    svc.egress = egress
    runner.capabilities_of = svc.capabilities
    if slack is not None:
        dms = SlackDMs(settings=settings, store=store, approvals=approvals, directory=directory, slack=slack,
                       tasks=tasks)
        interactions.dms = svc.dms = dms
        runner.on_reply, runner.on_reply_done = dms.on_reply, dms.on_reply_done
        approvals.on_decided = dms.on_decided
    return svc


def _stored(s: Services, row) -> bool:
    """False once a mentor has deleted the file from the Files page (the chat keeps its row)."""
    try:
        s.attachments.path(row)
    except OSError:
        return False
    return True


def file_views(s: Services, message_id: int, rows=None) -> list[dict[str, Any]]:
    rows = s.attachments.for_message(message_id) if rows is None else rows
    # Video stills are only for Claude; the page shows the video itself
    rows = [f for f in rows if f["kind"] != "frame"]
    return [{"id": f["id"], "name": f["name"], "size": size_label(f["size"]), "kind": f["kind"],
             "url": f"/files/{f['id']}", "removed": not _stored(s, f)} for f in rows]


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
        "reply_to": m["reply_to"], "can_review": False, "can_retry": False,
        "original": None, "tag": None, "tag_kind": None, "files": [], "sources": [], "searches": m["web_searches"] or 0,
    }
    if m["role"] == "assistant":
        if m["status"] == STREAMING:
            text, seq = s.runner.live_text(m["id"])
            view["html"], view["seq"] = plain_html(text), seq
        elif m["status"] == COMPLETE:
            view["html"] = markdown_html(m["content"])
        else:
            view["html"] = plain_html(m["content"])
        try:
            view["sources"] = json.loads(m["sources"] or "[]")
        except ValueError:
            pass
        view["can_retry"] = (m["status"] == ERROR and m["id"] == last_id
                             and (viewer.is_mentor or conv["owner_id"] == viewer.id))
        # Mentors see what each finished reply used and roughly cost (see pricing.py for the price list)
        tokens = sum(int(m[k] or 0)
                     for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        if viewer.is_mentor and m["status"] in (COMPLETE, ERROR) and tokens:
            view["usage"] = usage_line(tokens, cost_usd(
                m["model"] or conv["model"], input_tokens=m["input_tokens"], output_tokens=m["output_tokens"],
                cache_read_tokens=m["cache_read_tokens"], cache_write_tokens=m["cache_write_tokens"],
                web_searches=m["web_searches"], model_info=s.model_info))
        return view
    view["html"] = plain_html(m["content"])
    view["raw"] = m["content"]
    view["files"] = file_views(s, m["id"], files)
    status = m["status"]
    reviewer = m["reviewer_name"] or "a mentor"
    if status == PENDING:
        view["tag"], view["tag_kind"] = "Waiting for a mentor", "pending"
        view["can_review"] = viewer.is_mentor and not is_author
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
    elif status == GRACE:
        # Mentors see which messages skipped review. Students see them like any approved message.
        if viewer.is_mentor:
            view["tag"], view["tag_kind"] = "Sent without review, in the approval window", "approved"
        else:
            view["tag"], view["tag_kind"] = None, None
    return view


# --------------------------------------------------------------------------- pages
async def home(request: Request) -> Response:
    viewer = await current_viewer(request)
    return RedirectResponse("/code" if viewer else "/login", status_code=303)


async def login_page(request: Request) -> Response:
    viewer = await current_viewer(request)
    if viewer:
        return RedirectResponse("/code", status_code=303)
    error = request.session.pop("login_error", None)
    s = services(request)
    return render(request, "login.html", None, error=error, slack_ready=s.settings.slack_login_enabled,
                  dev_allowed=dev_login_allowed(request), local=is_local_request(request),
                  setup_open=not setup_flow.setup_complete(s.db))


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
    info: dict = {}
    if s.slack is not None:
        try:
            info = await s.slack.user_info(profile["slack_user_id"])
        except Exception:
            log.warning("Couldn't look up the Slack account %s", profile["slack_user_id"], exc_info=True)
            if not s.settings.allow_slack_guests:
                # Fail closed: without the account details we can't tell a guest from a member.
                request.session["login_error"] = "Couldn't reach Slack to finish signing in. Try again in a minute."
                return RedirectResponse("/login", status_code=303)
    if is_excluded_account(info, s.settings) and profile["slack_user_id"] not in s.settings.admin_slack_ids:
        request.session["login_error"] = ("SubZero AI is for full members of the team's Slack workspace. "
                                          "Guest accounts can't sign in. Ask a coach if you think this is a mistake.")
        s.db.audit("sign_in_refused", actor_id=None, details={"slack_user_id": profile["slack_user_id"],
                                                               "reason": "guest or bot account"})
        return RedirectResponse("/login", status_code=303)
    if not profile["avatar_url"]:
        profile["avatar_url"] = (info.get("profile") or {}).get("image_72")
    role = await s.directory.role_for(profile["slack_user_id"], force=True)
    user = s.store.upsert_user(slack_user_id=profile["slack_user_id"], name=profile["name"], role=role,
                               team_id=profile["team_id"], email=profile["email"], avatar_url=profile["avatar_url"],
                               login=True)
    return _finish_login(request, s, user)


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


CHATS_MOVED = "Chats have moved to Slack. Message the bot there."


async def moved_to_code(request: Request) -> Response:
    """The old chat, project and artifact addresses: the website is for Code now, and chatting happens in Slack."""
    request.session["flash"] = CHATS_MOVED
    return RedirectResponse("/code", status_code=303)


# --------------------------------------------------------------------------- profile
@page()
async def profile_page(request: Request, viewer: Viewer) -> Response:
    return render(request, "profile.html", viewer, flash=request.session.pop("flash", None))


@page()
async def profile_save(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    theme = str(form.get("theme") or "")
    if theme in THEMES:
        s.store.save_preferences(viewer.id, theme=theme)
    request.session["flash"] = "Saved."
    return RedirectResponse("/profile", status_code=303)


@page()
async def theme_save(request: Request, viewer: Viewer) -> Response:
    """Appearance, from the account menu. The script applies it at once; without the script the page reloads."""
    s = services(request)
    form = await request.form()
    theme = str(form.get("theme") or "")
    if theme not in THEMES:
        return JSONResponse({"ok": False, "error": "Pick light, dark or system."}, status_code=400)
    s.store.save_preferences(viewer.id, theme=theme)
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True})
    back = str(form.get("next") or "/code")
    return RedirectResponse(back if SAFE_NEXT.fullmatch(back) else "/code", status_code=303)


@page()
async def conversation_page(request: Request, viewer: Viewer) -> Response:
    """A chat, read-only, for mentors and admins. Students chat in Slack and get no chat pages, so they are sent to
    Code (and nothing about the chat is looked up for them)."""
    if not viewer.is_mentor:
        return await moved_to_code(request)
    s = services(request)
    conv = s.store.conversation(request.path_params["cid"])
    if conv is None or not s.store.can_view_conversation(viewer, conv):
        return error_page(request, viewer, 404, "Chat not found", "This chat doesn't exist, or it was deleted.")
    last_id = s.store.last_message_id(conv["id"])
    files = s.attachments.for_conversation(conv["id"])
    messages = [message_view(s, viewer, conv, m, last_id, files.get(m["id"], []))
                for m in s.store.messages(conv["id"])]
    if request.query_params.get("thread"):
        # The page asks for just the messages when it refreshes itself
        return render(request, "_thread.html", viewer, messages=messages)
    title = conv["title"]
    if not title:
        first = next((m for m in messages if m["role"] == "user"), None)
        title = short_text(first["raw"]) if first else "New chat"
    return render(request, "conversation.html", viewer, conv=conv, messages=messages, title=title,
                  flash=request.session.pop("flash", None))


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
                                avatar_url=viewer.avatar_url, theme=viewer.theme)
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
    s = services(request)
    window = [{"id": m["id"], "conversation_id": m["conversation_id"], "student": m["author_name"],
               "title": m["conversation_title"] or "New chat", "text": short_text(m["content"], 120),
               "time": fmt_time(s.store, m["created_at"])} for m in s.store.window_messages()]
    return render(request, "queue.html", viewer, items=_queue_items(s), window=window,
                  flash=request.session.pop("flash", None))


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
            "subteam": m["conversation_subteam"],
            "html": plain_html(m["content"]), "raw": m["content"],
            "waited": waited(m["created_at"]), "files": file_views(s, m["id"]),
            "screen_note": m["screen_note"],
            "previous": (prev["content"][:200] + ("…" if len(prev["content"]) > 200 else "")) if prev else None,
        })
    return items


async def link_gone(request: Request) -> Response:
    """Share links were removed. Every old address gives the same plain page; nothing is looked up."""
    response = render(request, "link_gone.html", None, status_code=410)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@page()
async def attachment_file(request: Request, viewer: Viewer) -> Response:
    """An old attached file, for mentors and admins (students have no chat pages, so no files either)."""
    s = services(request)
    row = s.attachments.get(request.path_params["aid"])
    msg = s.store.message(row["message_id"]) if row is not None else None
    conv = s.store.conversation(msg["conversation_id"]) if msg is not None else None
    if conv is None or not s.store.can_view_conversation(viewer, conv):
        return PlainTextResponse("Not found", status_code=404)
    try:
        path = s.attachments.path(row)
    except OSError:
        return PlainTextResponse("This file was removed from the server.", status_code=404)
    media_type = row["media_type"] if row["kind"] != "text" else "text/plain; charset=utf-8"
    disposition = "attachment" if request.query_params.get("download") else "inline"
    ascii_name = row["name"].encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    headers = {
        # A file is only ever shown, never run, even if someone opens it directly.
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
        "Content-Disposition": f'{disposition}; filename="{ascii_name}"',
        "Cache-Control": "private, max-age=3600",
    }
    # FileResponse answers range requests, which browsers (Safari above all) need to play and seek a video
    return FileResponse(path, media_type=media_type, headers=headers)


AUDIT_PAGE = 100  # entries per page


def _date_arg(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip()) if value else None
    except ValueError:
        return None


@page(require="admin")
async def audit_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    q = request.query_params
    actor, action = q.get("who", "").strip(), q.get("what", "").strip()
    since, until = _date_arg(q.get("from")), _date_arg(q.get("to"))
    number = max(1, int(q["page"])) if q.get("page", "").isdigit() else 1
    rows, total = s.store.audit_search(actor=actor, action=action, since=since, until=until, limit=AUDIT_PAGE,
                                       offset=(number - 1) * AUDIT_PAGE)
    entries = []
    for row in rows:
        details = json.loads(row["details"] or "{}")
        link = None
        if row["target_type"] == "conversation":
            link = f"/c/{row['target_id']}"
        elif row["target_type"] == "message" and details.get("conversation"):
            link = f"/c/{details['conversation']}#m{row['target_id']}"
        elif row["target_type"] == "code":
            link = f"/code/{row['target_id']}"
        entries.append({"time": fmt_time(s.store, row["created_at"]), "actor": row["actor_name"] or "System",
                        "action": row["action"].replace("_", " "),
                        "target": f"{row['target_type'] or ''} {row['target_id'] or ''}".strip(),
                        "link": link, "details": details})
    keep = {key: value for key, value in (("who", actor), ("what", action), ("from", q.get("from", "")),
                                          ("to", q.get("to", ""))) if value}
    pages = max(1, -(-total // AUDIT_PAGE))
    return render(request, "audit.html", viewer, entries=entries, total=total, number=number, pages=pages,
                  actors=s.store.audit_actors(), actions=[(a, a.replace("_", " ")) for a in s.store.audit_actions()],
                  who=actor, what=action, date_from=q.get("from", "") if since else "",
                  date_to=q.get("to", "") if until else "", filtered=bool(keep),
                  newer=f"/audit?{urlencode({**keep, 'page': number - 1})}" if number > 1 else None,
                  older=f"/audit?{urlencode({**keep, 'page': number + 1})}" if number < pages else None)


async def about_page(request: Request) -> Response:
    viewer = await current_viewer(request)
    return render(request, "about.html", viewer)


async def health(request: Request) -> Response:
    """For update.ps1 and the Windows service checks: the app is up, and what it knows about Slack, Claude,
    and backups. No secrets or names."""
    s = services(request)
    snap = s.health.snapshot()
    client = getattr(request.app.state, "socket_client", None)
    return JSONResponse({
        "ok": True, "version": __version__, "setup_complete": setup_flow.setup_complete(s.db),
        "slack": snap["slack"]["state"], "socket_mode": bool(client and client.connected.is_set()),
        "claude": snap["claude"]["state"], "last_backup_at": s.db.get_kv("last_backup_at"),
        "last_backup_ok": s.db.get_kv("last_backup_ok") != "0" if s.db.get_kv("last_backup_at") else None,
    })


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


@page(require="mentor")
async def queue_events(request: Request, viewer: Viewer) -> Response:
    return _event_stream(request, "queue", lambda event: "send")


# --------------------------------------------------------------------------- app
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
    from .web_admin import admin_routes
    from .web_code import code_routes
    from .web_files import files_routes

    settings = settings or load_settings()
    s = build_services(settings, slack=slack, claude=claude)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # Each streaming reply holds a worker thread, so allow plenty of them.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=32, thread_name_prefix="io"))
        s.db.execute("UPDATE messages SET status = 'error', error = 'The server restarted before Claude finished.' "
                     "WHERE status = 'streaming'")
        s.db.execute("UPDATE code_steps SET status = 'error', output = 'The server restarted while this was running.' "
                     "WHERE status = 'running'")
        for row in s.db.all("SELECT id FROM code_sessions WHERE engine = 'agent' AND status IN ('preparing', "
                            "'running')"):  # the new engine's runner is gone too: say what to do next
            s.code._note(row["id"], 0, RESTARTED)
        s.db.execute("UPDATE code_sessions SET status = 'failed', error = 'The server restarted while Claude was "
                     "working. Send a follow-up to carry on.' WHERE status IN ('preparing', 'running')")
        await ensure_team_id(s)
        if settings.code_sandbox == "hyperv" and s.egress is not None:
            await s.egress.start()  # a failure is kept as a plain message: sandboxes then refuse to start
        if (settings.code_sandbox == "hyperv" and s.code.engine_now() == "agent" and s.gateway is not None
                and resolve_backend(settings) != "fake"):  # test mode (pretend Claude) never needs the network
            await s.gateway.start()  # a failure is kept as a plain message the Settings overview shows
        try:
            await asyncio.to_thread(reap_sandboxes, s)  # sandboxes left by the last run, before any session starts
        except Exception:
            log.exception("Sandbox cleanup failed at startup")
        stop = asyncio.Event()
        loops: list[asyncio.Task] = []
        if background:
            loops.append(asyncio.create_task(_load_model_names(s)))
            loops.append(asyncio.create_task(_every(60, s.approvals.send_reminders, stop)))
            loops.append(asyncio.create_task(_every(86400, s.approvals.check_accounts, stop, first_delay=600)))
            loops.append(asyncio.create_task(_every(60, s.health.check, stop, first_delay=30)))
            loops.append(asyncio.create_task(_every(60, s.nightly.tick, stop, first_delay=20)))
            loops.append(asyncio.create_task(_every(86400, lambda: check_for_update(settings, s.db), stop,
                                                    first_delay=120)))
            if settings.socket_mode_enabled and isinstance(s.slack, SlackWebApi | FakeSlack):
                client = SocketModeClient(
                    open_url=lambda: s.slack.open_socket_url(settings.slack_app_token),
                    connect=socket_connect or WebsocketsTransport.connect,
                    dispatch=s.interactions.dispatch)
                app.state.socket_client = client
                s.health.socket_client = client
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
            await s.code.agent.shutdown()  # cuts off the new engine's runners
            s.bridge_login.cancel()  # a Claude subscription sign-in nobody finished
            if s.gateway is not None:
                await s.gateway.stop()
            if s.egress is not None:
                await s.egress.stop()  # closes any sandbox connection still open
            closer = getattr(s.claude, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    log.exception("Couldn't close the AI client")

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
        Route("/chats", moved_to_code),
        Route("/recents", moved_to_code),
        Route("/projects", moved_to_code),
        Route("/projects/{rest:path}", moved_to_code),
        Route("/artifacts", moved_to_code),
        Route("/artifacts/{rest:path}", moved_to_code),
        Route("/profile", profile_page),
        Route("/profile", profile_save, methods=["POST"]),
        Route("/profile/theme", theme_save, methods=["POST"]),
        Route("/c/{cid:int}", conversation_page),
        Route("/s/{token:str}", link_gone),
        Route("/s/{token:str}/x/{mid:int}/{n:int}", link_gone),
        Route("/files/{aid:int}", attachment_file),
        *review_routes,
        Route("/queue", queue_page),
        Route("/queue/items", queue_items),
        Route("/queue/events", queue_events),
        Route("/audit", audit_page),
        *admin_routes,
        *code_routes,
        *files_routes,
        Route("/about", about_page),
        Route("/healthz", health),
        Route("/assets/code.css", code_css),
        Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    middleware = [
        Middleware(SecurityHeaders, https=settings.https),
        Middleware(LimitBody, max_bytes=2 * 1024 * 1024),
        Middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="subzero_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=settings.https),
    ]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.services = s
    app.state.request_restart = None  # set by __main__: stops the app so the Windows service starts it again
    return app
