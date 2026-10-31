"""Settings that live in config.env, changed from the Settings page.

Most settings the Settings page changes are saved in the database (saved_settings.py). The ones here have to be in
config.env: secrets (Slack tokens), and settings the app needs before it opens the database or that it builds its
connections from at startup. Saving writes config.env in one step and never logs or shows a secret.

Each field says whether it applies right away or after a restart. Restart-only changes wait in
`Services.pending_restart` until the app restarts (the Restart button on the Settings page does that).
An environment variable on the mini PC wins over config.env, so a field set that way is shown but locked.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ai_backend import BACKENDS, resolve_backend
from .config import ENV_NAMES, MODEL_ID_SETTINGS, Settings, model_provider
from .connectors import REPO_RE
from .setup_flow import address_ok

CHANNEL_RE = re.compile(r"[CG][A-Z0-9]{2,}")


@dataclass(frozen=True)
class EnvField:
    name: str
    label: str
    kind: str  # text, url, base-url, int, bool, choice, secret, path, channel, repo, timezone
    help: str = ""
    restart: bool = False  # True: saved now, used after the next restart
    minimum: int = 0
    maximum: int = 0
    choices: tuple[tuple[str, str], ...] = ()
    optional: bool = False  # may be empty (a path or text), or removed (a secret)
    prefix: str = ""  # for secrets: what a real one starts with

    @property
    def env(self) -> str:
        return ENV_NAMES.get(self.name, self.name.upper())


BACKEND_CHOICES = (
    ("anthropic", "Anthropic API: Claude models only"),
    ("pi", "AI helper: Claude, OpenAI, Google, OpenRouter and the Claude subscription"),
    ("fake", "Pretend AI with canned replies (testing only)"),
)
assert tuple(c for c, _ in BACKEND_CHOICES) == BACKENDS

ENV_SECTIONS: dict[str, tuple[str, str, list[EnvField]]] = {
    "engine": ("AI engine", "Which program sends messages to the AI companies.", [
        EnvField("ai_backend", "AI engine", "choice",
                 "The AI helper is a small Node.js program that came with SubZero AI. It's needed for any company "
                 "besides Anthropic. After switching and restarting, press Run checks under Connected accounts.",
                 restart=True, choices=BACKEND_CHOICES),
        EnvField("pi_request_timeout", "Longest AI helper reply, in seconds", "int",
                 "Only for the AI helper. A reply that takes longer is stopped.", restart=True,
                 minimum=30, maximum=1800),
        EnvField("pi_idle_timeout", "Longest pause in an AI helper reply, in seconds", "int",
                 "Only for the AI helper. A reply that goes quiet for longer is stopped.", restart=True,
                 minimum=10, maximum=900),
        EnvField("pi_node", "Node.js program", "path",
                 "Leave empty to use the copy that came with SubZero AI.", restart=True, optional=True),
        EnvField("pi_gateway_script", "AI helper script", "path",
                 "Leave empty to use pi_gateway\\gateway.mjs in the SubZero AI folder.", restart=True, optional=True),
        EnvField("anthropic_base_url", "Anthropic API address", "base-url",
                 "Leave it as https://api.anthropic.com unless Anthropic tells you otherwise.", restart=True),
    ]),
    "site": ("Site and server", "The site's address, its clock, file limits and backups.", [
        EnvField("public_url", "Site address", "url",
                 "The https:// address people use, from the Cloudflare Tunnel. If it changes, also change the "
                 "Redirect URL in the Slack app (OAuth & Permissions) to the new address plus /auth/slack/callback, "
                 "or Sign in with Slack stops working.", restart=True),
        EnvField("app_name", "App name", "text", "Shown in the browser tab and the sidebar. Keep the team's name "
                 "and number out of it.", maximum=40),
        EnvField("timezone", "Time zone", "timezone",
                 "Used for message times, daily limits and the nightly backup. Like America/Chicago.", restart=True),
        EnvField("max_text_file_kb", "Largest text file, in KB", "int",
                 "Bigger text files cost more on every reply in the chat.", minimum=10, maximum=2000),
        EnvField("backup_dir", "Backups folder", "path",
                 "Leave empty for the backups folder in the data folder. A synced OneDrive or Google Drive folder "
                 "keeps a copy off the mini PC.", optional=True),
        EnvField("backup_hour", "Nightly backup hour", "int", "0 to 23, in the time zone above.", maximum=23),
        EnvField("check_for_updates", "Check for new versions once a day", "bool"),
        EnvField("update_repo", "Where new versions come from", "repo", "A GitHub repository as owner/name."),
    ]),
    "slack": ("Slack connection", "The Slack app that signs people in, posts review cards and answers messages.", [
        EnvField("slack_bot_token", "Bot token", "secret",
                 "Starts with xoxb-. In the Slack app's settings, under OAuth & Permissions.",
                 restart=True, prefix="xoxb-"),
        EnvField("slack_app_token", "App-level token", "secret",
                 "Starts with xapp-. Under Basic Information, App-Level Tokens, with the connections:write scope.",
                 restart=True, prefix="xapp-"),
        EnvField("slack_client_id", "Client ID", "text", "Under Basic Information, App Credentials.", maximum=40),
        EnvField("slack_client_secret", "Client secret", "secret", "Under Basic Information, App Credentials."),
        EnvField("slack_approvals_channel", "Mentor review channel", "channel",
                 "The private channel where review cards and mentor alerts go. The app must be invited to it "
                 "(type /invite @ and the app's name in the channel)."),
        EnvField("allow_slack_guests", "Slack guest accounts can sign in", "bool",
                 "Guests are often parents, sponsors, alumni or people from other teams, so this starts off."),
    ]),
}

# How a section is named in the middle of a sentence ("Saved Slack connection.")
SECTION_NAMES = {"engine": "AI engine", "site": "site and server", "slack": "Slack connection"}
assert set(SECTION_NAMES) == set(ENV_SECTIONS)

ENV_FIELDS: dict[str, EnvField] = {f.name: f for _, _, fields in ENV_SECTIONS.values() for f in fields}

# Settings that stay in config.env only, and why. Shown on the Settings page so nothing is a mystery.
FILE_ONLY = [
    ("DATA_DIR", "Where everything is kept. Moving it means moving the files, so it's done on the mini PC."),
    ("HOST and PORT", "The Cloudflare Tunnel points at 127.0.0.1:8787. Changing them here would cut off this page."),
    ("DEV_LOGIN", "Test sign-in lets anyone on the mini PC sign in as anyone. It must never be one click away."),
    ("SESSION_SECRET", "Made automatically. Changing it signs everyone out."),
]


def env_locked(field: EnvField) -> bool:
    """An environment variable sets it, which wins over config.env, so a saved change would be ignored."""
    if field.name == "ai_backend" and "FAKE_CLAUDE" in os.environ:
        return True
    return field.env in os.environ


def running_value(settings: Settings, name: str) -> Any:
    if name == "ai_backend":
        return resolve_backend(settings)
    return getattr(settings, name)


def serialize(field: EnvField, value: Any) -> str:
    if field.kind == "bool":
        return "1" if value else "0"
    return str(value)


def shown_value(settings: Settings, pending: Mapping[str, str], name: str) -> str:
    """What the form shows: a change waiting for a restart, else the value in use. Never used for secrets."""
    if name in pending:
        return pending[name]
    return serialize(ENV_FIELDS[name], running_value(settings, name))


def secret_hint(value: str) -> str:
    """The last four characters of a saved secret, so an admin can tell which one is saved."""
    return f"ends in {value[-4:]}" if len(value) >= 12 else ""


def _clean_path(raw: str, *, must_exist: bool) -> tuple[str, str]:
    path = Path(raw)
    if not path.is_absolute():
        return "", "Use the full path, like C:\\Folder\\file."
    if must_exist and not path.is_file():
        return "", "There's no file there."
    return str(path), ""


def check_field(field: EnvField, raw: str, settings: Settings) -> tuple[Any, str]:
    """Check one submitted value. Returns (value, error)."""
    if "\n" in raw or "\r" in raw:
        return None, "Paste it again on one line."
    kind = field.kind
    if kind == "bool":
        return raw in ("1", "on", "true"), ""
    if kind == "int":
        try:
            number = int(raw.replace(",", ""))
        except ValueError:
            return None, "Enter a whole number."
        if not field.minimum <= number <= field.maximum:
            return None, f"Enter a number from {field.minimum:,} to {field.maximum:,}."
        return number, ""
    if kind == "choice":
        if raw not in {c for c, _ in field.choices}:
            return None, "Pick one of the choices."
        return raw, ""
    if not raw:
        return ("", "") if field.optional else (None, "This can't be empty.")
    if kind == "text":
        if len(raw) > field.maximum:
            return None, f"Keep it under {field.maximum} characters."
        return raw, ""
    if kind == "url":
        url = raw.rstrip("/")
        if "://" not in url:
            url = "https://" + url
        if not address_ok(url):
            return None, "Enter the https:// address people use, like https://ai.yourteam.org, with no page path."
        return url, ""
    if kind == "base-url":
        parsed = urlparse(raw)
        local = parsed.hostname in ("localhost", "127.0.0.1")
        if not parsed.hostname or not (parsed.scheme == "https" or (parsed.scheme == "http" and local)):
            return None, "Enter an https:// address."
        return raw.rstrip("/"), ""
    if kind == "timezone":
        try:
            ZoneInfo(raw)
        except (ZoneInfoNotFoundError, ValueError):
            return None, "That isn't a time zone name. Use one like America/Chicago or America/New_York."
        return raw, ""
    if kind == "repo":
        if not REPO_RE.fullmatch(raw):
            return None, "Write it as owner/name."
        return raw, ""
    if kind == "channel":
        if not CHANNEL_RE.fullmatch(raw):
            return None, "Paste the channel ID. It starts with C or G (open the channel's details to find it)."
        return raw, ""
    if kind == "path":
        return _clean_path(raw, must_exist=field.name != "backup_dir")
    if kind == "secret":
        if re.search(r"\s", raw):
            return None, "It has a space in it. Copy it again."
        if not 8 <= len(raw) <= 512:
            return None, "That's too short or too long. Copy the whole thing again."
        if field.prefix and not raw.startswith(field.prefix):
            return None, f"It starts with {field.prefix}. Copy the right one."
        return raw, ""
    raise ValueError(kind)


def validate(section: str, form: Mapping[str, Any], settings: Settings,
             pending: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    """Check one config.env section's form. Returns (changed values, errors), keyed by setting name.
    A secret box left empty keeps the saved secret; a locked field is skipped."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for f in ENV_SECTIONS[section][2]:
        if env_locked(f):
            continue
        raw = str(form.get(f.name, "") or "").strip()
        if f.kind == "secret":
            if f.optional and form.get(f"remove_{f.name}") == "1":
                if getattr(settings, f.name):
                    values[f.name] = ""
                continue
            if not raw:
                continue
        elif f.kind != "bool" and raw == shown_value(settings, pending, f.name):
            continue  # unchanged, even if it wouldn't pass today's checks (like http://localhost in test mode)
        value, problem = check_field(f, raw, settings)
        if problem:
            errors[f.name] = problem
            continue
        if f.kind == "secret":
            if value != getattr(settings, f.name):
                values[f.name] = value
        elif serialize(f, value) != shown_value(settings, pending, f.name):
            values[f.name] = value
    if "ai_backend" in values:
        problem = backend_problem(values["ai_backend"], settings)
        if problem:
            errors["ai_backend"] = problem
            del values["ai_backend"]
    return values, errors


def backend_problem(backend: str, settings: Settings) -> str:
    """Why the app can't switch to this engine with the models and providers in use, or ''."""
    if backend != "anthropic":
        return ""
    if "claude-bridge" in settings.ai_providers:
        return ("The Claude subscription only works with the AI helper. Turn it off under AI providers in the "
                "Claude settings first.")
    from .saved_settings import FIELDS
    for name in MODEL_ID_SETTINGS:
        model = getattr(settings, name)
        if model_provider(model) not in ("anthropic", ""):
            label = FIELDS[name].label.lower() if name in FIELDS else name
            return f"The {label} is {model}, which needs the AI helper. Change it to an anthropic/ model first."
    return ""


def env_updates(values: Mapping[str, Any]) -> dict[str, str]:
    """The config.env lines for a set of changed values."""
    updates = {ENV_FIELDS[name].env: serialize(ENV_FIELDS[name], value) for name, value in values.items()}
    if "ai_backend" in values:
        updates["FAKE_CLAUDE"] = "1" if values["ai_backend"] == "fake" else "0"
    return updates


def describe(field: EnvField, before: Any, after: Any) -> tuple[str, str]:
    """A secret's before and after for the audit log: never the secret, only that it changed."""
    return ("saved" if before else "none"), ("removed" if not after else "replaced" if before else "saved")
