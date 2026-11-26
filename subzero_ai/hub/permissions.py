"""Roles and permissions.

Every person has one role. The role keys stay the ones SubZero AI already stores (admin, mentor, student), plus lead;
people see them as Coach, Mentor, Lead and Student. Each module lists its permissions with the roles that have them
by default, and coaches change that on the Permissions page. Coaches (admin) always have every permission, and a
permission's never_roles can't be given it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..db import Database, utcnow

ADMIN = "admin"
ROLES = (ADMIN, "mentor", "lead", "student")
ROLE_LABELS = {ADMIN: "Coach", "mentor": "Mentor", "lead": "Lead", "student": "Student"}


@dataclass(frozen=True)
class Permission:
    key: str  # "<module key>.<name>"
    label: str  # what it allows, in plain words
    default_roles: frozenset[str]
    never_roles: frozenset[str] = frozenset()


def role_label(role: str) -> str:
    """The name people see for a role key."""
    return ROLE_LABELS.get(role, role.capitalize())


def all_permissions() -> list[Permission]:
    """Every installed module's permissions, in module order."""
    from .modules import installed
    return [p for module in installed() for p in module.permissions]


def permission(key: str) -> Permission | None:
    return next((p for p in all_permissions() if p.key == key), None)


def default_permissions(role: str) -> frozenset[str]:
    """The permissions a role has before any change on the Permissions page."""
    if role == ADMIN:
        return frozenset(p.key for p in all_permissions())
    return frozenset(p.key for p in all_permissions() if role in p.default_roles)


class PermissionChangeError(ValueError):
    """A change the Permissions page refuses, with a sentence to show."""


class Grants:
    """Which permissions each role has: the defaults, with the changes saved in hub_role_permissions on top."""

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._cache: dict[str, frozenset[str]] | None = None

    def _load(self) -> dict[str, frozenset[str]]:
        with self._lock:
            if self._cache is not None:
                return self._cache
            changes: dict[tuple[str, str], bool] = {
                (r["role"], r["permission"]): bool(r["allowed"])
                for r in self.db.all("SELECT role, permission, allowed FROM hub_role_permissions")
            }
            table = {}
            for role in ROLES:
                granted = set(default_permissions(role))
                if role != ADMIN:
                    for p in all_permissions():
                        allowed = changes.get((role, p.key))
                        if allowed is True and role not in p.never_roles:
                            granted.add(p.key)
                        elif allowed is False:
                            granted.discard(p.key)
                table[role] = frozenset(granted)
            self._cache = table
            return table

    def for_role(self, role: str) -> frozenset[str]:
        """The permissions a role has now. An unknown role has none."""
        return self._load().get(role, frozenset())

    def allows(self, role: str, key: str) -> bool:
        return key in self.for_role(role)

    def set(self, role: str, key: str, allowed: bool, *, actor_id: int | None) -> bool:
        """Give a role a permission or take it away. Returns True if it changed. Raises PermissionChangeError."""
        if role not in ROLES:
            raise PermissionChangeError("That isn't a role.")
        if role == ADMIN:
            raise PermissionChangeError("Coaches always have every permission.")
        found = permission(key)
        if found is None:
            raise PermissionChangeError("That isn't a permission.")
        if allowed and role in found.never_roles:
            raise PermissionChangeError(f"{role_label(role)}s can't be given “{found.label}”.")
        if self.allows(role, key) == allowed:
            return False
        default = role in found.default_roles
        with self.db.transaction() as conn:
            if allowed == default:
                conn.execute("DELETE FROM hub_role_permissions WHERE role = ? AND permission = ?", (role, key))
            else:
                conn.execute(
                    "INSERT INTO hub_role_permissions (role, permission, allowed, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (role, permission) DO UPDATE SET allowed = excluded.allowed, "
                    "updated_at = excluded.updated_at", (role, key, int(allowed), utcnow()))
        with self._lock:
            self._cache = None
        self.db.audit("permission_changed", actor_id=actor_id, target_type="role", target_id=None,
                      details={"role": role, "permission": key, "allowed": allowed})
        return True
