"""Handles button clicks and modal submissions that arrive from Slack over Socket Mode."""

from __future__ import annotations

import logging
from typing import Any

from . import slack_cards
from .approvals import ApprovalService
from .config import Settings
from .live import Tasks
from .roles import Directory
from .store import MENTOR_ROLES, PENDING, Store, Viewer

log = logging.getLogger("subzero.slack")


class SlackInteractions:
    def __init__(self, *, settings: Settings, store: Store, approvals: ApprovalService, directory: Directory,
                 slack, tasks: Tasks):
        self.settings = settings
        self.store = store
        self.approvals = approvals
        self.directory = directory
        self.slack = slack
        self.tasks = tasks

    async def dispatch(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        """Called for every Socket Mode envelope. Must return quickly: the return value is the acknowledgement."""
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

    async def reviewer(self, slack_user_id: str, *, fresh: bool = True) -> Viewer | None:
        """The mentor behind a tap, or None. fresh=True re-reads #claude-approvals so someone just removed
        loses access at once; opening and submitting modals use the cached list, because Slack gives those
        3 seconds and the cache was refreshed within the last few minutes."""
        role = await self.directory.role_for(slack_user_id, force=fresh)
        if role not in MENTOR_ROLES:
            return None
        row = self.store.user_by_slack_id(slack_user_id)
        if row is None:
            name, avatar = slack_user_id, None
            try:
                info = await self.slack.user_info(slack_user_id)
                profile = info.get("profile") or {}
                name = profile.get("display_name") or profile.get("real_name") or info.get("real_name") or name
                avatar = profile.get("image_72")
            except Exception:
                log.warning("Couldn't look up Slack user %s", slack_user_id, exc_info=True)
            row = self.store.upsert_user(slack_user_id=slack_user_id, name=name, role=role, avatar_url=avatar)
        elif row["role"] != role:
            self.store.set_user_role(row["id"], role)
        return Viewer(id=row["id"], slack_user_id=slack_user_id, name=row["name"], role=role,
                      avatar_url=row["avatar_url"])

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
        user_id = (payload.get("user") or {}).get("id", "")
        channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id")
        try:
            message_id = int(action.get("value", ""))
        except ValueError:
            return
        # Edit and Reject open a modal, and Slack's trigger_id for that expires 3 seconds after the tap.
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
