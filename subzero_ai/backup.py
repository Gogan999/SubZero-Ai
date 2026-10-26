"""Backups of the database, attached files, and changed prompts, and putting one back.

A backup is one zip file in the backups folder, named for when it was made, like subzero-20261025-020000.zip.
config.env is left out on purpose: it holds the API key and Slack tokens, and the backups folder may be synced
to OneDrive or Google Drive. Keep a copy of config.env somewhere safe instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from . import __version__
from .config import Settings
from .db import MIGRATIONS

log = logging.getLogger("subzero.backup")

NAME_RE = re.compile(r"subzero-(\d{8})-(\d{6})(?:-([a-z0-9-]+))?\.zip")
MANIFEST = "backup.json"
DB_NAME = "subzero.db"
FOLDERS = ("files", "prompts")  # folders under DATA_DIR that go in each backup
# Videos (up to 50 MB each) are left out: every backup is a full copy, so 30 days of backups would hold 30 copies
# of every clip. The stills Claude saw are small and stay in. A restore keeps the videos already on disk.
VIDEO_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm")


class BackupError(Exception):
    pass


@dataclass
class BackupInfo:
    path: Path
    created: datetime  # local time on this PC
    size: int
    label: str

    @property
    def name(self) -> str:
        return self.path.name


def make_backup(settings: Settings, *, label: str = "", now: datetime | None = None) -> Path:
    """Copy the database (safely, even while the app is running), attached files, and prompts into a new zip."""
    if not settings.db_path.exists():
        raise BackupError(f"There's no database at {settings.db_path} to back up.")
    now = now or datetime.now()
    label = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    dest = settings.backups_path
    dest.mkdir(parents=True, exist_ok=True)
    name = f"subzero-{now:%Y%m%d-%H%M%S}{'-' + label if label else ''}.zip"
    final = dest / name
    partial = dest / (name + ".partial")
    with tempfile.TemporaryDirectory(prefix="subzero-backup-") as tmp:
        db_copy = Path(tmp) / DB_NAME
        _copy_database(settings.db_path, db_copy)
        schema = _schema_version(db_copy)
        file_count = 0
        videos_left_out = 0
        try:
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(db_copy, DB_NAME)
                for folder in FOLDERS:
                    root = settings.data_dir / folder
                    if not root.is_dir():
                        continue
                    for path in sorted(root.rglob("*")):
                        if not path.is_file() or path.name.endswith(".tmp"):
                            continue
                        if folder == "files" and path.suffix.lower() in VIDEO_SUFFIXES:
                            videos_left_out += 1
                            continue
                        zf.write(path, f"{folder}/{path.relative_to(root).as_posix()}")
                        file_count += folder == "files"
                zf.writestr(MANIFEST, json.dumps({
                    "app_version": __version__, "schema_version": schema,
                    "created_at": now.isoformat(timespec="seconds"), "attachments": file_count,
                    "videos_left_out": videos_left_out,
                }, indent=2))
            os.replace(partial, final)
        finally:
            partial.unlink(missing_ok=True)
    log.info("Backed up to %s", final)
    return final


def _copy_database(source: Path, target: Path) -> None:
    src = sqlite3.connect(source, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")  # one self-contained file, no -wal next to it
    finally:
        dst.close()
        src.close()


def _schema_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def list_backups(settings: Settings) -> list[BackupInfo]:
    """Backups in the backups folder, newest first."""
    folder = settings.backups_path
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob("subzero-*.zip"):
        match = NAME_RE.fullmatch(path.name)
        if not match:
            continue
        created = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
        found.append(BackupInfo(path, created, path.stat().st_size, match.group(3) or ""))
    return sorted(found, key=lambda b: b.created, reverse=True)


def prune_backups(settings: Settings, keep_days: int, *, now: datetime | None = None) -> list[Path]:
    """Delete backups older than keep_days. The newest backup is always kept."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=keep_days)
    backups = list_backups(settings)
    removed = []
    for info in backups[1:]:
        if info.created < cutoff:
            try:
                info.path.unlink()
                removed.append(info.path)
            except OSError:
                log.warning("Couldn't delete the old backup %s", info.path, exc_info=True)
    return removed


def find_backup(settings: Settings, name: str) -> Path:
    """A backup by file path, by file name in the backups folder, or "latest"."""
    if name == "latest":
        backups = list_backups(settings)
        if not backups:
            raise BackupError(f"There are no backups in {settings.backups_path}.")
        return backups[0].path
    path = Path(name)
    if path.is_file():
        return path
    candidate = settings.backups_path / name
    if candidate.is_file():
        return candidate
    raise BackupError(f"Couldn't find the backup {name}.")


def check_backup(path: Path) -> dict:
    """Open a backup and make sure it's complete and its database is sound. Returns its manifest."""
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError(f"{path.name} isn't a readable backup ({exc}).") from exc
    with zf, tempfile.TemporaryDirectory(prefix="subzero-check-") as tmp:
        names = set(zf.namelist())
        if MANIFEST not in names or DB_NAME not in names:
            raise BackupError(f"{path.name} isn't a SubZero AI backup.")
        manifest = json.loads(zf.read(MANIFEST))
        for name in names:
            _safe_member(name)
        db_copy = Path(tmp) / DB_NAME
        with zf.open(DB_NAME) as src, open(db_copy, "wb") as out:
            shutil.copyfileobj(src, out)
        _check_database(db_copy)
    return manifest


def _check_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"The database in the backup is damaged ({exc}).") from exc
    finally:
        conn.close()
    if result != "ok":
        raise BackupError(f"The database in the backup is damaged ({result}).")
    if version > len(MIGRATIONS):
        raise BackupError("This backup was made by a newer version of SubZero AI. Update the app first, "
                          "then restore it.")


def _safe_member(name: str) -> PurePosixPath:
    """Refuse zip entries that would land outside the data folder."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise BackupError(f"The backup has an unsafe file name: {name}")
    return path


def restore_backup(settings: Settings, path: Path, *, now: datetime | None = None) -> dict:
    """Put a backup back: the database, attached files, and prompts. The app must be stopped first.
    The current data is backed up first (labelled before-restore), so a restore can be undone."""
    manifest = check_backup(path)
    now = now or datetime.now()
    safety = None
    if settings.db_path.exists():
        safety = make_backup(settings, label="before-restore", now=now)
    data = settings.data_dir
    staging = Path(tempfile.mkdtemp(prefix="restore-", dir=data))
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                member = _safe_member(name)
                if name.endswith("/") or member.parts[0] not in (DB_NAME, *FOLDERS):
                    continue
                target = staging.joinpath(*member.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(settings.db_path) + suffix).unlink(missing_ok=True)
        os.replace(staging / DB_NAME, settings.db_path)
        for folder in FOLDERS:
            current = data / folder
            old = data / f"{folder}.old"
            if old.exists():
                shutil.rmtree(old)
            if current.exists():
                current.rename(old)
            incoming = staging / folder
            if incoming.exists():
                incoming.rename(current)
            elif folder == "files":
                current.mkdir()
            if folder == "files" and old.exists():
                _keep_videos(old, current)
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    log.info("Restored %s", path)
    return {"manifest": manifest, "safety_backup": safety}


def _keep_videos(old: Path, current: Path) -> None:
    """Backups leave videos out, so carry the ones on disk over into the restored files folder."""
    for path in old.rglob("*"):
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES:
            target = current / path.relative_to(old)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)


def size_text(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} bytes"
