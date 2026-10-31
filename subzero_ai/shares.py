"""Share links: a read-only copy of a chat for someone outside the team, such as a parent.

A link shows the chat as it was when it was shared, and only what the team could already see: approved
student messages and Claude's finished replies, with no names (people show as "Student" or "Mentor"), no
attached files and nothing that's waiting, rejected or handled privately. The chat's student or a mentor
asks for a link and says who it's for; a mentor approves it before it works (a mentor's own link works
straight away). Links expire after SHARE_DAYS days and can be revoked at any time, and a chat a mentor
makes private stops being shared."""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Database, utcnow
from .store import COMPLETE, SENT_TO_CLAUDE, Viewer

SHARE_DAYS = 30
MAX_AUDIENCE_CHARS = 120
MAX_OPEN_SHARES = 5  # pending or active links per chat

PENDING, ACTIVE, DECLINED, REVOKED = "pending", "active", "declined", "revoked"


@dataclass
class ShareError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def _expired(row: sqlite3.Row) -> bool:
    if not row["expires_at"]:
        return False
    return datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc)


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=SHARE_DAYS)).isoformat(timespec="seconds")


def state(row: sqlite3.Row) -> str:
    """pending, active, expired, declined or revoked."""
    if row["status"] == ACTIVE and _expired(row):
        return "expired"
    return row["status"]


class Shares:
    def __init__(self, db: Database):
        self.db = db

    _SELECT = ("SELECT s.*, c.title AS conversation_title, c.visibility, c.owner_id, "
               "q.name AS requester_name, q.slack_user_id AS requester_slack_id, q.notify_dm AS requester_notify_dm, "
               "r.name AS reviewer_name FROM shares s JOIN conversations c ON c.id = s.conversation_id "
               "JOIN users q ON q.id = s.requested_by LEFT JOIN users r ON r.id = s.reviewed_by ")

    def get(self, share_id: int) -> sqlite3.Row | None:
        return self.db.one(self._SELECT + "WHERE s.id = ?", (share_id,))

    def for_conversation(self, conversation_id: int) -> list[sqlite3.Row]:
        return self.db.all(self._SELECT + "WHERE s.conversation_id = ? ORDER BY s.id DESC", (conversation_id,))

    def pending(self) -> list[sqlite3.Row]:
        return self.db.all(self._SELECT + "WHERE s.status = 'pending' ORDER BY s.id")

    def count_pending(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM shares WHERE status = 'pending'")["n"]

    def public_messages(self, conversation_id: int, upto: int) -> list[sqlite3.Row]:
        """What a share link shows: approved student messages and finished replies, up to when it was shared."""
        sent = ",".join(f"'{status}'" for status in sorted(SENT_TO_CLAUDE))
        return self.db.all(
            "SELECT m.*, a.role AS author_role FROM messages m LEFT JOIN users a ON a.id = m.author_id "
            f"WHERE m.conversation_id = ? AND m.id <= ? AND ((m.role = 'user' AND m.status IN ({sent})) "
            f"OR (m.role = 'assistant' AND m.status = '{COMPLETE}')) ORDER BY m.id",
            (conversation_id, upto))

    def can_manage(self, viewer: Viewer, conv: sqlite3.Row) -> bool:
        return viewer.is_mentor or conv["owner_id"] == viewer.id

    # ------------------------------------------------------------------ requests
    def request(self, viewer: Viewer, conv: sqlite3.Row, audience: str) -> sqlite3.Row:
        if not self.can_manage(viewer, conv):
            raise ShareError("Only the student who started this chat, or a mentor, can share it.")
        if conv["visibility"] == "private":
            raise ShareError("This chat is private, so it can't be shared.")
        audience = " ".join((audience or "").split())
        if not audience:
            raise ShareError("Say who the link is for, such as \"my parents\". The mentor who approves it sees this.")
        if len(audience) > MAX_AUDIENCE_CHARS:
            raise ShareError(f"Keep \"who it's for\" under {MAX_AUDIENCE_CHARS} characters.")
        shown = self.public_messages(conv["id"], 2**62)
        if not any(m["role"] == "assistant" for m in shown):
            raise ShareError("There's nothing to share yet. Wait for Claude's first reply.")
        open_links = sum(1 for row in self.for_conversation(conv["id"]) if state(row) in (PENDING, ACTIVE))
        if open_links >= MAX_OPEN_SHARES:
            raise ShareError(f"This chat already has {MAX_OPEN_SHARES} share links. Revoke one first.")
        now = utcnow()
        mentor = viewer.is_mentor
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO shares (conversation_id, token, upto_message_id, audience, status, requested_by, "
                "reviewed_by, created_at, reviewed_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conv["id"], secrets.token_urlsafe(24), shown[-1]["id"], audience, ACTIVE if mentor else PENDING,
                 viewer.id, viewer.id if mentor else None, now, now if mentor else None,
                 _expiry() if mentor else None))
            share_id = cur.lastrowid
            self.db.audit("share_created" if mentor else "share_requested", actor_id=viewer.id,
                          target_type="conversation", target_id=conv["id"],
                          details={"share": share_id, "for": audience, "up_to_message": shown[-1]["id"]}, conn=conn)
        return self.get(share_id)

    def review(self, viewer: Viewer, share_id: int, approve: bool) -> sqlite3.Row:
        if not viewer.is_mentor:
            raise ShareError("Only mentors review share links.")
        row = self.get(share_id)
        if row is None:
            raise ShareError("That share link doesn't exist.")
        if row["status"] != PENDING:
            raise ShareError("Someone already handled this share link.")
        if approve and row["visibility"] == "private":
            raise ShareError("This chat was made private, so it can't be shared.")
        now = utcnow()
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE shares SET status = ?, reviewed_by = ?, reviewed_at = ?, expires_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (ACTIVE if approve else DECLINED, viewer.id, now, _expiry() if approve else None, share_id)).rowcount
            if not changed:
                raise ShareError("Someone already handled this share link.")
            self.db.audit("share_approved" if approve else "share_declined", actor_id=viewer.id,
                          target_type="conversation", target_id=row["conversation_id"],
                          details={"share": share_id, "for": row["audience"]}, conn=conn)
        return self.get(share_id)

    def revoke(self, viewer: Viewer, share_id: int) -> sqlite3.Row:
        row = self.get(share_id)
        if row is None or not (viewer.is_mentor or row["requested_by"] == viewer.id or row["owner_id"] == viewer.id):
            raise ShareError("That share link doesn't exist.")
        if row["status"] in (DECLINED, REVOKED):
            return row
        with self.db.transaction() as conn:
            conn.execute("UPDATE shares SET status = 'revoked', reviewed_at = COALESCE(reviewed_at, ?) WHERE id = ?",
                         (utcnow(), share_id))
            self.db.audit("share_revoked", actor_id=viewer.id, target_type="conversation",
                          target_id=row["conversation_id"], details={"share": share_id, "for": row["audience"]},
                          conn=conn)
        return self.get(share_id)

    # -------------------------------------------------------------------- public
    def check(self, token: str) -> sqlite3.Row | None:
        """The share behind a link, if the link works right now."""
        row = self.db.one(self._SELECT + "WHERE s.token = ?", (token,)) if token and len(token) <= 64 else None
        if row is None or row["status"] != ACTIVE or _expired(row) or row["visibility"] == "private":
            return None
        return row

    def open(self, token: str) -> sqlite3.Row | None:
        """Like check, and counts a view."""
        row = self.check(token)
        if row is not None:
            self.db.execute("UPDATE shares SET views = views + 1 WHERE id = ?", (row["id"],))
        return row
