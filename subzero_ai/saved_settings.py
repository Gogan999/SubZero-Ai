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
from typing import Any

from .config import Settings, convert
from .db import Database, utcnow
from .generation import PACKAGE_PROMPTS, PROMPT_FILES

log = logging.getLogger("subzero.settings")

EFFORTS = ("low", "medium", "high")
MODEL_RE = re.compile(r"claude-[a-z0-9][a-z0-9.\-]*")
SLACK_ID_RE = re.compile(r"[UW][A-Z0-9]{2,}")
# These cost more and need 30-day data retention at Anthropic, so the plan keeps them off.
BLOCKED_MODEL_WORDS = ("fable", "mythos")


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str  # model, effort, int, bool, lines, ids
    help: str = ""
    minimum: int = 0
    maximum: int = 0
    optional: bool = False  # for ids: an empty list is allowed


SECTIONS: dict[str, tuple[str, list[Field]]] = {
    "claude": ("Claude", [
        Field("model_default", "Default model", "model", "Every new chat uses this model."),
        Field("model_hard", "Hard-problem model", "model",
              "Mentors can switch a chat to this model. It costs about twice as much."),
        Field("model_titles", "Title model", "model", "Writes the short titles in the chat list."),
        Field("effort_default", "Everyday effort", "effort", "Higher effort gives more careful answers and costs more."),
        Field("effort_high", "Effort for explainers and files", "effort",
              "Used for interactive explainers and messages with files attached, which are usually code reviews."),
        Field("max_tokens_chat", "Longest reply, in tokens", "int", "8,000 tokens is about 6,000 words.", 500, 64000),
        Field("max_tokens_explainer", "Longest explainer reply, in tokens", "int",
              "An explainer is a whole web page, so it needs more room.", 1000, 64000),
        Field("long_chat_tokens", "Suggest a fresh chat after this many tokens", "int",
              "Long chats cost more on every reply.", 10000, 1000000),
    ]),
    "limits": ("Limits and review", [
        Field("limit_pending_per_student", "Waiting messages per student", "int",
              "How many messages one student can have waiting for review at once.", 1, 20),
        Field("limit_approved_per_day", "Approved messages per student per day", "int",
              "The count resets at midnight.", 1, 500),
        Field("reminder_minutes", "Remind mentors after this many minutes", "int",
              "Posts one reminder in the card's Slack thread.", 10, 1440),
        Field("mentors_need_approval", "Mentors' messages also wait for another mentor", "bool"),
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
    if kind == "lines":
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


def validate(section: str, form: Mapping[str, Any], *, actor_slack_id: str = "") -> tuple[dict[str, Any], dict[str, str]]:
    """Check one section's form. Returns (values, errors), keyed by setting name."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for f in SECTIONS[section][1]:
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
            if not MODEL_RE.fullmatch(raw):
                errors[f.name] = "Enter a Claude model ID, like claude-sonnet-5-5."
            elif any(word in raw for word in BLOCKED_MODEL_WORDS):
                errors[f.name] = ("Fable and Mythos models are kept off: they cost more and Anthropic keeps their "
                                  "data for 30 days.")
            else:
                values[f.name] = raw
        elif f.kind == "effort":
            if raw not in EFFORTS:
                errors[f.name] = "Pick low, medium, or high."
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
