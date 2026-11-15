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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
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

from . import __version__, maintenance, saved_settings, setup_flow, slack_cards
from .approvals import ApprovalService, Outcome
from .attachments import Attachments, size_label
from .bridge_login import BridgeLogin
from .auth import LoginError, SlackLogin, profile_from_claims
from .ai_backend import Capabilities, capabilities_for, make_client, resolve_backend
from .claude import ClaudeClient
from .code_runner import CodeRunner
from .egress_proxy import EgressProxy, TokenStore
from .sandbox import SandboxManager, reap_sandboxes, watch_sandbox_disks
from .config import Settings, load_settings, normalize_model_id
from .connectors import GitHub
from .db import Database
from .generation import ReplyRunner
from .health import Health
from .live import Broker, Tasks
from .maintenance import Nightly, check_for_update
# MODEL_LABELS and model_label stay importable from here (tests and older code use them)
from .model_choices import model_block, model_label, option_label, selectable_models
from .pricing import cost_usd, usage_line
from .render import markdown_html, plain_html, pygments_css
from .roles import DEV_USERS, RECOVERY_USER, Directory, is_excluded_account
from .screener import Screener
from .slack_api import FakeSlack, SlackWebApi
from .slack_dm import SlackDMs
from .slack_handlers import SlackInteractions
from .socket_mode import SocketModeClient, WebsocketsTransport
# Moved to web_common.py; every name stays importable from here (tests and older code use them)
from .web_common import (  # noqa: F401
    CSP,
    HERE,
    RECENTS,
    SAFE_NEXT,
    BodyTooBig,
    LimitBody,
    SecurityHeaders,
    Services,
    _finish_login,
    _health_alerts,
    _load_model_names,
    check_csrf,
    csrf_token,
    current_viewer,
    dev_login_allowed,
    error_page,
    fmt_time,
    is_local_request,
    json_error,
    log,
    page,
    render,
    services,
    short_text,
    templates,
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

def mentor_chat_model(s: Services, raw: object) -> tuple[str, str]:
    """The model a mentor picked for a new chat, or the default. A pick that isn't in the list for new chats (or
    that fails a check now) gives the default and a plain note saying why."""
    default = normalize_model_id(s.settings.model_default)
    model = normalize_model_id(str(raw or "").strip())
    if not model:
        return default, ""
    if model in {option["id"] for option in selectable_models(s, purpose="chat")}:
        return model, ""
    problem = model_block(s.settings, s.db, model) or "It isn't on the list of models for new chats."
    return default, (f"{model_label(model, s.model_names)} can't be used for this chat. {problem} "
                     f"This chat uses {model_label(default, s.model_names)} instead.")


def build_services(settings: Settings, *, slack=None, claude=None) -> Services:
    db = Database(settings.db_path)
    saved_settings.load_into(settings, db)
    if not setup_flow.setup_complete(db) and setup_flow.config_is_complete(settings):
        # Set up by hand in config.env (versions before 0.4 did this): lock the setup page.
        setup_flow.mark_setup_complete(db, actor="config.env was already complete")
    store = Store(db, timezone_name=settings.timezone, show_pending_to_team=settings.show_pending_to_team,
                  show_rejected_to_team=settings.show_rejected_to_team)
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
    directory = Directory(settings, slack)
    interactions = SlackInteractions(settings=settings, store=store, approvals=approvals, directory=directory,
                                     slack=slack, tasks=tasks)
    svc = Services(settings, db, store, slack, claude, broker, tasks, runner, approvals, directory, interactions,
                   SlackLogin(settings), attachments, health, Nightly(settings, db, store.tz), github,
                   CodeRunner(settings=settings, db=db, claude=claude, github=github, tasks=tasks,
                              sandboxes=sandboxes, slack=slack), screener, sandboxes=sandboxes)
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


def file_views(s: Services, message_id: int, rows=None) -> list[dict[str, Any]]:
    rows = s.attachments.for_message(message_id) if rows is None else rows
    # Video stills are only for Claude; the page shows the video itself
    rows = [f for f in rows if f["kind"] != "frame"]
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
    elif status == GRACE:
        # Mentors see which messages skipped review. Students see them like any approved message.
        if viewer.is_mentor:
            view["tag"], view["tag_kind"] = "Sent without review, in the approval window", "approved"
        else:
            view["tag"], view["tag_kind"] = None, None
    return view


def conversation_list(s: Services, viewer: Viewer, scope: str, query: str, subteam: str | None = None,
                      owner_id: int | None = None, limit: int = 200):
    rows = s.store.list_conversations(viewer, scope=scope, query=query, subteam=subteam, owner_id=owner_id,
                                      limit=limit)
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


def list_context(s: Services, viewer: Viewer, request: Request) -> dict[str, Any]:
    """The Chats page list with the filters from the query string (search, subteam, and who started the chat)."""
    params = request.query_params
    scope = "mine" if params.get("scope") == "mine" else "team"
    query = (params.get("q") or "").strip()[:100]
    subteam = params.get("subteam") if params.get("subteam") in s.settings.subteams else None
    owner = None
    if (params.get("owner") or "").isdigit():
        row = s.store.user(int(params["owner"]))
        if row is not None:
            owner = {"id": row["id"], "name": row["name"]}
    return {
        "items": conversation_list(s, viewer, scope, query, subteam, owner["id"] if owner else None),
        "scope": scope, "query": query, "subteams": s.settings.subteams, "subteam_filter": subteam, "owner_filter": owner,
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


def greeting(s: Services, viewer: Viewer) -> str:
    hour = datetime.now(s.store.tz).hour
    part = "morning" if 4 <= hour < 12 else "afternoon" if hour < 18 else "evening"
    return f"Good {part}, {viewer.name.split()[0]}"


@page()
async def chats(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    if any(key in request.query_params for key in ("q", "scope", "subteam", "owner")):
        # The chat list and its filters moved to the Chats page; keep old links working
        return RedirectResponse(f"/recents?{request.url.query}", status_code=303)
    model_options = ([(o["id"], option_label(o)) for o in selectable_models(s, purpose="chat")]
                     if viewer.is_mentor else [])
    return render(request, "chats.html", viewer, current_id=None, subteams=s.settings.subteams,
                  greeting=greeting(s, viewer), model_name=model_label(s.settings.model_default, s.model_names),
                  model_options=model_options, model_default_id=normalize_model_id(s.settings.model_default))


@page()
async def recents_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    return render(request, "recents.html", viewer, flash=request.session.pop("flash", None),
                  **list_context(s, viewer, request))


# --------------------------------------------------------------------------- profile
@page()
async def profile_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    row = s.store.user(viewer.id)
    needs_review = (not viewer.is_mentor) or s.settings.mentors_need_approval
    usage = None
    if needs_review and not viewer.is_mentor:
        usage = {"approved": s.store.count_approved_today(viewer.id), "daily": s.settings.limit_approved_per_day,
                 "waiting": s.store.count_pending_for(viewer.id), "max_waiting": s.settings.limit_pending_per_student}
    return render(request, "profile.html", viewer, row=row, subteams=s.settings.subteams, usage=usage,
                  slack_on=s.slack is not None, flash=request.session.pop("flash", None))


@page()
async def profile_save(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    subteam = str(form.get("default_subteam") or "")
    s.store.save_preferences(viewer.id, notify_dm=form.get("notify_dm") == "1",
                             default_subteam=subteam if subteam in s.settings.subteams else None)
    theme = str(form.get("theme") or "")
    if theme in THEMES:
        s.store.save_preferences(viewer.id, theme=theme)
    request.session["flash"] = "Saved your preferences."
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
    back = str(form.get("next") or "/chats")
    return RedirectResponse(back if SAFE_NEXT.fullmatch(back) else "/chats", status_code=303)


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
    if needs_review and own and not waiting and s.approvals.window_applies(viewer, conv["id"]):
        needs_review = False  # a mentor approved this chat a few minutes ago, so the next message goes straight on
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
    models = []
    if viewer.is_mentor:
        models = [(o["id"], option_label(o)) for o in selectable_models(s, purpose="chat")]
        if normalize_model_id(conv["model"]) not in {value for value, _ in models}:
            models.append((conv["model"], f"{model_label(conv['model'], s.model_names)} (not allowed now)"))
    return render(request, "conversation.html", viewer, conv=conv, messages=messages, own=own, title=title,
                  can_send=own, waiting=waiting, replying=replying, composer_hint=hint, needs_review=needs_review,
                  model_options=models, model_name=model_label(conv["model"], s.model_names), current_id=conv["id"],
                  long_chat=long_chat, flash=request.session.pop("flash", None))


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
    """Mentors can move a conversation to any model in the list for new chats (see model_choices)."""
    s = services(request)
    conv = s.store.conversation(request.path_params["cid"])
    if conv is None:
        return error_page(request, viewer, 404, "Chat not found", "This chat doesn't exist.")
    form = await request.form()
    model = normalize_model_id(form.get("model", ""))
    if model not in {option["id"] for option in selectable_models(s, purpose="chat")}:
        return JSONResponse({"ok": False, "error": "Pick one of the listed models."}, status_code=400)
    if model != normalize_model_id(conv["model"]):
        s.store.set_model(conv["id"], model)
        s.db.audit("model_changed", actor_id=viewer.id, target_type="conversation", target_id=conv["id"],
                   details={"from": conv["model"], "to": model})
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True})
    return RedirectResponse(f"/c/{conv['id']}", status_code=303)


@page(require="admin")
async def delete_chat(request: Request, viewer: Viewer) -> Response:
    """Admins (only) can delete a chat for good: its messages, files and share links go, the audit log keeps who
    deleted it and what it was called, and any review cards still waiting in Slack lose their buttons."""
    s = services(request)
    cid = request.path_params["cid"]
    # Review cards for messages still waiting; read them before the delete takes them away
    cards = s.db.all("SELECT sc.channel, sc.ts, u.name AS author_name FROM slack_cards sc "
                     "JOIN messages m ON m.id = sc.message_id LEFT JOIN users u ON u.id = m.author_id "
                     "WHERE m.conversation_id = ? AND m.status = 'pending'", (cid,))
    deleted = maintenance.delete_chat(s.db, s.attachments.files_dir, cid, actor_id=viewer.id)
    if deleted is None:
        return error_page(request, viewer, 404, "Chat not found", "This chat doesn't exist, or it was already deleted.")
    # Anyone with the chat open is sent back to their chat list; the review queue drops its messages
    s.broker.publish(f"conv:{cid}", {"type": "visibility"})
    s.broker.publish("queue", {"type": "queue"})
    if cards and s.slack is not None:
        s.tasks.spawn(_mark_cards_deleted(s, cards, viewer, deleted["title"]), name=f"chat-deleted-{cid}")
    request.session["flash"] = f"Deleted the chat “{deleted['title'] or 'New chat'}”."
    return RedirectResponse("/recents", status_code=303)


async def _mark_cards_deleted(s: Services, cards, viewer: Viewer, title: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for card in cards:
        name = card["author_name"] or "A student"
        slack_id = viewer.slack_user_id if viewer.slack_user_id[:1] in ("U", "W") else None  # not test sign-ins
        blocks = slack_cards.deleted_card(student_name=name, title=title, admin_slack_id=slack_id, deleted_at=now)
        try:
            await s.slack.update_message(card["channel"], card["ts"], slack_cards.card_fallback_text(name, "deleted"),
                                         blocks)
        except Exception:
            log.exception("Couldn't update a Slack card after deleting a chat")


UPLOAD_REFUSED = "Files can't be attached any more. Paste the part you need into your message instead."


async def _refuse_upload(form) -> bool:
    """True when the request carries a file. Files aren't accepted any more; the temporary copies are deleted."""
    found = any(hasattr(value, "read") and getattr(value, "filename", "") for _, value in form.multi_items())
    if found:
        await form.close()
    return found


@page()
async def new_chat(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    subteam = form.get("subteam") if form.get("subteam") in s.settings.subteams else None
    if subteam is None:
        # The new-chat page has no subteam picker; the person's default from Profile and preferences fills it in
        default = s.store.user(viewer.id)["default_subteam"]
        subteam = default if default in s.settings.subteams else None
    if await _refuse_upload(form):
        return JSONResponse({"ok": False, "error": UPLOAD_REFUSED, "code": "invalid"}, status_code=400)
    # Only mentors pick the model. A student's "model" field is ignored: the chat gets the default model.
    chat_model, note = s.settings.model_default, ""
    if viewer.is_mentor:
        chat_model, note = mentor_chat_model(s, form.get("model"))
    outcome = await s.approvals.submit(viewer, str(form.get("text", "")), subteam=subteam,
                                       model=chat_model if viewer.is_mentor else None)
    if not outcome.ok:
        return json_error(outcome)
    if note:
        request.session["flash"] = note
    return JSONResponse({"ok": True, "url": f"/c/{outcome.conversation_id}", "message_id": outcome.message_id})


@page()
async def send_message(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    if await _refuse_upload(form):
        return JSONResponse({"ok": False, "error": UPLOAD_REFUSED, "code": "invalid"}, status_code=400)
    outcome = await s.approvals.submit(viewer, str(form.get("text", "")), conversation_id=request.path_params["cid"])
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
    code_waiting = [{"id": r["id"], "title": r["title"], "owner": r["owner_name"], "pending": r["pending"],
                     "request": r["status"] == "awaiting_approval"}
                    for r in s.code.list() if r["status"] in ("waiting", "awaiting_approval")]
    return render(request, "queue.html", viewer, items=_queue_items(s), window=window,
                  code_waiting=code_waiting, flash=request.session.pop("flash", None))


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
    """An attached file, for anyone who can see the message it belongs to."""
    s = services(request)
    row = s.attachments.get(request.path_params["aid"])
    msg = s.store.message(row["message_id"]) if row is not None else None
    conv = s.store.conversation(msg["conversation_id"]) if msg is not None else None
    if conv is None or not s.store.can_view_message(viewer, conv, msg):
        return PlainTextResponse("Not found", status_code=404)
    try:
        path = s.attachments.path(row)
    except OSError:
        return PlainTextResponse("This file is missing from the server.", status_code=404)
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
    from .web_admin import admin_routes, apply_live
    from .web_code import code_routes

    settings = settings or load_settings()
    s = build_services(settings, slack=slack, claude=claude)
    apply_live(s)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # Each streaming reply holds a worker thread, so allow plenty of them.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=32, thread_name_prefix="io"))
        s.db.execute("UPDATE messages SET status = 'error', error = 'The server restarted before Claude finished.' "
                     "WHERE status = 'streaming'")
        s.db.execute("UPDATE code_steps SET status = 'error', output = 'The server restarted while this was running.' "
                     "WHERE status = 'running'")
        s.db.execute("UPDATE code_sessions SET status = 'failed', error = 'The server restarted while Claude was "
                     "working. Send a follow-up to carry on.' WHERE status IN ('preparing', 'running')")
        await ensure_team_id(s)
        if settings.code_sandbox == "hyperv" and s.egress is not None:
            await s.egress.start()  # a failure is kept as a plain message: sandboxes then refuse to start
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
            s.bridge_login.cancel()  # a Claude subscription sign-in nobody finished
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
        Route("/chats", chats),
        Route("/chats/new", new_chat, methods=["POST"]),
        Route("/recents", recents_page),
        Route("/profile", profile_page),
        Route("/profile", profile_save, methods=["POST"]),
        Route("/profile/theme", theme_save, methods=["POST"]),
        Route("/c/{cid:int}", conversation_page),
        Route("/c/{cid:int}/messages", send_message, methods=["POST"]),
        Route("/c/{cid:int}/m/{mid:int}", message_partial),
        Route("/c/{cid:int}/events", conversation_events),
        Route("/c/{cid:int}/model", set_model, methods=["POST"]),
        Route("/c/{cid:int}/delete", delete_chat, methods=["POST"]),
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
