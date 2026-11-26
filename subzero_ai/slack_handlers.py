"""Handles button clicks and modal submissions that arrive from Slack over Socket Mode."""

from __future__ import annotations

import logging
from typing import Any

from . import slack_cards
from .approvals import ApprovalService
from .config import Settings
from .live import Tasks
from .roles import Directory
from .store import PENDING, Store, Viewer

log = logging.getLogger("subzero.slack")


async def viewer_for_slack_user(store: Store, slack, slack_user_id: str, role: str,
                                info: dict[str, Any] | None = None,
                                permissions: frozenset[str] | None = None) -> Viewer:
    """The app account for a Slack member: created from their Slack profile the first time (pass `info` if the
    profile was already fetched), and given the role they have now."""
    row = store.user_by_slack_id(slack_user_id)
    if row is None:
        name, avatar = slack_user_id, None
        try:
            if info is None:
                info = await slack.user_info(slack_user_id)
            profile = info.get("profile") or {}
            name = profile.get("display_name") or profile.get("real_name") or info.get("real_name") or name
            avatar = profile.get("image_72")
        except Exception:
            log.warning("Couldn't look up Slack user %s", slack_user_id, exc_info=True)
        row = store.upsert_user(slack_user_id=slack_user_id, name=name, role=role, avatar_url=avatar)
    elif row["role"] != role:
        store.set_user_role(row["id"], role)
    return Viewer(id=row["id"], slack_user_id=slack_user_id, name=row["name"], role=role,
                  avatar_url=row["avatar_url"], permissions=permissions)


class SlackInteractions:
    def __init__(self, *, settings: Settings, store: Store, approvals: ApprovalService, directory: Directory,
                 slack, tasks: Tasks):
        self.settings = settings
        self.store = store
        self.approvals = approvals
        self.directory = directory
        self.slack = slack
        self.tasks = tasks
        self.dms = None  # SlackDMs, set by build_services
        self.router = None  # hub.router.SlackRouter, set by build_services

    async def dispatch(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        """Handles one Socket Mode envelope and returns its acknowledgement."""
        if envelope.get("type") == "events_api":
            event = (envelope.get("payload") or {}).get("event") or {}
            if event.get("type") == "message" and event.get("channel_type") == "im":
                self.tasks.spawn(self._direct_message(event), name="slack-dm")
            elif event.get("type") == "app_home_opened" and event.get("tab") == "home" and self.router is not None:
                self.tasks.spawn(self.router.publish_home(event.get("user", "")), name="slack-home")
            return None
        if envelope.get("type") == "slash_commands":
            if self.router is None:
                return None
            return await self.router.command(envelope.get("payload") or {})
        if envelope.get("type") != "interactive":
            return None
        payload = envelope.get("payload") or {}
        kind = payload.get("type")
        if kind == "block_actions":
            self.tasks.spawn(self.block_action(payload), name="slack-action")
            return None
        if kind == "view_submission":
            return await self.view_submission(payload)
        return None

    async def _direct_message(self, event: dict[str, Any]) -> None:
        """Offers a DM to the modules first, then to the AI."""
        if self.router is not None and await self.router.direct_message(event):
            return
        if self.dms is not None:
            await self.dms.handle_event(event)

    async def reviewer(self, slack_user_id: str, *, fresh: bool = True) -> Viewer | None:
        """The mentor behind a tap, or None. fresh=True re-reads #claude-approvals instead of using the cached list."""
        role = await self.directory.role_for(slack_user_id, force=fresh)
        if not self.directory.allows(role, "ai.mentor"):
            return None
        return await viewer_for_slack_user(self.store, self.slack, slack_user_id, role,
                                           permissions=self.directory.permissions_for(role))

    async def _tell(self, channel: str | None, user: str, text: str) -> None:
        try:
            if channel:
                await self.slack.post_ephemeral(channel, user, text)
            else:
                await self.slack.dm(user, text)
        except Exception:
            log.warning("Couldn't send a note to %s", user, exc_info=True)

    async def block_action(self, payload: dict[str, Any]) -> None:
        action = (payload.get("actions") or [{}])[0]
        action_id = action.get("action_id")
        if ":" in str(action_id) and self.router is not None:
            await self.router.action(payload, str(action_id))
            return
        user_id = (payload.get("user") or {}).get("id", "")
        channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id")
        try:
            message_id = int(action.get("value", ""))
        except ValueError:
            return
        # Edit and Reject use the cached mentor list; other taps re-read it.
        reviewer = await self.reviewer(user_id, fresh=action_id not in ("edit", "reject"))
        if reviewer is None:
            await self._tell(channel, user_id, "Only mentors in this channel can review messages.")
            return

        if action_id in ("edit", "reject"):
            msg = self.store.message(message_id)
            if msg is None or msg["status"] != PENDING:
                outcome = self.approvals._already_handled(message_id)
                await self._tell(channel, user_id, outcome.message)
                return
            if action_id == "edit":
                if len(msg["content"]) > slack_cards.MODAL_TEXT_LIMIT:
                    view = slack_cards.too_long_to_edit_modal(self.approvals.link(msg["conversation_id"]))
                else:
                    view = slack_cards.edit_modal(message_id, msg["content"])
            else:
                view = slack_cards.reject_modal(message_id)
            await self.slack.open_view(payload.get("trigger_id", ""), view)
            return

        if action_id == "approve":
            outcome = await self.approvals.approve(reviewer, message_id)
        elif action_id == "handle_private":
            outcome = await self.approvals.hold(reviewer, message_id)
        else:
            return
        if not outcome.ok:
            await self._tell(channel, user_id, outcome.message)

    async def view_submission(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        view = payload.get("view") or {}
        callback = view.get("callback_id")
        if callback not in ("edit_submit", "reject_submit"):
            return None
        block = "edited" if callback == "edit_submit" else "reason"
        try:
            message_id = int(view.get("private_metadata", ""))
        except ValueError:
            return {"response_action": "errors", "errors": {block: "This form is out of date. Close it and try again."}}
        reviewer = await self.reviewer((payload.get("user") or {}).get("id", ""), fresh=False)
        if reviewer is None:
            return {"response_action": "errors", "errors": {block: "Only mentors can review messages."}}
        values = (view.get("state") or {}).get("values") or {}
        text = ((values.get(block) or {}).get("text") or {}).get("value") or ""
        if callback == "edit_submit":
            outcome = await self.approvals.approve(reviewer, message_id, edited_text=text)
        else:
            outcome = await self.approvals.reject(reviewer, message_id, text)
        if not outcome.ok:
            return {"response_action": "errors", "errors": {block: outcome.message}}
        return {"response_action": "clear"}
