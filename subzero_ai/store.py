"""Queries for users, conversations, and messages, plus the rules for who can see what."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import Database, utcnow

MENTOR_ROLES = {"mentor", "admin"}

# Statuses a user message can have
PENDING = "pending"
APPROVED = "approved"
EDITED = "edited"
AUTO = "auto"  # a mentor's own message, sent without review
REJECTED = "rejected"
HELD = "held"
WITHDRAWN = "withdrawn"
SENT_TO_CLAUDE = {APPROVED, EDITED, AUTO}

# Statuses for Claude's replies
STREAMING = "streaming"
COMPLETE = "complete"
ERROR = "error"


@dataclass
class Viewer:
    """The signed-in person looking at a page."""

    id: int
    slack_user_id: str
    name: str
    role: str
    avatar_url: str | None = None

    @property
    def is_mentor(self) -> bool:
        return self.role in MENTOR_ROLES

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Store:
    def __init__(self, db: Database, *, timezone_name: str = "America/Chicago",
                 show_pending_to_team: bool = False, show_rejected_to_team: bool = False):
        self.db = db
        self.tz = ZoneInfo(timezone_name)
        self.show_pending_to_team = show_pending_to_team
        self.show_rejected_to_team = show_rejected_to_team

    # ------------------------------------------------------------------ users
    def upsert_user(self, *, slack_user_id: str, name: str, role: str, team_id: str | None = None,
                    email: str | None = None, avatar_url: str | None = None, login: bool = False) -> sqlite3.Row:
        now = utcnow()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT id FROM users WHERE slack_user_id = ?", (slack_user_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET name = ?, role = ?, slack_team_id = COALESCE(?, slack_team_id), "
                    "email = COALESCE(?, email), avatar_url = COALESCE(?, avatar_url), active = 1, "
                    "last_login_at = CASE WHEN ? THEN ? ELSE last_login_at END WHERE id = ?",
                    (name, role, team_id, email, avatar_url, int(login), now, row["id"]),
                )
                user_id = row["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO users (slack_user_id, slack_team_id, name, email, avatar_url, role, "
                    "created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (slack_user_id, team_id, name, email, avatar_url, role, now, now if login else None),
                )
                user_id = cur.lastrowid
            return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def user(self, user_id: int) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))

    def user_by_slack_id(self, slack_user_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM users WHERE slack_user_id = ?", (slack_user_id,))

    def set_user_role(self, user_id: int, role: str) -> None:
        self.db.execute("UPDATE users SET role = ? WHERE id = ? AND role != ?", (role, user_id, role))

    def deactivate_user(self, user_id: int) -> None:
        self.db.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))

    def active_users(self) -> list[sqlite3.Row]:
        return self.db.all("SELECT * FROM users WHERE active = 1")

    # ---------------------------------------------------------- conversations
    def create_conversation(self, conn: sqlite3.Connection, *, owner_id: int, model: str,
                            subteam: str | None = None) -> int:
        now = utcnow()
        cur = conn.execute(
            "INSERT INTO conversations (owner_id, subteam, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (owner_id, subteam, model, now, now),
        )
        return cur.lastrowid

    def conversation(self, conversation_id: int) -> sqlite3.Row | None:
        return self.db.one(
            "SELECT c.*, u.name AS owner_name, u.slack_user_id AS owner_slack_id, u.avatar_url AS owner_avatar "
            "FROM conversations c JOIN users u ON u.id = c.owner_id WHERE c.id = ?",
            (conversation_id,),
        )

    def set_title(self, conversation_id: int, title: str) -> None:
        self.db.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))

    def make_private(self, conversation_id: int, conn: sqlite3.Connection | None = None) -> None:
        sql = "UPDATE conversations SET visibility = 'private', updated_at = ? WHERE id = ?"
        if conn is not None:
            conn.execute(sql, (utcnow(), conversation_id))
        else:
            self.db.execute(sql, (utcnow(), conversation_id))

    def touch(self, conn: sqlite3.Connection, conversation_id: int) -> None:
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (utcnow(), conversation_id))

    def can_view_conversation(self, viewer: Viewer, conv: sqlite3.Row) -> bool:
        if viewer.is_mentor or conv["owner_id"] == viewer.id:
            return True
        if conv["visibility"] != "team":
            return False
        # Other students only see a conversation once something in it has been approved.
        row = self.db.one(
            "SELECT 1 FROM messages WHERE conversation_id = ? AND role = 'user' AND status IN ('approved','edited','auto') LIMIT 1",
            (conv["id"],),
        )
        return row is not None

    def set_model(self, conversation_id: int, model: str) -> None:
        self.db.execute("UPDATE conversations SET model = ? WHERE id = ?", (model, conversation_id))

    def list_conversations(self, viewer: Viewer, *, scope: str = "team", query: str = "",
                           subteam: str | None = None, owner_id: int | None = None,
                           limit: int = 200) -> list[sqlite3.Row]:
        params: list = []
        where = []
        if subteam:
            where.append("c.subteam = ?")
            params.append(subteam)
        if owner_id is not None:
            where.append("c.owner_id = ?")
            params.append(owner_id)
        if scope == "mine":
            where.append("c.owner_id = ?")
            params.append(viewer.id)
        if not viewer.is_mentor:
            where.append(
                "(c.owner_id = ? OR (c.visibility = 'team' AND EXISTS (SELECT 1 FROM messages m2 WHERE "
                "m2.conversation_id = c.id AND m2.role = 'user' AND m2.status IN ('approved','edited','auto'))))"
            )
            params.append(viewer.id)
        if query:
            where.append(
                "(c.title LIKE ? OR EXISTS (SELECT 1 FROM messages m3 WHERE m3.conversation_id = c.id AND "
                "m3.content LIKE ? AND (m3.role = 'assistant' OR m3.status IN ('approved','edited','auto') OR c.owner_id = ?)))"
            )
            like = f"%{query}%"
            params += [like, like, viewer.id]
        sql = (
            "SELECT c.*, u.name AS owner_name, u.avatar_url AS owner_avatar, "
            "(SELECT COUNT(*) FROM messages p WHERE p.conversation_id = c.id AND p.status = 'pending') AS pending_count, "
            "(SELECT content FROM messages f WHERE f.conversation_id = c.id AND f.role = 'user' ORDER BY f.id LIMIT 1) AS first_text "
            "FROM conversations c JOIN users u ON u.id = c.owner_id "
            + ("WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY c.updated_at DESC LIMIT ?"
        )
        params.append(limit)
        return self.db.all(sql, tuple(params))

    # --------------------------------------------------------------- messages
    def add_message(self, conn: sqlite3.Connection, *, conversation_id: int, author_id: int | None,
                    role: str, content: str, status: str, reply_to: int | None = None,
                    model: str | None = None) -> int:
        now = utcnow()
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, author_id, role, content, status, reply_to, model, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, author_id, role, content, status, reply_to, model, now, now),
        )
        self.touch(conn, conversation_id)
        return cur.lastrowid

    def message(self, message_id: int) -> sqlite3.Row | None:
        return self.db.one(
            "SELECT m.*, a.name AS author_name, a.avatar_url AS author_avatar, a.slack_user_id AS author_slack_id, "
            "a.role AS author_role, r.name AS reviewer_name, r.slack_user_id AS reviewer_slack_id "
            "FROM messages m LEFT JOIN users a ON a.id = m.author_id LEFT JOIN users r ON r.id = m.reviewed_by "
            "WHERE m.id = ?",
            (message_id,),
        )

    def messages(self, conversation_id: int) -> list[sqlite3.Row]:
        return self.db.all(
            "SELECT m.*, a.name AS author_name, a.avatar_url AS author_avatar, a.slack_user_id AS author_slack_id, "
            "a.role AS author_role, r.name AS reviewer_name, r.slack_user_id AS reviewer_slack_id "
            "FROM messages m LEFT JOIN users a ON a.id = m.author_id LEFT JOIN users r ON r.id = m.reviewed_by "
            "WHERE m.conversation_id = ? ORDER BY m.id",
            (conversation_id,),
        )

    def can_view_message(self, viewer: Viewer, conv: sqlite3.Row, msg: sqlite3.Row) -> bool:
        if not self.can_view_conversation(viewer, conv):
            return False
        if viewer.is_mentor or conv["owner_id"] == viewer.id:
            return msg["status"] != WITHDRAWN or msg["author_id"] == viewer.id or viewer.is_mentor
        if msg["role"] == "assistant":
            return True
        status = msg["status"]
        if status in SENT_TO_CLAUDE:
            return True
        if status == PENDING:
            return self.show_pending_to_team
        if status == REJECTED:
            return self.show_rejected_to_team
        return False

    def visible_messages(self, viewer: Viewer, conv: sqlite3.Row) -> list[sqlite3.Row]:
        return [m for m in self.messages(conv["id"]) if self.can_view_message(viewer, conv, m)]

    def pending_messages(self) -> list[sqlite3.Row]:
        return self.db.all(
            "SELECT m.*, a.name AS author_name, a.avatar_url AS author_avatar, a.slack_user_id AS author_slack_id, "
            "c.title AS conversation_title, c.subteam AS conversation_subteam "
            "FROM messages m JOIN users a ON a.id = m.author_id JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.status = 'pending' ORDER BY m.id"
        )

    def count_pending_for(self, user_id: int) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM messages WHERE author_id = ? AND status = 'pending'", (user_id,))
        return row["n"]

    def conversation_has_pending(self, conversation_id: int) -> bool:
        return self.db.one(
            "SELECT 1 FROM messages WHERE conversation_id = ? AND status = 'pending' LIMIT 1", (conversation_id,)
        ) is not None

    def conversation_is_replying(self, conversation_id: int) -> bool:
        return self.db.one(
            "SELECT 1 FROM messages WHERE conversation_id = ? AND status = 'streaming' LIMIT 1", (conversation_id,)
        ) is not None

    def last_message_id(self, conversation_id: int) -> int | None:
        row = self.db.one("SELECT MAX(id) AS id FROM messages WHERE conversation_id = ?", (conversation_id,))
        return row["id"] if row else None

    def count_approved_today(self, user_id: int) -> int:
        local_midnight = datetime.now(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        since = local_midnight.astimezone(timezone.utc).isoformat(timespec="seconds")
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM messages WHERE author_id = ? AND status IN ('approved','edited') "
            "AND reviewed_at >= ?",
            (user_id, since),
        )
        return row["n"]

    def previous_reply(self, conversation_id: int, before_message_id: int) -> sqlite3.Row | None:
        return self.db.one(
            "SELECT * FROM messages WHERE conversation_id = ? AND role = 'assistant' AND status = 'complete' "
            "AND id < ? ORDER BY id DESC LIMIT 1",
            (conversation_id, before_message_id),
        )

    def reply_for(self, user_message_id: int) -> sqlite3.Row | None:
        return self.db.one(
            "SELECT * FROM messages WHERE reply_to = ? ORDER BY id DESC LIMIT 1", (user_message_id,)
        )

    def has_completed_reply(self, conversation_id: int) -> int:
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ? AND role = 'assistant' AND status = 'complete'",
            (conversation_id,),
        )
        return row["n"]

    # ------------------------------------------------------------ slack cards
    def save_card(self, message_id: int, channel: str, ts: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO slack_cards (message_id, channel, ts) VALUES (?, ?, ?)",
            (message_id, channel, ts),
        )

    def card(self, message_id: int) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM slack_cards WHERE message_id = ?", (message_id,))

    def cards_needing_reminder(self, minutes: int) -> list[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        return self.db.all(
            "SELECT s.*, m.created_at AS message_created_at FROM slack_cards s JOIN messages m ON m.id = s.message_id "
            "WHERE m.status = 'pending' AND s.reminded_at IS NULL AND m.created_at <= ?",
            (cutoff,),
        )

    def mark_reminded(self, message_id: int) -> None:
        self.db.execute("UPDATE slack_cards SET reminded_at = ? WHERE message_id = ?", (utcnow(), message_id))

    # ------------------------------------------------------------------ audit
    def audit_entries(self, limit: int = 300) -> list[sqlite3.Row]:
        return self.db.all(
            "SELECT l.*, u.name AS actor_name FROM audit_log l LEFT JOIN users u ON u.id = l.actor_id "
            "ORDER BY l.id DESC LIMIT ?",
            (limit,),
        )

    # ------------------------------------------------------------------ times
    def local_time(self, iso: str | None) -> datetime | None:
        if not iso:
            return None
        return datetime.fromisoformat(iso).astimezone(self.tz)
