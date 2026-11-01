"""Admin pages: the Settings page (health, backups, settings, and the system prompt layers), the first-run
setup page, and the recovery sign-in. Setup and recovery answer only on the mini PC itself."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from . import __version__, model_check, saved_settings, server_settings, setup_flow
from .ai_backend import resolve_backend
from .bridge_login import LoginError, login_env, looks_like_code
from .backup import list_backups, size_text
from .config import MODEL_ID_SETTINGS, PROVIDERS, model_provider, parse_env_file, update_env_file
from .connectors import BRANCH_RE, REPO_RE, ConnectorError
from .db import utcnow
from .maintenance import update_available
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
from .web import (
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
)

log = logging.getLogger("subzero.admin")

CHANNEL_RE = server_settings.CHANNEL_RE


def apply_live(s: Services) -> None:
    """Hand changed settings to the parts of the app that keep their own copy."""
    s.store.show_pending_to_team = s.settings.show_pending_to_team
    s.store.show_rejected_to_team = s.settings.show_rejected_to_team


def flash(request: Request, text: str) -> None:
    request.session["flash"] = text


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
                               "On the mini PC, run update.cmd in the SubZero AI folder.",
                     "link": newer.get("url")})
    else:
        rows.append({"label": "Version", "state": __version__, "kind": "plain", "detail": "Up to date as of the "
                     "last check." if s.db.get_kv("latest_release") else ""})
    return rows


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
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY",
           "openrouter": "OPENROUTER_API_KEY"}


def settings_context(s: Services, errors: dict[str, str] | None = None,
                     submitted: dict[str, str] | None = None, env_section: str = "") -> dict[str, Any]:
    """Everything the Settings page shows. `env_section` names a config.env section whose save failed, so
    `errors` and `submitted` belong to it instead of the database sections."""
    db_errors, db_submitted = (None, None) if env_section else (errors, submitted)
    sections = []
    for key, (title, fields) in saved_settings.SECTIONS.items():
        items = []
        for f in fields:
            value = (db_submitted or {}).get(f.name)
            if value is None:
                value = saved_settings.current(s.settings, f.name)
            item = {"field": f, "value": value, "error": (db_errors or {}).get(f.name)}
            if f.kind == "ids":
                item["people"] = _names_for(s, getattr(s.settings, f.name))
            elif f.kind == "providers":
                on = set(value.split())
                item["choices"] = [{"id": p, "label": saved_settings.PROVIDER_LABELS[p], "on": p in on,
                                    "help": saved_settings.PROVIDER_HELP.get(p, "")} for p in PROVIDERS]
            items.append(item)
        sections.append({"key": key, "title": title, "fields": items})
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
                "size": size_text(b.size)} for b in list_backups(s.settings)[:5]]
    connectors = {"repo": s.settings.connector_github_repo, "branch": s.settings.connector_github_branch,
                  "token_set": bool(s.settings.connector_github_token), "drive": s.settings.connector_drive_links}
    ai_keys = []
    for p in PROVIDERS:
        saved = getattr(s.settings, KEY_SETTINGS[p], "") if p in KEY_SETTINGS else ""
        if p in KEY_SETTINGS and (p in s.settings.ai_providers or saved):  # a turned-off provider's key can go
            ai_keys.append({"id": p, "label": saved_settings.PROVIDER_LABELS[p], "field": KEY_SETTINGS[p],
                            "set": bool(saved), "hint": server_settings.secret_hint(saved), "help": KEY_HELP[p],
                            "on": p in s.settings.ai_providers, "locked": KEY_ENV[p] in os.environ})
    return {"health": health_rows(s), "sections": sections, "ai_keys": ai_keys, "safety": safety_context(s),
            "prompts": prompts, "history": history,
            "backups": backups, "backup_folder": str(s.settings.backups_path), "connectors": connectors,
            "env_sections": env_sections_context(s, env_section, errors, submitted),
            "accounts": accounts_context(s), "checks": checks_context(s), "bridge": bridge_context(s), "file_only": server_settings.FILE_ONLY,
            "pending": [server_settings.ENV_FIELDS[n].label for n in s.pending_restart],
            "config_path": str(s.settings.config_path)}


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
                  **settings_context(s))


@page(require="admin")
async def save_section(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    section = request.path_params["section"]
    if section not in saved_settings.SECTIONS:
        return error_page(request, viewer, 404, "Not found", "That settings section doesn't exist.")
    form = await request.form()
    values, errors = saved_settings.validate(section, form, actor_slack_id=viewer.slack_user_id,
                                                    settings=s.settings, db=s.db)
    if errors:
        submitted = {f.name: saved_settings.form_text(form, f.name) for f in saved_settings.SECTIONS[section][1]}
        return render(request, "settings.html", viewer, status_code=400,
                      flash="Some settings need fixing. They're marked below.",
                      **settings_context(s, errors, submitted))
    changed = saved_settings.save(s.db, s.settings, values, viewer.id)
    apply_live(s)
    if "ai_providers" in changed:
        use_new_bridge(s)
        refresh_model_names(s)
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
    typed = {p: str(form.get(name, "") or "").strip() for p, name in KEY_SETTINGS.items()}
    typed = {p: key for p, key in typed.items() if key}
    removed = [p for p in KEY_SETTINGS if form.get(f"remove_{p}") == "1" and getattr(s.settings, KEY_SETTINGS[p])
               and p not in typed]
    problem = ""
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
                       "Turn it on under Claude settings first.")
        else:
            problem = key_problem(provider, key)
        if problem:
            break
    if not typed and not removed and not problem:
        problem = "Paste a key into at least one box."
    if problem:
        flash(request, f"Keys weren't saved. {problem}")
        return RedirectResponse("/settings#ai-keys", status_code=303)
    _save_config(s, {**{KEY_ENV[p]: key for p, key in typed.items()}, **{KEY_ENV[p]: "" for p in removed}})
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
    flash(request, " ".join(parts))
    return RedirectResponse("/settings#ai-keys", status_code=303)


GITHUB_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([^/\s]+/[^/#?\s]+)")


@page(require="admin")
async def save_connectors(request: Request, viewer: Viewer) -> Response:
    """The GitHub repository and Google Drive links for the composer's connectors. The token goes in config.env
    with the other secrets and is never shown again."""
    s = services(request)
    form = {k: str(v).strip() for k, v in (await request.form()).items() if isinstance(v, str)}
    repo, branch = form.get("connector_github_repo", ""), form.get("connector_github_branch", "")
    match = GITHUB_URL_RE.match(repo)
    if match:
        repo = match.group(1).removesuffix(".git")
    token = "" if form.get("remove_token") == "1" else (form.get("connector_github_token") or s.settings.connector_github_token)
    drive = form.get("connector_drive_links") == "1"
    problem = ""
    if any("\n" in value or "\r" in value for value in form.values()):
        problem = "A value has a line break in it. Paste it again on one line."
    elif repo and not REPO_RE.fullmatch(repo):
        problem = "Enter the repository as owner/name, like the part after github.com/ in its address."
    elif branch and not BRANCH_RE.fullmatch(branch):
        problem = "That branch name has characters GitHub doesn't allow. Leave it empty for the default branch."
    elif token and not token.startswith(("github_pat_", "ghp_")):
        problem = "A GitHub token starts with github_pat_ (fine-grained) or ghp_. Copy it again."
    elif repo:
        try:
            await asyncio.to_thread(s.github.check, repo, branch, token)
        except ConnectorError as exc:
            problem = f"Couldn't read {repo}: {exc}"
    if problem:
        flash(request, f"Connectors weren't saved. {problem}")
        return RedirectResponse("/settings#connectors", status_code=303)
    before = {"repository": s.settings.connector_github_repo, "branch": s.settings.connector_github_branch,
              "token": "saved" if s.settings.connector_github_token else "none", "drive links": s.settings.connector_drive_links}
    after = {"repository": repo, "branch": branch, "token": "saved" if token else "none", "drive links": drive}
    token_changed = token != s.settings.connector_github_token
    if before == after and not token_changed:
        flash(request, "Nothing changed in connectors.")
        return RedirectResponse("/settings#connectors", status_code=303)
    _save_config(s, {"CONNECTOR_GITHUB_REPO": repo, "CONNECTOR_GITHUB_BRANCH": branch, "CONNECTOR_GITHUB_TOKEN": token,
                     "CONNECTOR_DRIVE_LINKS": "true" if drive else "false"})
    s.settings.connector_github_repo, s.settings.connector_github_branch, s.settings.connector_github_token = repo, branch, token
    s.settings.connector_drive_links = drive
    changes = {key: {"from": before[key], "to": after[key]} for key in before if before[key] != after[key]}
    if token_changed and "token" not in changes:
        changes["token"] = {"from": "saved", "to": "replaced"}
    s.db.audit("connectors_changed", actor_id=viewer.id, details=changes)
    flash(request, "Saved connectors." + (f" Everyone can now add files from {repo}." if repo else ""))
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
                      **settings_context(s, errors, submitted, env_section=section))
    if not values:
        flash(request, f"Nothing changed in {name}.")
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
    text = f"Saved {name}."
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
        results = await run_checks(s.settings, slack=s.slack, claude=s.claude)
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
        flash(request, "The checks are already running. Refresh in a minute.")
    else:
        s.checks_running = utcnow()
        s.db.audit("checks_started", actor_id=viewer.id)
        s.tasks.spawn(_run_checks(s, viewer.id, viewer.name), name="settings-checks")
        flash(request, "Checks started. Refresh in a minute.")
    return RedirectResponse("/settings#checks", status_code=303)


@page(require="admin")
async def bridge_sign_in(request: Request, viewer: Viewer) -> Response:
    """Start Claude Code's sign-in on the mini PC and show its sign-in address, to open on any device."""
    s = services(request)
    cli = claude_cli_path(s.settings.pi_gateway_script or None)
    if cli is None:
        flash(request, "Couldn't find the Claude Code program that comes with the AI helper. Install the newest "
                       "SubZero AI zip, then try again.")
        return RedirectResponse("/settings#bridge", status_code=303)
    try:
        await asyncio.to_thread(s.bridge_login.start, cli, login_env(s.settings.data_dir), viewer.name)
    except LoginError as exc:
        flash(request, str(exc))
        return RedirectResponse("/settings#bridge", status_code=303)
    s.db.audit("bridge_sign_in_started", actor_id=viewer.id)
    flash(request, "Open the sign-in page, sign in, then paste the code here.")
    return RedirectResponse("/settings#bridge", status_code=303)


@page(require="admin")
async def bridge_code(request: Request, viewer: Viewer) -> Response:
    """Hand the pasted code to the waiting sign-in. The code itself is never logged, saved or shown again."""
    s = services(request)
    form = await request.form()
    code = str(form.get("code") or "").strip()
    if not s.bridge_login.waiting():
        flash(request, "That sign-in isn't waiting anymore. Press Sign in again to get a new address.")
        return RedirectResponse("/settings#bridge", status_code=303)
    if not looks_like_code(code):
        flash(request, "That doesn't look like the code. Copy the whole code from the Claude page and paste it "
                       "again." if code else "Paste the code from the Claude page first.")
        return RedirectResponse("/settings#bridge", status_code=303)
    data_dir = s.settings.data_dir
    try:
        await asyncio.to_thread(s.bridge_login.finish, code, lambda: bridge_logged_in(data_dir))
    except LoginError as exc:
        s.db.audit("bridge_sign_in_failed", actor_id=viewer.id)
        flash(request, f"Not signed in. {exc}")
        return RedirectResponse("/settings#bridge", status_code=303)
    use_new_bridge(s)
    s.db.audit("bridge_signed_in", actor_id=viewer.id)
    flash(request, "Signed in to the Claude subscription." + (
        "" if BRIDGE_PROVIDER in s.settings.ai_providers else
        " Turn on Claude subscription under AI providers to use it."))
    return RedirectResponse("/settings#bridge", status_code=303)


@page(require="admin")
async def bridge_cancel(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    s.bridge_login.cancel()
    flash(request, "Sign-in stopped.")
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
    flash(request, f"Saved the {labels[name].lower()} layer. It applies to the next reply."
          if changed else f"Nothing changed in the {labels[name].lower()} layer.")
    return RedirectResponse(f"/settings#prompt-{name.removesuffix('.md')}", status_code=303)


@page(require="admin")
async def backup_now(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    name = await s.nightly.run(label="manual")
    s.db.audit("backup_made", actor_id=viewer.id, details={"file": name})
    flash(request, f"Backed up to {name}." if name else
          "The backup failed. The health list says why; check that the backups folder exists.")
    return RedirectResponse("/settings#health", status_code=303)


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
        flash(request, f"The safety check didn't start. {problem}")
        return RedirectResponse("/settings#model-check", status_code=303)
    s.model_checks_running[model] = utcnow()
    s.db.audit("model_check_started", actor_id=viewer.id, details={"model": model})
    s.tasks.spawn(_run_safety_check(s, model, viewer.id, viewer.name), name=f"model-check-{model}")
    flash(request, "Safety check started. Refresh in a minute.")
    return RedirectResponse("/settings#model-check", status_code=303)


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
