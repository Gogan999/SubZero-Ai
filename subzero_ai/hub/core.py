"""The Hub's own module: people, roles and permissions."""

from __future__ import annotations

from .modules import Module
from .permissions import Permission

MIGRATIONS = (
    # Version 1: roles picked on the People page, and changes to the role permissions
    """
    ALTER TABLE users ADD COLUMN assigned_role TEXT;

    CREATE TABLE hub_role_permissions (
        role TEXT NOT NULL,
        permission TEXT NOT NULL,
        allowed INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (role, permission)
    );
    """,
    # Version 2: an app name saved on the Settings page as the old default becomes SubZero Hub
    """
    UPDATE settings SET value = 'SubZero Hub' WHERE key = 'app_name' AND value = 'SubZero AI';
    """,
)


class HubCore(Module):
    key = "hub"
    name = "SubZero Hub"
    permissions = (
        Permission("hub.manage", "Settings, People, Permissions and the audit log", frozenset({"admin"}),
                   never_roles=frozenset({"student"})),
    )
    migrations = MIGRATIONS
