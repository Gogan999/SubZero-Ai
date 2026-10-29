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

from . import __version__, saved_settings, setup_flow
from .approvals import ApprovalService, Outcome
from .attachments import (
    ACCEPT,
    FRAME_MAX_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_FRAMES_PER_VIDEO,
    MAX_MESSAGE_BYTES,
    AttachmentError,
    clean_name,
    Attachments,
    Prepared,
    prepare,
    prepare_frame,
    size_label,
)
from .auth import LoginError, SlackLogin, profile_from_claims
from .claude import ClaudeClient, FakeClaude
from .code_runner import CodeRunner
from .config import Settings, load_settings
from .connectors import ConnectorError, Drive, GitHub
from .db import Database
from .generation import STYLES, ReplyRunner, clean_options, load_options, options_labels
from .health import Health
from .live import Broker, Tasks
from .maintenance import Nightly, check_for_update
from .projects import MAX_PROJECT_FILES, ProjectError, Projects
from .shares import SHARE_DAYS, ShareError, Shares, state as share_state
from .render import (
    EXPLAINER_CSP,
    EXPLAINER_MAX_BYTES,
    explainer_download,
    explainer_figure,
    explainer_title,
    explainers,
    markdown_html,
    plain_html,
    pygments_css,
)
from .roles import DEV_USERS, RECOVERY_USER, Directory, is_excluded_account
from .slack_api import FakeSlack, SlackWebApi
from .slack_handlers import SlackInteractions
from .socket_mode import SocketModeClient, WebsocketsTransport
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
HERE = Path(__file__).parent

MODEL_LABELS = {
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-opus-5-5": "Opus 5.5",
    "claude-haiku-5-5": "Haiku 5.5",
}


# How many of a person's own chats the sidebar lists before "All chats"
RECENTS = 25

# claude.ai features with a place in the sidebar but not built yet (none right now). Each gets a "Coming soon"
# page that says what it will do, so students aren't left guessing. docs/FEATURES_TODO.md tracks them.
COMING_SOON: dict[str, dict] = {}


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
    # Explainer titles and sizes per finished reply, for the Artifacts gallery (a finished reply never changes)
    artifact_index: dict[int, list[tuple[int, str, int]]] = field(default_factory=dict)
    # Clients made from tokens typed on the setup page, before they're saved (tests swap these out)
    slack_factory: Callable[[str], Any] = SlackWebApi
    claude_factory: Callable[[str], Any] = ClaudeClient


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
        claude = FakeClaude() if settings.fake_claude else ClaudeClient(
            settings.anthropic_api_key, base_url=settings.anthropic_base_url)
    broker, tasks = Broker(), Tasks()
    attachments = Attachments(db, settings.data_dir / "files")
    projects = Projects(db, settings.data_dir / "files")
    github = GitHub(settings)
    runner = ReplyRunner(settings=settings, db=db, store=store, claude=claude, broker=broker, tasks=tasks,
                         attachments=attachments, projects=projects)
    approvals = ApprovalService(settings=settings, db=db, store=store, slack=slack, broker=broker, tasks=tasks,
                                runner=runner, attachments=attachments)
    runner.on_alert = approvals.mentor_alert
    health = Health(settings, slack, claude)
    runner.on_result = lambda ok, error: health.record("claude", ok, error)
    directory = Directory(settings, slack)
    interactions = SlackInteractions(settings=settings, store=store, approvals=approvals, directory=directory,
                                     slack=slack, tasks=tasks)
    return Services(settings, db, store, slack, claude, broker, tasks, runner, approvals, directory, interactions,
                    SlackLogin(settings), attachments, health, Nightly(settings, db, store.tz), projects,
                    Shares(db), github, Drive(settings),
                    CodeRunner(settings=settings, db=db, claude=claude, github=github, tasks=tasks))


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


def tool_options(s: Services, viewer: Viewer, form) -> dict:
    return clean_options(form, can_research=viewer.is_mentor or s.settings.research_for_students,
                         search_on=s.settings.web_search_enabled)


def upload_context() -> dict[str, Any]:
    return {"accept": ACCEPT, "max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES,
            "max_message_bytes": MAX_MESSAGE_BYTES, "max_frames": MAX_FRAMES_PER_VIDEO,
            "styles": [(key, label) for key, (label, _) in STYLES.items()]}


async def read_uploads(form, settings: Settings) -> list[Prepared]:
    """The files sent with a message, checked and converted. Raises AttachmentError."""
    try:
        uploads = [f for f in form.getlist("files") if hasattr(f, "read") and getattr(f, "filename", "")]
        if len(uploads) > MAX_FILES:
            raise AttachmentError(f"You can attach up to {MAX_FILES} files to one message.")
        prepared = []
        total = 0
        for upload in uploads:
            data = await upload.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if total > MAX_MESSAGE_BYTES:
                raise AttachmentError(f"Those files add up to more than {MAX_MESSAGE_BYTES // (1024 * 1024)} MB. "
                                      "Send fewer or smaller files with this message.")
            prepared.append(await asyncio.to_thread(prepare, upload.filename, data,
                                                    max_text_bytes=settings.max_text_file_kb * 1024))
        return await _with_frames(form, prepared)
    finally:
        await form.close()  # delete the temporary copies of the uploads


async def connector_files(s: Services, form, already: int) -> list[Prepared]:
    """Files picked from the team's GitHub repository or pasted as Google Drive links, fetched now and stored
    like attachments, so mentors see them on the review card. Raises AttachmentError."""
    paths = [str(p) for p in form.getlist("github") if str(p).strip()]
    links = [str(link) for link in form.getlist("drive") if str(link).strip()]
    if not paths and not links:
        return []
    if paths and not s.github.configured:
        raise ConnectorError("GitHub isn't set up. An admin can add the team's repository in Settings.")
    if links and not s.drive.enabled:
        raise ConnectorError("Adding Google Drive links is switched off. Download the file and attach it instead.")
    if already + len(paths) + len(links) > MAX_FILES:
        raise AttachmentError(f"You can attach up to {MAX_FILES} files to one message.")
    limit = s.settings.max_text_file_kb * 1024
    items = []
    for path in dict.fromkeys(paths):
        items.append(await asyncio.to_thread(s.github.fetch, path, max_text_bytes=limit))
    for link in dict.fromkeys(links):
        items.append(await asyncio.to_thread(s.drive.fetch, link, max_text_bytes=limit))
    return items


async def _with_frames(form, prepared: list[Prepared]) -> list[Prepared]:
    """Still frames the browser took from each video (named "<video name>@<seconds>"), placed right after their
    video. Frames for a video that wasn't sent, or past the per-video limit, are dropped."""
    frames: dict[str, list[Prepared]] = {}
    for upload in form.getlist("frames"):
        if not hasattr(upload, "read"):
            continue
        raw_name, _, seconds = (getattr(upload, "filename", "") or "").rpartition("@")
        video_name = clean_name(raw_name)
        target = next((p for p in prepared if p.kind == "video" and p.name == video_name), None)
        if target is None or len(frames.get(video_name, [])) >= MAX_FRAMES_PER_VIDEO:
            continue
        stem = seconds.rsplit(".", 1)[0]
        at = int(stem) if stem.isdigit() else 0
        data = await upload.read(FRAME_MAX_BYTES + 1)
        if len(data) > FRAME_MAX_BYTES:
            continue
        try:
            frames.setdefault(video_name, []).append(await asyncio.to_thread(prepare_frame, video_name, at, data))
        except AttachmentError:
            continue  # a bad frame just means Claude sees one fewer still
    out = []
    for item in prepared:
        out.append(item)
        if item.kind == "video":
            out += frames.get(item.name, [])
    return out



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
    from .web_admin import health_rows
    return health_rows(s)


def error_page(request: Request, viewer: Viewer | None, code: int, title: str, detail: str) -> HTMLResponse:
    return render(request, "error.html", viewer, status_code=code, title=title, detail=detail)


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
        "original": None, "tag": None, "tag_kind": None, "files": [], "explainer": False, "tools": [],
        "sources": [], "searches": m["web_searches"] or 0,
    }
    if m["role"] == "assistant":
        if m["status"] == STREAMING:
            text, seq = s.runner.live_text(m["id"])
            view["html"], view["seq"] = plain_html(text), seq
        elif m["status"] == COMPLETE:
            view["html"] = markdown_html(m["content"], explainer_base=f"/c/{conv['id']}/x/{m['id']}")
        else:
            view["html"] = plain_html(m["content"])
        try:
            view["sources"] = json.loads(m["sources"] or "[]")
        except ValueError:
            pass
        view["can_retry"] = (m["status"] == ERROR and m["id"] == last_id
                             and (viewer.is_mentor or conv["owner_id"] == viewer.id))
        return view
    view["html"] = plain_html(m["content"])
    view["raw"] = m["content"]
    view["files"] = file_views(s, m["id"], files)
    view["explainer"] = bool(m["wants_explainer"])
    view["tools"] = options_labels(load_options(m["options"]))
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
                      owner_id: int | None = None, limit: int = 200, project_id: int | None = None):
    rows = s.store.list_conversations(viewer, scope=scope, query=query, subteam=subteam, owner_id=owner_id,
                                      project_id=project_id, limit=limit)
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
    return render(request, "chats.html", viewer, current_id=None, subteams=s.settings.subteams,
                  greeting=greeting(s, viewer), model_name=model_label(s.settings.model_default),
                  **upload_context())


@page()
async def recents_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    return render(request, "recents.html", viewer, **list_context(s, viewer, request))


def coming_soon(key: str):
    @page()
    async def endpoint(request: Request, viewer: Viewer) -> Response:
        return render(request, "coming_soon.html", viewer, feature=COMING_SOON[key])
    return endpoint


ARTIFACTS_SHOWN = 300


def _explainer_index(s: Services, message_id: int) -> list[tuple[int, str, int]]:
    """(number, title, size) for each explainer in a finished reply, worked out once and then remembered."""
    if message_id not in s.artifact_index:
        msg = s.store.message(message_id)
        s.artifact_index[message_id] = [(i, explainer_title(page_html), len(page_html.encode("utf-8")))
                                        for i, page_html in enumerate(explainers(msg["content"] if msg else ""))]
    return s.artifact_index[message_id]


def artifact_list(s: Services, viewer: Viewer, scope: str, query: str) -> list[dict[str, Any]]:
    """Every interactive explainer the viewer can see, newest first. An explainer is only as visible as the
    reply it's in, so private chats stay private and other students' chats show once something was approved."""
    convs: dict[int, Any] = {}
    items: list[dict[str, Any]] = []
    needle = query.lower()
    # can_view_message only needs these for a finished reply
    reply = {"role": "assistant", "status": COMPLETE, "author_id": None}
    for row in s.store.explainer_replies(owner_id=viewer.id if scope == "mine" else None):
        cid = row["conversation_id"]
        if cid not in convs:
            convs[cid] = s.store.conversation(cid)
        conv = convs[cid]
        if conv is None or not s.store.can_view_message(viewer, conv, reply):
            continue
        chat_title = conv["title"] or "New chat"
        for index, page_title, size in _explainer_index(s, row["id"]):
            if size > EXPLAINER_MAX_BYTES:
                continue  # the chat shows these as code, not as a working explainer
            title = page_title or f"Explainer from {chat_title}"
            if needle and needle not in title.lower() and needle not in chat_title.lower():
                continue
            items.append({"url": f"/artifacts/{row['id']}/{index}", "title": title, "chat_title": chat_title,
                          "chat_url": f"/c/{cid}#m{row['id']}", "owner": conv["owner_name"],
                          "subteam": conv["subteam"], "private": conv["visibility"] == "private",
                          "time": fmt_time(s.store, row["created_at"])})
        if len(items) >= ARTIFACTS_SHOWN:
            break
    return items[:ARTIFACTS_SHOWN]


@page()
async def artifacts_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    scope = "mine" if request.query_params.get("scope") == "mine" else "team"
    query = (request.query_params.get("q") or "").strip()[:100]
    return render(request, "artifacts.html", viewer, items=artifact_list(s, viewer, scope, query), scope=scope,
                  query=query)


@page()
async def artifact_page(request: Request, viewer: Viewer) -> Response:
    """One explainer on its own page, with the same Full screen, Download and View code actions as in the chat."""
    s = services(request)
    msg = s.store.message(request.path_params["mid"])
    conv = s.store.conversation(msg["conversation_id"]) if msg is not None else None
    if (conv is None or msg["role"] != "assistant" or msg["status"] != COMPLETE
            or not s.store.can_view_message(viewer, conv, msg)):
        return error_page(request, viewer, 404, "Artifact not found",
                          "This artifact doesn't exist, or it's in a chat that's private to the student and mentors.")
    pages = explainers(msg["content"])
    index = request.path_params["n"]
    if index >= len(pages) or len(pages[index].encode("utf-8")) > EXPLAINER_MAX_BYTES:
        return error_page(request, viewer, 404, "Artifact not found", "This chat doesn't have that explainer.")
    chat_title = conv["title"] or "New chat"
    title = explainer_title(pages[index]) or f"Explainer from {chat_title}"
    figure = explainer_figure(pages[index], f"/c/{conv['id']}/x/{msg['id']}/{index}")
    return render(request, "artifact.html", viewer, title=title, figure=figure, conv=conv, chat_title=chat_title,
                  chat_url=f"/c/{conv['id']}#m{msg['id']}", time=fmt_time(s.store, msg["created_at"]))


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


# --------------------------------------------------------------------------- projects
def project_views(s: Services, archived: bool = False) -> list[dict[str, Any]]:
    return [{"id": p["id"], "name": p["name"], "description": p["description"], "chats": p["chat_count"],
             "files": p["file_count"], "has_instructions": bool(p["instructions"]),
             "updated": fmt_time(s.store, p["updated_at"])} for p in s.projects.list(archived=archived)]


@page()
async def projects_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    archived = viewer.is_mentor and request.query_params.get("archived") == "1"
    return render(request, "projects.html", viewer, items=project_views(s, archived), archived=archived,
                  flash=request.session.pop("flash", None), error=request.session.pop("project_error", None))


@page(require="mentor")
async def project_create(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    try:
        project_id = s.projects.create(viewer.id, str(form.get("name", "")), str(form.get("description", "")),
                                       str(form.get("instructions", "")))
    except ProjectError as exc:
        request.session["project_error"] = str(exc)
        return RedirectResponse("/projects#new-project", status_code=303)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _project_for(request: Request, viewer: Viewer):
    """The project in the URL, if this viewer may see it (archived ones are for mentors only)."""
    project = services(request).projects.get(request.path_params["pid"])
    if project is None or (project["archived"] and not viewer.is_mentor):
        return None
    return project


@page()
async def project_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    project = _project_for(request, viewer)
    if project is None:
        return error_page(request, viewer, 404, "Project not found", "This project doesn't exist, or it was archived.")
    files = [{"id": f["id"], "name": f["name"], "kind": f["kind"], "size": size_label(f["size"]),
              "url": f"/projects/{project['id']}/files/{f['id']}"} for f in s.projects.files(project["id"])]
    return render(request, "project.html", viewer, project=project, files=files, max_project_files=MAX_PROJECT_FILES,
                  chats=conversation_list(s, viewer, "team", "", project_id=project["id"]),
                  flash=request.session.pop("flash", None), error=request.session.pop("project_error", None),
                  model_name=model_label(s.settings.model_default), **upload_context())


def _project_action(kind: str):
    @page(require="mentor")
    async def action(request: Request, viewer: Viewer) -> Response:
        s = services(request)
        project = s.projects.get(request.path_params["pid"])
        if project is None:
            return error_page(request, viewer, 404, "Project not found", "This project doesn't exist.")
        form = await request.form()
        try:
            if kind == "edit":
                changed = s.projects.update(project["id"], viewer.id, str(form.get("name", "")),
                                            str(form.get("description", "")), str(form.get("instructions", "")))
                request.session["flash"] = "Saved the project." if changed else "Nothing changed."
            elif kind == "files":
                items = await read_uploads(form, s.settings)
                if not items:
                    raise ProjectError("Choose a file to add.")
                s.projects.add_files(project["id"], viewer.id, items)
                request.session["flash"] = f"Added {len(items)} file{'s' if len(items) != 1 else ''}."
            elif kind == "remove":
                s.projects.remove_file(project["id"], request.path_params["fid"], viewer.id)
                request.session["flash"] = "Removed the file."
            elif kind == "archive":
                archive = not project["archived"]
                s.projects.set_archived(project["id"], viewer.id, archive)
                request.session["flash"] = ("Archived the project. Its chats keep working, and you can restore it "
                                            "from Archived projects." if archive else "Restored the project.")
                if archive:
                    return RedirectResponse("/projects", status_code=303)
        except (ProjectError, AttachmentError) as exc:
            request.session["project_error"] = str(exc)
        return RedirectResponse(f"/projects/{project['id']}", status_code=303)

    action.__name__ = f"project_{kind}"
    return action


@page()
async def project_file(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    project = _project_for(request, viewer)
    row = s.projects.file(project["id"], request.path_params["fid"]) if project is not None else None
    if row is None:
        return PlainTextResponse("Not found", status_code=404)
    try:
        path = s.projects.path(row)
    except OSError:
        return PlainTextResponse("This file is missing from the server.", status_code=404)
    media_type = row["media_type"] if row["kind"] != "text" else "text/plain; charset=utf-8"
    ascii_name = row["name"].encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    disposition = "attachment" if request.query_params.get("download") else "inline"
    headers = {"Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
               "Content-Disposition": f'{disposition}; filename="{ascii_name}"', "Cache-Control": "private, max-age=3600"}
    return FileResponse(path, media_type=media_type, headers=headers)


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
    models = [(m, model_label(m)) for m in dict.fromkeys([s.settings.model_default, s.settings.model_hard, conv["model"]])]
    return render(request, "conversation.html", viewer, conv=conv, messages=messages, own=own, title=title,
                  can_send=own, waiting=waiting, replying=replying, composer_hint=hint, needs_review=needs_review,
                  model_options=models, model_name=model_label(conv["model"]), current_id=conv["id"],
                  long_chat=long_chat, **upload_context())


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
    subteam = form.get("subteam") if form.get("subteam") in s.settings.subteams else None
    if subteam is None:
        # The new-chat page has no subteam picker; the person's default from Profile and preferences fills it in
        default = s.store.user(viewer.id)["default_subteam"]
        subteam = default if default in s.settings.subteams else None
    project_id = None
    if str(form.get("project_id") or "").isdigit():
        project = s.projects.active(int(form["project_id"]))
        if project is None:
            await form.close()
            return JSONResponse({"ok": False, "error": "That project was archived. Start the chat from New chat "
                                                      "instead.", "code": "invalid"}, status_code=400)
        project_id = project["id"]
    try:
        files = await read_uploads(form, s.settings)
        files += await connector_files(s, form, len(files))
    except AttachmentError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "code": "invalid"}, status_code=400)
    outcome = await s.approvals.submit(viewer, str(form.get("text", "")), subteam=subteam, files=files,
                                       explainer=form.get("explainer") == "1", project_id=project_id,
                                       options=tool_options(s, viewer, form))
    if not outcome.ok:
        return json_error(outcome)
    return JSONResponse({"ok": True, "url": f"/c/{outcome.conversation_id}", "message_id": outcome.message_id})


@page()
async def send_message(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    try:
        files = await read_uploads(form, s.settings)
        files += await connector_files(s, form, len(files))
    except AttachmentError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "code": "invalid"}, status_code=400)
    outcome = await s.approvals.submit(viewer, str(form.get("text", "")), conversation_id=request.path_params["cid"],
                                       files=files, explainer=form.get("explainer") == "1",
                                       options=tool_options(s, viewer, form))
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
    shares = [share_link_view(s, row) for row in s.shares.pending()]
    window = [{"id": m["id"], "conversation_id": m["conversation_id"], "student": m["author_name"],
               "title": m["conversation_title"] or "New chat", "text": short_text(m["content"], 120),
               "time": fmt_time(s.store, m["created_at"])} for m in s.store.window_messages()]
    code_waiting = [{"id": r["id"], "title": r["title"], "owner": r["owner_name"], "pending": r["pending"]}
                    for r in s.code.list() if r["status"] == "waiting"]
    return render(request, "queue.html", viewer, items=_queue_items(s), window=window, shares=shares,
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
            "subteam": m["conversation_subteam"], "project": m["project_name"],
            "html": plain_html(m["content"]), "raw": m["content"],
            "waited": waited(m["created_at"]), "files": file_views(s, m["id"]),
            "explainer": bool(m["wants_explainer"]), "tools": options_labels(load_options(m["options"])),
            "screen_note": m["screen_note"],
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
async def github_browse(request: Request, viewer: Viewer) -> Response:
    """One folder of the team's GitHub repository, for the composer's "Add from GitHub" picker."""
    s = services(request)
    if not s.github.configured:
        return JSONResponse({"ok": False, "error": "GitHub isn't set up. An admin can add the team's repository "
                                                   "in Settings."}, status_code=404)
    path = request.query_params.get("path", "")
    try:
        entries = await asyncio.to_thread(s.github.list, path)
    except ConnectorError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "repo": s.settings.connector_github_repo, "path": path.strip("/"),
                         "entries": [{"name": e.name, "path": e.path, "kind": e.kind,
                                      "size": size_label(e.size) if e.kind == "file" else "",
                                      "supported": e.supported} for e in entries]})


# --------------------------------------------------------------------------- share links
SHARE_HEADERS = {"X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
SHARE_STATES = {"pending": "Waiting for a mentor", "active": "Working", "expired": "Expired",
                "declined": "Not approved", "revoked": "Revoked"}


def share_link_view(s: Services, row) -> dict[str, Any]:
    current = share_state(row)
    return {"id": row["id"], "url": s.settings.url(f"/s/{row['token']}"), "audience": row["audience"],
            "state": current, "label": SHARE_STATES[current], "requester": row["requester_name"],
            "reviewer": row["reviewer_name"], "views": row["views"], "title": row["conversation_title"] or "New chat",
            "conversation_id": row["conversation_id"], "created": fmt_time(s.store, row["created_at"]),
            "expires": fmt_time(s.store, row["expires_at"]) if row["expires_at"] else None}


def shared_messages(s: Services, row, explainer_base: Callable[[int], str]) -> list[dict[str, Any]]:
    """The chat as a share link shows it: no names, no files, nothing that wasn't approved."""
    items = []
    for m in s.shares.public_messages(row["conversation_id"], row["upto_message_id"]):
        item = {"role": m["role"], "time": fmt_time(s.store, m["created_at"]), "sources": [], "files": 0}
        if m["role"] == "assistant":
            item["html"] = markdown_html(m["content"], explainer_base=explainer_base(m["id"]))
            try:
                item["sources"] = json.loads(m["sources"] or "[]")
            except ValueError:
                pass
        else:
            item["who"] = "Mentor" if m["author_role"] in ("mentor", "admin") else "Student"
            item["html"] = plain_html(m["content"])
            item["files"] = sum(1 for f in s.attachments.for_message(m["id"]) if f["kind"] != "frame")
        items.append(item)
    return items


def _share_conversation(s: Services, viewer: Viewer, cid: int):
    conv = s.store.conversation(cid)
    if conv is None or not s.store.can_view_conversation(viewer, conv) or not s.shares.can_manage(viewer, conv):
        return None
    return conv


@page()
async def share_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    conv = _share_conversation(s, viewer, request.path_params["cid"])
    if conv is None:
        return error_page(request, viewer, 404, "Chat not found",
                          "Only the student who started a chat, or a mentor, can share it.")
    return _render_share(request, viewer, s, conv, flash=request.session.pop("flash", None),
                         error=request.session.pop("share_error", None))


def _render_share(request: Request, viewer: Viewer, s: Services, conv, *, flash=None, error=None, audience="",
                  status_code: int = 200) -> Response:
    links = [share_link_view(s, row) for row in s.shares.for_conversation(conv["id"])]
    return render(request, "share.html", viewer, status_code=status_code, conv=conv, links=links, days=SHARE_DAYS,
                  title=conv["title"] or "New chat", flash=flash, error=error, audience=audience)


@page()
async def share_create(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    conv = _share_conversation(s, viewer, request.path_params["cid"])
    if conv is None:
        return error_page(request, viewer, 404, "Chat not found",
                          "Only the student who started a chat, or a mentor, can share it.")
    form = await request.form()
    audience = str(form.get("audience", ""))
    try:
        row = s.shares.request(viewer, conv, audience)
    except ShareError as exc:
        return _render_share(request, viewer, s, conv, error=str(exc), audience=audience[:120], status_code=400)
    if row["status"] == "active":
        request.session["flash"] = "Made a share link. Copy it below."
    else:
        request.session["flash"] = "Asked a mentor to approve the link. It starts working once they do."
        if s.slack is not None and s.settings.slack_approvals_channel:
            text = (f"{viewer.name} asked to share the chat \"{row['conversation_title'] or 'New chat'}\" outside "
                    f"the team, for {row['audience']}. Approve or decline it: {s.settings.url('/queue#shares')}")
            s.tasks.spawn(_slack_note(s, s.settings.slack_approvals_channel, text), name=f"share-{row['id']}")
    return RedirectResponse(f"/c/{conv['id']}/share", status_code=303)


async def _slack_note(s: Services, channel: str, text: str, dm: bool = False) -> None:
    try:
        if dm:
            await s.slack.dm(channel, text)
        else:
            await s.slack.post_message(channel, text)
    except Exception:
        log.exception("Couldn't send a Slack message about a share link")


def _share_action(kind: str):
    @page()
    async def action(request: Request, viewer: Viewer) -> Response:
        s = services(request)
        sid = request.path_params["sid"]
        back = "/queue#shares" if kind in ("approve", "decline") else None
        try:
            if kind == "revoke":
                row = s.shares.revoke(viewer, sid)
                request.session["flash"] = "Revoked the link. It stops working right away."
            else:
                row = s.shares.review(viewer, sid, approve=kind == "approve")
                request.session["flash"] = ("Approved the share link." if kind == "approve"
                                            else "Declined the share link.")
                if s.slack is not None and row["requester_notify_dm"] and row["requester_slack_id"]:
                    link = s.settings.url(f"/c/{row['conversation_id']}/share")
                    verb = "approved" if kind == "approve" else "didn't approve"
                    text = f"{viewer.name} {verb} your link to share \"{row['conversation_title'] or 'New chat'}\". {link}"
                    s.tasks.spawn(_slack_note(s, row["requester_slack_id"], text, dm=True), name=f"share-dm-{sid}")
        except ShareError as exc:
            request.session["share_error"] = str(exc)
            row = s.shares.get(sid)
        if back is None:
            back = f"/c/{row['conversation_id']}/share" if row is not None else "/chats"
        elif "share_error" in request.session:
            request.session["flash"] = request.session.pop("share_error")
        return RedirectResponse(back, status_code=303)

    action.__name__ = f"share_{kind}"
    return action


@page()
async def share_preview(request: Request, viewer: Viewer) -> Response:
    """What a link shows, for the mentor deciding on it (and the person who asked for it)."""
    s = services(request)
    row = s.shares.get(request.path_params["sid"])
    conv = s.store.conversation(row["conversation_id"]) if row is not None else None
    if conv is None or not (viewer.is_mentor or row["requested_by"] == viewer.id):
        return error_page(request, viewer, 404, "Not found", "This share link doesn't exist.")
    cid = conv["id"]
    messages = shared_messages(s, row, lambda mid: f"/c/{cid}/x/{mid}")
    return render(request, "shared.html", viewer, share=share_link_view(s, row), messages=messages,
                  title=conv["title"] or "New chat", preview=True)


async def shared_page(request: Request) -> Response:
    """A share link, for someone outside the team. No sign-in."""
    s = services(request)
    token = request.path_params["token"]
    row = s.shares.open(token)
    if row is None:
        return render(request, "error.html", None, status_code=404, title="Link not working",
                      detail="This share link was revoked, has expired, or never existed. Ask the person who sent "
                             "it for a new one.")
    messages = shared_messages(s, row, lambda mid: f"/s/{token}/x/{mid}")
    response = render(request, "shared.html", None, share=share_link_view(s, row), messages=messages,
                      title=row["conversation_title"] or "Shared chat", preview=False)
    response.headers.update(SHARE_HEADERS)
    return response


async def shared_explainer(request: Request) -> Response:
    s = services(request)
    row = s.shares.check(request.path_params["token"])
    mid, index = request.path_params["mid"], request.path_params["n"]
    msg = s.store.message(mid) if row is not None else None
    if (msg is None or msg["conversation_id"] != row["conversation_id"] or msg["id"] > row["upto_message_id"]
            or msg["role"] != "assistant" or msg["status"] != COMPLETE):
        return PlainTextResponse("Not found", status_code=404)
    pages = explainers(msg["content"])
    if index >= len(pages):
        return PlainTextResponse("Not found", status_code=404)
    return HTMLResponse(pages[index], headers={"Content-Security-Policy": EXPLAINER_CSP, **SHARE_HEADERS})


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
        elif row["target_type"] == "project":
            link = f"/projects/{row['target_id']}"
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
        stop = asyncio.Event()
        loops: list[asyncio.Task] = []
        if background:
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
        Route("/projects", projects_page),
        Route("/projects/new", project_create, methods=["POST"]),
        Route("/projects/{pid:int}", project_page),
        Route("/projects/{pid:int}/edit", _project_action("edit"), methods=["POST"]),
        Route("/projects/{pid:int}/files", _project_action("files"), methods=["POST"]),
        Route("/projects/{pid:int}/files/{fid:int}", project_file),
        Route("/projects/{pid:int}/files/{fid:int}/remove", _project_action("remove"), methods=["POST"]),
        Route("/projects/{pid:int}/archive", _project_action("archive"), methods=["POST"]),
        Route("/artifacts", artifacts_page),
        Route("/artifacts/{mid:int}/{n:int}", artifact_page),
        *[Route(f"/{key}", coming_soon(key)) for key in COMING_SOON],
        Route("/c/{cid:int}", conversation_page),
        Route("/c/{cid:int}/messages", send_message, methods=["POST"]),
        Route("/c/{cid:int}/m/{mid:int}", message_partial),
        Route("/c/{cid:int}/events", conversation_events),
        Route("/c/{cid:int}/model", set_model, methods=["POST"]),
        Route("/c/{cid:int}/x/{mid:int}/{n:int}", explainer_page),
        Route("/connectors/github", github_browse),
        Route("/c/{cid:int}/share", share_page),
        Route("/c/{cid:int}/share", share_create, methods=["POST"]),
        Route("/shares/{sid:int}/preview", share_preview),
        Route("/shares/{sid:int}/approve", _share_action("approve"), methods=["POST"]),
        Route("/shares/{sid:int}/decline", _share_action("decline"), methods=["POST"]),
        Route("/shares/{sid:int}/revoke", _share_action("revoke"), methods=["POST"]),
        Route("/s/{token:str}", shared_page),
        Route("/s/{token:str}/x/{mid:int}/{n:int}", shared_explainer),
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
        # The files, plus the video stills the browser adds (each a small JPEG)
        Middleware(LimitBody, max_bytes=MAX_MESSAGE_BYTES + 10 * 1024 * 1024),
        Middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="subzero_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=settings.https),
    ]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.services = s
    app.state.request_restart = None  # set by __main__: stops the app so the Windows service starts it again
    return app
