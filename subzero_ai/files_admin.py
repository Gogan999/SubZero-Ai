"""What is stored on the mini PC that came from people's work, and deleting it: old chat attachments, old project
files, Code workspaces, sandbox disks and old-chat archives. No web code lives here (web_files.py is the page).

Every item has an opaque id made here ("a-12", "w-7", "z-old-chats-20260101-120000.zip"). A path is never taken from a
form: the id is checked against a strict pattern, then looked up in the database (or the backups folder), and the path
is worked out from that and checked to be inside the folder it must be in. The sandbox base image is not a kind of item,
and sandboxes are only ever deleted through the sandbox manager, which names the sandbox's own disk."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import archive
from .code_runner import STUCK_SESSION_DAYS
from .config import Settings
from .db import Database, utcnow
from .sandbox import EXTEND_LIMIT, PREFIX, SandboxError

log = logging.getLogger("subzero.files")

ATTACHMENT, PROJECT, WORKSPACE, SANDBOX, ARCHIVE = "attachment", "project", "workspace", "sandbox", "archive"
KINDS = (ATTACHMENT, PROJECT, WORKSPACE, SANDBOX, ARCHIVE)
LABELS = {
    ATTACHMENT: "Old chat files",
    PROJECT: "Old project files",
    WORKSPACE: "Code copies",
    SANDBOX: "Sandbox disks",
    ARCHIVE: "Old-chat archives",
}
HINTS = {
    ATTACHMENT: "Pictures, text files and videos from chats on the website, from before chat moved to Slack.",
    PROJECT: "Reference files from the old Projects feature, which no longer exists.",
    WORKSPACE: "The copy of the repository that a Code session works in, and the changes it saved.",
    SANDBOX: "The disk of the virtual computer a Code session runs its commands in.",
    ARCHIVE: "Zip files made by Settings, Old website chats. They are the only copy of those chats once deleted.",
}
# Code sessions in these states are working or waiting for someone, so their files and sandbox are in use
ACTIVE_STATES = ("awaiting_approval", "preparing", "running", "waiting")
ID_RE = re.compile(r"(?:[apws]-[1-9]\d{0,8}|z-old-chats-\d{8}-\d{6}\.zip)")
BULK_TYPED_OVER = 20  # deleting more than this many items at once needs the number typed
AUDIT_NAMES = 20  # names kept in one audit row
SAFE_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_VIEW_BYTES = 5 * 1024 * 1024  # a text file bigger than this is offered as a download, not shown


@dataclass
class Item:
    id: str
    kind: str
    name: str
    owner: str
    owner_id: int | None
    size: int
    when: str  # ISO time the item was made or last used (for age)
    detail: str = ""
    link: str = ""  # a page in the app about this item (its chat or Code session)
    view: str = ""  # "image" or "text" when the browser can show it
    in_use: bool = False
    path: Path | None = field(default=None, repr=False)

    @property
    def number(self) -> str:
        return self.id.split("-", 1)[1]

    def age_days(self, now: datetime | None = None) -> float:
        try:
            when = datetime.fromisoformat(self.when)
        except ValueError:
            return 0.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, ((now or datetime.now(timezone.utc)) - when).total_seconds() / 86400)


@dataclass
class Outcome:
    deleted: list[Item] = field(default_factory=list)
    bytes: int = 0
    in_use: int = 0
    failed: list[str] = field(default_factory=list)


def valid_id(item_id: str) -> bool:
    return bool(ID_RE.fullmatch(item_id or ""))


def size_of_tree(root: Path) -> int:
    """Bytes in a folder, not following links."""
    total = 0
    for base, _dirs, names in os.walk(root, followlinks=False):
        for name in names:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


def inside(root: Path, relative: str) -> Path | None:
    """The file `relative` under `root`, only if it exists and really is inside root (links and .. resolved)."""
    base = Path(root).resolve()
    try:
        path = (base / relative).resolve()
    except (OSError, ValueError):
        return None
    return path if base in path.parents and path.is_file() else None


def view_kind(kind: str, media_type: str, name: str, size: int) -> str:
    """What the browser may show inline: pictures in safe formats, and plain text. Nothing is ever rendered as HTML."""
    if media_type in SAFE_IMAGES:
        return "image"
    if kind == "text" and size <= MAX_VIEW_BYTES:
        return "text"
    return ""


def audit_names(items: list[Item]) -> tuple[list[str], int]:
    names = [f"{i.id}: {i.name[:40]}" for i in items[:AUDIT_NAMES]]
    return names, max(0, len(items) - AUDIT_NAMES)


class FileStore:
    def __init__(self, settings: Settings, db: Database, code: Any = None, sandboxes: Any = None):
        self.settings = settings
        self.db = db
        self.code = code
        self.sandboxes = sandboxes
        self.files_dir = settings.data_dir / "files"
        self.code_dir = settings.data_dir / "code"

    # ------------------------------------------------------------------ inventory
    def items(self, kinds: tuple[str, ...] = KINDS) -> list[Item]:
        found: list[Item] = []
        if ATTACHMENT in kinds:
            found += self._attachments()
        if PROJECT in kinds:
            found += self._project_files()
        if WORKSPACE in kinds:
            found += self._workspaces()
        if SANDBOX in kinds:
            found += self._sandbox_disks()
        if ARCHIVE in kinds:
            found += self._archives()
        return found

    def _attachments(self) -> list[Item]:
        rows = self.db.all(
            "SELECT a.id, a.name, a.kind, a.media_type, a.path, a.created_at, c.id AS chat_id, c.owner_id, "
            "u.name AS owner FROM attachments a JOIN messages m ON m.id = a.message_id "
            "JOIN conversations c ON c.id = m.conversation_id LEFT JOIN users u ON u.id = c.owner_id ORDER BY a.id")
        out = []
        for row in rows:
            path = inside(self.files_dir, row["path"])
            if path is None:
                continue  # the file is already gone, so there is nothing to show or delete
            size = path.stat().st_size
            what = {"video": "Video", "frame": "Still from a video", "image": "Picture", "text": "Text file"}.get(
                row["kind"], "File")
            out.append(Item(f"a-{row['id']}", ATTACHMENT, row["name"], row["owner"] or "", row["owner_id"], size,
                            row["created_at"], f"{what}, in an old chat", f"/c/{row['chat_id']}",
                            view_kind(row["kind"], row["media_type"], row["name"], size), path=path))
        return out

    def _project_files(self) -> list[Item]:
        rows = self.db.all(
            "SELECT f.id, f.name, f.kind, f.media_type, f.path, f.created_at, f.added_by, p.name AS project, "
            "u.name AS owner FROM project_files f JOIN projects p ON p.id = f.project_id "
            "LEFT JOIN users u ON u.id = f.added_by ORDER BY f.id")
        out = []
        for row in rows:
            path = inside(self.files_dir, row["path"])
            if path is None:
                continue
            size = path.stat().st_size
            out.append(Item(f"p-{row['id']}", PROJECT, row["name"], row["owner"] or "", row["added_by"], size,
                            row["created_at"], f"In the old project \"{row['project']}\"", "",
                            view_kind(row["kind"], row["media_type"], row["name"], size), path=path))
        return out

    def session_in_use(self, session_id: int) -> bool:
        """True when a Code session is starting, working or waiting for a person, so its files can't be deleted."""
        row = self.db.one("SELECT status FROM code_sessions WHERE id = ?", (session_id,))
        if row is None:
            return False
        busy = getattr(self.code, "_busy", ()) or ()
        return row["status"] in ACTIVE_STATES or session_id in busy

    def _workspaces(self) -> list[Item]:
        if not self.code_dir.is_dir():
            return []
        rows = self.db.all("SELECT s.id, s.title, s.status, s.updated_at, s.created_by, u.name AS owner "
                           "FROM code_sessions s LEFT JOIN users u ON u.id = s.created_by ORDER BY s.id")
        out = []
        for row in rows:
            folder = self.code_dir / str(row["id"])
            if folder.is_symlink() or not folder.is_dir():
                continue
            out.append(Item(f"w-{row['id']}", WORKSPACE, row["title"], row["owner"] or "", row["created_by"],
                            size_of_tree(folder), row["updated_at"], f"Session {row['status']}", f"/code/{row['id']}",
                            in_use=self.session_in_use(row["id"]), path=folder))
        return out

    def _sandbox_rows(self) -> list[sqlite3.Row]:
        return self.db.all(
            "SELECT sb.*, cs.title AS session_title, cs.status AS session_status, cs.created_by AS owner_id, "
            "u.name AS owner FROM sandbox_sessions sb LEFT JOIN code_sessions cs ON cs.id = sb.code_session_id "
            "LEFT JOIN users u ON u.id = cs.created_by WHERE sb.state != 'destroyed' AND sb.name LIKE ? "
            "ORDER BY sb.id", (PREFIX + "%",))

    def _disk_size(self, row: sqlite3.Row) -> int:
        """How big the sandbox's disk is now. 0 when it can't be read (the sandbox is already gone, or off)."""
        if self.sandboxes is None:
            return 0
        try:
            backend = self.sandboxes.backend(row["backend"])
            if hasattr(backend, "disk_bytes"):
                return int(backend.disk_bytes(self.sandboxes.handle_of(row)) or 0)
            root = getattr(backend, "root", None)
            home = Path(root) / row["name"] if root else None
            return size_of_tree(home) if home is not None and home.is_dir() else 0
        except Exception:
            log.info("Couldn't read the size of sandbox %s", row["name"], exc_info=True)
            return 0

    def _sandbox_disks(self) -> list[Item]:
        out = []
        for row in self._sandbox_rows():
            sid = row["code_session_id"]
            title = row["session_title"] or "A deleted session"
            out.append(Item(f"s-{row['id']}", SANDBOX, title, row["owner"] or "", row["owner_id"],
                            self._disk_size(row), row["created_at"], f"Sandbox {row['state']}",
                            f"/code/{sid}" if row["session_title"] else "", in_use=self._sandbox_in_use(row)))
        return out

    def _sandbox_in_use(self, row: sqlite3.Row) -> bool:
        if row["state"] == "creating":
            return True
        return row["code_session_id"] is not None and self.session_in_use(row["code_session_id"])

    def _archives(self) -> list[Item]:
        out = []
        for info in archive.list_archives(self.settings):
            when = datetime.fromtimestamp(info.path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            out.append(Item(f"z-{info.name}", ARCHIVE, info.name, "", None, info.size, when,
                            "Made by an admin in Settings", path=info.path))
        return out

    def find(self, item_id: str) -> Item | None:
        """One item by its id, or None when the id is malformed or points at nothing."""
        if not valid_id(item_id):
            return None
        kind = {"a": ATTACHMENT, "p": PROJECT, "w": WORKSPACE, "s": SANDBOX, "z": ARCHIVE}[item_id[0]]
        return next((i for i in self.items((kind,)) if i.id == item_id), None)

    def find_all(self, ids: list[str]) -> list[Item]:
        """The items for these ids, once each, in the order given. Unknown or malformed ids are dropped."""
        wanted = list(dict.fromkeys(i for i in ids if valid_id(i)))
        if not wanted:
            return []
        kinds = tuple(k for k, letter in zip(KINDS, "apwsz") if any(i.startswith(letter + "-") for i in wanted))
        by_id = {i.id: i for i in self.items(kinds)}
        return [by_id[i] for i in wanted if i in by_id]

    # ------------------------------------------------------------------ summary
    def disk(self) -> dict[str, Any]:
        """Free and total space on the drive holding the data folder, and whether free space is low."""
        folder = self.settings.data_dir
        while not folder.exists() and folder != folder.parent:
            folder = folder.parent
        usage = shutil.disk_usage(folder)
        free_gb = usage.free / 1024 ** 3
        low = free_gb < max(10, int(self.settings.sandbox_min_free_gb)) or usage.free < usage.total * 0.1
        return {"free": usage.free, "total": usage.total, "low": low,
                "min_gb": max(10, int(self.settings.sandbox_min_free_gb))}

    def rules(self) -> list[dict[str, str]]:
        """The automatic clean-up rules in plain words, with their current values."""
        st = self.settings
        keep = int(st.code_keep_days)
        days = int(st.retention_days)
        return [
            {"what": "Finished Code sessions",
             "rule": f"The copy of the repository is deleted {keep} days after the session ends. The steps stay, so the "
                     "session can still be read." if keep > 0 else "The copy of the repository is deleted "
                     "right away once a session has ended."},
            {"what": "Stuck Code sessions",
             "rule": f"A session left waiting or working with no activity for {STUCK_SESSION_DAYS} days is stopped and "
                     "its copy is deleted."},
            {"what": "Sandboxes",
             "rule": f"A sandbox is deleted {int(st.sandbox_ttl_hours)} hours after it is made, or when its session "
                     f"ends. A mentor can extend it, up to {EXTEND_LIMIT.days} days in all."},
            {"what": "Old chats and their files",
             "rule": (f"Chats with no activity for {days} days are deleted with their files."
                      if days > 0 else "Chats and their files are kept forever.")},
            {"what": "Old project files", "rule": "Never deleted automatically. Delete them here."},
            {"what": "Old-chat archives", "rule": "Never deleted automatically. Delete them here."},
            {"what": "Nightly backups",
             "rule": f"Backups are kept for {int(st.backup_keep_days)} days. Not listed here: they are in Settings."},
        ]

    def backup_note(self) -> str:
        """Files deleted here can still be in a nightly backup. Say when the newest backup is deleted."""
        last = self.db.get_kv("last_backup_at")
        if not last:
            return ""
        try:
            expires = datetime.fromisoformat(last) + timedelta(days=int(self.settings.backup_keep_days))
        except ValueError:
            return ""
        return expires.strftime("%b %d").replace(" 0", " ")

    # ------------------------------------------------------------------ deleting
    def delete(self, items: list[Item], *, actor_id: int, mode: str) -> Outcome:
        """Delete these items, checking again that each is still safe to delete. Writes the audit rows (one per kind)."""
        outcome = Outcome()
        for item in items:
            try:
                size = self._delete_one(item, actor_id)
            except InUse:
                outcome.in_use += 1
            except (OSError, SandboxError, FileNotFoundError) as exc:
                log.warning("Couldn't delete %s", item.id, exc_info=True)
                outcome.failed.append(f"{item.name}: {str(exc) or type(exc).__name__}"[:120])
            else:
                if size is not None:
                    outcome.deleted.append(item)
                    outcome.bytes += size
        for kind in KINDS:
            gone = [i for i in outcome.deleted if i.kind == kind]
            if not gone:
                continue
            names, more = audit_names(gone)
            details: dict[str, Any] = {"kind": kind, "mode": mode, "count": len(gone),
                                       "bytes": sum(i.size for i in gone), "items": names}
            if more:
                details["more"] = more
            self.db.audit("files_deleted", actor_id=actor_id, target_type="files", details=details)
        return outcome

    def _delete_one(self, item: Item, actor_id: int) -> int | None:
        """Delete one item and return the bytes freed, or None when it was already gone. Raises InUse."""
        letter = item.id[0]
        number = int(item.number) if letter != "z" else 0
        if letter == "a":
            return self._delete_attachment(number)
        if letter == "p":
            return self._delete_project_file(number)
        if letter == "w":
            return self._delete_workspace(number)
        if letter == "s":
            return self._delete_sandbox(number, actor_id)
        return self._delete_archive(item.id[2:])

    def _delete_attachment(self, attachment_id: int) -> int | None:
        row = self.db.one("SELECT path FROM attachments WHERE id = ?", (attachment_id,))
        path = inside(self.files_dir, row["path"]) if row is not None else None
        if path is None:
            return None
        size = path.stat().st_size
        path.unlink()  # the row stays, so the old chat still lists the file, as removed
        return size

    def _delete_project_file(self, file_id: int) -> int | None:
        row = self.db.one("SELECT path, project_id FROM project_files WHERE id = ?", (file_id,))
        path = inside(self.files_dir, row["path"]) if row is not None else None
        if path is None:
            return None
        size = path.stat().st_size
        path.unlink()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM project_files WHERE id = ?", (file_id,))
            pid = row["project_id"]
            # A project with no files left goes too, unless a chat still points at it
            conn.execute("DELETE FROM projects WHERE id = ? AND NOT EXISTS (SELECT 1 FROM project_files "
                         "WHERE project_id = ?) AND NOT EXISTS (SELECT 1 FROM conversations WHERE project_id = ?)",
                         (pid, pid, pid))
        return size

    def _delete_workspace(self, session_id: int) -> int | None:
        if self.session_in_use(session_id):
            raise InUse
        row = self.db.one("SELECT id FROM code_sessions WHERE id = ?", (session_id,))
        folder = self.code_dir / str(session_id)
        root = self.code_dir.resolve()
        if row is None or folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != root:
            return None
        size = size_of_tree(folder)
        shutil.rmtree(folder)
        if folder.exists():
            raise OSError("Some files couldn't be deleted. Try again in a minute.")
        self.db.execute(
            "INSERT INTO code_steps (session_id, turn, kind, output, status, created_at) VALUES (?, 0, 'note', ?, "
            "'done', ?)", (session_id, "A mentor deleted this session's copy of the repository from the Files page. "
                                       "The changes it saved (the patch) were deleted with it, so they can't be "
                                       "downloaded any more.", utcnow()))
        return size

    def _delete_sandbox(self, row_id: int, actor_id: int) -> int | None:
        if self.sandboxes is None:
            raise SandboxError("Sandboxes are turned off, so there is nothing to delete.")
        row = next((r for r in self._sandbox_rows() if r["id"] == row_id), None)
        if row is None:
            return None
        if self._sandbox_in_use(row):
            raise InUse
        size = self._disk_size(row)
        self.sandboxes.destroy_record(row, reason="deleted from the Files page", actor_id=actor_id)
        return size

    def _delete_archive(self, name: str) -> int | None:
        path = archive.archive_path(self.settings, name)
        if path is None:
            return None
        size = path.stat().st_size
        path.unlink()
        return size


class InUse(Exception):
    """The item belongs to a Code session that is starting, working or waiting."""
