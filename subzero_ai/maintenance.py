"""Jobs that keep the app tidy: the nightly backup, deleting chats past the retention period, and checking
GitHub for a newer release."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import __version__
from .backup import make_backup, prune_backups
from .config import Settings
from .db import Database, utcnow

log = logging.getLogger("subzero.maintenance")


def backup_due(last_iso: str | None, now: datetime, hour: int) -> bool:
    """True when no backup has been made since the most recent nightly slot (hour:00 local time).
    now is the local time, with its time zone."""
    slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now < slot:
        slot -= timedelta(days=1)
    if not last_iso:
        return True
    return datetime.fromisoformat(last_iso) < slot


def delete_old_chats(db: Database, files_dir: Path, days: int, *, now: datetime | None = None) -> int:
    """Delete chats with no activity for `days` days, with their messages and attached files.
    0 days keeps everything. Returns how many chats were deleted."""
    if days <= 0:
        return 0
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.transaction() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM conversations WHERE updated_at < ?", (cutoff,))]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        paths = [row["path"] for row in conn.execute(
            f"SELECT a.path FROM attachments a JOIN messages m ON m.id = a.message_id "
            f"WHERE m.conversation_id IN ({marks})", ids)]
        conn.execute(f"DELETE FROM conversations WHERE id IN ({marks})", ids)
        db.audit("old_chats_deleted", actor_id=None, details={"chats": len(ids), "older_than_days": days},
                 conn=conn)
    root = files_dir.resolve()
    for relative in paths:
        path = (files_dir / relative).resolve()
        if root in path.parents:
            path.unlink(missing_ok=True)
    log.info("Deleted %s chats with no activity for %s days", len(ids), days)
    return len(ids)


def prune_dm_messages(db: Database, days: int = 7, *, now: datetime | None = None) -> int:
    """Forget Slack message ids older than `days` days. They only exist to catch Slack delivering a message twice."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(timespec="seconds")
    return db.execute("DELETE FROM slack_dm_messages WHERE created_at < ?", (cutoff,)).rowcount


class Nightly:
    """Runs the backup once a night at BACKUP_HOUR, then removes old backups and old chats."""

    def __init__(self, settings: Settings, db: Database, tz):
        self.settings = settings
        self.db = db
        self.tz = tz
        self._lock = asyncio.Lock()

    @property
    def last_backup_at(self) -> str | None:
        return self.db.get_kv("last_backup_at")

    @property
    def last_backup_error(self) -> str | None:
        return self.db.get_kv("last_backup_error")

    async def tick(self) -> bool:
        """Called every minute. Returns True if the nightly jobs ran."""
        if not backup_due(self.last_backup_at, datetime.now(self.tz), self.settings.backup_hour):
            return False
        await self.run()
        return True

    async def run(self, label: str = "") -> str | None:
        """Back up now (and tidy up). Returns the backup's file name, or None if it failed."""
        async with self._lock:
            try:
                path = await asyncio.to_thread(make_backup, self.settings, label=label)
            except Exception as exc:
                log.exception("Backup failed")
                self.db.set_kv("last_backup_error", f"{utcnow()} {exc}")
                # Count the attempt so a broken backup folder doesn't retry every minute; it tries again tomorrow.
                self.db.set_kv("last_backup_at", utcnow())
                self.db.set_kv("last_backup_ok", "0")
                self.db.audit("backup_failed", actor_id=None, details={"error": str(exc)})
                return None
            self.db.set_kv("last_backup_at", utcnow())
            self.db.set_kv("last_backup_ok", "1")
            self.db.set_kv("last_backup_file", path.name)
            self.db.delete_kv("last_backup_error")
            await asyncio.to_thread(prune_backups, self.settings, self.settings.backup_keep_days)
            try:
                await asyncio.to_thread(delete_old_chats, self.db, self.settings.data_dir / "files",
                                        self.settings.retention_days)
            except Exception:
                log.exception("Couldn't delete old chats")
            try:
                await asyncio.to_thread(prune_dm_messages, self.db)
            except Exception:
                log.exception("Couldn't prune old Slack message ids")
            try:
                from .code_runner import prune_workspaces
                await asyncio.to_thread(prune_workspaces, self.db, self.settings.data_dir)
            except Exception:
                log.exception("Couldn't delete old Code copies")
            return path.name


# --------------------------------------------------------------------------- updates
def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3]) or (0,)


def fetch_latest_release(repo: str, session: requests.Session | None = None) -> dict:
    """The newest published release of the app on GitHub."""
    session = session or requests.Session()
    resp = session.get(f"https://api.github.com/repos/{repo}/releases/latest",
                       headers={"Accept": "application/vnd.github+json"}, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    return {"version": str(body.get("tag_name", "")).lstrip("v"), "url": body.get("html_url", ""),
            "checked_at": utcnow()}


async def check_for_update(settings: Settings, db: Database, fetch=fetch_latest_release) -> dict | None:
    """Look for a newer release once a day and remember the answer for the admin page."""
    if not settings.check_for_updates or not settings.update_repo:
        return None
    try:
        latest = await asyncio.to_thread(fetch, settings.update_repo)
    except Exception as exc:
        log.info("Couldn't check for updates: %s", exc)
        return None
    db.set_kv("latest_release", json.dumps(latest))
    return latest


def update_available(db: Database) -> dict | None:
    """The newer release, if the last check found one."""
    raw = db.get_kv("latest_release")
    if not raw:
        return None
    try:
        latest = json.loads(raw)
    except ValueError:
        return None
    if latest.get("version") and version_tuple(latest["version"]) > version_tuple(__version__):
        return latest
    return None
