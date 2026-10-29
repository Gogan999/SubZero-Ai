"""SQLite storage. One file, versioned schema, short-lived connections."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # Version 1: milestones 1 and 2
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        slack_user_id TEXT NOT NULL UNIQUE,
        slack_team_id TEXT,
        name TEXT NOT NULL,
        email TEXT,
        avatar_url TEXT,
        role TEXT NOT NULL DEFAULT 'student',
        subteam TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_login_at TEXT
    );

    CREATE TABLE conversations (
        id INTEGER PRIMARY KEY,
        owner_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT,
        subteam TEXT,
        visibility TEXT NOT NULL DEFAULT 'team' CHECK (visibility IN ('team', 'private')),
        model TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_conversations_updated ON conversations(updated_at DESC);

    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        author_id INTEGER REFERENCES users(id),
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL DEFAULT '',
        original_content TEXT,
        status TEXT NOT NULL,
        reviewed_by INTEGER REFERENCES users(id),
        reviewed_at TEXT,
        review_note TEXT,
        reply_to INTEGER REFERENCES messages(id),
        model TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cache_read_tokens INTEGER,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_messages_conversation ON messages(conversation_id, id);
    CREATE INDEX idx_messages_status ON messages(status);

    CREATE TABLE slack_cards (
        message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
        channel TEXT NOT NULL,
        ts TEXT NOT NULL,
        reminded_at TEXT
    );

    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        actor_id INTEGER REFERENCES users(id),
        action TEXT NOT NULL,
        target_type TEXT,
        target_id INTEGER,
        details TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_audit_created ON audit_log(created_at DESC);

    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # Version 2: milestone 3 (attachments and interactive explainers)
    """
    ALTER TABLE messages ADD COLUMN wants_explainer INTEGER NOT NULL DEFAULT 0;

    CREATE TABLE attachments (
        id INTEGER PRIMARY KEY,
        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('text', 'image')),
        media_type TEXT NOT NULL,
        size INTEGER NOT NULL,
        path TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_attachments_message ON attachments(message_id, id);
    """,
    # Version 3: milestone 4 (settings changed on the admin page; they win over config.env)
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_by INTEGER REFERENCES users(id),
        updated_at TEXT NOT NULL
    );
    """,
    # Version 4: video attachments, and the still frames taken from them for Claude
    """
    CREATE TABLE attachments_new (
        id INTEGER PRIMARY KEY,
        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('text', 'image', 'video', 'frame')),
        media_type TEXT NOT NULL,
        size INTEGER NOT NULL,
        path TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    INSERT INTO attachments_new SELECT * FROM attachments;
    DROP TABLE attachments;
    ALTER TABLE attachments_new RENAME TO attachments;
    CREATE INDEX idx_attachments_message ON attachments(message_id, id);
    """,
    # Version 5: the approval window and pre-screen
    """
    ALTER TABLE messages ADD COLUMN screen_note TEXT;
    """,
    # Version 6: projects, which group chats and give them shared instructions and reference files
    """
    CREATE TABLE projects (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        instructions TEXT NOT NULL DEFAULT '',
        created_by INTEGER REFERENCES users(id),
        archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE project_files (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('text', 'image')),
        media_type TEXT NOT NULL,
        size INTEGER NOT NULL,
        path TEXT NOT NULL,
        added_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_project_files ON project_files(project_id, id);

    ALTER TABLE conversations ADD COLUMN project_id INTEGER REFERENCES projects(id);
    CREATE INDEX idx_conversations_project ON conversations(project_id, updated_at DESC);
    """,
    # Version 7: each person's preferences (Profile and preferences, Appearance)
    """
    ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'system';
    ALTER TABLE users ADD COLUMN notify_dm INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE users ADD COLUMN default_subteam TEXT;
    """,
    # Version 8: composer tools (response style, extended thinking, web search, research) and what they found
    """
    ALTER TABLE messages ADD COLUMN options TEXT;
    ALTER TABLE messages ADD COLUMN web_searches INTEGER;
    ALTER TABLE messages ADD COLUMN sources TEXT;
    """,
    # Version 9: read-only share links for people outside the team, approved by a mentor
    """
    CREATE TABLE shares (
        id INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        token TEXT NOT NULL UNIQUE,
        upto_message_id INTEGER NOT NULL,
        audience TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'declined', 'revoked')),
        requested_by INTEGER NOT NULL REFERENCES users(id),
        reviewed_by INTEGER REFERENCES users(id),
        views INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        reviewed_at TEXT,
        expires_at TEXT
    );
    CREATE INDEX idx_shares_conversation ON shares(conversation_id, id);
    CREATE INDEX idx_shares_status ON shares(status, id);
    """,
    # Version 10: Code sessions, where Claude works in a copy of the team's repository and a mentor approves
    # every file change and command
    """
    CREATE TABLE code_sessions (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        task TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('preparing', 'running', 'waiting', 'done', 'stopped', 'failed')),
        model TEXT NOT NULL,
        repo TEXT NOT NULL,
        ref TEXT NOT NULL DEFAULT '',
        transcript TEXT NOT NULL DEFAULT '[]',
        error TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE code_steps (
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES code_sessions(id) ON DELETE CASCADE,
        turn INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('text', 'tool', 'note')),
        tool TEXT,
        tool_use_id TEXT,
        input TEXT,
        output TEXT,
        status TEXT NOT NULL CHECK (status IN ('done', 'pending', 'approved', 'declined', 'running', 'error')),
        reviewed_by INTEGER REFERENCES users(id),
        review_note TEXT,
        created_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE INDEX idx_code_steps ON code_steps(session_id, id);
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
                conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {index}; COMMIT;")
        finally:
            conn.close()

    # Small helpers used across the app -------------------------------------
    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.connect() as conn:
            return conn.execute(sql, params)

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))

    def delete_kv(self, key: str) -> None:
        self.execute("DELETE FROM kv WHERE key = ?", (key,))

    def audit(
        self,
        action: str,
        *,
        actor_id: int | None,
        target_type: str | None = None,
        target_id: int | None = None,
        details: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        sql = (
            "INSERT INTO audit_log (actor_id, action, target_type, target_id, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        params = (
            actor_id,
            action,
            target_type,
            target_id,
            json.dumps(details or {}, ensure_ascii=False),
            utcnow(),
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            self.execute(sql, params)
