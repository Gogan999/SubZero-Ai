"""Projects: a group of chats about one mechanism or subsystem, with instructions and reference files that
Claude reads in every chat in the project.

Mentors set projects up and everyone on the team can chat in them. Instructions and files go to Claude without
a per-message review, so only mentors may change them; every change goes in the audit log, and the approval
card for a student message names the project it's in."""

from __future__ import annotations

import base64
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .attachments import Prepared
from .db import Database, utcnow

MAX_NAME_CHARS = 80
MAX_DESCRIPTION_CHARS = 300
MAX_INSTRUCTIONS_CHARS = 8000
MAX_PROJECT_FILES = 20
# Everything in a project is sent with every reply in it, so keep the total small enough to stay cheap.
MAX_PROJECT_TEXT_BYTES = 400 * 1024


@dataclass
class ProjectError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def clean(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


class Projects:
    def __init__(self, db: Database, files_dir: Path):
        self.db = db
        self.files_dir = Path(files_dir)

    # ------------------------------------------------------------------ projects
    def get(self, project_id: int | None) -> sqlite3.Row | None:
        if project_id is None:
            return None
        return self.db.one("SELECT * FROM projects WHERE id = ?", (project_id,))

    def active(self, project_id: int | None) -> sqlite3.Row | None:
        row = self.get(project_id)
        return row if row is not None and not row["archived"] else None

    def list(self, *, archived: bool = False) -> list[sqlite3.Row]:
        return self.db.all(
            "SELECT p.*, (SELECT COUNT(*) FROM conversations c WHERE c.project_id = p.id) AS chat_count, "
            "(SELECT COUNT(*) FROM project_files f WHERE f.project_id = p.id) AS file_count "
            "FROM projects p WHERE p.archived = ? ORDER BY p.updated_at DESC",
            (int(archived),),
        )

    @staticmethod
    def _check(name: str, description: str, instructions: str) -> None:
        if not name:
            raise ProjectError("Give the project a name.")
        if len(name) > MAX_NAME_CHARS:
            raise ProjectError(f"Keep the name under {MAX_NAME_CHARS} characters.")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise ProjectError(f"Keep the description under {MAX_DESCRIPTION_CHARS} characters.")
        if len(instructions) > MAX_INSTRUCTIONS_CHARS:
            raise ProjectError(f"Keep the instructions under {MAX_INSTRUCTIONS_CHARS:,} characters.")

    def create(self, actor_id: int, name: str, description: str = "", instructions: str = "") -> int:
        name, description, instructions = clean(name), clean(description), clean(instructions)
        self._check(name, description, instructions)
        now = utcnow()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO projects (name, description, instructions, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (name, description, instructions, actor_id, now, now))
            project_id = cur.lastrowid
            self.db.audit("project_created", actor_id=actor_id, target_type="project", target_id=project_id,
                          details={"name": name, "instructions": instructions}, conn=conn)
        return project_id

    def update(self, project_id: int, actor_id: int, name: str, description: str, instructions: str) -> bool:
        row = self.get(project_id)
        if row is None:
            raise ProjectError("That project doesn't exist.")
        name, description, instructions = clean(name), clean(description), clean(instructions)
        self._check(name, description, instructions)
        changes = {key: {"from": row[key], "to": value}
                   for key, value in (("name", name), ("description", description), ("instructions", instructions))
                   if row[key] != value}
        if not changes:
            return False
        with self.db.transaction() as conn:
            conn.execute("UPDATE projects SET name = ?, description = ?, instructions = ?, updated_at = ? WHERE id = ?",
                         (name, description, instructions, utcnow(), project_id))
            self.db.audit("project_changed", actor_id=actor_id, target_type="project", target_id=project_id,
                          details=changes, conn=conn)
        return True

    def set_archived(self, project_id: int, actor_id: int, archived: bool) -> None:
        self.db.execute("UPDATE projects SET archived = ?, updated_at = ? WHERE id = ?",
                        (int(archived), utcnow(), project_id))
        self.db.audit("project_archived" if archived else "project_restored", actor_id=actor_id,
                      target_type="project", target_id=project_id)

    def touch(self, conn: sqlite3.Connection, project_id: int) -> None:
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utcnow(), project_id))

    # --------------------------------------------------------------------- files
    def files(self, project_id: int) -> list[sqlite3.Row]:
        return self.db.all("SELECT * FROM project_files WHERE project_id = ? ORDER BY id", (project_id,))

    def file(self, project_id: int, file_id: int) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM project_files WHERE id = ? AND project_id = ?", (file_id, project_id))

    def add_files(self, project_id: int, actor_id: int, items: list[Prepared]) -> None:
        if any(item.kind not in ("text", "image") for item in items):
            raise ProjectError("Projects take code, text and PNG or JPG files. Attach videos to a chat message instead.")
        existing = self.files(project_id)
        if len(existing) + len(items) > MAX_PROJECT_FILES:
            raise ProjectError(f"A project can hold up to {MAX_PROJECT_FILES} files. Remove some first.")
        text_bytes = sum(f["size"] for f in existing if f["kind"] == "text") + sum(
            item.size for item in items if item.kind == "text")
        if text_bytes > MAX_PROJECT_TEXT_BYTES:
            raise ProjectError(f"A project's text files can add up to {MAX_PROJECT_TEXT_BYTES // 1024} KB, because "
                               "Claude reads all of them for every reply. Add just the files that matter most.")
        folder = self.files_dir / "projects"
        folder.mkdir(parents=True, exist_ok=True)
        with self.db.transaction() as conn:
            for item in items:
                ext = ".txt" if item.kind == "text" else ".png" if item.media_type == "image/png" else ".jpg"
                relative = f"projects/{project_id}-{secrets.token_hex(8)}{ext}"
                (self.files_dir / relative).write_bytes(item.data)
                conn.execute(
                    "INSERT INTO project_files (project_id, name, kind, media_type, size, path, added_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (project_id, item.name, item.kind, item.media_type, item.size, relative, actor_id, utcnow()))
            self.touch(conn, project_id)
            self.db.audit("project_files_added", actor_id=actor_id, target_type="project", target_id=project_id,
                          details={"files": [f"{item.name} ({item.size:,} bytes)" for item in items]}, conn=conn)

    def remove_file(self, project_id: int, file_id: int, actor_id: int) -> None:
        row = self.file(project_id, file_id)
        if row is None:
            raise ProjectError("That file is already gone.")
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM project_files WHERE id = ?", (file_id,))
            self.touch(conn, project_id)
            self.db.audit("project_file_removed", actor_id=actor_id, target_type="project", target_id=project_id,
                          details={"file": row["name"]}, conn=conn)
        try:
            self.path(row).unlink()
        except OSError:
            pass

    def path(self, row: sqlite3.Row) -> Path:
        path = (self.files_dir / row["path"]).resolve()
        if self.files_dir.resolve() not in path.parents or not path.is_file():
            raise FileNotFoundError(row["path"])
        return path

    # ------------------------------------------------------------- for Claude
    def system_text(self, project: sqlite3.Row) -> str:
        """The project's instructions, added after the cached system prompt."""
        text = f'This chat is part of the team project "{project["name"]}".'
        if project["description"]:
            text += f" About the project: {project['description']}"
        if project["instructions"]:
            text += ("\n\nThe team's mentors wrote these instructions for chats in this project. Follow them unless "
                     "they conflict with the safety rules above:\n<project_instructions>\n"
                     + project["instructions"] + "\n</project_instructions>")
        return text

    def claude_blocks(self, project_id: int) -> list[dict]:
        """The project's reference files as content blocks, put before the chat's first message."""
        rows = self.files(project_id)
        if not rows:
            return []
        blocks: list[dict] = [{"type": "text", "text": "Reference files the team's mentors added to this project:"}]
        for row in rows:
            name = row["name"].replace('"', "'")
            try:
                data = self.path(row).read_bytes()
            except OSError:
                blocks.append({"type": "text", "text": f'<file name="{name}">(This file is missing.)</file>'})
                continue
            if row["kind"] == "image":
                blocks.append({"type": "text", "text": f"Image: {name}"})
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": row["media_type"], "data": base64.b64encode(data).decode("ascii")}})
            else:
                text = data.decode("utf-8", errors="replace")
                blocks.append({"type": "text", "text": f'<file name="{name}">\n{text}\n</file>'})
        blocks.append({"type": "text", "text": "End of the project's reference files. The chat starts below."})
        return blocks

