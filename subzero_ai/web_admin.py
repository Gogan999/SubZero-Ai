"""Admin pages: the Settings page (health, backups, settings, and the system prompt layers), the first-run
setup page, and the recovery sign-in. Setup and recovery answer only on the mini PC itself."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from . import __version__, archive, model_check, saved_settings, server_settings, setup_flow
from .ai_backend import resolve_backend
from .bridge_login import LoginError, login_env, looks_like_code
from .backup import list_backups, size_text
from .config import MODEL_ID_SETTINGS, PROVIDERS, model_provider, normalize_model_id, parse_env_file, update_env_file
from .connectors import BRANCH_RE, REPO_RE, ConnectorError
from .db import utcnow
from .maintenance import update_available
from .model_choices import MODEL_PURPOSE, group_options, selectable_models
from .pi_client import (
    BRIDGE_PROVIDER,
    KEY_SETTINGS,
    bridge_credentials_path,
    bridge_logged_in,
    claude_cli_path,
    provider_keys,
    settings_bridge_env,
)
from .roles import RECOVERY_USER, is_excluded_account
from .saved_settings import SLACK_ID_RE
from .store import Viewer
from .web_common import (
    Services,
    _finish_login,
    _load_model_names,
    check_csrf,
    error_page,
    fmt_time,
    is_local_request,
    page,
    render,
    services,
    waited,
)

log = logging.getLogger("subzero.admin")

CHANNEL_RE = server_settings.CHANNEL_RE


def flash(request: Request, text: str) -> None:
    request.session["flash"] = text


# The tab each config.env box is on
ENV_TABS = {"engine": "ai", "site": "site", "slack": "slack"}


BRIDGE_WHERE = "Claude subscription box, AI and keys tab"


def _ago(store, iso: str | None) -> str:
    return fmt_time(store, iso) if iso else ""


# --------------------------------------------------------------------------- settings page
def health_rows(s: Services) -> list[dict[str, Any]]:
    """The health list at the top of the Settings page: one row per thing that can break.
    Rows with alert set also put a "needs attention" mark on the Settings link."""
    rows = []
    snap = s.health.snapshot()
    for key, label in (("slack", "Slack"), ("claude", "Claude API")):
        item = snap[key]
        if item["state"] == "ok":
            state, kind, detail = "Working", "ok", ""
        elif item["state"] == "failing":
            since = datetime.fromtimestamp(item["since"], timezone.utc).isoformat()
            state, kind = "Not working", "bad"
            detail = f"Since {fmt_time(s.store, since)}. {item['detail']}".strip()
        elif item["state"] == "off":
            state, kind, detail = "Not set up", "bad", item["detail"]
        else:
            state, kind = "Not checked yet", "unknown"
            detail = ("Checked after the next reply." if key == "claude"
                      else "Checked within a few minutes of starting.")
        rows.append({"label": label, "state": state, "kind": kind, "detail": detail,
                     "alert": item["state"] == "failing"})

    gateway = code_gateway_row(s)
    if gateway is not None:
        rows.append(gateway)

    last = s.db.get_kv("last_backup_at")
    if s.db.get_kv("last_backup_ok") == "0":
        error = (s.db.get_kv("last_backup_error") or "").split(" ", 1)[-1]
        rows.append({"label": "Last backup", "state": "Failed", "kind": "bad",
                     "detail": f"{_ago(s.store, last)}. {error}", "alert": True})
    elif last:
        rows.append({"label": "Last backup", "state": "Done", "kind": "ok",
                     "detail": f"{_ago(s.store, last)}, {s.db.get_kv('last_backup_file') or ''}"})
    else:
        rows.append({"label": "Last backup", "state": "None yet", "kind": "unknown",
                     "detail": f"The first one runs within a minute of starting, then nightly at "
                               f"{s.settings.backup_hour}:00."})

    newer = update_available(s.db)
    if newer:
        rows.append({"label": "Version", "state": "Update available", "kind": "unknown",
                     "detail": f"You have {__version__}. Version {newer['version']} is out. "
                               "On the server PC, open the SubZero AI folder and run update.cmd.",
                     "link": newer.get("url")})
    else:
        rows.append({"label": "Version", "state": __version__, "kind": "plain", "detail": "Up to date as of the "
                     "last check." if s.db.get_kv("latest_release") else ""})
    return rows


def code_gateway_row(s: Services) -> dict[str, Any] | None:
    """The Code gateway's health: whether it is running, and whether the chosen Code sign-in has anything to sign in with.
    Only shown when the new Code engine uses Hyper-V sandboxes (the only case that needs the gateway)."""
    st = s.settings
    if st.code_sandbox != "hyperv" or s.code.engine_now() != "agent" or resolve_backend(st) == "fake":
        return None
    gateway = s.gateway
    if gateway is None or not gateway.running:
        why = (gateway.error if gateway is not None and gateway.error else "It hasn't started yet.")
        return {"label": "Code gateway", "state": "Not running", "kind": "bad", "alert": True,
                "detail": f"Claude in the sandboxes can't reach the AI service. {why} Restart SubZero AI after "
                          "fixing the sandbox settings."}
    problem = s.code.agent.credential_problem()
    if problem:
        return {"label": "Code gateway", "state": "Not signed in", "kind": "bad", "alert": True, "detail": problem}
    how = "the team subscription" if st.code_auth == "subscription" else "the API key"
    return {"label": "Code gateway", "state": "Working", "kind": "ok", "alert": False,
            "detail": f"Claude in the sandboxes reaches the AI service through {gateway.host}:{gateway.port}, signed in "
                      f"with {how}."}


def _names_for(s: Services, slack_ids) -> str:
    """Who a list of Slack member IDs is, for the hint under the field."""
    names = []
    for slack_id in sorted(slack_ids):
        user = s.store.user_by_slack_id(slack_id)
        names.append(user["name"] if user else f"{slack_id} (hasn't signed in yet)")
    return ", ".join(names)


KEY_HELP = {
    "anthropic": "Create the key inside the SubZero AI workspace at platform.claude.com (Settings, API keys). "
                 "It starts with sk-ant-.",
    "openai": "Make a key at platform.openai.com, under API keys.",
    "google": "Make a key at aistudio.google.com, with Get API key.",
    "openrouter": "Make a key at openrouter.ai, under Keys.",
}
# The team subscription token for Code sessions (from `claude setup-token`). It is saved with the API keys below.
CODE_KEY, CODE_KEY_ENV = "code_subscription_token", "CODE_SUBSCRIPTION_TOKEN"
# Shown on the Code tab whenever the page is drawn, whichever sign-in is chosen. It isn't a setting and can't be hidden.
CODE_SUBSCRIPTION_WARNING = ("Team subscription is for testing only. Anthropic's terms don't allow sharing a "
                             "subscription through an app, and they can limit the account without notice. Use an API "
                             "key for regular use.")
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY",
           "openrouter": "OPENROUTER_API_KEY"}


# --------------------------------------------------------------------------- tabs and boxes
TAB_NAMES = {"overview": "Overview", "code": "Code", "ai": "AI and keys", "review": "Slack chat review",
             "people": "People", "slack": "Slack", "site": "Site and server", "history": "History"}


@dataclass(frozen=True)
class Panel:
    """One box of database settings on the Settings page: a form that saves some of a section's fields."""

    id: str
    tab: str
    section: str  # the saved_settings section these fields belong to (the form posts to /settings/<section>)
    title: str
    fields: tuple[str, ...]
    help: str = ""


PANELS: tuple[Panel, ...] = (
    Panel("code-engine", "code", "code", "Code engine", ("code_engine",),
          "Which engine runs Code sessions. The new engine lets Claude use a full shell inside the session's own "
          "sandbox. The classic engine is the older one, kept as the fallback: a mentor approves each change and "
          "command, and it can run without a sandbox. If the new engine ever misbehaves, switch back here. Sessions "
          "already started keep the engine they began with."),
    Panel("code-auth", "code", "code", "Code sign-in", ("code_auth",),
          "How Code signs in to Anthropic: with the team's API key (AI and keys tab), or with a Claude subscription "
          "token, for testing only."),
    Panel("code-students", "code", "code", "Students and the sandbox",
          ("code_student_access", "code_sandbox", "sandbox_base_image"),
          "Students need the Hyper-V sandbox. Every student request still waits for a mentor to approve it."),
    Panel("code-plan", "code", "code", "How sessions work",
          ("code_plan_first_students", "code_plan_first_mentors", "code_web_tools_students",
           "code_web_tools_mentors", "code_sub_agents"),
          "The starting choices for new sessions, who may search the web, and helpers."),
    Panel("code-limits", "code", "code", "Cost and capacity",
          ("code_cap_student_usd", "code_cap_mentor_usd", "code_max_running", "code_agent_max_turns",
           "code_keep_days"),
          "Spending caps, how many sessions work at once, and how long finished sessions keep their files."),
    Panel("code-student-limits", "code", "code", "Student limits",
          ("code_student_max_open", "code_student_daily_sessions", "code_student_max_turns",
           "code_student_command_timeout")),
    Panel("code-classic", "code", "code", "Classic engine only",
          ("code_student_free_rein", "code_mentor_free_rein", "code_sandbox_commands", "code_commands",
           "code_command_timeout"),
          "These settings only matter when the Code engine above is set to Classic engine."),
    Panel("code-advanced", "code", "code", "Advanced sandbox settings",
          ("sandbox_switch", "sandbox_host_address", "sandbox_proxy_port", "sandbox_gateway_port",
           "sandbox_allowed_hosts", "sandbox_allowed_ports", "sandbox_proxy_mb_limit", "sandbox_proxy_max_per_sandbox",
           "sandbox_guest_sudo", "sandbox_memory_mb", "sandbox_cpus", "sandbox_disk_gb", "sandbox_disk_cap_gb",
           "sandbox_min_free_gb", "sandbox_ttl_hours", "sandbox_max_sessions"),
          "The sandbox network, the proxy and gateway ports, and each sandbox's memory, processors and disk. The "
          "standard values work for most teams."),
    Panel("ai-providers", "ai", "claude", "AI providers", ("ai_providers",)),
    Panel("claude", "ai", "claude", "Models and effort",
          ("model_default", "model_hard", "model_titles", "model_check_judge", "effort_default", "max_tokens_chat")),
    Panel("review-limits", "review", "limits", "Limits and review",
          ("limit_pending_per_student", "limit_approved_per_day", "reminder_minutes", "mentors_need_approval")),
    Panel("review-window", "review", "window", "Approval window", ("approval_window_minutes",)),
    Panel("review-screener", "review", "window", "Screener", ("screener_enabled", "model_screener")),
    Panel("review-always", "review", "window", "Students who always wait", ("always_review_slack_ids",)),
    Panel("people", "people", "people", "Admins", ("admin_slack_ids",)),
    Panel("slack-dms", "slack", "limits", "Messages to the bot in Slack", ("slack_dms_enabled",)),
    Panel("site-data", "site", "data", "Data and backups", ("retention_days", "backup_keep_days")),
)
PANEL_BY_ID = {p.id: p for p in PANELS}

# Boxes and tabs that used to have these addresses (#name) and where they are now, so an old link or bookmark lands
# on the right tab. Any old address not listed here still exists on the page under the same name.
LEGACY_ANCHORS = {
    "code": "code-engine", "limits": "review-limits", "window": "review-window",
    "data": "site-data",
}


def anchors_context() -> dict[str, str]:
    return dict(LEGACY_ANCHORS)


def _field_item(s: Services, f: saved_settings.Field, errors: dict[str, str] | None,
                submitted: dict[str, str] | None) -> dict[str, Any]:
    value = (submitted or {}).get(f.name)
    if value is None:
        value = saved_settings.current(s.settings, f.name)
    item: dict[str, Any] = {"field": f, "value": value, "error": (errors or {}).get(f.name)}
    if f.kind == "model":
        # A dropdown of the models that can be picked here, plus Other for a model id not in the list
        options = selectable_models(s, purpose=MODEL_PURPOSE[f.name], include_unchecked=True)
        listed = {o["id"] for o in options}
        chosen = normalize_model_id(value)
        item.update(groups=group_options(options), other=saved_settings.OTHER_MODEL,
                    pick=chosen if chosen in listed else saved_settings.OTHER_MODEL,
                    typed="" if chosen in listed else value)
    elif f.kind == "ids":
        item["people"] = _names_for(s, getattr(s.settings, f.name))
    elif f.kind == "providers":
        on = set(value.split())
        item["choices"] = [{"id": p, "label": saved_settings.PROVIDER_LABELS[p], "on": p in on,
                            "help": saved_settings.PROVIDER_HELP.get(p, "")} for p in PROVIDERS]
    return item


def panels_context(s: Services, errors: dict[str, str] | None = None,
                   submitted: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Every database settings box, by id, with its fields ready to show."""
    out = {}
    for panel in PANELS:
        fields = [saved_settings.FIELDS[name] for name in panel.fields]
        out[panel.id] = {"id": panel.id, "tab": panel.tab, "section": panel.section, "title": panel.title,
                         "help": panel.help, "action": f"/settings/{panel.section}",
                         "fields": [_field_item(s, f, errors, submitted) for f in fields]}
    return out


def waiting_context(s: Services) -> dict[str, Any]:
    """Messages that are waiting for a mentor right now, for the Overview."""
    pending = s.store.pending_messages()
    oldest = min((m["created_at"] for m in pending), default="")
    return {"count": len(pending), "oldest": waited(oldest) if oldest else "",
            "can_post": s.slack is not None and bool(s.settings.slack_approvals_channel)}


async def directory_ids(s: Services) -> set[str] | None:
    """The mentors in the review channel (None when Slack can't be asked). Never blocks the page for long."""
    if s.slack is None or not s.settings.slack_approvals_channel:
        return None
    try:
        return set(await asyncio.wait_for(s.directory.mentor_ids(), timeout=3))
    except Exception:
        return None


def people_context(s: Services, mentor_ids: set[str] | None) -> dict[str, Any]:
    """Who is a mentor or an admin right now, by name, for the read-only list on the People tab."""
    admins = set(s.settings.admin_slack_ids)
    rows = []
    for slack_id in sorted(admins | (mentor_ids or set())):
        user = s.store.user_by_slack_id(slack_id)
        rows.append({"name": user["name"] if user else f"{slack_id} (hasn't signed in yet)",
                     "role": "Admin" if slack_id in admins else "Mentor"})
    rows.sort(key=lambda r: (r["role"] != "Admin", r["name"].lower()))
    fetched = getattr(s.directory, "_fetched_at", None)  # when the directory last heard from Slack (monotonic)
    as_of = ""
    if mentor_ids is not None and fetched is not None:
        taken = datetime.now(timezone.utc) - timedelta(seconds=max(time.monotonic() - fetched, 0))
        as_of = fmt_time(s.store, taken.isoformat(timespec="seconds"))
    return {"rows": rows, "as_of": as_of, "known": bool(as_of) or not s.settings.slack_approvals_channel}


def directory_warning(s: Services) -> str:
    """The mentor list's warning, if the directory has one (the directory may not have the check yet)."""
    check = getattr(s.directory, "stale_warning", None)
    if not callable(check):
        return ""
    try:
        return str(check() or "")
    except Exception:
        log.exception("The mentor list check failed")
        return ""


def _date(s: Services, iso: str) -> str:
    local = s.store.local_time(iso)
    return f"{local:%b} {local.day}, {local:%Y}" if local else ""


def archive_context(s: Services) -> dict[str, Any]:
    """The Old website chats box in History: how many there are, the running job, and the archives made."""
    summary = archive.summarize(s.db)
    job = archive.job_state(s.db)
    if job:
        job["running"] = job.get("state") == "running"
        job["percent"] = min(100, int(100 * job.get("done", 0) / max(job.get("total", 1), 1)))
        job["word"] = "Archive" if job.get("kind") == "archive" else "Delete"
        job["when"] = fmt_time(s.store, job.get("finished_at") or job.get("started_at"))
    return {
        "chats": summary.chats, "messages": summary.messages, "files": summary.files,
        "size": size_text(summary.file_bytes), "videos": summary.videos, "video_size": size_text(summary.video_bytes),
        "projects": summary.projects, "project_files": summary.project_files, "shares": summary.shares,
        "oldest": _date(s, summary.oldest), "newest": _date(s, summary.newest), "job": job,
        "confirm_word": archive.CONFIRM_WORD,
        "archives": [{"name": a.name, "time": f"{a.created:%b} {a.created.day}, {a.created:%Y}, "
                                              f"{a.created:%I:%M %p}".replace(" 0", " "), "size": size_text(a.size)}
                     for a in archive.list_archives(s.settings)]}


def settings_context(s: Services, errors: dict[str, str] | None = None,
                     submitted: dict[str, str] | None = None, env_section: str = "",
                     mentor_ids: set[str] | None = None) -> dict[str, Any]:
    """Everything the Settings page shows. `env_section` names a config.env section whose save failed, so
    `errors` and `submitted` belong to it instead of the database boxes."""
    db_errors, db_submitted = (None, None) if env_section else (errors, submitted)
    panels = panels_context(s, db_errors, db_submitted)
    prompts = []
    for name, label, help_text in saved_settings.PROMPT_LAYERS:
        text, custom = saved_settings.prompt_layer(s.settings, name)
        prompts.append({"name": name, "slug": name.removesuffix(".md"), "label": label, "help": help_text,
                        "text": text, "custom": custom})
    history = []
    for row in s.db.all(
            "SELECT a.created_at, a.action, a.details, u.name AS actor_name FROM audit_log a "
            "LEFT JOIN users u ON u.id = a.actor_id WHERE a.action IN ('setting_changed', 'prompt_changed') "
            "ORDER BY a.id DESC LIMIT 20"):
        details = json.loads(row["details"] or "{}")
        what = details.get("setting") or f"{details.get('layer', 'System prompt')} prompt layer"
        change = ""
        if row["action"] == "setting_changed":
            change = f"{details.get('from', '')} → {details.get('to', '')}".replace("\n", ", ")
        history.append({"time": fmt_time(s.store, row["created_at"]), "who": row["actor_name"] or "Setup",
                        "what": what, "change": change})
    backups = [{"name": b.name, "time": f"{b.created:%b} {b.created.day}, {b.created:%I:%M %p}".replace(" 0", " "),
                "size": size_text(b.size)} for b in list_backups(s.settings)[:8]]
    connectors = {"repo": s.settings.connector_github_repo, "branch": s.settings.connector_github_branch,
                  "token_set": bool(s.settings.connector_github_token)}
    ai_keys = []
    for p in PROVIDERS:
        saved = getattr(s.settings, KEY_SETTINGS[p], "") if p in KEY_SETTINGS else ""
        if p in KEY_SETTINGS and (p in s.settings.ai_providers or saved):  # a turned-off provider's key can go
            ai_keys.append({"id": p, "label": saved_settings.PROVIDER_LABELS[p], "field": KEY_SETTINGS[p],
                            "set": bool(saved), "hint": server_settings.secret_hint(saved), "help": KEY_HELP[p],
                            "on": p in s.settings.ai_providers, "locked": KEY_ENV[p] in os.environ})
    code_key = {"field": CODE_KEY, "set": bool(s.settings.code_subscription_token),
                "hint": server_settings.secret_hint(s.settings.code_subscription_token),
                "locked": CODE_KEY_ENV in os.environ}
    return {"health": health_rows(s), "panels": panels, "ai_keys": ai_keys, "code_key": code_key,
            "tabs": TAB_NAMES, "anchors": anchors_context(), "code_warning": CODE_SUBSCRIPTION_WARNING,
            "subscription_on": s.settings.code_auth == "subscription",
            "waiting": waiting_context(s), "people": people_context(s, mentor_ids),
            "directory_warning": directory_warning(s), "archive": archive_context(s),
            "safety": safety_context(s),
            "prompts": prompts, "history": history,
            "backups": backups, "backup_folder": str(s.settings.backups_path), "connectors": connectors,
            "env_sections": env_sections_context(s, env_section, errors, submitted),
            "accounts": accounts_context(s), "checks": checks_context(s), "bridge": bridge_context(s),
            "file_only": server_settings.FILE_ONLY,
            "pending": [server_settings.ENV_FIELDS[n].label for n in s.pending_restart],
            "config_path": str(s.settings.config_path)}


async def full_context(s: Services, errors: dict[str, str] | None = None, submitted: dict[str, str] | None = None,
                       env_section: str = "") -> dict[str, Any]:
    """settings_context plus the answer from Slack about who the mentors are."""
    return settings_context(s, errors, submitted, env_section, mentor_ids=await directory_ids(s))


def env_sections_context(s: Services, failed_section: str = "", errors: dict[str, str] | None = None,
                         submitted: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The config.env sections of the Settings page. Secrets come back only as Saved and their last four
    characters. After a failed save, that section shows what was typed (never a typed secret)."""
    out = []
    for key, (title, intro, fields) in server_settings.ENV_SECTIONS.items():
        mine = key == failed_section
        items = []
        for f in fields:
            item: dict[str, Any] = {"field": f, "locked": server_settings.env_locked(f),
                                    "pending": f.name in s.pending_restart,
                                    "error": (errors or {}).get(f.name) if mine else None}
            if f.kind == "secret":
                saved = getattr(s.settings, f.name)
                if f.name in s.pending_restart:  # the new one is in config.env, not in use yet
                    saved = parse_env_file(s.settings.config_path).get(f.env, saved)
                item.update(value="", set=bool(saved), hint=server_settings.secret_hint(saved))
            else:
                value = (submitted or {}).get(f.name) if mine else None
                item["value"] = value if value is not None else server_settings.shown_value(
                    s.settings, s.pending_restart, f.name)
            items.append(item)
        out.append({"key": key, "title": title, "name": server_settings.SECTION_NAMES[key], "intro": intro,
                    "fields": items})
    return out


def accounts_context(s: Services) -> list[dict[str, Any]]:
    """One row per outside account the app uses, with whether it's connected and where to change it."""
    st = s.settings
    rows = []
    if not st.slack_bot_token:
        slack = ("Not connected", "bad", "Add the bot and app-level tokens under Slack connection.")
    else:
        socket = s.health.socket_client
        live = bool(socket and socket.connected.is_set())
        where = f"Workspace: {s.slack_workspace}. " if s.slack_workspace else ""
        slack = ("Connected" if live or socket is None else "Connecting", "ok" if live or socket is None else "unknown",
                 where + ("Sign in with Slack is set up." if st.slack_login_enabled
                          else "Sign in with Slack needs the client ID and secret."))
    rows.append({"label": "Slack", "state": slack[0], "kind": slack[1], "detail": slack[2], "link": "#env-slack"})
    for p in PROVIDERS:
        on = p in st.ai_providers
        if p == BRIDGE_PROVIDER:
            signed_in = bridge_logged_in(st.data_dir)
            if signed_in:
                state, kind = ("Signed in", "ok") if on else ("Signed in, turned off", "plain")
            else:
                state, kind = ("Not signed in", "bad") if on else ("Off", "plain")
            rows.append({"label": saved_settings.PROVIDER_LABELS[p], "state": state, "kind": kind,
                         "link": "#bridge",
                         "detail": "Use Sign in under Claude subscription." if not signed_in else
                                   "Replies use the subscription's limits."})
            continue
        key = getattr(st, KEY_SETTINGS[p], "")
        if on:
            state, kind = ("Key saved", "ok") if key else ("No key", "bad")
        else:
            state, kind = ("Off", "plain")
        rows.append({"label": saved_settings.PROVIDER_LABELS[p], "state": state, "kind": kind, "link": "#ai-keys",
                     "detail": (server_settings.secret_hint(key).capitalize() + ".") if key else ""})
    repo = st.connector_github_repo
    rows.append({"label": "GitHub", "state": "Connected" if repo else "Off", "kind": "ok" if repo else "plain",
                 "link": "#connectors", "detail": (f"{repo}, " + ("with a saved token." if st.connector_github_token
                                                                 else "public, no token.")) if repo else ""})
    rows.append({"label": "Cloudflare Tunnel", "state": "Address set" if st.https else "Not set",
                 "kind": "ok" if st.https else "bad", "link": "#env-site",
                 "detail": f"{st.public_url}. The tunnel itself is managed in Cloudflare's Zero Trust dashboard."})
    return rows


def bridge_context(s: Services) -> dict[str, Any]:
    """The Claude subscription box: signed in or not, and a sign-in waiting for its code."""
    st = s.settings
    waiting = s.bridge_login.waiting()
    return {"signed_in": bridge_logged_in(st.data_dir), "on": BRIDGE_PROVIDER in st.ai_providers,
            "needs_pi": resolve_backend(st) != "pi", "found": claude_cli_path(st.pi_gateway_script or None) is not None,
            "waiting": waiting, "url": s.bridge_login.url if waiting else "", "by": s.bridge_login.by if waiting else ""}


def checks_context(s: Services) -> dict[str, Any]:
    raw = s.db.get_kv("settings_checks")
    last = json.loads(raw) if raw else None
    if last:
        last["time"] = fmt_time(s.store, last["at"])
        last["fails"] = sum(r["status"] == "FAIL" for r in last["results"])
    return {"last": last, "running": bool(s.checks_running)}


def safety_context(s: Services) -> dict[str, Any]:
    """The Model safety check box: the latest result per model and the checks running now."""
    rows = []
    for r in model_check.latest_results(s.db):
        rows.append({**r, "time": fmt_time(s.store, r["checked_at"]), "running": r["model"] in s.model_checks_running,
                     "failed": [{"name": model_check.CASE_LABELS.get(c["case"], c["case"]), "detail": c["detail"]}
                                for c in r["details"] if not c["passed"]]})
    shown = {r["model"] for r in rows}
    running = [{"model": m, "since": fmt_time(s.store, at)} for m, at in s.model_checks_running.items()]
    return {"rows": rows, "running": running, "running_only": [r for r in running if r["model"] not in shown],
            "default": s.settings.model_default}


async def learn_workspace(s: Services) -> None:
    """Ask Slack for the workspace's name once, for the Connected accounts list. Never blocks the page long."""
    if s.slack_workspace or s.slack is None or not s.settings.slack_bot_token:
        return
    if time.monotonic() - s.slack_workspace_asked < 600:
        return  # Slack didn't answer recently: don't slow every page load while it's down
    s.slack_workspace_asked = time.monotonic()
    try:
        auth = await asyncio.wait_for(s.slack.auth_test(), timeout=3)
        s.slack_workspace = auth.get("team") or auth.get("team_id") or ""
    except Exception:
        pass  # the health list already says when Slack isn't working


@page(require="admin")
async def settings_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    await learn_workspace(s)
    return render(request, "settings.html", viewer, flash=request.session.pop("flash", None),
                  **await full_context(s))


@page(require="admin")
async def save_section(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    section = request.path_params["section"]
    if section not in saved_settings.SECTIONS:
        return error_page(request, viewer, 404, "Not found", "That settings section doesn't exist.")
    form = saved_settings.model_choices_form(await request.form(), section)
    # A box on the page saves only its own fields. A form without a box name saves the whole section.
    panel = PANEL_BY_ID.get(str(form.get("_panel", "") or ""))
    if panel is not None and panel.section != section:
        panel = None
    only = set(panel.fields) if panel is not None else None
    values, errors = saved_settings.validate(section, form, actor_slack_id=viewer.slack_user_id,
                                             settings=s.settings, db=s.db, only=only)
    if errors:
        submitted = {f.name: saved_settings.form_text(form, f.name) for f in saved_settings.SECTIONS[section][1]
                     if only is None or f.name in only}
        return render(request, "settings.html", viewer, status_code=400,
                      flash="Some settings need fixing. They're marked below.",
                      **await full_context(s, errors, submitted))
    changed = saved_settings.save(s.db, s.settings, values, viewer.id)
    if "ai_providers" in changed:
        use_new_bridge(s)
        refresh_model_names(s)
    if panel is not None:
        where = f"on the {TAB_NAMES[panel.tab]} tab"
        flash(request, f"Saved {panel.title} {where}." if changed else f"Nothing changed in {panel.title} {where}.")
        return RedirectResponse(f"/settings#{panel.id}", status_code=303)
    title = saved_settings.SECTIONS[section][0]
    title = "Claude settings" if section == "claude" else title.lower()
    flash(request, f"Saved {title}." if changed else f"Nothing changed in {title}.")
    return RedirectResponse(f"/settings#{section}", status_code=303)


_background: set = set()


def refresh_model_names(s: Services) -> None:
    """Re-read the gateway's model names in the background (providers or keys changed). Never blocks or fails."""
    if getattr(s.claude, "list_models", None) is None:
        return
    task = asyncio.get_running_loop().create_task(_load_model_names(s))
    _background.add(task)
    task.add_done_callback(_background.discard)


def use_new_keys(s: Services) -> None:
    """Hand the saved API keys to the running AI client, so they work without a restart."""
    set_keys = getattr(s.claude, "set_keys", None)
    if set_keys is not None:
        set_keys(provider_keys(s.settings))


def use_new_bridge(s: Services) -> None:
    """Turn the Claude subscription models on or off in the running AI client, without a restart."""
    set_bridge = getattr(s.claude, "set_bridge", None)
    if set_bridge is not None:
        set_bridge(settings_bridge_env(s.settings))


def key_problem(provider: str, key: str) -> str:
    """Why a typed API key can't be right, or an empty string. Only checks the shape, never the key itself."""
    name = saved_settings.PROVIDER_LABELS[provider]
    if re.search(r"\s", key):
        return f"The {name} key has a space or line break in it. Paste it again on one line."
    if not 16 <= len(key) <= 512:
        return f"That doesn't look like a {name} key. Copy the whole key again."
    if provider == "anthropic" and not key.startswith("sk-ant-"):
        return "The Anthropic key starts with sk-ant-. Create it inside the SubZero AI workspace."
    return ""


@page(require="admin")
async def save_ai_keys(request: Request, viewer: Viewer) -> Response:
    """Save API keys for the providers that are turned on. They go in config.env and are never shown again."""
    s = services(request)
    form = await request.form()
    # The token box on the Code tab posts here too, and comes back to itself
    on_code = form.get("back") == "code"
    anchor, where = (("code-auth", "Code sign-in box, Code tab") if on_code
                     else ("ai-keys", "API keys box, AI and keys tab"))
    typed = {p: str(form.get(name, "") or "").strip() for p, name in KEY_SETTINGS.items()}
    typed = {p: key for p, key in typed.items() if key}
    removed = [p for p in KEY_SETTINGS if form.get(f"remove_{p}") == "1" and getattr(s.settings, KEY_SETTINGS[p])
               and p not in typed]
    code_typed = str(form.get(CODE_KEY, "") or "").strip() if CODE_KEY_ENV not in os.environ else ""
    code_removed = (form.get(f"remove_{CODE_KEY}") == "1" and bool(s.settings.code_subscription_token)
                    and not code_typed and CODE_KEY_ENV not in os.environ)
    problem = ""
    if code_typed and (re.search(r"\s", code_typed) or not 16 <= len(code_typed) <= 512):
        problem = ("The subscription token has a space in it or doesn't look complete. Copy the whole token again "
                   "(it comes from running claude setup-token).")
    for provider in removed:
        using = [saved_settings.FIELDS[n].label.lower() for n in MODEL_ID_SETTINGS
                 if provider in s.settings.ai_providers and model_provider(getattr(s.settings, n)) == provider]
        if using:
            problem = (f"The {saved_settings.PROVIDER_LABELS[provider]} key is still needed by the {using[0]}. "
                       "Change that model first.")
            break
    for provider, key in typed.items():
        if problem:
            break
        if provider not in s.settings.ai_providers:
            problem = (f"{saved_settings.PROVIDER_LABELS[provider]} isn't turned on. "
                       "Turn it on under AI providers (AI and keys tab) first.")
        else:
            problem = key_problem(provider, key)
        if problem:
            break
    if not typed and not removed and not code_typed and not code_removed and not problem:
        problem = "Paste a key into at least one box."
    if problem:
        flash(request, f"Keys weren't saved. {problem} ({where}.)")
        return RedirectResponse(f"/settings#{anchor}", status_code=303)
    updates = {**{KEY_ENV[p]: key for p, key in typed.items()}, **{KEY_ENV[p]: "" for p in removed}}
    if code_typed or code_removed:
        updates[CODE_KEY_ENV] = code_typed  # empty when removed
    _save_config(s, updates)
    if code_typed or code_removed:
        s.settings.code_subscription_token = code_typed
        s.db.audit("code_token_saved" if code_typed else "code_token_removed", actor_id=viewer.id, details={})
    for provider, key in typed.items():
        setattr(s.settings, KEY_SETTINGS[provider], key)
    for provider in removed:
        setattr(s.settings, KEY_SETTINGS[provider], "")
    use_new_keys(s)
    refresh_model_names(s)
    parts = []
    if typed:
        s.db.audit("ai_key_saved", actor_id=viewer.id, details={"providers": list(typed)})
        parts.append("Saved the key for " + ", ".join(saved_settings.PROVIDER_LABELS[p] for p in typed)
                     + ". It works from the next message.")
    if removed:
        s.db.audit("ai_key_removed", actor_id=viewer.id, details={"providers": removed})
        parts.append("Removed the key for " + ", ".join(saved_settings.PROVIDER_LABELS[p] for p in removed) + ".")
    if code_typed:
        parts.append("Saved the team subscription token for Code. It works from the next request.")
    if code_removed:
        parts.append("Removed the team subscription token for Code.")
    flash(request, " ".join(parts) + f" ({where}.)")
    return RedirectResponse(f"/settings#{anchor}", status_code=303)


GITHUB_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([^/\s]+/[^/#?\s]+)")


def _connector_form(s: Services, form: dict[str, str]) -> tuple[str, str, str, str]:
    """Read the repository box: (repository, branch, token, problem). Checks the shape only; GitHub isn't asked."""
    repo, branch = form.get("connector_github_repo", ""), form.get("connector_github_branch", "")
    match = GITHUB_URL_RE.match(repo)
    if match:
        repo = match.group(1).removesuffix(".git")
    token = ("" if form.get("remove_token") == "1"
             else (form.get("connector_github_token") or s.settings.connector_github_token))
    problem = ""
    if any("\n" in value or "\r" in value for value in form.values()):
        problem = "A value has a line break in it. Paste it again on one line."
    elif repo and not REPO_RE.fullmatch(repo):
        problem = "Enter the repository as owner/name, like the part after github.com/ in its address."
    elif branch and not BRANCH_RE.fullmatch(branch):
        problem = "That branch name has characters GitHub doesn't allow. Leave it empty for the default branch."
    elif token and not token.startswith(("github_pat_", "ghp_")):
        problem = "A GitHub token starts with github_pat_ (fine-grained) or ghp_. Copy it again."
    return repo, branch, token, problem


CONNECTORS_WHERE = "Repository and branch box, Code tab"


@page(require="admin")
async def save_connectors(request: Request, viewer: Viewer) -> Response:
    """The GitHub repository Code works in. The token goes in config.env with the other secrets and is never
    shown again."""
    s = services(request)
    form = {k: str(v).strip() for k, v in (await request.form()).items() if isinstance(v, str)}
    repo, branch, token, problem = _connector_form(s, form)
    if not problem and repo:
        try:
            await asyncio.to_thread(s.github.check, repo, branch, token)
        except ConnectorError as exc:
            problem = f"Couldn't read {repo}: {exc}"
    if problem:
        flash(request, f"Connectors weren't saved. {problem} ({CONNECTORS_WHERE}.)")
        return RedirectResponse("/settings#connectors", status_code=303)
    before = {"repository": s.settings.connector_github_repo, "branch": s.settings.connector_github_branch,
              "token": "saved" if s.settings.connector_github_token else "none"}
    after = {"repository": repo, "branch": branch, "token": "saved" if token else "none"}
    token_changed = token != s.settings.connector_github_token
    if before == after and not token_changed:
        flash(request, "Nothing changed in connectors. (Repository and branch box, Code tab.)")
        return RedirectResponse("/settings#connectors", status_code=303)
    _save_config(s, {"CONNECTOR_GITHUB_REPO": repo, "CONNECTOR_GITHUB_BRANCH": branch,
                     "CONNECTOR_GITHUB_TOKEN": token})
    s.settings.connector_github_repo, s.settings.connector_github_branch = repo, branch
    s.settings.connector_github_token = token
    changes = {key: {"from": before[key], "to": after[key]} for key in before if before[key] != after[key]}
    if token_changed and "token" not in changes:
        changes["token"] = {"from": "saved", "to": "replaced"}
    s.db.audit("connectors_changed", actor_id=viewer.id, details=changes)
    flash(request, "Saved connectors." + (f" Code now works in a copy of {repo}." if repo else "")
          + f" ({CONNECTORS_WHERE}.)")
    return RedirectResponse("/settings#connectors", status_code=303)


@page(require="admin")
async def check_connectors(request: Request, viewer: Viewer) -> Response:
    """The Check button: try the repository, branch and token as typed, without saving anything."""
    s = services(request)
    form = {k: str(v).strip() for k, v in (await request.form()).items() if isinstance(v, str)}
    repo, branch, token, problem = _connector_form(s, form)
    if not problem and not repo:
        problem = "Type a repository first."
    if not problem:
        try:
            await asyncio.to_thread(s.github.check, repo, branch, token)
        except ConnectorError as exc:
            problem = f"Couldn't read {repo}: {exc}"
    flash(request, (f"Check failed. {problem}" if problem else f"The check worked: GitHub let us read {repo}. "
                                                              "Nothing was saved yet.") + f" ({CONNECTORS_WHERE}.)")
    return RedirectResponse("/settings#connectors", status_code=303)


async def verify_slack(s: Services, bot: str, app: str) -> tuple[dict[str, Any], str]:
    """Try a bot token and an app-level token with Slack. Returns (auth.test's answer, problem)."""
    client = s.slack_factory(bot)
    try:
        auth = await client.auth_test()
    except Exception as exc:
        return {}, f"Slack didn't accept the bot token ({getattr(exc, 'error', '') or exc})."
    try:
        await client.open_socket_url(app)
    except Exception as exc:
        return {}, (f"Slack didn't accept the app-level token ({getattr(exc, 'error', '') or exc}). "
                    "Check that it has the connections:write scope.")
    return auth, ""


@page(require="admin")
async def save_env_section(request: Request, viewer: Viewer) -> Response:
    """Save one config.env section: AI engine, Site and server, or Slack connection. Secrets are checked, written
    to config.env, and never shown or logged. Settings that need a restart wait for the Restart button."""
    s = services(request)
    section = request.path_params["section"]
    if section not in server_settings.ENV_SECTIONS:
        return error_page(request, viewer, 404, "Not found", "That settings section doesn't exist.")
    name = server_settings.SECTION_NAMES[section]
    form = await request.form()
    values, errors = server_settings.validate(section, form, s.settings, s.pending_restart)
    extra: dict[str, str] = {}
    if not errors and section == "slack":
        bot = values.get("slack_bot_token", s.settings.slack_bot_token)
        app = values.get("slack_app_token", s.settings.slack_app_token)
        if ("slack_bot_token" in values or "slack_app_token" in values) and bot and app:
            auth, problem = await verify_slack(s, bot, app)
            if problem:
                errors["slack_bot_token" if "bot token" in problem else "slack_app_token"] = problem
            elif "slack_bot_token" in values:
                team = auth.get("team_id", "")
                if s.settings.slack_team_id and team and team != s.settings.slack_team_id:
                    errors["slack_bot_token"] = (
                        "That token is for a different Slack workspace. Everyone's accounts belong to the current "
                        "one, so switching workspaces has to be done on the setup page (reset-setup on the mini PC).")
                elif team and not s.settings.slack_team_id:
                    extra["SLACK_TEAM_ID"] = team
        channel = values.get("slack_approvals_channel")
        if channel and not errors and bot:
            try:
                await s.slack_factory(bot).channel_members(channel)
            except Exception as exc:
                errors["slack_approvals_channel"] = (
                    f"The app can't see that channel ({getattr(exc, 'error', '') or exc}). Invite the app to the "
                    "channel, then try again.")
    if errors:
        submitted = {f.name: str(form.get(f.name, "") or "") for f in server_settings.ENV_SECTIONS[section][2]
                     if f.kind != "secret"}
        return render(request, "settings.html", viewer, status_code=400,
                      flash="Some settings need fixing. They're marked below.",
                      **await full_context(s, errors, submitted, env_section=section))
    where = f"on the {TAB_NAMES[ENV_TABS[section]]} tab"
    if not values:
        flash(request, f"Nothing changed in {name} {where}.")
        return RedirectResponse(f"/settings#env-{section}", status_code=303)
    _save_config(s, {**server_settings.env_updates(values), **extra})
    waiting = []
    for name, value in values.items():
        f = server_settings.ENV_FIELDS[name]
        if f.kind == "secret":
            was, now = server_settings.describe(f, getattr(s.settings, name), value)
        else:
            was, now = server_settings.shown_value(s.settings, s.pending_restart, name), server_settings.serialize(
                f, value)
        if f.restart:
            if f.kind != "secret" and server_settings.serialize(f, value) == server_settings.serialize(
                    f, server_settings.running_value(s.settings, name)):
                s.pending_restart.pop(name, None)  # changed back to what's running
            else:
                s.pending_restart[name] = "" if f.kind == "secret" else server_settings.serialize(f, value)
                waiting.append(f.label)
        else:
            setattr(s.settings, name, value)
        s.db.audit("setting_changed", actor_id=viewer.id, details={"setting": f.label, "from": was, "to": now})
    for key, value in extra.items():
        if key == "SLACK_TEAM_ID":
            s.settings.slack_team_id = value
    if "slack_bot_token" in values:
        s.slack_workspace = ""
    text = f"Saved {name} {where}."
    if waiting:
        text += " " + ", ".join(waiting) + (" applies" if len(waiting) == 1 else " apply") + " after a restart."
    flash(request, text)
    return RedirectResponse(f"/settings#env-{section}", status_code=303)


@page(require="admin")
async def restart_app(request: Request, viewer: Viewer) -> Response:
    """Restart the app so config.env changes take effect. The Windows service starts it again in a few seconds."""
    s = services(request)
    restart = request.app.state.request_restart
    if restart is None:
        flash(request, "This copy can't restart itself. Restart SubZero AI on the mini PC (or restart the "
                       "SubZeroAI service in Windows Services).")
        return RedirectResponse("/settings#restart", status_code=303)
    s.db.audit("app_restarted", actor_id=viewer.id, details={"settings": [
        server_settings.ENV_FIELDS[n].label for n in s.pending_restart]})
    asyncio.get_running_loop().call_later(1, restart)
    return render(request, "restarting.html", viewer)


async def _run_checks(s: Services, actor_id: int, actor_name: str) -> None:
    """The background job behind Run checks. Saves the results for the Settings page; always clears the mark."""
    from .checks import run_checks
    try:
        engine = s.code.engine_now() == "agent"
        results = await run_checks(s.settings, slack=s.slack, claude=s.claude, egress=s.egress,
                                   gateway=s.gateway if engine else None,
                                   credential_problem=s.code.agent.credential_problem() if engine else "")
        s.db.set_kv("settings_checks", json.dumps({
            "at": utcnow(), "by": actor_name,
            "results": [{"status": r.status, "title": r.title, "detail": r.detail} for r in results]}))
        s.db.audit("checks_done", actor_id=actor_id,
                   details={"problems": sum(r.status == "FAIL" for r in results)})
    except Exception as exc:
        log.exception("The settings checks failed to run")
        s.db.set_kv("settings_checks", json.dumps({"at": utcnow(), "by": actor_name, "results": [
            {"status": "FAIL", "title": "The checks couldn't finish", "detail": type(exc).__name__}]}))
    finally:
        s.checks_running = ""


@page(require="admin")
async def start_checks(request: Request, viewer: Viewer) -> Response:
    """Run the same checks as check.cmd, in the background: they can take longer than the tunnel waits."""
    s = services(request)
    if s.checks_running:
        flash(request, "The checks are already running. Refresh in a minute. (Check everything box, Overview tab.)")
    else:
        s.checks_running = utcnow()
        s.db.audit("checks_started", actor_id=viewer.id)
        s.tasks.spawn(_run_checks(s, viewer.id, viewer.name), name="settings-checks")
        flash(request, "Checks started. Refresh in a minute. The results appear in Check everything on the "
                       "Overview tab.")
    return RedirectResponse("/settings#checks", status_code=303)


@page(require="admin")
async def bridge_sign_in(request: Request, viewer: Viewer) -> Response:
    """Start Claude Code's sign-in on the mini PC and show its sign-in address, to open on any device."""
    s = services(request)
    cli = claude_cli_path(s.settings.pi_gateway_script or None)
    if cli is None:
        flash(request, "Couldn't find the Claude Code program that comes with the AI helper. Install the newest "
                       f"SubZero AI zip, then try again. ({BRIDGE_WHERE}.)")
        return RedirectResponse("/settings#bridge", status_code=303)
    try:
        await asyncio.to_thread(s.bridge_login.start, cli, login_env(s.settings.data_dir), viewer.name)
    except LoginError as exc:
        flash(request, f"{exc} ({BRIDGE_WHERE}.)")
        return RedirectResponse("/settings#bridge", status_code=303)
    s.db.audit("bridge_sign_in_started", actor_id=viewer.id)
    flash(request, f"Open the sign-in page, sign in, then paste the code here. ({BRIDGE_WHERE}.)")
    return RedirectResponse("/settings#bridge", status_code=303)


@page(require="admin")
async def bridge_code(request: Request, viewer: Viewer) -> Response:
    """Hand the pasted code to the waiting sign-in. The code itself is never logged, saved or shown again."""
    s = services(request)
    form = await request.form()
    code = str(form.get("code") or "").strip()
    if not s.bridge_login.waiting():
        flash(request, "That sign-in isn't waiting anymore. Press Sign in again to get a new address. "
                       f"({BRIDGE_WHERE}.)")
        return RedirectResponse("/settings#bridge", status_code=303)
    if not looks_like_code(code):
        flash(request, "That doesn't look like the whole code. Copy all of it from the Claude page, including the "
                       "part after the #, and paste it again." if code else "Paste the code from the Claude page first.")
        return RedirectResponse("/settings#bridge", status_code=303)
    data_dir = s.settings.data_dir
    try:
        await asyncio.to_thread(s.bridge_login.finish, code, lambda: bridge_logged_in(data_dir))
    except LoginError as exc:
        s.db.audit("bridge_sign_in_failed", actor_id=viewer.id)
        flash(request, f"Not signed in. {exc} ({BRIDGE_WHERE}.)")
        return RedirectResponse("/settings#bridge", status_code=303)
    use_new_bridge(s)
    s.db.audit("bridge_signed_in", actor_id=viewer.id)
    flash(request, "Signed in to the Claude subscription." + (
        "" if BRIDGE_PROVIDER in s.settings.ai_providers else
        " Turn on Claude subscription under AI providers to use it.") + f" ({BRIDGE_WHERE}.)")
    return RedirectResponse("/settings#bridge", status_code=303)


@page(require="admin")
async def bridge_cancel(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    s.bridge_login.cancel()
    flash(request, f"Sign-in stopped. ({BRIDGE_WHERE}.)")
    return RedirectResponse("/settings#bridge", status_code=303)


@page(require="admin")
async def bridge_sign_out(request: Request, viewer: Viewer) -> Response:
    """Forget the Claude subscription's login. Sign in again from the same box."""
    s = services(request)
    path = bridge_credentials_path(s.settings.data_dir)
    if not path.is_file():
        flash(request, "The Claude subscription isn't signed in.")
    else:
        try:
            path.unlink()
        except OSError as exc:  # on Windows, the AI helper may have the file open
            log.warning("Couldn't remove the Claude subscription login: %s", type(exc).__name__)
            flash(request, "Couldn't sign out right now because the AI helper is using the login. Try again in a "
                           "minute, or restart first.")
            return RedirectResponse("/settings#bridge", status_code=303)
        use_new_bridge(s)
        s.db.audit("bridge_signed_out", actor_id=viewer.id)
        flash(request, "Signed out of the Claude subscription. Its models stop working until someone signs in "
                       "again.")
    return RedirectResponse("/settings#bridge", status_code=303)


@page(require="admin")
async def save_prompt(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    name = request.path_params["layer"] + ".md"
    labels = {n: label for n, label, _ in saved_settings.PROMPT_LAYERS}
    if name not in labels:
        return error_page(request, viewer, 404, "Not found", "That prompt layer doesn't exist.")
    form = await request.form()
    if form.get("reset") == "1":
        text = saved_settings.builtin_prompt(name)
    else:
        text = str(form.get("text", ""))
        if not text.strip():
            flash(request, f"The {labels[name].lower()} layer can't be empty. Use Restore built-in to undo changes.")
            return RedirectResponse(f"/settings#prompt-{name.removesuffix('.md')}", status_code=303)
        if len(text) > 50_000:
            flash(request, f"The {labels[name].lower()} layer is too long. Keep it under 50,000 characters.")
            return RedirectResponse(f"/settings#prompt-{name.removesuffix('.md')}", status_code=303)
    changed = saved_settings.save_prompt(s.db, s.settings, name, text, viewer.id)
    flash(request, (f"Saved the {labels[name].lower()} layer. It applies to the next reply."
                    if changed else f"Nothing changed in the {labels[name].lower()} layer.")
          + " (System prompt box, Slack chat review tab.)")
    return RedirectResponse(f"/settings#prompt-{name.removesuffix('.md')}", status_code=303)


@page(require="admin")
async def backup_now(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    name = await s.nightly.run(label="manual")
    s.db.audit("backup_made", actor_id=viewer.id, details={"file": name})
    flash(request, f"Backed up to {name}. It is listed in Backups on the History tab." if name else
          "The backup failed. The health list on the Overview tab says why; check that the backups folder exists.")
    return RedirectResponse("/settings#backups", status_code=303)


async def _run_safety_check(s: Services, model: str, actor_id: int, actor_name: str) -> None:
    """The background job behind the Run button. Always clears the 'running' mark."""
    try:
        result = await model_check.run_model_check(s.claude, s.settings, model)
        model_check.save_result(s.db, result, actor_name)
        s.db.audit("model_check_done", actor_id=actor_id, details={
            "model": model, "chat_passed": result.chat_passed, "screener_passed": result.screener_passed,
            "failed": [c.case for c in result.failed], "check_version": result.check_version})
    except Exception as exc:
        log.exception("The model safety check on %s failed to run", model)
        s.db.audit("model_check_done", actor_id=actor_id, details={
            "model": model, "error": type(exc).__name__})
    finally:
        s.model_checks_running.pop(model, None)


@page(require="admin")
async def run_model_check(request: Request, viewer: Viewer) -> Response:
    """Start the safety check as a background job: a full check takes longer than the tunnel waits."""
    s = services(request)
    form = await request.form()
    model, problem = saved_settings.check_model(str(form.get("model", "") or ""), s.settings.ai_providers,
                                                resolve_backend(s.settings))
    if not problem:
        problem = model_check.judge_problem(s.settings)
    if not problem and model in s.model_checks_running:
        problem = f"A safety check on {model} is already running."
    if problem:
        flash(request, f"The safety check didn't start. {problem} (Model safety check box, AI and keys tab.)")
        return RedirectResponse("/settings#model-check", status_code=303)
    s.model_checks_running[model] = utcnow()
    s.db.audit("model_check_started", actor_id=viewer.id, details={"model": model})
    s.tasks.spawn(_run_safety_check(s, model, viewer.id, viewer.name), name=f"model-check-{model}")
    flash(request, "Safety check started. Refresh in a minute. The results appear in Model safety check on the "
                   "AI and keys tab.")
    return RedirectResponse("/settings#model-check", status_code=303)


# --------------------------------------------------------------------------- waiting messages, old chats
@page(require="admin")
async def post_cards_again(request: Request, viewer: Viewer) -> Response:
    """Post the approval card again for every message still waiting for a mentor. It doesn't change who may approve:
    the cards are the same ones, and the buttons check the person who taps them."""
    s = services(request)
    where = "(Slack messages waiting box, Overview tab.)"
    if s.slack is None or not s.settings.slack_approvals_channel:
        flash(request, f"Slack isn't connected or no review channel is set, so there is nowhere to post the cards. "
                       f"{where}")
        return RedirectResponse("/settings#waiting", status_code=303)
    result = await s.approvals.repost_cards()
    s.db.audit("cards_reposted", actor_id=viewer.id, details=result)
    if not result["posted"] and not result["failed"]:
        flash(request, f"No messages are waiting, so no cards were posted. {where}")
    else:
        text = f"Posted {result['posted']} card{'s' if result['posted'] != 1 else ''} again."
        if result["failed"]:
            text += f" {result['failed']} couldn't be posted. Check that the app is in the review channel."
        flash(request, f"{text} {where}")
    return RedirectResponse("/settings#waiting", status_code=303)


ARCHIVE_WHERE = "Old website chats box, History tab"


@page(require="admin")
async def archive_start(request: Request, viewer: Viewer) -> Response:
    """Start saving all old website chats into a zip file in the backups folder, in the background."""
    s = services(request)
    form = await request.form()
    job = archive.start_job(s.db, "archive", viewer.name)
    if job is None:
        flash(request, f"An archive or delete is already running. Wait for it to finish. ({ARCHIVE_WHERE}.)")
    else:
        include_videos = form.get("include_videos") == "1"
        s.db.audit("old_chats_archive_started", actor_id=viewer.id, details={"videos": include_videos})
        s.tasks.spawn(archive.run_archive_job(s.settings, s.db, job, actor_id=viewer.id, include_videos=include_videos),
                      name="old-chats-archive")
        flash(request, f"Started saving the archive. The progress shows below. ({ARCHIVE_WHERE}.)")
    return RedirectResponse("/settings#old-chats", status_code=303)


@page(require="admin")
async def archive_delete_old(request: Request, viewer: Viewer) -> Response:
    """Delete every old website chat, after a backup, once the admin has typed the confirmation word."""
    s = services(request)
    form = await request.form()
    if str(form.get("confirm", "") or "").strip() != archive.CONFIRM_WORD:
        flash(request, f"Nothing was deleted. Type the word {archive.CONFIRM_WORD} in capital letters to confirm. "
                       f"({ARCHIVE_WHERE}.)")
        return RedirectResponse("/settings#old-chats", status_code=303)
    job = archive.start_job(s.db, "delete", viewer.name)
    if job is None:
        flash(request, f"An archive or delete is already running. Wait for it to finish. ({ARCHIVE_WHERE}.)")
    else:
        slack_id = viewer.slack_user_id if SLACK_ID_RE.fullmatch(viewer.slack_user_id or "") else None
        s.db.audit("old_chats_delete_started", actor_id=viewer.id)
        s.tasks.spawn(archive.run_delete_job(s.settings, s.db, s.slack, job, actor_id=viewer.id,
                                             admin_slack_id=slack_id), name="old-chats-delete")
        flash(request, f"Started. A backup is made first, then the chats are deleted. ({ARCHIVE_WHERE}.)")
    return RedirectResponse("/settings#old-chats", status_code=303)


@page(require="admin")
async def archive_status(request: Request, viewer: Viewer) -> Response:
    """The running job's progress, for the page to show without reloading."""
    job = archive.job_state(services(request).db) or {}
    return JSONResponse({"state": job.get("state", ""), "step": job.get("step", ""), "done": job.get("done", 0),
                         "total": job.get("total", 1)})


@page(require="admin")
async def archive_download(request: Request, viewer: Viewer) -> Response:
    """Send one archive. Only a file named like the ones we make, directly in the backups folder."""
    s = services(request)
    path = archive.archive_path(s.settings, request.path_params["name"])
    if path is None:
        return error_page(request, viewer, 404, "Not found", "There's no archive with that name.")
    s.db.audit("old_chats_archive_downloaded", actor_id=viewer.id, details={"file": path.name})
    return FileResponse(path, media_type="application/zip", filename=path.name)


@page(require="admin")
async def archive_remove(request: Request, viewer: Viewer) -> Response:
    """Delete one archive file."""
    s = services(request)
    path = archive.archive_path(s.settings, request.path_params["name"])
    if path is None:
        flash(request, f"There's no archive with that name. ({ARCHIVE_WHERE}.)")
    else:
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError:
            flash(request, f"Couldn't delete {path.name} right now. Try again in a minute. ({ARCHIVE_WHERE}.)")
            return RedirectResponse("/settings#old-chats", status_code=303)
        s.db.audit("old_chats_archive_deleted", actor_id=viewer.id, details={"file": path.name, "bytes": size})
        flash(request, f"Deleted the archive {path.name}. ({ARCHIVE_WHERE}.)")
    return RedirectResponse("/settings#old-chats", status_code=303)


# --------------------------------------------------------------------------- setup page
def _setup_allowed(request: Request) -> bool:
    return is_local_request(request) and not setup_flow.setup_complete(services(request).db)


def _not_found() -> Response:
    return PlainTextResponse("Not found", status_code=404)


def setup_context(s: Services, errors: dict[str, str] | None = None, notes: dict[str, str] | None = None,
                  channels: list[dict[str, str]] | None = None, form: dict[str, str] | None = None) -> dict[str, Any]:
    done = setup_flow.steps(s.settings)
    address = s.settings.public_url if done["address"] else ""
    return {
        "done": done, "all_done": all(done.values()), "errors": errors or {}, "notes": notes or {},
        "form": form or {}, "address": address,
        "create_app_link": setup_flow.create_app_link(setup_flow.slack_manifest(s.settings)) if address else "",
        "redirect_url": s.settings.url("/auth/slack/callback") if address else "",
        "channels": channels, "channel": s.settings.slack_approvals_channel,
        "admins": "\n".join(sorted(s.settings.admin_slack_ids)),
        "config_path": str(s.settings.config_path),
    }


async def _channel_choices(s: Services) -> tuple[list[dict[str, str]] | None, str]:
    if not s.settings.slack_bot_token:
        return None, ""
    try:
        return await s.slack_factory(s.settings.slack_bot_token).private_channels(), ""
    except Exception as exc:
        if getattr(exc, "error", "") == "missing_scope":
            return None, ("Couldn't list the private channels: the Slack app is missing the "
                          f"{getattr(exc, 'needed', '') or 'groups:read'} permission. Add it under OAuth & "
                          "Permissions, reinstall the app, and paste the new bot token.")
        return None, f"Couldn't list the private channels ({getattr(exc, 'error', '') or exc})."


async def setup_page(request: Request) -> Response:
    if not _setup_allowed(request):
        return _not_found()
    s = services(request)
    channels, problem = await _channel_choices(s)
    return render(request, "setup.html", None, **setup_context(s, channels=channels,
                                                               notes={"channel": problem} if problem else None))


def _save_config(s: Services, updates: dict[str, str]) -> None:
    update_env_file(s.settings.config_path, updates)
    s.settings.config_file = str(s.settings.config_path)


async def setup_step(request: Request) -> Response:
    if not _setup_allowed(request):
        return _not_found()
    if not await check_csrf(request):
        return error_page(request, None, 403, "Your session expired", "Reload the setup page and try again.")
    s = services(request)
    step = request.path_params["step"]
    form = {k: str(v).strip() for k, v in (await request.form()).items() if isinstance(v, str)}
    errors: dict[str, str] = {}
    notes: dict[str, str] = {}
    if any("\n" in value or "\r" in value for key, value in form.items() if key != "admins"):
        # Tokens copied from a wrapped page sometimes pick up a line break; config.env can't hold one.
        errors[step] = "That value has a line break in it. Paste it again on one line."
        channels, problem = await _channel_choices(s)
        return render(request, "setup.html", None, status_code=400,
                      **setup_context(s, errors, None, channels, form))

    if step == "address":
        url = form.get("public_url", "").rstrip("/")
        if not url.startswith("https://") and "://" not in url:
            url = "https://" + url
        if not setup_flow.address_ok(url):
            errors["address"] = ("Enter the address people will use, like https://ai.yourteam.org, with no page "
                                 "path after it. It has to start with https:// and be a real domain.")
        else:
            _save_config(s, {"PUBLIC_URL": url})
            s.settings.public_url = url

    elif step == "slack":
        values = {
            "bot": form.get("slack_bot_token") or s.settings.slack_bot_token,
            "app": form.get("slack_app_token") or s.settings.slack_app_token,
            "id": form.get("slack_client_id") or s.settings.slack_client_id,
            "secret": form.get("slack_client_secret") or s.settings.slack_client_secret,
        }
        if not values["bot"].startswith("xoxb-"):
            errors["slack"] = "The bot token starts with xoxb-. Find it under OAuth & Permissions."
        elif not values["app"].startswith("xapp-"):
            errors["slack"] = ("The app-level token starts with xapp-. Make one under Basic Information, "
                               "App-Level Tokens, with the connections:write scope.")
        elif not values["id"] or not values["secret"]:
            errors["slack"] = "Copy the client ID and client secret from Basic Information, App Credentials."
        else:
            auth, problem = await verify_slack(s, values["bot"], values["app"])
            if problem:
                errors["slack"] = problem
            else:
                _save_config(s, {"SLACK_BOT_TOKEN": values["bot"], "SLACK_APP_TOKEN": values["app"],
                                 "SLACK_CLIENT_ID": values["id"], "SLACK_CLIENT_SECRET": values["secret"]})
                s.settings.slack_bot_token, s.settings.slack_app_token = values["bot"], values["app"]
                s.settings.slack_client_id, s.settings.slack_client_secret = values["id"], values["secret"]
                notes["slack"] = f"Connected to the {auth.get('team') or 'team'} workspace."

    elif step == "channel":
        channel = form.get("channel_id") or form.get("channel", "")
        if not s.settings.slack_bot_token:
            errors["channel"] = "Connect the Slack app first."
        elif not CHANNEL_RE.fullmatch(channel):
            errors["channel"] = "Pick the channel, or paste its channel ID (it starts with C or G)."
        else:
            try:
                members = await s.slack_factory(s.settings.slack_bot_token).channel_members(channel)
            except Exception as exc:
                errors["channel"] = (f"The app can't see that channel ({getattr(exc, 'error', '') or exc}). "
                                     "Type /invite @SubZero AI in the channel, then try again.")
            else:
                _save_config(s, {"SLACK_APPROVALS_CHANNEL": channel})
                s.settings.slack_approvals_channel = channel
                count = max(len(members) - 1, 0)  # not counting the app itself
                notes["channel"] = f"Saved. The channel has {count} mentor{'s' if count != 1 else ''} in it."

    elif step == "claude":
        key = form.get("anthropic_api_key") or s.settings.anthropic_api_key
        if not key.startswith("sk-ant-"):
            errors["claude"] = "The API key starts with sk-ant-. Create it inside the SubZero AI workspace."
        else:
            try:
                await s.claude_factory(key).complete(
                    model=s.settings.model_default, system="Reply with the word OK.",
                    messages=[{"role": "user", "content": "Connection check"}], max_tokens=16)
            except Exception as exc:
                errors["claude"] = f"Claude didn't accept the key: {exc}"
            else:
                updates = {"ANTHROPIC_API_KEY": key, "FAKE_CLAUDE": "0"}
                if s.settings.ai_backend.strip().lower() == "fake":
                    updates["AI_BACKEND"] = "anthropic"
                    s.settings.ai_backend = "anthropic"
                _save_config(s, updates)
                s.settings.anthropic_api_key = key
                use_new_keys(s)
                notes["claude"] = f"The key works with {s.settings.model_default}."

    elif step == "admins":
        ids = [part for part in re.split(r"[\s,]+", form.get("admins", "")) if part]
        bad = [item for item in ids if not SLACK_ID_RE.fullmatch(item)]
        if not ids or bad:
            errors["admins"] = ("Enter at least one Slack member ID, one per line. They start with U or W, like "
                                "U012AB3CD.")
        else:
            names = []
            if s.settings.slack_bot_token:
                client = s.slack_factory(s.settings.slack_bot_token)
                for item in ids:
                    try:
                        info = await client.user_info(item)
                    except Exception as exc:
                        errors["admins"] = f"Slack doesn't know {item} ({getattr(exc, 'error', '') or exc})."
                        break
                    if info.get("deleted") or info.get("is_bot") or is_excluded_account(info, s.settings):
                        errors["admins"] = f"{item} is a bot, guest, or deactivated account."
                        break
                    names.append((info.get("profile") or {}).get("real_name") or info.get("real_name") or item)
            if "admins" not in errors:
                saved_settings.save(s.db, s.settings, {"admin_slack_ids": frozenset(ids)}, None)
                notes["admins"] = "Admins: " + (", ".join(names) if names else ", ".join(ids)) + "."

    elif step == "finish":
        if not setup_flow.config_is_complete(s.settings):
            errors["finish"] = "Finish the steps marked To do first."
        else:
            code = setup_flow.new_recovery_code(s.db)
            setup_flow.mark_setup_complete(s.db)
            restart = request.app.state.request_restart
            if restart is not None:
                asyncio.get_running_loop().call_later(2, restart)
            return render(request, "setup_done.html", None, code=code, restarting=restart is not None,
                          public_url=s.settings.public_url)
    else:
        return _not_found()

    channels, problem = await _channel_choices(s)
    if problem and "channel" not in notes:
        notes["channel"] = problem
    return render(request, "setup.html", None, status_code=400 if errors else 200,
                  **setup_context(s, errors, notes, channels, form if errors else None))


# --------------------------------------------------------------------------- recovery sign-in
async def recovery_login(request: Request) -> Response:
    if not is_local_request(request):
        return _not_found()
    s = services(request)
    if request.method == "GET":
        return render(request, "recovery.html", None, error=None)
    if not await check_csrf(request):
        return render(request, "recovery.html", None, status_code=403,
                      error="Your session expired. Reload the page and try again.")
    form = await request.form()
    await asyncio.sleep(0.5)  # slow down guessing
    if not setup_flow.check_recovery_code(s.db, str(form.get("code", ""))):
        s.db.audit("recovery_sign_in_refused", actor_id=None)
        return render(request, "recovery.html", None, status_code=400,
                      error="That recovery code didn't work. Check it and try again.")
    user = s.store.upsert_user(slack_user_id=RECOVERY_USER["slack_user_id"], name=RECOVERY_USER["name"],
                               role="admin", login=True)
    s.db.audit("recovery_sign_in", actor_id=user["id"])
    request.session["next"] = "/settings"
    return _finish_login(request, s, user)


admin_routes = [
    Route("/settings", settings_page),
    Route("/settings/backup", backup_now, methods=["POST"]),
    Route("/settings/model-check", run_model_check, methods=["POST"]),
    Route("/settings/prompts/{layer}", save_prompt, methods=["POST"]),
    Route("/settings/connectors", save_connectors, methods=["POST"]),
    Route("/settings/connectors/check", check_connectors, methods=["POST"]),
    Route("/settings/repost-cards", post_cards_again, methods=["POST"]),
    Route("/settings/archive/start", archive_start, methods=["POST"]),
    Route("/settings/archive/delete-old", archive_delete_old, methods=["POST"]),
    Route("/settings/archive/status", archive_status),
    Route("/settings/archives/{name}", archive_download),
    Route("/settings/archives/{name}/delete", archive_remove, methods=["POST"]),
    Route("/settings/ai-keys", save_ai_keys, methods=["POST"]),
    Route("/settings/env/{section}", save_env_section, methods=["POST"]),
    Route("/settings/restart", restart_app, methods=["POST"]),
    Route("/settings/checks", start_checks, methods=["POST"]),
    Route("/settings/bridge/sign-out", bridge_sign_out, methods=["POST"]),
    Route("/settings/bridge/sign-in", bridge_sign_in, methods=["POST"]),
    Route("/settings/bridge/code", bridge_code, methods=["POST"]),
    Route("/settings/bridge/cancel", bridge_cancel, methods=["POST"]),
    Route("/settings/{section}", save_section, methods=["POST"]),
    Route("/setup", setup_page),
    Route("/setup/{step}", setup_step, methods=["POST"]),
    Route("/auth/recovery", recovery_login, methods=["GET", "POST"]),
]
