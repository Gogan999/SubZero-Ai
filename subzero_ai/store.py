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

    def save_preferences(self, user_id: int, *, theme: str | None = None, notify_dm: bool | None = None,
                         default_subteam: str | None | bool = False) -> None:
        """Change any of the given preferences. default_subteam=None clears it; False leaves it alone."""
        if theme is not None:
            self.db.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, user_id))
        if notify_dm is not None:
            self.db.execute("UPDATE users SET notify_dm = ? WHERE id = ?", (int(notify_dm), user_id))
        if default_subteam is not False:
            self.db.execute("UPDATE users SET default_subteam = ? WHERE id = ?", (default_subteam, user_id))

    def deactivate_user(self, user_id: int) -> None:
        self.db.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))

    def active_users(self) -> list[sqlite3.Row]:
        return self.db.all("SELECT * FROM users WHERE active = 1")

    # ---------------------------------------------------------- conversations
    def create_conversation(self, conn: sqlite3.Connection, *, owner_id: int, model: str,
                            subteam: str | None = None, project_id: int | None = None) -> int:
        now = utcnow()
        cur = conn.execute(
            "INSERT INTO conversations (owner_id, subteam, model, project_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (owner_id, subteam, model, project_id, now, now),
        )
        return cur.lastrowid

    def conversation(self, conversation_id: int) -> sqlite3.Row | None:
        return self.db.one(
            "SELECT c.*, u.name AS owner_name, u.slack_user_id AS owner_slack_id, u.avatar_url AS owner_avatar, "
            "p.name AS project_name FROM conversations c JOIN users u ON u.id = c.owner_id "
            "LEFT JOIN projects p ON p.id = c.project_id WHERE c.id = ?",
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
            "SELECT 1 FROM messages WHERE conversation_id = ? AND role = 'user' AND status IN ('approved','edited','auto','grace') LIMIT 1",
            (conv["id"],),
        )
        return row is not None

    def set_model(self, conversation_id: int, model: str) -> None:
        self.db.execute("UPDATE conversations SET model = ? WHERE id = ?", (model, conversation_id))

    def list_conversations(self, viewer: Viewer, *, scope: str = "team", query: str = "",
                           subteam: str | None = None, owner_id: int | None = None,
                           project_id: int | None = None, limit: int = 200) -> list[sqlite3.Row]:
        params: list = []
        where = []
        if project_id is not None:
            where.append("c.project_id = ?")
            params.append(project_id)
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
                "m2.conversation_id = c.id AND m2.role = 'user' AND m2.status IN ('approved','edited','auto','grace'))))"
            )
            params.append(viewer.id)
        if query:
            where.append(
                "(c.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM messages m3 WHERE m3.conversation_id = c.id AND "
                "m3.content LIKE ? ESCAPE '\\' AND (m3.role = 'assistant' OR m3.status IN ('approved','edited','auto','grace') OR c.owner_id = ?)))"
            )
            # Search for what was typed: % and _ are wildcards in LIKE, so escape them.
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
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
                    model: str | None = None, wants_explainer: bool = False, options: str | None = None) -> int:
        now = utcnow()
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, author_id, role, content, status, reply_to, model, "
            "wants_explainer, options, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, author_id, role, content, status, reply_to, model, int(wants_explainer), options,
             now, now),
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
            "c.title AS conversation_title, c.subteam AS conversation_subteam, p.name AS project_name "
            "FROM messages m JOIN users a ON a.id = m.author_id JOIN conversations c ON c.id = m.conversation_id "
            "LEFT JOIN projects p ON p.id = c.project_id "
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

    def web_searches_today(self) -> int:
        """Web searches the whole team's replies have used since local midnight."""
        local_midnight = datetime.now(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        since = local_midnight.astimezone(timezone.utc).isoformat(timespec="seconds")
        row = self.db.one("SELECT COALESCE(SUM(web_searches), 0) AS n FROM messages WHERE role = 'assistant' "
                          "AND created_at >= ?", (since,))
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

    def last_context_tokens(self, conversation_id: int) -> int:
        """How many tokens Claude read for the latest reply: roughly the size of the whole chat."""
        row = self.db.one(
            "SELECT COALESCE(input_tokens, 0) + COALESCE(cache_read_tokens, 0) AS n FROM messages "
            "WHERE conversation_id = ? AND role = 'assistant' AND status = 'complete' ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        )
        return row["n"] if row else 0

    def has_completed_reply(self, conversation_id: int) -> int:
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ? AND role = 'assistant' AND status = 'complete'",
            (conversation_id,),
        )
        return row["n"]

    def explainer_replies(self, *, owner_id: int | None = None, limit: int = 5000) -> list[sqlite3.Row]:
        """Finished replies that hold at least one interactive explainer, newest first, without their text. The
        caller checks who may see each one."""
        where = ("m.role = 'assistant' AND m.status = 'complete' "
                 "AND (m.content LIKE '%```explainer%' OR m.content LIKE '%~~~explainer%')")
        params: list = []
        if owner_id is not None:
            where += " AND c.owner_id = ?"
            params.append(owner_id)
        params.append(limit)
        return self.db.all(
            "SELECT m.id, m.conversation_id, m.created_at FROM messages m "
            f"JOIN conversations c ON c.id = m.conversation_id WHERE {where} ORDER BY m.id DESC LIMIT ?",
            tuple(params),
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
