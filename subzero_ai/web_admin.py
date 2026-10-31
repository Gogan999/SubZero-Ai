"""Admin pages: the Settings page (health, backups, settings, and the system prompt layers), the first-run
setup page, and the recovery sign-in. Setup and recovery answer only on the mini PC itself."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from . import __version__, model_check, saved_settings, setup_flow
from .ai_backend import resolve_backend
from .backup import list_backups, size_text
from .config import PROVIDERS, update_env_file
from .connectors import BRANCH_RE, REPO_RE, ConnectorError
from .db import utcnow
from .maintenance import update_available
from .pi_client import KEY_SETTINGS, provider_keys, settings_bridge_env
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

CHANNEL_RE = re.compile(r"[CG][A-Z0-9]{2,}")


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
                     submitted: dict[str, str] | None = None) -> dict[str, Any]:
    sections = []
    for key, (title, fields) in saved_settings.SECTIONS.items():
        items = []
        for f in fields:
            value = (submitted or {}).get(f.name)
            if value is None:
                value = saved_settings.current(s.settings, f.name)
            item = {"field": f, "value": value, "error": (errors or {}).get(f.name)}
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
    ai_keys = [{"id": p, "label": saved_settings.PROVIDER_LABELS[p], "field": KEY_SETTINGS[p],
                "set": bool(getattr(s.settings, KEY_SETTINGS[p])), "help": KEY_HELP[p]}
               for p in PROVIDERS if p in s.settings.ai_providers and p in KEY_SETTINGS]
    return {"health": health_rows(s), "sections": sections, "ai_keys": ai_keys, "safety": safety_context(s),
            "prompts": prompts, "history": history,
            "backups": backups, "backup_folder": str(s.settings.backups_path), "connectors": connectors}


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


@page(require="admin")
async def settings_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
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
    problem = ""
    for provider, key in typed.items():
        if provider not in s.settings.ai_providers:
            problem = (f"{saved_settings.PROVIDER_LABELS[provider]} isn't turned on. "
                       "Turn it on under Claude settings first.")
        else:
            problem = key_problem(provider, key)
        if problem:
            break
    if not typed:
        problem = "Paste a key into at least one box."
    if problem:
        flash(request, f"Keys weren't saved. {problem}")
        return RedirectResponse("/settings#ai-keys", status_code=303)
    _save_config(s, {KEY_ENV[p]: key for p, key in typed.items()})
    for provider, key in typed.items():
        setattr(s.settings, KEY_SETTINGS[provider], key)
    use_new_keys(s)
    refresh_model_names(s)
    names = [saved_settings.PROVIDER_LABELS[p] for p in typed]
    s.db.audit("ai_key_saved", actor_id=viewer.id, details={"providers": list(typed)})
    flash(request, "Saved the key for " + ", ".join(names) + ". It works from the next message.")
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
            client = s.slack_factory(values["bot"])
            try:
                auth = await client.auth_test()
            except Exception as exc:
                errors["slack"] = f"Slack didn't accept the bot token ({getattr(exc, 'error', '') or exc})."
            else:
                try:
                    await client.open_socket_url(values["app"])
                except Exception as exc:
                    errors["slack"] = (f"Slack didn't accept the app-level token ({getattr(exc, 'error', '') or exc}). "
                                       "Check that it has the connections:write scope.")
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
    Route("/settings/{section}", save_section, methods=["POST"]),
    Route("/setup", setup_page),
    Route("/setup/{step}", setup_step, methods=["POST"]),
    Route("/auth/recovery", recovery_login, methods=["GET", "POST"]),
]
