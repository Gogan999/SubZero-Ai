"""Modules: each tool in the Hub is one Module, which tells the core what it adds.

A module has a key (its URL prefix, permission prefix and table prefix), its permissions, and its own list of
database migrations, applied in order and recorded per module in hub_module_migrations. Every method is optional.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .permissions import Permission

if TYPE_CHECKING:
    from starlette.routing import BaseRoute

    from ..store import Viewer


@dataclass(frozen=True)
class NavItem:
    label: str
    href: str
    icon: str  # a name from _icons.html


class Module:
    key: str = ""
    name: str = ""
    permissions: tuple[Permission, ...] = ()
    migrations: tuple[str, ...] = ()

    def routes(self) -> list[BaseRoute]:
        """The module's pages, mounted under /<key>."""
        return []

    def nav(self, viewer: Viewer) -> list[NavItem]:
        """The module's entries in the tool switcher, for this viewer."""
        return []

    async def on_dm(self, services: Any, event: dict[str, Any], text: str) -> bool:
        """A message sent to the bot in Slack. Returns True if this module handled it."""
        return False

    async def on_action(self, services: Any, payload: dict[str, Any], action_id: str) -> bool:
        """A Slack button tap whose action_id starts with "<key>:". Returns True if handled."""
        return False

    async def tick(self, services: Any) -> None:
        """Called every minute."""


def installed() -> list[Module]:
    """The modules in this install, in menu order."""
    from ..modules import MODULES
    return MODULES


def module_versions(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute("CREATE TABLE IF NOT EXISTS hub_module_migrations (module TEXT PRIMARY KEY, version INTEGER NOT NULL)")
    return {row[0]: row[1] for row in conn.execute("SELECT module, version FROM hub_module_migrations")}


def migrate_modules(conn: sqlite3.Connection) -> None:
    """Apply each module's migrations that haven't run yet, one transaction per migration."""
    versions = module_versions(conn)
    for module in installed():
        done = versions.get(module.key, 0)
        for index, script in enumerate(module.migrations[done:], start=done + 1):
            conn.executescript(
                "BEGIN;" + script
                + f"INSERT INTO hub_module_migrations (module, version) VALUES ('{module.key}', {index}) "
                f"ON CONFLICT (module) DO UPDATE SET version = {index}; COMMIT;")
