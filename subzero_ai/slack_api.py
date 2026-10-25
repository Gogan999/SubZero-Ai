"""Thin wrapper around the Slack Web API, plus an in-memory stand-in for tests and local runs."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import anyio
import requests

log = logging.getLogger("subzero.slack")


class SlackError(Exception):
    def __init__(self, method: str, error: str):
        super().__init__(f"Slack {method} failed: {error}")
        self.method = method
        self.error = error


class SlackWebApi:
    """Calls Slack over HTTPS with the bot token. Every request is form-encoded,
    with nested values (blocks, views) sent as JSON strings, which Slack accepts for all methods."""

    def __init__(self, bot_token: str, *, base_url: str = "https://slack.com/api/",
                 session: requests.Session | None = None, timeout: float = 15):
        self.bot_token = bot_token
        self.base_url = base_url
        self.session = session or requests.Session()
        self.timeout = timeout

    @staticmethod
    def _form_value(value: Any) -> Any:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return value

    def _call_sync(self, method: str, data: dict[str, Any], token: str | None = None) -> dict[str, Any]:
        form = {k: self._form_value(v) for k, v in data.items() if v is not None}
        headers = {"Authorization": f"Bearer {token or self.bot_token}"}
        for attempt in range(3):
            resp = self.session.post(self.base_url + method, data=form, headers=headers, timeout=self.timeout)
            if resp.status_code == 429 and attempt < 2:
                time.sleep(min(int(resp.headers.get("Retry-After", "2")), 30))
                continue
            resp.raise_for_status()
            body = resp.json()
            if not body.get("ok"):
                raise SlackError(method, body.get("error", "unknown_error"))
            return body
        raise SlackError(method, "rate_limited")

    async def call(self, method: str, token: str | None = None, **data: Any) -> dict[str, Any]:
        return await anyio.to_thread.run_sync(lambda: self._call_sync(method, data, token))

    # Messages ----------------------------------------------------------------
    async def post_message(self, channel: str, text: str, blocks: list | None = None,
                           thread_ts: str | None = None) -> dict[str, Any]:
        body = await self.call("chat.postMessage", channel=channel, text=text, blocks=blocks,
                               thread_ts=thread_ts, unfurl_links=False, unfurl_media=False)
        return {"channel": body["channel"], "ts": body["ts"]}

    async def update_message(self, channel: str, ts: str, text: str, blocks: list | None = None) -> None:
        await self.call("chat.update", channel=channel, ts=ts, text=text, blocks=blocks or [])

    async def post_ephemeral(self, channel: str, user: str, text: str) -> None:
        await self.call("chat.postEphemeral", channel=channel, user=user, text=text)

    async def dm(self, user_id: str, text: str, blocks: list | None = None) -> None:
        opened = await self.call("conversations.open", users=user_id)
        await self.post_message(opened["channel"]["id"], text, blocks)

    async def open_view(self, trigger_id: str, view: dict) -> None:
        await self.call("views.open", trigger_id=trigger_id, view=view)

    # Lookups -----------------------------------------------------------------
    async def channel_members(self, channel: str) -> set[str]:
        members: set[str] = set()
        cursor = None
        while True:
            body = await self.call("conversations.members", channel=channel, limit=500, cursor=cursor)
            members.update(body.get("members", []))
            cursor = (body.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return members

    async def user_info(self, user_id: str) -> dict[str, Any]:
        body = await self.call("users.info", user=user_id)
        return body["user"]

    async def private_channels(self) -> list[dict[str, str]]:
        """Private channels the bot has been invited to, for picking the approvals channel."""
        channels: list[dict[str, str]] = []
        cursor = None
        while True:
            body = await self.call("conversations.list", types="private_channel", exclude_archived=True,
                                   limit=200, cursor=cursor)
            channels += [{"id": c["id"], "name": c.get("name", c["id"])} for c in body.get("channels", [])]
            cursor = (body.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return sorted(channels, key=lambda c: c["name"])

    async def auth_test(self) -> dict[str, Any]:
        return await self.call("auth.test")

    async def open_socket_url(self, app_token: str) -> str:
        body = await self.call("apps.connections.open", token=app_token)
        return body["url"]


@dataclass
class FakeSlack:
    """Records what the app would have sent to Slack. Used by tests and by local runs without Slack."""

    members: set[str] = field(default_factory=set)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    posts: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    ephemerals: list[dict[str, Any]] = field(default_factory=list)
    dms: list[dict[str, Any]] = field(default_factory=list)
    views: list[dict[str, Any]] = field(default_factory=list)
    team_id: str = "TDEV"
    channels: list[dict[str, str]] = field(default_factory=list)
    fail_posts: bool = False
    fail_auth: bool = False
    _ts: int = 1000

    async def post_message(self, channel: str, text: str, blocks: list | None = None,
                           thread_ts: str | None = None) -> dict[str, Any]:
        if self.fail_posts:
            raise SlackError("chat.postMessage", "channel_not_found")
        self._ts += 1
        ts = f"{self._ts}.000100"
        self.posts.append({"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts, "ts": ts})
        return {"channel": channel, "ts": ts}

    async def update_message(self, channel: str, ts: str, text: str, blocks: list | None = None) -> None:
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})

    async def post_ephemeral(self, channel: str, user: str, text: str) -> None:
        self.ephemerals.append({"channel": channel, "user": user, "text": text})

    async def dm(self, user_id: str, text: str, blocks: list | None = None) -> None:
        self.dms.append({"user": user_id, "text": text, "blocks": blocks})

    async def open_view(self, trigger_id: str, view: dict) -> None:
        self.views.append({"trigger_id": trigger_id, "view": view})

    async def channel_members(self, channel: str) -> set[str]:
        return set(self.members)

    async def user_info(self, user_id: str) -> dict[str, Any]:
        return self.users.get(user_id, {"id": user_id, "deleted": False,
                                        "real_name": user_id, "profile": {"real_name": user_id}})

    async def private_channels(self) -> list[dict[str, str]]:
        return list(self.channels)

    async def auth_test(self) -> dict[str, Any]:
        if self.fail_auth:
            raise SlackError("auth.test", "invalid_auth")
        return {"ok": True, "team_id": self.team_id, "team": "Test workspace", "user_id": "UBOT"}

    async def open_socket_url(self, app_token: str) -> str:
        return "wss://fake.invalid/socket"
