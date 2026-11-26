"""Routes Slack DMs, button taps and Home tab opens to the Hub's modules."""

from __future__ import annotations

import logging
from typing import Any

from ..roles import is_excluded_account
from ..slack_handlers import viewer_for_slack_user
from . import modules

log = logging.getLogger("subzero.hub")


class SlackRouter:
    def __init__(self, services: Any):
        self.services = services

    async def direct_message(self, event: dict[str, Any]) -> bool:
        """Offers a top-level DM from a person to each module in turn. Returns True if one handled it.
        Thread replies, edits, bot messages and messages with files are left to the AI."""
        if event.get("subtype") or event.get("bot_id") or not event.get("user") or event.get("files"):
            return False
        if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
            return False
        text = (event.get("text") or "").strip()
        for module in modules.installed():
            try:
                if await module.on_dm(self.services, event, text):
                    return True
            except Exception:
                log.exception("The %s module failed on a Slack DM", module.key)
                return True
        return False

    async def action(self, payload: dict[str, Any], action_id: str) -> None:
        """A button tap with a "<module key>:" action_id."""
        key = action_id.split(":", 1)[0]
        module = next((m for m in modules.installed() if m.key == key), None)
        if module is None:
            log.warning("No module for Slack action %s", action_id)
            return
        try:
            await module.on_action(self.services, payload, action_id)
        except Exception:
            log.exception("The %s module failed on Slack action %s", key, action_id)

    async def command(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """A slash command, answered by the module that lists it."""
        name = payload.get("command", "")
        module = next((m for m in modules.installed() if name in m.commands), None)
        if module is None:
            return None
        try:
            return await module.on_command(self.services, payload)
        except Exception:
            log.exception("The %s module failed on %s", module.key, name)
            return {"response_type": "ephemeral", "text": "Something went wrong. Try again in a minute."}

    async def viewer(self, slack_user_id: str):
        """The app account for a Slack member who may use the Hub, or None."""
        s = self.services
        try:
            info = await s.slack.user_info(slack_user_id)
        except Exception:
            log.warning("Couldn't look up Slack user %s", slack_user_id, exc_info=True)
            return None
        if slack_user_id not in s.settings.admin_slack_ids:
            team = s.settings.slack_team_id
            if is_excluded_account(info, s.settings) or (team and info.get("team_id") not in (None, team)):
                return None
        row = s.store.user_by_slack_id(slack_user_id)
        if row is not None and not row["active"]:
            return None
        role = await s.directory.role_for(slack_user_id)
        return await viewer_for_slack_user(s.store, s.slack, slack_user_id, role, info,
                                           permissions=s.directory.permissions_for(role))

    def home_view(self, viewer) -> dict[str, Any]:
        """The Home tab: a heading and link, then each module's section."""
        s = self.services
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": s.settings.app_name}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"<{s.settings.url('/')}|Open the website>"}},
        ]
        for module in modules.installed():
            section = module.slack_home_blocks(s, viewer) if viewer is not None else []
            if section:
                blocks += [{"type": "divider"}, *section]
        if viewer is None:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                       "text": "This Slack account can't use the team's tools."}})
        return {"type": "home", "blocks": blocks}

    async def publish_home(self, slack_user_id: str) -> None:
        if not slack_user_id or self.services.slack is None:
            return
        viewer = await self.viewer(slack_user_id)
        try:
            await self.services.slack.publish_home(slack_user_id, self.home_view(viewer))
        except Exception:
            log.warning("Couldn't publish the Slack Home tab for %s", slack_user_id, exc_info=True)
