"""Who is a coach (admin), mentor, lead or student, and what their role lets them do.

In order: admins named in settings and the recovery sign-in are coaches; a role a coach picked on the People page
as coach wins next; members of the private approvals channel in Slack (checked live and cached for a few minutes)
are mentors while mentors_from_channel is on; then the role picked on the People page; everyone else is a student.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import Settings
from .db import Database
from .hub.permissions import ADMIN, ROLES, Grants, default_permissions

log = logging.getLogger("subzero.roles")

# Built-in people for local testing (only reachable when DEV_LOGIN=1 on the PC itself).
DEV_USERS = [
    {"slack_user_id": "DEV-student-ava", "name": "Ava (student)", "role": "student"},
    {"slack_user_id": "DEV-student-ben", "name": "Ben (student)", "role": "student"},
    {"slack_user_id": "DEV-lead-lee", "name": "Lee (lead)", "role": "lead"},
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
    """Slack accounts that can't use the app: deleted ones, bots, and guests unless guests are allowed."""
    if info.get("deleted") or info.get("is_bot"):
        return True
    guest = info.get("is_restricted") or info.get("is_ultra_restricted")
    return bool(guest) and not settings.allow_slack_guests


class Directory:
    def __init__(self, settings: Settings, slack, ttl_seconds: float = 300, *, db: Database | None = None):
        self.settings = settings
        self.slack = slack
        self.ttl = ttl_seconds
        self.db = db
        self.grants = Grants(db) if db is not None else None
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

    def assigned_role(self, slack_user_id: str) -> str | None:
        """The role picked on the People page, if any."""
        if self.db is None:
            return None
        row = self.db.one("SELECT assigned_role FROM users WHERE slack_user_id = ?", (slack_user_id,))
        role = row["assigned_role"] if row is not None else None
        return role if role in ROLES else None

    async def role_for(self, slack_user_id: str, force: bool = False) -> str:
        if slack_user_id in self.settings.admin_slack_ids or slack_user_id == RECOVERY_USER["slack_user_id"]:
            return ADMIN
        assigned = self.assigned_role(slack_user_id)
        if is_dev_user(slack_user_id):
            return assigned or _DEV_ROLES.get(slack_user_id, "student")
        if assigned == ADMIN:
            return ADMIN
        if self.settings.mentors_from_channel and slack_user_id in await self.mentor_ids(force=force):
            return "mentor"
        return assigned or "student"

    def permissions_for(self, role: str) -> frozenset[str]:
        """The permissions a role has now, with the Permissions page's changes."""
        return self.grants.for_role(role) if self.grants is not None else default_permissions(role)

    def allows(self, role: str, permission: str) -> bool:
        return role == ADMIN or permission in self.permissions_for(role)
