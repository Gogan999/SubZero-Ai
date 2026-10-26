"""Runs Claude's reply to an approved message and streams it to everyone watching."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from .attachments import Attachments
from .config import Settings
from .db import Database, utcnow
from .live import Broker, Tasks
from .store import COMPLETE, ERROR, SENT_TO_CLAUDE, STREAMING, Store

log = logging.getLogger("subzero.generation")

ALERT_MARK = "[MENTOR ALERT]"
# Pictures from earlier messages re-sent with each reply (the newest message always sends all of its own)
HISTORY_IMAGES = 20
PACKAGE_PROMPTS = Path(__file__).parent / "prompts"
PROMPT_FILES = ("safety.md", "team.md", "formats.md")

EXPLAINER_REQUEST = (
    "The student may want an interactive explainer: their latest message mentions one or a simulation. "
    "Include one if it fits the formatting rules, plus a short explanation outside it.")

# Phrases that suggest a student wants something they can play with. A match only gives the reply room for a
# whole page; Claude still decides. Kept narrow so FRC words like "build artifact" or "AdvantageScope
# animation" don't count.
EXPLAINER_WORDS = re.compile(
    r"\b(explainers?|simulations?|simulate|interactive (graph|diagram|plot|chart|demo|model|page|explanation)s?"
    r"|something interactive)\b", re.I)


def asks_for_explainer(text: str) -> bool:
    return bool(EXPLAINER_WORDS.search(text or ""))

TITLE_SYSTEM = ("Write a title of 3 to 6 words for this robotics team chat. "
                "Reply with the title only, with no quotes and no ending punctuation.")


def load_system_prompt(settings: Settings) -> str:
    """The three layers: safety and conduct, team context, output formats.
    A file with the same name in DATA_DIR/prompts replaces the built-in one."""
    parts = []
    for name in PROMPT_FILES:
        override = settings.data_dir / "prompts" / name
        path = override if override.exists() else PACKAGE_PROMPTS / name
        parts.append(path.read_text(encoding="utf-8").strip())
    text = "\n\n".join(parts)
    return text.replace("{app_name}", settings.app_name)


def build_context(rows, up_to_id: int, files: dict[int, list[dict]] | None = None) -> list[dict]:
    """Messages Claude should see: approved student messages (with their attachments, given as content
    blocks in files) and finished replies, in order. Consecutive messages from the same side are merged
    so the turns alternate."""
    files = files or {}
    out: list[dict] = []
    for m in rows:
        if m["id"] > up_to_id:
            break
        if m["role"] == "user" and m["status"] in SENT_TO_CLAUDE:
            role, text = "user", m["content"]
        elif m["role"] == "assistant" and m["status"] == COMPLETE and m["content"].strip():
            role, text = "assistant", m["content"]
        else:
            continue
        blocks = files.get(m["id"]) if role == "user" else None
        content: str | list[dict] = [*blocks, {"type": "text", "text": text}] if blocks else text
        if out and out[-1]["role"] == role:
            out[-1]["content"] = _join(out[-1]["content"], content)
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _join(first: str | list[dict], second: str | list[dict]) -> str | list[dict]:
    if isinstance(first, str) and isinstance(second, str):
        return first + "\n\n" + second

    def as_blocks(content):
        return [{"type": "text", "text": content}] if isinstance(content, str) else content

    return as_blocks(first) + as_blocks(second)


def detect_alert(head: str, final: bool) -> tuple[bool, bool, str]:
    """Look at the start of a reply. Returns (decided, is_alert, text_to_show)."""
    stripped = head.lstrip()
    if stripped.startswith(ALERT_MARK):
        rest = stripped[len(ALERT_MARK):]
        if "\n" in rest:
            return True, True, rest.split("\n", 1)[1].lstrip("\n")
        if final:
            return True, True, rest.strip()
        return False, False, ""
    if ALERT_MARK.startswith(stripped) and not final:
        return False, False, ""
    return True, False, head


class ReplyRunner:
    def __init__(self, *, settings: Settings, db: Database, store: Store, claude, broker: Broker, tasks: Tasks,
                 attachments: Attachments):
        self.settings = settings
        self.attachments = attachments
        self.db = db
        self.store = store
        self.claude = claude
        self.broker = broker
        self.tasks = tasks
        self.on_alert: Callable[[int, int], Awaitable[None]] | None = None
        self.on_result: Callable[[bool, str], None] | None = None  # tells the health checks how the call went
        # Text streamed so far for replies in progress, so someone who opens the chat mid-reply sees all of it.
        self._live: dict[int, list[str]] = {}

    def live_text(self, reply_id: int) -> tuple[str, int]:
        """The text streamed so far and how many chunks it contains."""
        parts = self._live.get(reply_id, [])
        return "".join(parts), len(parts)

    def start(self, conversation_id: int, user_message_id: int) -> int:
        conv = self.store.conversation(conversation_id)
        with self.db.transaction() as conn:
            reply_id = self.store.add_message(
                conn, conversation_id=conversation_id, author_id=None, role="assistant", content="",
                status=STREAMING, reply_to=user_message_id, model=conv["model"])
        self.broker.publish(f"conv:{conversation_id}", {"type": "changed", "id": reply_id})
        self.tasks.spawn(self._run(conversation_id, user_message_id, reply_id), name=f"reply-{reply_id}")
        return reply_id

    def history_files(self, conversation_id: int, user_message_id: int) -> dict[int, list[dict]]:
        """Attachments for each message. Images and video stills are sent for the newest message, and for
        earlier ones only while the total stays under HISTORY_IMAGES, newest first. Sending every picture on
        every turn would cost a lot and, past 100 images or 32 MB, the API refuses the whole request."""
        by_message = self.attachments.for_conversation(conversation_id)
        budget = HISTORY_IMAGES
        out: dict[int, list[dict]] = {}
        for mid in sorted(by_message, reverse=True):
            rows = by_message[mid]
            pictures = sum(1 for r in rows if r["kind"] in ("image", "frame"))
            send = mid >= user_message_id or pictures <= budget
            if mid < user_message_id and send:
                budget -= pictures
            out[mid] = self.attachments.claude_blocks(rows, images=send)
        return out

    async def _run(self, conversation_id: int, user_message_id: int, reply_id: int) -> None:
        topic = f"conv:{conversation_id}"
        conv = self.store.conversation(conversation_id)
        files = self.history_files(conversation_id, user_message_id)
        context = build_context(self.store.messages(conversation_id), user_message_id, files)
        asked = self.store.message(user_message_id)
        wants_explainer = bool(asked and asked["wants_explainer"])
        # Explainers and questions with files attached (usually code reviews) get more effort, and explainers
        # get room for a whole web page. Everyday questions stay at the default, which costs less.
        if wants_explainer:
            max_tokens, effort = self.settings.max_tokens_explainer, self.settings.effort_high
        elif user_message_id in files:
            max_tokens, effort = self.settings.max_tokens_chat, self.settings.effort_high
        else:
            max_tokens, effort = self.settings.max_tokens_chat, self.settings.effort_default
        parts: list[str] = []
        self._live[reply_id] = parts
        head = ""
        decided = False
        alert = False
        error = ""
        usage: dict = {}

        def emit(text: str) -> None:
            if text:
                seq = len(parts)
                parts.append(text)
                self.broker.publish(topic, {"type": "delta", "id": reply_id, "text": text, "seq": seq})

        try:
            async for event in self.claude.stream(
                model=conv["model"], system=load_system_prompt(self.settings), messages=context,
                max_tokens=max_tokens, effort=effort, system_extra=EXPLAINER_REQUEST if wants_explainer else None,
            ):
                if event.kind == "text":
                    if decided:
                        emit(event.text)
                        continue
                    head += event.text
                    decided, alert, shown = detect_alert(head, final=False)
                    if decided:
                        if alert:
                            await self._raise_alert(conversation_id, user_message_id)
                        emit(shown)
                elif event.kind == "usage" and event.usage:
                    usage.update({k: v for k, v in event.usage.items() if v is not None})
                elif event.kind == "error":
                    error = event.error
                    break
            if not decided and head:
                _, alert, shown = detect_alert(head, final=True)
                if alert:
                    await self._raise_alert(conversation_id, user_message_id)
                emit(shown)
        except Exception as exc:  # never leave a reply stuck as "streaming"
            log.exception("Reply %s failed", reply_id)
            error = f"Something went wrong while Claude was replying ({type(exc).__name__})."

        text = "".join(parts).strip()
        status = COMPLETE if text and not error else ERROR
        if status == ERROR and not error:
            error = "Claude returned an empty reply."
        self.db.execute(
            "UPDATE messages SET content = ?, status = ?, error = ?, input_tokens = ?, output_tokens = ?, "
            "cache_read_tokens = ?, updated_at = ? WHERE id = ?",
            (text, status, error or None, usage.get("input_tokens"), usage.get("output_tokens"),
             usage.get("cache_read_input_tokens"), utcnow(), reply_id),
        )
        self._live.pop(reply_id, None)
        self.broker.publish(topic, {"type": "done", "id": reply_id})
        if self.on_result is not None:
            self.on_result(status == COMPLETE, error)
        if status == COMPLETE and not conv["title"]:
            await self._make_title(conversation_id)

    async def _raise_alert(self, conversation_id: int, user_message_id: int) -> None:
        """Hide the chat from the team before any of the reply is shown, then tell mentors."""
        if self.on_alert is None:
            return
        try:
            await self.on_alert(conversation_id, user_message_id)
        except Exception:
            log.exception("Couldn't raise the mentor alert for conversation %s", conversation_id)

    async def _make_title(self, conversation_id: int) -> None:
        first = self.db.one(
            "SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' AND status IN "
            "('approved','edited','auto') ORDER BY id LIMIT 1", (conversation_id,))
        if not first:
            return
        title = ""
        try:
            title = await self.claude.complete(
                model=self.settings.model_titles, system=TITLE_SYSTEM,
                messages=[{"role": "user", "content": first["content"][:2000]}], max_tokens=30)
        except Exception:
            log.warning("Couldn't generate a title for conversation %s", conversation_id, exc_info=True)
        title = title.strip().strip("\"'“”").rstrip(".!?:;").strip()
        if not title:
            title = " ".join(first["content"].split()[:6])
        title = title[:60]
        self.store.set_title(conversation_id, title)
        self.broker.publish(f"conv:{conversation_id}", {"type": "title", "title": title})
