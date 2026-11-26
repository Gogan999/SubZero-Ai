"""The AI chat (SubZero AI) as a Hub module. Its pages, tables and Slack handling are the existing ones."""

from __future__ import annotations

from ..hub.modules import Module
from ..hub.permissions import Permission


class AIModule(Module):
    key = "ai"
    name = "SubZero AI"
    permissions = (
        Permission("ai.mentor", "Act as a mentor in SubZero AI: review messages, see every chat, send without review",
                   frozenset({"admin", "mentor"}), never_roles=frozenset({"student"})),
    )
