"""The approval loop: submitting messages, mentor decisions, Slack cards, DMs, alerts, reminders."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from . import slack_cards
from .attachments import MAX_FILES, Attachments, Prepared
from .config import Settings
from .db import Database, utcnow
from .generation import ReplyRunner, asks_for_explainer, load_options, options_labels
from .live import Broker, Tasks
from .roles import is_builtin_user, is_excluded_account
from .screener import Screener
from .store import (
    APPROVED,
    AUTO,
    EDITED,
    ERROR,
    GRACE,
    HELD,
    PENDING,
    REJECTED,
    SENT_TO_CLAUDE,
    WITHDRAWN,
    Store,
    Viewer,
)

log = logging.getLogger("subzero.approvals")

MAX_MESSAGE_CHARS = 20000
MAX_REASON_CHARS = 500

STATUS_WORDS = {
    APPROVED: "approved",
    EDITED: "edited and approved",
    REJECTED: "rejected",
    HELD: "handled privately",
    WITHDRAWN: "withdrawn",
}


def clean_text(text: str | None) -> str:
    """Browsers send line breaks as CRLF. Store plain newlines, trimmed."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


@dataclass
class Outcome:
    ok: bool
    code: str = "ok"  # ok, invalid, limit, forbidden, not_found, already_handled
    message: str = ""
    message_id: int | None = None
    conversation_id: int | None = None


class ApprovalService:
    def __init__(self, *, settings: Settings, db: Database, store: Store, slack, broker: Broker,
                 tasks: Tasks, runner: ReplyRunner, attachments: Attachments, screener: Screener | None = None):
        self.settings = settings
        self.screener = screener or Screener(settings, runner.claude)
        # One send at a time per chat, so two quick sends can't both slip through the approval window
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self.attachments = attachments
        self.db = db
        self.store = store
        self.slack = slack
        self.broker = broker
        self.tasks = tasks
        self.runner = runner
        # Posts a decision for a message that came in through a Slack DM and returns True if it did, so the usual
        # DM isn't sent as well. Slack DMs register this.
        self.on_decided: Callable[[int], Awaitable[bool]] | None = None

    # ------------------------------------------------------------------ helpers
    def link(self, conversation_id: int) -> str:
        return self.settings.url(f"/c/{conversation_id}")

    def _publish(self, conversation_id: int, event: dict) -> None:
        self.broker.publish(f"conv:{conversation_id}", event)

    def _queue_changed(self) -> None:
        self.broker.publish("queue", {"type": "queue"})

    @property
    def _slack_ready(self) -> bool:
        return self.slack is not None and bool(self.settings.slack_approvals_channel)

    def _already_handled(self, message_id: int) -> Outcome:
        msg = self.store.message(message_id)
        if msg is None:
            return Outcome(False, "not_found", "That message no longer exists.")
        words = STATUS_WORDS.get(msg["status"], msg["status"])
        who = msg["reviewer_name"]
        text = f"Already {words} by {who}." if who and msg["status"] != WITHDRAWN else f"Already {words}."
        return Outcome(False, "already_handled", text, message_id, msg["conversation_id"])

    # ---------------------------------------------------------------- submit
    async def submit(self, viewer: Viewer, text: str, conversation_id: int | None = None,
                     subteam: str | None = None, files: list[Prepared] | tuple = (),
                     explainer: bool = False, project_id: int | None = None,
                     options: dict | None = None, on_saved: Callable[[int, int], None] | None = None) -> Outcome:
        """on_saved(conversation_id, message_id), if given, is called right after the message is saved and
        before anything else happens to it (the reply starting, the Slack card)."""
        text = clean_text(text)
        # There's no explainer switch any more: asking for one in words is how a student requests it
        explainer = explainer or asks_for_explainer(text)
        if not text:
            if files:
                return Outcome(False, "invalid", "Add a message saying what you'd like Claude to do with the files.")
            return Outcome(False, "invalid", "Type a message first.")
        if len([f for f in files if f.kind != "frame"]) > MAX_FILES:
            return Outcome(False, "invalid", f"You can attach up to {MAX_FILES} files to one message.")
        if len(text) > MAX_MESSAGE_CHARS:
            return Outcome(False, "invalid", f"That message is over {MAX_MESSAGE_CHARS:,} characters. "
                                             "Shorten it or split it into parts.")
        if conversation_id is None:
            return await self._submit(viewer, text, None, subteam, files, explainer, project_id, options or {},
                                      on_saved)
        lock = self._chat_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            return await self._submit(viewer, text, conversation_id, subteam, files, explainer, None, options or {},
                                      on_saved)

    def window_applies(self, viewer: Viewer, conversation_id: int | None, files=()) -> bool:
        """Whether this message can skip review because a mentor approved the chat a few minutes ago.
        Messages with files always wait: a mentor hasn't seen the file."""
        return (conversation_id is not None and not files
                and viewer.slack_user_id not in self.settings.always_review_slack_ids
                and self.store.window_open(conversation_id, self.settings.approval_window_minutes))

    async def _submit(self, viewer: Viewer, text: str, conversation_id: int | None, subteam: str | None,
                      files, explainer: bool, project_id: int | None, options: dict,
                      on_saved: Callable[[int, int], None] | None = None) -> Outcome:
        needs_review = (not viewer.is_mentor) or self.settings.mentors_need_approval

        if conversation_id is not None:
            conv = self.store.conversation(conversation_id)
            if conv is None:
                return Outcome(False, "not_found", "That chat doesn't exist.")
            if conv["owner_id"] != viewer.id:
                return Outcome(False, "forbidden", "Only the person who started this chat can add to it. "
                                                   "Start your own chat to ask something new.")
            if needs_review and self.store.conversation_has_pending(conversation_id):
                return Outcome(False, "limit", "Your last message in this chat is still waiting for a mentor.")
            if self.store.conversation_is_replying(conversation_id):
                return Outcome(False, "limit", "Wait for Claude to finish replying, then send your next message.")

        if needs_review and not viewer.is_mentor:
            if self.store.count_approved_today(viewer.id) >= self.settings.limit_approved_per_day:
                return Outcome(False, "limit", f"You've reached today's limit of {self.settings.limit_approved_per_day} "
                                               "approved messages. It resets at midnight.")

        searching = bool(options.get("search") or options.get("research"))
        if (options.get("search") or options.get("research")) and self.runner.searches_left() <= 0:
            return Outcome(False, "limit", "Web search isn't available right now: the team has used today's "
                                           "searches, or an admin turned it off. Turn off Web search in the tools "
                                           "menu and send again.")

        status = PENDING if needs_review else AUTO
        screen = None  # "passed", "off", or the reason the screener flagged it
        screen_note = None
        # Web searches cost money per search, so a message asking for them always waits for a mentor
        if needs_review and not searching and self.window_applies(viewer, conversation_id, files):
            if self.settings.screener_enabled:
                verdict = await self.screener.check(text)
                if verdict.passed:
                    status, screen = GRACE, "passed"
                else:
                    screen = screen_note = verdict.note
            else:
                status, screen = GRACE, "off"

        if status == PENDING and not viewer.is_mentor:
            pending = self.store.count_pending_for(viewer.id)
            if pending >= self.settings.limit_pending_per_student:
                return Outcome(False, "limit", f"You have {pending} messages waiting for review. "
                                               "Wait for a mentor before sending more.")
        with self.db.transaction() as conn:
            if conversation_id is None:
                conversation_id = self.store.create_conversation(
                    conn, owner_id=viewer.id, model=self.settings.model_default, subteam=subteam,
                    project_id=project_id)
                self.db.audit("conversation_started", actor_id=viewer.id, target_type="conversation",
                              target_id=conversation_id, conn=conn)
            message_id = self.store.add_message(conn, conversation_id=conversation_id, author_id=viewer.id,
                                                role="user", content=text, status=status,
                                                wants_explainer=explainer,
                                                options=json.dumps(options) if options else None)
            if screen_note:
                conn.execute("UPDATE messages SET screen_note = ? WHERE id = ?", (screen_note, message_id))
            for item in files:
                self.attachments.save(conn, message_id, item)
            details = {"conversation": conversation_id, "status": status, "chars": len(text)}
            if files:
                details["files"] = [f"{item.name} ({item.size:,} bytes)" for item in files if item.kind != "frame"]
            if explainer:
                details["explainer"] = True
            if options:
                details["tools"] = options_labels(options)
            self.db.audit("message_sent", actor_id=viewer.id, target_type="message", target_id=message_id,
                          details=details, conn=conn)
            if status == GRACE:
                self.db.audit("message_auto_approved", actor_id=None, target_type="message", target_id=message_id,
                              details={"conversation": conversation_id, "student": viewer.name,
                                       "window_minutes": self.settings.approval_window_minutes,
                                       "screener": "passed" if screen == "passed" else "turned off"}, conn=conn)
            elif screen_note:
                self.db.audit("message_flagged_by_screener", actor_id=None, target_type="message",
                              target_id=message_id, details={"conversation": conversation_id, "reason": screen_note},
                              conn=conn)

        if on_saved is not None:
            try:
                on_saved(conversation_id, message_id)
            except Exception:  # the message is saved; a failing caller hook must not stop review or the reply
                log.exception("A submit hook failed for message %s", message_id)
        self._publish(conversation_id, {"type": "changed", "id": message_id})
        if status == PENDING:
            self._queue_changed()
            self.tasks.spawn(self._post_card(message_id), name=f"card-{message_id}")
        else:
            self.runner.start(conversation_id, message_id)
        return Outcome(True, message_id=message_id, conversation_id=conversation_id)

    async def _post_card(self, message_id: int) -> None:
        if not self._slack_ready:
            return
        msg = self.store.message(message_id)
        conv = self.store.conversation(msg["conversation_id"])
        previous = self.store.previous_reply(conv["id"], message_id)
        blocks = slack_cards.approval_card(
            message_id=message_id, student_name=msg["author_name"], subteam=conv["subteam"], title=conv["title"],
            text=msg["content"], link=self.link(conv["id"]),
            previous_reply=previous["content"] if previous else None,
            files=[(f["name"], f["size"]) for f in self.attachments.for_message(message_id) if f["kind"] != "frame"],
            explainer=bool(msg["wants_explainer"]), project=conv["project_name"],
            tools=options_labels(load_options(msg["options"])), screen_note=msg["screen_note"])
        try:
            posted = await self.slack.post_message(self.settings.slack_approvals_channel,
                                                   slack_cards.card_fallback_text(msg["author_name"]), blocks)
            self.store.save_card(message_id, posted["channel"], posted["ts"])
        except Exception as exc:
            log.exception("Couldn't post the approval card for message %s", message_id)
            self.db.audit("slack_card_failed", actor_id=None, target_type="message", target_id=message_id,
                          details={"error": str(exc)})

    # -------------------------------------------------------------- decisions
    async def approve(self, reviewer: Viewer, message_id: int, edited_text: str | None = None) -> Outcome:
        if not reviewer.is_mentor:
            return Outcome(False, "forbidden", "Only mentors can approve messages.")
        msg = self.store.message(message_id)
        if msg is None or msg["role"] != "user":
            return Outcome(False, "not_found", "That message no longer exists.")
        if msg["author_id"] == reviewer.id:
            return Outcome(False, "forbidden", "Another mentor needs to approve your message.")
        new_text = msg["content"]
        status = APPROVED
        if edited_text is not None:
            edited_text = clean_text(edited_text)
            if not edited_text:
                return Outcome(False, "invalid", "The edited message can't be empty.")
            if len(edited_text) > MAX_MESSAGE_CHARS:
                return Outcome(False, "invalid", "The edited message is too long.")
            if edited_text != msg["content"].strip():
                new_text, status = edited_text, EDITED
        now = utcnow()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE messages SET status = ?, content = ?, original_content = ?, reviewed_by = ?, "
                "reviewed_at = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                (status, new_text, msg["content"] if status == EDITED else None, reviewer.id, now, now, message_id),
            )
            if cur.rowcount == 0:
                handled = True
            else:
                handled = False
                details = {"conversation": msg["conversation_id"]}
                if status == EDITED:
                    details.update({"before": msg["content"], "after": new_text})
                self.db.audit("message_" + status, actor_id=reviewer.id, target_type="message",
                              target_id=message_id, details=details, conn=conn)
                self.store.touch(conn, msg["conversation_id"])
        if handled:
            return self._already_handled(message_id)
        self._publish(msg["conversation_id"], {"type": "changed", "id": message_id})
        self._queue_changed()
        self.runner.start(msg["conversation_id"], message_id)
        self.tasks.spawn(self._after_review(message_id), name=f"after-review-{message_id}")
        return Outcome(True, message_id=message_id, conversation_id=msg["conversation_id"])

    async def reject(self, reviewer: Viewer, message_id: int, reason: str) -> Outcome:
        if not reviewer.is_mentor:
            return Outcome(False, "forbidden", "Only mentors can reject messages.")
        reason = clean_text(reason)
        if not reason:
            return Outcome(False, "invalid", "Add a reason the student will see.")
        if len(reason) > MAX_REASON_CHARS:
            return Outcome(False, "invalid", f"Keep the reason under {MAX_REASON_CHARS} characters.")
        return await self._decide(reviewer, message_id, REJECTED, reason=reason)

    async def hold(self, reviewer: Viewer, message_id: int) -> Outcome:
        if not reviewer.is_mentor:
            return Outcome(False, "forbidden", "Only mentors can handle messages privately.")
        return await self._decide(reviewer, message_id, HELD)

    async def _decide(self, reviewer: Viewer, message_id: int, status: str, reason: str | None = None) -> Outcome:
        msg = self.store.message(message_id)
        if msg is None or msg["role"] != "user":
            return Outcome(False, "not_found", "That message no longer exists.")
        now = utcnow()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE messages SET status = ?, review_note = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, reason, reviewer.id, now, now, message_id),
            )
            if cur.rowcount == 0:
                handled = True
            else:
                handled = False
                if status == HELD:
                    self.store.make_private(msg["conversation_id"], conn=conn)
                self.db.audit("message_" + status, actor_id=reviewer.id, target_type="message", target_id=message_id,
                              details={"conversation": msg["conversation_id"], "reason": reason}, conn=conn)
        if handled:
            return self._already_handled(message_id)
        event_type = "visibility" if status == HELD else "changed"
        self._publish(msg["conversation_id"], {"type": event_type, "id": message_id})
        self._queue_changed()
        self.tasks.spawn(self._after_review(message_id), name=f"after-review-{message_id}")
        return Outcome(True, message_id=message_id, conversation_id=msg["conversation_id"])

    async def withdraw(self, viewer: Viewer, message_id: int) -> Outcome:
        msg = self.store.message(message_id)
        if msg is None or msg["role"] != "user":
            return Outcome(False, "not_found", "That message no longer exists.")
        if msg["author_id"] != viewer.id:
            return Outcome(False, "forbidden", "You can only withdraw your own messages.")
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE messages SET status = 'withdrawn', updated_at = ? WHERE id = ? AND status = 'pending'",
                (utcnow(), message_id)).rowcount
            if changed:
                self.db.audit("message_withdrawn", actor_id=viewer.id, target_type="message", target_id=message_id,
                              details={"conversation": msg["conversation_id"]}, conn=conn)
        if not changed:
            return self._already_handled(message_id)
        self._publish(msg["conversation_id"], {"type": "changed", "id": message_id})
        self._queue_changed()
        self.tasks.spawn(self._after_review(message_id), name=f"after-withdraw-{message_id}")
        return Outcome(True, message_id=message_id, conversation_id=msg["conversation_id"])

    async def retry(self, viewer: Viewer, message_id: int) -> Outcome:
        """Run Claude again for an approved message whose reply failed. No new approval is needed."""
        msg = self.store.message(message_id)
        if msg is None or msg["role"] != "user" or msg["status"] not in SENT_TO_CLAUDE:
            return Outcome(False, "not_found", "There's nothing to retry for that message.")
        conv = self.store.conversation(msg["conversation_id"])
        if not (viewer.is_mentor or conv["owner_id"] == viewer.id):
            return Outcome(False, "forbidden", "Only the student who asked or a mentor can retry.")
        reply = self.store.reply_for(message_id)
        if reply is not None and reply["status"] != ERROR:
            return Outcome(False, "invalid", "Claude already answered this message.")
        if reply is not None and self.store.last_message_id(conv["id"]) != reply["id"]:
            return Outcome(False, "invalid", "Only the latest reply in a chat can be retried.")
        self.db.audit("reply_retried", actor_id=viewer.id, target_type="message", target_id=message_id)
        self.runner.start(conv["id"], message_id)
        return Outcome(True, message_id=message_id, conversation_id=conv["id"])

    async def _after_review(self, message_id: int) -> None:
        """Update the Slack card and DM the student."""
        msg = self.store.message(message_id)
        conv = self.store.conversation(msg["conversation_id"])
        link = self.link(conv["id"])
        outcome = msg["status"]
        card = self.store.card(message_id)
        if card is not None and self.slack is not None:
            blocks = slack_cards.handled_card(
                student_name=msg["author_name"], subteam=conv["subteam"], title=conv["title"],
                text=msg["original_content"] or msg["content"], link=link, outcome=outcome,
                mentor_slack_id=msg["reviewer_slack_id"] if outcome != WITHDRAWN else None,
                acted_at=msg["reviewed_at"] or msg["updated_at"], reason=msg["review_note"])
            try:
                await self.slack.update_message(card["channel"], card["ts"],
                                                slack_cards.card_fallback_text(msg["author_name"], outcome), blocks)
            except Exception:
                log.exception("Couldn't update the Slack card for message %s", message_id)
        if outcome in (APPROVED, EDITED, REJECTED) and self.on_decided is not None:
            try:
                if await self.on_decided(message_id):
                    return
            except Exception:
                log.exception("Couldn't post the decision for message %s in Slack", message_id)
        if outcome in (APPROVED, EDITED, REJECTED) and self.slack is not None and msg["author_notify_dm"]:
            text = slack_cards.dm_text(outcome, title=conv["title"], link=link, mentor_name=msg["reviewer_name"],
                                       reason=msg["review_note"])
            try:
                await self.slack.dm(msg["author_slack_id"], text)
            except Exception:
                log.exception("Couldn't DM the student about message %s", message_id)

    # ------------------------------------------------------------------ alerts
    async def mentor_alert(self, conversation_id: int, user_message_id: int) -> None:
        """Claude flagged a possible wellbeing concern: hide the chat from the team and tell mentors."""
        self.store.make_private(conversation_id)
        self.db.audit("mentor_alert", actor_id=None, target_type="conversation", target_id=conversation_id,
                      details={"message": user_message_id})
        self._publish(conversation_id, {"type": "visibility"})
        if self._slack_ready:
            self.tasks.spawn(self._post_alert(conversation_id), name=f"alert-{conversation_id}")
        else:
            log.warning("MENTOR ALERT in conversation %s (Slack isn't connected, so check the app)", conversation_id)

    async def _post_alert(self, conversation_id: int) -> None:
        conv = self.store.conversation(conversation_id)
        try:
            await self.slack.post_message(
                self.settings.slack_approvals_channel, "Mentor alert: a student may need support",
                slack_cards.alert_card(student_name=conv["owner_name"], title=conv["title"],
                                       link=self.link(conversation_id)))
        except Exception:
            log.exception("Couldn't post the mentor alert for conversation %s", conversation_id)

    # --------------------------------------------------------------- reminders
    async def send_reminders(self) -> int:
        if not self._slack_ready:
            return 0
        sent = 0
        for card in self.store.cards_needing_reminder(self.settings.reminder_minutes):
            created = datetime.fromisoformat(card["message_created_at"])
            hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
            age = f"{hours:.0f} hours" if hours >= 1.5 else "over an hour"
            try:
                await self.slack.post_message(card["channel"], f"Still waiting for a mentor. Sent {age} ago.",
                                              thread_ts=card["ts"])
                self.store.mark_reminded(card["message_id"])
                sent += 1
            except Exception:
                log.exception("Couldn't post a reminder for message %s", card["message_id"])
        return sent

    async def check_accounts(self) -> int:
        """Deactivate people who were removed from Slack or became guests. Runs daily."""
        if self.slack is None:
            return 0
        removed = 0
        for user in self.store.active_users():
            if is_builtin_user(user["slack_user_id"]):
                continue
            try:
                info = await self.slack.user_info(user["slack_user_id"])
            except Exception:
                log.warning("Couldn't check Slack account %s", user["slack_user_id"], exc_info=True)
                continue
            if is_excluded_account(info, self.settings) and user["slack_user_id"] not in self.settings.admin_slack_ids:
                self.store.deactivate_user(user["id"])
                self.db.audit("user_deactivated", actor_id=None, target_type="user", target_id=user["id"])
                removed += 1
        return removed
