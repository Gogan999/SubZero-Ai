"""Files attached to old chat messages. Nothing can be attached any more (0.11), but the Review page still shows
the files that mentors saw before, so this only reads them: names, sizes, and the stored file itself. Files live in
DATA_DIR/files and the database keeps their names and sizes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import Database


def size_label(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB" if size >= 10 * 1024 else f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


class Attachments:
    """Stored attachments of old messages, read only."""

    def __init__(self, db: Database, files_dir: Path):
        self.db = db
        self.files_dir = Path(files_dir)

    def get(self, attachment_id: int) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))

    def for_message(self, message_id: int) -> list[sqlite3.Row]:
        return self.db.all("SELECT * FROM attachments WHERE message_id = ? ORDER BY id", (message_id,))

    def for_conversation(self, conversation_id: int) -> dict[int, list[sqlite3.Row]]:
        rows = self.db.all(
            "SELECT a.* FROM attachments a JOIN messages m ON m.id = a.message_id "
            "WHERE m.conversation_id = ? ORDER BY a.id", (conversation_id,))
        out: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            out.setdefault(row["message_id"], []).append(row)
        return out

    def path(self, row: sqlite3.Row) -> Path:
        path = (self.files_dir / row["path"]).resolve()
        if self.files_dir.resolve() not in path.parents or not path.is_file():
            raise FileNotFoundError(row["path"])
        return path
