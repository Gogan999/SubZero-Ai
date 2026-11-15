"""Queries for users, conversations, and messages, plus the rules for who can see what."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import Database, utcnow

MENTOR_ROLES = {"mentor", "admin"}
THEMES = ("system", "light", "dark")

# Statuses a user message can have
PENDING = "pending"
APPROVED = "approved"
EDITED = "edited"
AUTO = "auto"  # a mentor's own message, sent without review
GRACE = "grace"  # a student's message sent without review, inside the approval window
REJECTED = "rejected"
HELD = "held"
WITHDRAWN = "withdrawn"
SENT_TO_CLAUDE = {APPROVED, EDITED, AUTO, GRACE}

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
    theme: str = "system"

    @property
    def is_mentor(self) -> bool:
        return self.role in MENTOR_ROLES

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Store:
    def __init__(self, db: Database, *, timezone_name: str = "America/Chicago"):
        self.db = db
        self.tz = ZoneInfo(timezone_name)

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

    def save_preferences(self, user_id: int, *, theme: str) -> None:
        self.db.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, user_id))

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
        """Chats are read on the website by mentors and admins only. Students use Slack."""
        return viewer.is_mentor

    def set_model(self, conversation_id: int, model: str) -> None:
        self.db.execute("UPDATE conversations SET model = ? WHERE id = ?", (model, conversation_id))

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
            "a.role AS author_role, a.notify_dm AS author_notify_dm, r.name AS reviewer_name, "
            "r.slack_user_id AS reviewer_slack_id "
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
            "SELECT COUNT(*) AS n FROM messages WHERE author_id = ? AND ((status IN ('approved','edited') "
            "AND reviewed_at >= ?) OR (status = 'grace' AND created_at >= ?))",
            (user_id, since, since),
        )
        return row["n"]

    def window_open(self, conversation_id: int, minutes: int) -> bool:
        """Whether a student's next message in this chat can skip review: the latest message they sent was
        approved by a mentor, or itself sent in the window, less than this many minutes ago. A rejected,
        privately handled, flagged, or withdrawn message closes the window (so a student can't withdraw a
        flagged message and reword it until the screener passes it), and so does a MENTOR ALERT (which makes the
        chat private). The first message of every chat is always reviewed."""
        if minutes <= 0:
            return False
        conv = self.db.one("SELECT visibility FROM conversations WHERE id = ?", (conversation_id,))
        if conv is None or conv["visibility"] != "team":
            return False
        last = self.db.one(
            "SELECT status, created_at, reviewed_at FROM messages WHERE conversation_id = ? AND role = 'user' "
            "ORDER BY id DESC LIMIT 1", (conversation_id,))
        if last is None:
            return False
        if last["status"] in (APPROVED, EDITED):
            cleared = last["reviewed_at"]
        elif last["status"] == GRACE:
            cleared = last["created_at"]
        else:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        return bool(cleared) and cleared > cutoff

    def window_messages(self, hours: int = 24, limit: int = 50) -> list[sqlite3.Row]:
        """Messages sent without review in the approval window, newest first, for mentors to look over."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        return self.db.all(
            "SELECT m.*, a.name AS author_name, c.title AS conversation_title "
            "FROM messages m JOIN users a ON a.id = m.author_id JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.status = 'grace' AND m.created_at >= ? ORDER BY m.id DESC LIMIT ?",
            (since, limit),
        )

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

    # -------------------------------------------------------------- slack DMs
    def claim_dm_event(self, channel: str, ts: str) -> bool:
        """Remember a Slack message. False when it was seen before (Slack delivered it twice)."""
        cur = self.db.execute("INSERT OR IGNORE INTO slack_dm_messages (channel, ts, created_at) VALUES (?, ?, ?)",
                              (channel, ts, utcnow()))
        return cur.rowcount > 0

    def release_dm_event(self, channel: str, ts: str) -> None:
        """Forget a Slack message that couldn't be processed, so Slack's retry is handled."""
        self.db.execute("DELETE FROM slack_dm_messages WHERE channel = ? AND ts = ? AND message_id IS NULL",
                        (channel, ts))

    def link_dm_message(self, channel: str, ts: str, message_id: int) -> None:
        self.db.execute("UPDATE slack_dm_messages SET message_id = ? WHERE channel = ? AND ts = ?",
                        (message_id, channel, ts))

    def dm_message(self, message_id: int) -> sqlite3.Row | None:
        """Where a message came in from Slack, or None if it was sent on the website."""
        return self.db.one("SELECT * FROM slack_dm_messages WHERE message_id = ?", (message_id,))

    def link_slack_thread(self, conversation_id: int, channel: str, thread_ts: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO slack_threads (conversation_id, channel, thread_ts) VALUES (?, ?, ?)",
                        (conversation_id, channel, thread_ts))

    def slack_thread(self, conversation_id: int) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM slack_threads WHERE conversation_id = ?", (conversation_id,))

    def conversation_in_thread(self, channel: str, thread_ts: str) -> int | None:
        row = self.db.one("SELECT conversation_id FROM slack_threads WHERE channel = ? AND thread_ts = ?",
                          (channel, thread_ts))
        return row["conversation_id"] if row else None

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
