"""Runs Claude's reply to an approved message and streams it to everyone watching."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from .ai_backend import Capabilities, capabilities_for, resolve_backend
from .attachments import Attachments
from .config import Settings, normalize_model_id
from .db import Database, utcnow
from .live import Broker, Tasks
# Moved to prompts.py; these names stay importable from here (saved_settings and tests use them)
from .prompts import PACKAGE_PROMPTS, PROMPT_FILES, load_safety_prompt, load_system_prompt  # noqa: F401
from .store import COMPLETE, ERROR, SENT_TO_CLAUDE, STREAMING, Store

log = logging.getLogger("subzero.generation")

ALERT_MARK = "[MENTOR ALERT]"

TITLE_SYSTEM = ("Write a title of 3 to 6 words for this robotics team chat. "
                "Reply with the title only, with no quotes and no ending punctuation.")


def attached_note(count: int) -> str:
    """What the history says in place of files that used to be attached to a message."""
    return f"[{count} attached file{'s' if count != 1 else ''} not shown]"


def build_context(rows, up_to_id: int, attached: dict[int, int] | None = None) -> list[dict]:
    """Messages Claude should see: approved student messages and finished replies, in order. Old messages that
    had files attached get a short note saying so (`attached` is the number of files per message id), because
    files aren't sent to Claude any more. Consecutive messages from the same side are merged so the turns
    alternate."""
    attached = attached or {}
    out: list[dict] = []
    for m in rows:
        if m["id"] > up_to_id:
            break
        if m["role"] == "user" and m["status"] in SENT_TO_CLAUDE:
            role, text = "user", m["content"]
            if attached.get(m["id"]):
                text += "\n\n" + attached_note(attached[m["id"]])
        elif m["role"] == "assistant" and m["status"] == COMPLETE and m["content"].strip():
            role, text = "assistant", m["content"]
        else:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] = _join(out[-1]["content"], text)
        else:
            out.append({"role": role, "content": text})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _join(first: str, second: str) -> str:
    return first + "\n\n" + second


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
        self.attachments = attachments  # only to count old attached files for the history note
        self.db = db
        self.store = store
        self.claude = claude
        self.broker = broker
        self.tasks = tasks
        # model id -> Capabilities; build_services swaps in one that knows the gateway's model info
        self.capabilities_of: Callable[[str], Capabilities] = lambda model: capabilities_for(
            model, resolve_backend(settings))
        self.on_alert: Callable[[int, int], Awaitable[None]] | None = None
        self.on_result: Callable[[bool, str], None] | None = None  # tells the health checks how the call went
        # Slack DMs: (conversation id, user message id, reply id) when a reply starts and again when it is saved
        self.on_reply: Callable[[int, int, int], Awaitable[None]] | None = None
        self.on_reply_done: Callable[[int, int, int], Awaitable[None]] | None = None
        # Text streamed so far for replies in progress, so someone who opens the chat mid-reply sees all of it.
        self._live: dict[int, list[str]] = {}

    def live_text(self, reply_id: int) -> tuple[str, int]:
        """The text streamed so far and how many chunks it contains."""
        parts = self._live.get(reply_id, [])
        return "".join(parts), len(parts)

    def reply_model(self, conv) -> str:
        """The model for the next reply. A chat's model is checked again here (its provider and key, the Model
        safety check). If it can't be used any more, the chat moves to the default model and the change is audited,
        so the reply still happens."""
        # Imported here: model_choices imports saved_settings, which imports this module
        from .model_choices import model_block

        model = normalize_model_id(conv["model"])
        default = normalize_model_id(self.settings.model_default)
        if model == default:
            return model
        problem = model_block(self.settings, self.db, model)
        if not problem:
            return model
        self.store.set_model(conv["id"], default)
        self.db.audit("model_fallback", actor_id=None, target_type="conversation", target_id=conv["id"],
                      details={"from": model, "to": default, "reason": problem})
        log.info("Chat %s moved from %s to %s: %s", conv["id"], model, default, problem)
        return default

    def start(self, conversation_id: int, user_message_id: int) -> int:
        conv = self.store.conversation(conversation_id)
        model = self.reply_model(conv)
        with self.db.transaction() as conn:
            reply_id = self.store.add_message(
                conn, conversation_id=conversation_id, author_id=None, role="assistant", content="",
                status=STREAMING, reply_to=user_message_id, model=model)
        self.broker.publish(f"conv:{conversation_id}", {"type": "changed", "id": reply_id})
        if self.on_reply is not None:
            self.tasks.spawn(self._hook(self.on_reply, conversation_id, user_message_id, reply_id),
                             name=f"reply-start-{reply_id}")
        self.tasks.spawn(self._run(conversation_id, user_message_id, reply_id), name=f"reply-{reply_id}")
        return reply_id

    @staticmethod
    async def _hook(hook: Callable[[int, int, int], Awaitable[None]], conversation_id: int, user_message_id: int,
                    reply_id: int) -> None:
        """Run a reply hook. A failing hook is logged and never breaks the reply."""
        try:
            await hook(conversation_id, user_message_id, reply_id)
        except Exception:
            log.exception("A reply hook failed for reply %s", reply_id)

    def attached_counts(self, conversation_id: int) -> dict[int, int]:
        """How many old files each message in the chat had. They aren't sent to Claude; the history just says so."""
        return {mid: n for mid, rows in self.attachments.for_conversation(conversation_id).items()
                if (n := sum(1 for r in rows if r["kind"] != "frame"))}

    async def _run(self, conversation_id: int, user_message_id: int, reply_id: int) -> None:
        topic = f"conv:{conversation_id}"
        conv = self.store.conversation(conversation_id)  # a chat's old project_id is ignored
        context = build_context(self.store.messages(conversation_id), user_message_id,
                                self.attached_counts(conversation_id))
        max_tokens, effort = self.settings.max_tokens_chat, self.settings.effort_default
        web_searches = 0  # replies don't search unless a tool asks for it, and none is offered per message now
        sources: list[dict[str, str]] = []
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
                max_tokens=max_tokens, effort=effort, system_extra=None, web_searches=web_searches,
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
                elif event.kind == "search":
                    self.broker.publish(topic, {"type": "searching", "id": reply_id, "query": event.text[:200]})
                elif event.kind == "sources" and event.sources:
                    known = {s["url"] for s in sources}
                    sources += [s for s in event.sources if s["url"] not in known and s["url"].startswith("https://")]
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
            "cache_read_tokens = ?, cache_write_tokens = ?, web_searches = ?, sources = ?, updated_at = ? WHERE id = ?",
            (text, status, error or None, usage.get("input_tokens"), usage.get("output_tokens"),
             usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"),
             usage.get("web_searches") or (1 if sources else None),
             json.dumps([{"url": s["url"], "title": s["title"][:200]} for s in sources[:20]]) if sources else None,
             utcnow(), reply_id),
        )
        self._live.pop(reply_id, None)
        self.broker.publish(topic, {"type": "done", "id": reply_id})
        if self.on_result is not None:
            self.on_result(status == COMPLETE, error)
        if self.on_reply_done is not None:
            await self._hook(self.on_reply_done, conversation_id, user_message_id, reply_id)
        if status == COMPLETE and not conv["title"]:
            await self._make_title(conversation_id)

    def searches_left(self) -> int:
        if not self.settings.web_search_enabled:
            return 0
        return max(0, self.settings.web_search_daily_limit - self.store.web_searches_today())

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
