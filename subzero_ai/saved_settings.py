"""Settings that admins change on the Settings page.

They're saved in the database, win over config.env, and apply to the next request without a restart.
The three system prompt layers are saved as files in DATA_DIR/prompts, which the app already reads.
Every change goes in the audit log with the old and new value.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .ai_backend import resolve_backend
from .config import MODEL_ID_SETTINGS, PROVIDERS, Settings, convert, normalize_model_id
from .db import Database, utcnow
from .generation import PACKAGE_PROMPTS, PROMPT_FILES
from .model_check import requirement_problem

log = logging.getLogger("subzero.settings")

EFFORT_CHOICES = (("low", "Low"), ("medium", "Medium"), ("high", "High"))
SANDBOX_CHOICES = (("off", "Off: commands run on the computer (as before)"),
                   ("hyperv", "Hyper-V: each session gets its own virtual computer"),
                   ("fake", "Fake: this computer, no isolation (testing only)"))
SWITCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,62}")
# The dropdown value that means "type the model id under Other"; never a real model id
OTHER_MODEL = "__other__"
# provider/model. The model part can hold more slashes (OpenRouter ids look like meta-llama/llama-4).
MODEL_RE = re.compile(r"([a-z0-9][a-z0-9_-]{0,30})/([A-Za-z0-9][A-Za-z0-9._:/-]{0,99})")
PROVIDER_LABELS = {"anthropic": "Anthropic (Claude)", "openai": "OpenAI", "google": "Google (Gemini)",
                   "openrouter": "OpenRouter", "claude-bridge": "Claude subscription (Claude Code)"}
# A note shown under a provider's checkbox
PROVIDER_HELP = {
    "claude-bridge": ("Replies use the logged-in Claude subscription's limits. When they run out, students get an "
                      "error until it resets, so keep an API-key model as the hard-problem fallback. It has to pass "
                      "the safety check like any model. Sign in once under Claude subscription in AI and keys. "
                      "Needs AI_BACKEND=pi."),
}
SLACK_ID_RE = re.compile(r"[UW][A-Z0-9]{2,}")
# These cost more and need 30-day data retention at Anthropic, so the plan keeps them off.
BLOCKED_MODEL_WORDS = ("fable", "mythos")


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str  # model, choice, int, bool, lines, ids, providers, path, name
    help: str = ""
    minimum: int = 0
    maximum: int = 0
    optional: bool = False  # for ids and path: an empty value is allowed
    choices: tuple[tuple[str, str], ...] = ()  # for choice: (saved value, label) pairs
    error: str = ""  # for path and name: what a value that isn't allowed looks like, in plain words


SECTIONS: dict[str, tuple[str, list[Field]]] = {
    "claude": ("Claude", [
        Field("ai_providers", "AI providers", "providers",
              "Companies whose models can be used. Add each one's API key under API keys below. Anything besides "
              "Anthropic also needs the AI helper (set in config.env)."),
        Field("model_default", "Default model", "model",
              "Every new chat uses this model. Write it as provider/model, like anthropic/claude-sonnet-5-5."),
        Field("model_hard", "Hard-problem model", "model",
              "Mentors can switch a chat to this model. It costs about twice as much."),
        Field("model_titles", "Title model", "model", "Writes the short titles in the chat list."),
        Field("model_check_judge", "Safety check judge", "model",
              "Grades the answers in the Model safety check. It has to be a Claude model, since it decides which "
              "other models are safe enough for students."),
        Field("effort_default", "Everyday effort", "choice", "Higher effort gives more careful answers and costs more.",
              choices=EFFORT_CHOICES),
        Field("effort_high", "Effort for explainers and files", "choice",
              "Used for interactive explainers and messages with files attached, which are usually code reviews.",
              choices=EFFORT_CHOICES),
        Field("max_tokens_chat", "Longest reply, in tokens", "int", "8,000 tokens is about 6,000 words.", 500, 64000),
        Field("max_tokens_explainer", "Longest explainer reply, in tokens", "int",
              "An explainer is a whole web page, so it needs more room.", 1000, 64000),
        Field("long_chat_tokens", "Suggest a fresh chat after this many tokens", "int",
              "Long chats cost more on every reply.", 10000, 1000000),
        Field("web_search_enabled", "Claude can search the web when asked", "bool",
              "Web search and Research in the composer's tools menu. Each search costs about a cent."),
        Field("web_search_daily_limit", "Web searches per day, for the whole team", "int",
              "A web search reply uses up to 5, a Research report up to 20. Resets at midnight.", 0, 2000),
        Field("research_for_students", "Students can ask for Research reports", "bool",
              "Research runs many searches and writes a long report, so it starts as mentors only."),
    ]),
    "limits": ("Limits and review", [
        Field("limit_pending_per_student", "Waiting messages per student", "int",
              "How many messages one student can have waiting for review at once.", 1, 20),
        Field("limit_approved_per_day", "Approved messages per student per day", "int",
              "The count resets at midnight.", 1, 500),
        Field("reminder_minutes", "Remind mentors after this many minutes", "int",
              "Posts one reminder in the card's Slack thread.", 10, 1440),
        Field("mentors_need_approval", "Mentors' messages also wait for another mentor", "bool"),
        Field("slack_dms_enabled", "Let people message the bot in Slack", "bool",
              "Messages sent to the bot in Slack wait for a mentor exactly like messages on the website."),
        Field("show_pending_to_team", "Other students can see messages that are waiting", "bool"),
        Field("show_rejected_to_team", "Other students can see rejected messages and the reason", "bool"),
    ]),
    "window": ("Approval window", [
        Field("approval_window_minutes", "Minutes a student can keep chatting after an approval", "int",
              "After a mentor approves a message, the student's next messages in that chat go straight to Claude "
              "until they stop for this long. The first message of every chat, messages with files, and anything "
              "after a rejection or mentor alert still wait. 0 turns this off.", 0, 120),
        Field("screener_enabled", "Screen messages in the window with a small model first", "bool",
              "Anything it flags waits for a mentor, and so does everything if it can't be reached. Costs a "
              "fraction of a cent per message."),
        Field("model_screener", "Screener model", "model", "A small, fast model is enough."),
        Field("always_review_slack_ids", "Students who always wait for a mentor", "ids",
              "Slack member IDs, one per line. These students never get the approval window. In Slack, click a "
              "person's name, open the three-dot menu, and choose Copy member ID.", optional=True),
    ]),
    "data": ("Data and backups", [
        Field("retention_days", "Delete chats with no activity for this many days", "int",
              "730 days keeps this season and the next. 0 keeps chats forever.", 0, 3650),
        Field("backup_keep_days", "Keep nightly backups for this many days", "int", "", 1, 365),
    ]),
    "code": ("Code", [
        Field("code_commands", "Commands Claude can ask to run", "lines",
              "One program name per line, like gradlew. Claude can only ask to run these, from the repository "
              "folder, and a mentor approves each command before it runs."),
        Field("code_command_timeout", "Stop a command after this many seconds", "int",
              "A full Gradle build on the mini PC can take a few minutes.", 30, 3600),
        Field("code_sandbox", "Run Code commands in a sandbox", "choice",
              "Hyper-V gives each Code session its own virtual computer on an internal network, deleted when the "
              "session ends. Fake runs commands on this computer with no protection, so use it only for testing. "
              "Changing this applies to the next command.", choices=SANDBOX_CHOICES),
        Field("sandbox_ttl_hours", "Delete a sandbox this many hours after it was made", "int",
              "Even if its session is still waiting. The session's copy of the repository is kept, and a new "
              "sandbox is made when it's needed again.", 1, 168),
        Field("sandbox_max_sessions", "Sandboxes that can run at the same time", "int",
              "A new session waits for a free sandbox.", 1, 10),
        Field("sandbox_memory_mb", "Memory for each sandbox, in MB", "int",
              "Applies to sandboxes made after the change.", 1024, 16384),
        Field("sandbox_cpus", "Processors for each sandbox", "int",
              "Applies to sandboxes made after the change.", 1, 8),
        Field("sandbox_disk_gb", "Disk space each sandbox can use, in GB", "int",
              "The base disk must be this small or smaller.", 5, 100),
        Field("sandbox_min_free_gb", "Keep this much disk free, in GB", "int",
              "No new sandbox is made while the disk holding the base image has less free space than this.", 1, 500),
        Field("sandbox_base_image", "Base disk for sandboxes", "path",
              "The full path to the base disk file (.vhdx) on the mini PC. Leave empty unless sandboxes are on.",
              optional=True, error="Use the full path to the disk file, like C:\\SubZeroAI\\sandbox\\base.vhdx."),
        Field("sandbox_switch", "Internal network switch for sandboxes", "name",
              "The Hyper-V internal switch sandboxes are attached to, with the host on 10.77.0.1. The host must "
              "firewall that adapter. A sandbox on any other switch is never started.",
              error="Use letters, numbers, spaces, dots, dashes and underscores, up to 63 characters."),
        Field("sandbox_proxy_port", "Host port sandboxes may reach", "int",
              "The one port on this PC that a sandbox may reach over the internal switch, for the egress proxy. "
              "The sandbox network setup script and the check both use it.", 1, 65535),
    ]),
    "people": ("Subteams and admins", [
        Field("subteams", "Subteams", "lines", "One per line. Students pick one when they start a chat."),
        Field("admin_slack_ids", "Admins' Slack member IDs", "ids",
              "One per line. In Slack, click a person's name, open the three-dot menu, and choose Copy member ID. "
              "Admins can change these settings and read the audit log."),
    ]),
}

FIELDS: dict[str, Field] = {f.name: f for _, fields in SECTIONS.values() for f in fields}

PROMPT_LAYERS: list[tuple[str, str, str]] = [
    ("safety.md", "Safety and conduct",
     "The child-safety rules: age-appropriate content, honesty about being an AI, privacy, shop safety, "
     "and MENTOR ALERT. Ask a coach to review any change."),
    ("team.md", "Team context", "The robot code language and framework, units, shop rules, and conventions."),
    ("formats.md", "Output formats", "How Claude formats code and interactive explainers."),
]
assert [name for name, _, _ in PROMPT_LAYERS] == list(PROMPT_FILES)


# --------------------------------------------------------------------------- values
def serialize(name: str, value: Any) -> str:
    kind = FIELDS[name].kind
    if kind == "bool":
        return "1" if value else "0"
    if kind in ("lines", "providers"):
        return "\n".join(value)
    if kind == "ids":
        return "\n".join(sorted(value))
    return str(value)


def current(settings: Settings, name: str) -> str:
    return serialize(name, getattr(settings, name))


def load_into(settings: Settings, db: Database) -> None:
    """Apply the values saved on the Settings page over the ones from config.env."""
    for row in db.all("SELECT key, value FROM settings"):
        if row["key"] not in FIELDS:
            continue
        try:
            setattr(settings, row["key"], convert(row["key"], row["value"]))
        except (ValueError, StopIteration):
            log.warning("Ignoring a saved setting that couldn't be read: %s", row["key"])


class _FormView:
    """The submitted form with some dropdown choices swapped for the values typed under Other."""

    def __init__(self, form: Mapping[str, Any], overrides: dict[str, str]):
        self._form = form
        self._overrides = overrides

    def get(self, name: str, default: Any = None) -> Any:
        return self._overrides[name] if name in self._overrides else self._form.get(name, default)

    def getlist(self, name: str) -> list[Any]:
        return [self._overrides[name]] if name in self._overrides else self._form.getlist(name)

    def __contains__(self, name: object) -> bool:
        return name in self._overrides or name in self._form


def model_choices_form(form: Mapping[str, Any], section: str) -> Mapping[str, Any]:
    """Model dropdowns post the chosen model, or OTHER_MODEL plus the typed id in `<name>_other`. Returns the form
    with the typed id in place of OTHER_MODEL, so validate() checks it exactly as a typed box."""
    overrides = {}
    for f in SECTIONS[section][1]:
        if f.kind == "model" and form.get(f.name) == OTHER_MODEL:
            overrides[f.name] = str(form.get(f"{f.name}_other", "") or "")
    return _FormView(form, overrides) if overrides else form


def form_text(form: Mapping[str, Any], name: str) -> str:
    """A form value as text. A field with several values (checkboxes) comes back one per line."""
    getlist = getattr(form, "getlist", None)
    if getlist is not None:
        return "\n".join(str(v) for v in getlist(name))
    return str(form.get(name, "") or "")


def check_path(raw: str) -> bool:
    """An absolute Windows or POSIX path with no control characters or quotes that could break a script."""
    if len(raw) > 260 or any(ord(ch) < 32 or ch in "\"`$;'" for ch in raw):
        return False
    return PureWindowsPath(raw).is_absolute() or PurePosixPath(raw).is_absolute()


def check_providers(raw: str) -> tuple[tuple[str, ...], str]:
    """Turn the submitted provider list into (providers in a fixed order, error)."""
    items = [part.lower() for part in re.split(r"[\s,]+", raw) if part]
    unknown = [item for item in items if item not in PROVIDERS]
    if unknown:
        return (), f"{unknown[0]} isn't a provider we support. Choose from {', '.join(PROVIDERS)}."
    chosen = tuple(p for p in PROVIDERS if p in items)
    if not chosen:
        return (), "Turn on at least one provider."
    return chosen, ""


def check_model(raw: str, providers, backend: str) -> tuple[str, str]:
    """Check one model id. Returns (normalized id, error)."""
    model = normalize_model_id(raw)
    match = MODEL_RE.fullmatch(model)
    if not match:
        return "", "Use provider/model, like anthropic/claude-sonnet-5-5."
    provider = match.group(1)
    if any(word in model.lower() for word in BLOCKED_MODEL_WORDS):
        return "", ("Fable and Mythos models are kept off: they cost more and Anthropic keeps their "
                    "data for 30 days.")
    if provider not in PROVIDERS:
        return "", f"{provider} isn't a provider we support. Use {', '.join(PROVIDERS)}."
    if provider not in providers:
        return "", f"{PROVIDER_LABELS[provider]} isn't turned on. Turn it on under AI providers first."
    if backend == "anthropic" and provider != "anthropic":
        return "", ("Only anthropic/ models work with AI_BACKEND=anthropic. Set AI_BACKEND=pi in config.env to use "
                    "other companies.")
    return model, ""


def judge_ok(model: str) -> bool:
    """The safety check's judge decides which models students can use, so it must be a Claude model."""
    provider, _, name = normalize_model_id(model).partition("/")
    return provider in ("anthropic", "claude-bridge") and name.startswith("claude-")


# The settings a model can only be newly set for after passing the Model safety check (True: the screener part)
CHECKED_MODEL_SETTINGS = {"model_default": False, "model_hard": False, "model_screener": True}


def validate(section: str, form: Mapping[str, Any], *, actor_slack_id: str = "",
             settings: Settings | None = None, db: Database | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """Check one section's form. Returns (values, errors), keyed by setting name.
    Pass the current settings so models are checked against the providers that are on and the AI backend.
    Pass the database to also require the Model safety check for a changed default, hard-problem or screener
    model (a value that isn't changing is left alone)."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    fields = SECTIONS[section][1]
    providers: tuple[str, ...] = tuple(settings.ai_providers) if settings is not None else ("anthropic",)
    backend = resolve_backend(settings) if settings is not None else "anthropic"
    if any(f.name == "ai_providers" for f in fields) and "ai_providers" in form:  # absent means unchanged
        chosen, problem = check_providers(form_text(form, "ai_providers"))
        if problem:
            errors["ai_providers"] = problem
        else:
            providers = values["ai_providers"] = chosen
            if ("claude-bridge" in chosen and settings is not None and "claude-bridge" not in settings.ai_providers
                    and backend != "pi"):
                errors["ai_providers"] = ("The Claude subscription needs the AI helper. Set AI_BACKEND=pi in "
                                          "config.env and restart, then turn it on.")
                del values["ai_providers"]
                providers = tuple(settings.ai_providers)
    for f in fields:
        raw = str(form.get(f.name, "") or "").strip()
        if f.kind == "bool":
            values[f.name] = raw in ("1", "on", "true")
        elif f.kind == "int":
            try:
                number = int(raw.replace(",", ""))
            except ValueError:
                errors[f.name] = "Enter a whole number."
                continue
            if not f.minimum <= number <= f.maximum:
                errors[f.name] = f"Enter a number from {f.minimum:,} to {f.maximum:,}."
                continue
            values[f.name] = number
        elif f.kind == "model":
            if f.name == "model_check_judge" and f.name not in form:
                continue  # added in 0.7: a form from before then leaves it alone
            model, problem = check_model(raw, providers, backend)
            if not problem and db is not None and f.name in CHECKED_MODEL_SETTINGS:
                unchanged = settings is not None and model == normalize_model_id(getattr(settings, f.name))
                if not unchanged:
                    problem = requirement_problem(db, model, screener=CHECKED_MODEL_SETTINGS[f.name])
            if not problem and f.name == "model_check_judge" and not judge_ok(model):
                problem = "The judge has to be a Claude model, like anthropic/claude-sonnet-5-5."
            if problem:
                errors[f.name] = problem
            else:
                values[f.name] = model
        elif f.kind == "providers":
            continue  # checked above
        elif f.kind in ("path", "name"):
            if f.optional and not raw:
                values[f.name] = ""
            elif (f.kind == "path" and not check_path(raw)) or (f.kind == "name" and not SWITCH_RE.fullmatch(raw)):
                errors[f.name] = f.error
            else:
                values[f.name] = raw
        elif f.kind == "choice":
            allowed = [value for value, _ in f.choices]
            if raw not in allowed:
                joined = ", ".join(allowed[:-1]) + (", or " if len(allowed) > 2 else " or ") + allowed[-1]
                errors[f.name] = f"Pick {joined}."
            else:
                values[f.name] = raw
        elif f.kind == "lines":
            items = tuple(dict.fromkeys(line.strip() for line in raw.splitlines() if line.strip()))
            if not items:
                errors[f.name] = "Add at least one."
            elif len(items) > 12 or any(len(item) > 40 for item in items):
                errors[f.name] = "Use up to 12, each up to 40 characters."
            else:
                values[f.name] = items
        elif f.kind == "ids":
            items = [part.strip() for part in re.split(r"[\s,]+", raw) if part.strip()]
            bad = [item for item in items if not SLACK_ID_RE.fullmatch(item)]
            if bad:
                errors[f.name] = f"{bad[0]} isn't a Slack member ID. They start with U or W, like U012AB3CD."
            elif not items and not f.optional:
                errors[f.name] = "Name at least one admin."
            elif (f.name == "admin_slack_ids" and actor_slack_id and SLACK_ID_RE.fullmatch(actor_slack_id)
                  and actor_slack_id not in items):
                errors[f.name] = "You can't remove yourself. Ask another admin to do it."
            else:
                values[f.name] = frozenset(items)
    if "ai_providers" in values and settings is not None:
        # A provider can't be turned off while a model setting still uses it
        in_form = {f.name for f in fields}
        for name in MODEL_ID_SETTINGS:
            if name in in_form:
                model = values.get(name, "")  # empty when it failed its own check, which is reported already
            else:
                model = getattr(settings, name)
            provider = model.split("/", 1)[0] if "/" in model else ""
            if provider and provider not in values["ai_providers"]:
                errors["ai_providers"] = (f"{PROVIDER_LABELS.get(provider, provider)} is still used by "
                                          f"{FIELDS[name].label.lower()}. Change that model first.")
                break
        if "ai_providers" in errors:
            del values["ai_providers"]
    return values, errors


def save(db: Database, settings: Settings, values: Mapping[str, Any], actor_id: int | None) -> list[str]:
    """Save the values that changed, apply them now, and log each change. Returns the changed names."""
    changed = []
    for name, value in values.items():
        before, after = current(settings, name), serialize(name, value)
        if before == after:
            continue
        db.execute("INSERT INTO settings (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by, "
                   "updated_at = excluded.updated_at", (name, after, actor_id, utcnow()))
        setattr(settings, name, value)
        db.audit("setting_changed", actor_id=actor_id, details={"setting": FIELDS[name].label, "from": before,
                                                                 "to": after})
        changed.append(name)
    return changed


# --------------------------------------------------------------------------- prompt layers
def prompt_layer(settings: Settings, name: str) -> tuple[str, bool]:
    """A layer's text, and whether it's been changed from the built-in version."""
    override = settings.data_dir / "prompts" / name
    if override.exists():
        return override.read_text(encoding="utf-8"), True
    return (PACKAGE_PROMPTS / name).read_text(encoding="utf-8"), False


def builtin_prompt(name: str) -> str:
    return (PACKAGE_PROMPTS / name).read_text(encoding="utf-8")


def save_prompt(db: Database, settings: Settings, name: str, text: str, actor_id: int | None) -> bool:
    """Save a layer. Text that matches the built-in version removes the override. Returns True if it changed."""
    if name not in PROMPT_FILES:
        raise ValueError(name)
    text = text.replace("\r\n", "\n").strip() + "\n"
    before, _ = prompt_layer(settings, name)
    if before.strip() == text.strip():
        return False
    override = settings.data_dir / "prompts" / name
    if text.strip() == builtin_prompt(name).strip():
        override.unlink(missing_ok=True)
    else:
        override.parent.mkdir(parents=True, exist_ok=True)
        temp = override.with_name(name + ".tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, override)
    label = next(label for n, label, _ in PROMPT_LAYERS if n == name)
    db.audit("prompt_changed", actor_id=actor_id, details={"layer": label, "before": before.strip(),
                                                            "after": text.strip()})
    return True
