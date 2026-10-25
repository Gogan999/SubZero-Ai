"""Who is a student, mentor, or admin.

Admins are named in settings. Mentors are the members of the private approvals channel in Slack,
checked live and cached for a few minutes. Everyone else who signs in is a student.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import Settings

log = logging.getLogger("subzero.roles")

# Built-in people for local testing (only reachable when DEV_LOGIN=1 on the PC itself).
DEV_USERS = [
    {"slack_user_id": "DEV-student-ava", "name": "Ava (student)", "role": "student"},
    {"slack_user_id": "DEV-student-ben", "name": "Ben (student)", "role": "student"},
    {"slack_user_id": "DEV-mentor-kim", "name": "Mentor Kim", "role": "mentor"},
    {"slack_user_id": "DEV-admin-coach", "name": "Head coach", "role": "admin"},
]
_DEV_ROLES = {u["slack_user_id"]: u["role"] for u in DEV_USERS}

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
    def __init__(self, settings: Settings, slack, ttl_seconds: float = 300):
        self.settings = settings
        self.slack = slack
        self.ttl = ttl_seconds
        self._mentors: set[str] = set()
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def mentor_ids(self, force: bool = False) -> set[str]:
        channel = self.settings.slack_approvals_channel
        if not channel or self.slack is None:
            return set()
        if not force and self._fresh():
            return self._mentors
        async with self._lock:
            if not force and self._fresh():
                return self._mentors
            try:
                self._mentors = set(await self.slack.channel_members(channel))
                self._fetched_at = time.monotonic()
            except Exception:
                log.exception("Couldn't read the approvals channel members; using the last known list")
        return self._mentors

    def _fresh(self) -> bool:
        return self._fetched_at is not None and time.monotonic() - self._fetched_at < self.ttl

    def invalidate(self) -> None:
        self._fetched_at = None

    async def role_for(self, slack_user_id: str, force: bool = False) -> str:
        if slack_user_id in self.settings.admin_slack_ids or slack_user_id == RECOVERY_USER["slack_user_id"]:
            return "admin"
        if is_dev_user(slack_user_id):
            return _DEV_ROLES.get(slack_user_id, "student")
        if slack_user_id in await self.mentor_ids(force=force):
            return "mentor"
        return "student"
