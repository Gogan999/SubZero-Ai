"""The AI chat (SubZero AI) as a Hub module. Its pages, tables and Slack handling are the existing ones."""

from __future__ import annotations

from typing import Any

from ..hub.modules import Module, NavItem
from ..hub.permissions import Permission


class AIModule(Module):
    key = "ai"
    name = "SubZero AI"
    permissions = (
        Permission("ai.mentor", "Act as a mentor in SubZero AI: review messages, see every chat, send without review",
                   frozenset({"admin", "mentor"}), never_roles=frozenset({"student"})),
    )

    def nav(self, viewer) -> list[NavItem]:
        return [NavItem("SubZero AI", "/chats", "chats")]

    def slack_home_blocks(self, services: Any, viewer) -> list[dict[str, Any]]:
        lines = [f"*SubZero AI*  <{services.settings.url('/chats')}|Open the chat>"]
        if services.settings.slack_dms_enabled:
            reviewed = not viewer.is_mentor or services.settings.mentors_need_approval
            lines.append("Ask the AI by sending a message in the Messages tab."
                         + (" A mentor checks it first." if reviewed else ""))
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
