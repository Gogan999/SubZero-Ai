"""Who is a student, mentor, or admin.

Admins are named in settings. Mentors are the members of the private approvals channel in Slack,
checked live and cached for a few minutes. Everyone else who signs in is a student.

Each good read of the channel is also saved (in the database, with its time). If Slack can't be reached and there is no
list in memory (right after the app starts), the saved list is used, as long as it is under a week old. Nobody who
wasn't on the last good list ever becomes a mentor that way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings
from .db import Database

log = logging.getLogger("subzero.roles")

# Built-in people for local testing (only reachable when DEV_LOGIN=1 on the PC itself).
DEV_USERS = [
    {"slack_user_id": "DEV-student-ava", "name": "Ava (student)", "role": "student"},
    {"slack_user_id": "DEV-student-ben", "name": "Ben (student)", "role": "student"},
    {"slack_user_id": "DEV-mentor-kim", "name": "Mentor Kim", "role": "mentor"},
    {"slack_user_id": "DEV-admin-coach", "name": "Head coach", "role": "admin"},
]
_DEV_ROLES = {u["slack_user_id"]: u["role"] for u in DEV_USERS}

MENTOR_LIST_KEY = "mentor_list"  # kv row: {"ids": [...], "at": "<iso time of the last good read>"}
MENTOR_LIST_MAX_AGE = timedelta(days=7)  # an older saved list isn't used
MENTOR_LIST_RESAVE = timedelta(hours=1)  # an unchanged list is saved again this often, so its time stays current
RETRY_AFTER_FAILURE = 30.0  # seconds before another page asks Slack again after a failed read

# The admin who signs in on the mini PC with the recovery code when Slack sign-in is broken.
RECOVERY_USER = {"slack_user_id": "LOCAL-recovery", "name": "Recovery admin", "role": "admin"}


def is_dev_user(slack_user_id: str) -> bool:
    return slack_user_id.startswith("DEV-")


def is_builtin_user(slack_user_id: str) -> bool:
    """Sample people and the recovery admin, who aren't Slack accounts."""
    return slack_user_id.startswith(("DEV-", "LOCAL-"))


def is_excluded_account(info: dict, settings: Settings) -> bool:
    """Slack accounts that can't use the app: deleted ones, bots, and (unless allowed) guests,
    who might be parents, sponsors, alumni, or other teams' mentors."""
    if info.get("deleted") or info.get("is_bot"):
        return True
    guest = info.get("is_restricted") or info.get("is_ultra_restricted")
    return bool(guest) and not settings.allow_slack_guests


class Directory:
    def __init__(self, settings: Settings, slack, ttl_seconds: float = 300, db: Database | None = None):
        self.settings = settings
        self.slack = slack
        self.ttl = ttl_seconds
        self.db = db  # where the last good mentor list is saved (None: not saved)
        self._mentors: set[str] = set()
        self._have_list = False  # a list (from Slack or from disk) is in memory
        self._fetched_at: float | None = None
        self._failed_at: float | None = None
        self._saved_at: datetime | None = None  # when the list in memory was read from Slack
        self._using_saved = False  # the list in memory came from disk, not from Slack
        self._warning: str | None = None
        self._lock = asyncio.Lock()

    async def mentor_ids(self, force: bool = False) -> set[str]:
        channel = self.settings.slack_approvals_channel
        if not channel or self.slack is None:
            return set()
        if not force and (self._fresh() or self._recently_failed()):
            return self._mentors
        async with self._lock:
            if not force and (self._fresh() or self._recently_failed()):
                return self._mentors
            try:
                members = set(await self.slack.channel_members(channel))
            except Exception:
                log.exception("Couldn't read the approvals channel members; using the last known list")
                self._failed_at = time.monotonic()
                self._use_last_known()
            else:
                self._mentors, self._have_list, self._using_saved = members, True, False
                self._fetched_at, self._failed_at, self._warning = time.monotonic(), None, None
                self._saved_at = datetime.now(timezone.utc)
                self._save(members)
        return self._mentors

    def _fresh(self) -> bool:
        return self._fetched_at is not None and time.monotonic() - self._fetched_at < self.ttl

    def _recently_failed(self) -> bool:
        return self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_AFTER_FAILURE

    def invalidate(self) -> None:
        self._fetched_at = None
        self._failed_at = None

    # ---- the last good list, kept on disk
    def _save(self, members: set[str]) -> None:
        if self.db is None:
            return
        try:
            saved = self._load()
            now = datetime.now(timezone.utc)
            if saved is not None and saved[0] == members and now - saved[1] < MENTOR_LIST_RESAVE:
                return
            stamp = now.isoformat(timespec="seconds")
            self.db.set_kv(MENTOR_LIST_KEY, json.dumps({"ids": sorted(members), "at": stamp}))
        except Exception:
            log.warning("Couldn't save the mentor list", exc_info=True)

    def _load(self) -> tuple[set[str], datetime] | None:
        raw = self.db.get_kv(MENTOR_LIST_KEY) if self.db is not None else None
        if not raw:
            return None
        try:
            data = json.loads(raw)
            ids = {str(i) for i in data["ids"]}
            at = datetime.fromisoformat(data["at"])
        except (ValueError, KeyError, TypeError):
            return None
        return ids, at if at.tzinfo else at.replace(tzinfo=timezone.utc)

    def _use_last_known(self) -> None:
        """Slack couldn't be read. A list already in memory stays. With none (just after start-up), use the saved list
        if it is under a week old. Either way, say so (stale_warning)."""
        if self._have_list:
            when = self._saved_at
            since = self._when(when) if when else "earlier"
            self._warning = ("Slack didn't answer when the app asked who the mentors are, so it is using the list from "
                             f"{since}. Mentors keep their access until Slack answers again.")
            return
        saved = self._load()
        if saved is not None and datetime.now(timezone.utc) - saved[1] <= MENTOR_LIST_MAX_AGE:
            self._mentors, self._have_list, self._using_saved, self._saved_at = saved[0], True, True, saved[1]
            self._warning = ("Slack didn't answer when the app asked who the mentors are, so it is using the list it "
                             f"saved on {self._when(saved[1])}. Mentors keep their access until Slack answers again.")
            log.warning("Using the mentor list saved on %s because Slack couldn't be read", saved[1].isoformat())
            return
        self._warning = ("Slack didn't answer when the app asked who the mentors are, and there is no recent saved "
                         "list, so only admins can act as mentors until Slack answers.")
        if saved is not None:
            log.warning("The saved mentor list is older than %s days, so it isn't used", MENTOR_LIST_MAX_AGE.days)

    def _when(self, moment: datetime) -> str:
        try:
            local = moment.astimezone(ZoneInfo(self.settings.timezone))
        except (ValueError, ZoneInfoNotFoundError):
            local = moment.astimezone()
        return f"{local.strftime('%b')} {local.day} at {local.strftime('%I:%M %p').lstrip('0')}"

    def stale_warning(self) -> str | None:
        """Plain words for the Settings page when the mentor list couldn't be read from Slack on the last try, or None
        when it was read fine (or mentors come from nowhere, because no approvals channel is set)."""
        return self._warning

    async def role_for(self, slack_user_id: str, force: bool = False) -> str:
        if slack_user_id in self.settings.admin_slack_ids or slack_user_id == RECOVERY_USER["slack_user_id"]:
            return "admin"
        if is_dev_user(slack_user_id):
            return _DEV_ROLES.get(slack_user_id, "student")
        if slack_user_id in await self.mentor_ids(force=force):
            return "mentor"
        return "student"
