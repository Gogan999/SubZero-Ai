"""Keeps track of whether Slack and the Claude API are working, for the admin page and /healthz,
and sends admins a Slack DM when either has been failing for 15 minutes (and again when it recovers)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import Settings
from .saved_settings import SLACK_ID_RE

log = logging.getLogger("subzero.health")

ALERT_AFTER_SECONDS = 15 * 60
SLACK_PROBE_SECONDS = 5 * 60
NAMES = {"slack": "Slack", "claude": "the Claude API"}


@dataclass
class Status:
    ok_at: float | None = None
    failing_since: float | None = None
    detail: str = ""
    alerted: bool = False

    @property
    def state(self) -> str:
        if self.failing_since is not None:
            return "failing"
        return "ok" if self.ok_at is not None else "unknown"


class Health:
    def __init__(self, settings: Settings, slack, *, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.slack = slack
        self.clock = clock
        self.status = {name: Status() for name in NAMES}
        self.socket_client = None  # set when Socket Mode starts
        self._probed_at: float | None = None

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        status = self.status[name]
        now = self.clock()
        if ok:
            status.ok_at, status.failing_since, status.detail = now, None, ""
        else:
            status.detail = detail[:300]
            if status.failing_since is None:
                status.failing_since = now

    async def check(self) -> None:
        """Called every minute: probe Slack now and then (it's free), then send any alerts that are due."""
        now = self.clock()
        if self.slack is not None and (self._probed_at is None or now - self._probed_at >= SLACK_PROBE_SECONDS
                                       or self.status["slack"].failing_since is not None):
            self._probed_at = now
            try:
                await self.slack.auth_test()
                problem = ""
            except Exception as exc:
                problem = f"Slack didn't answer ({getattr(exc, 'error', '') or type(exc).__name__})."
            client = self.socket_client
            if not problem and client is not None and not client.connected.is_set():
                problem = "The connection that carries Slack button taps is down."
            self.record("slack", not problem, problem)
        await self._send_alerts()

    async def _send_alerts(self) -> None:
        now = self.clock()
        for name, status in self.status.items():
            if status.failing_since is not None and not status.alerted:
                if now - status.failing_since >= ALERT_AFTER_SECONDS:
                    minutes = int((now - status.failing_since) // 60)
                    text = (f"{self.settings.app_name}: calls to {NAMES[name]} have been failing for {minutes} "
                            f"minutes. {status.detail} Check the admin page: {self.settings.url('/settings')}")
                    if await self._dm_admins(text):
                        status.alerted = True
            elif status.failing_since is None and status.alerted:
                if await self._dm_admins(f"{self.settings.app_name}: {NAMES[name]} is working again."):
                    status.alerted = False

    async def _dm_admins(self, text: str) -> bool:
        if self.slack is None:
            return False
        sent = False
        for admin in sorted(self.settings.admin_slack_ids):
            if not SLACK_ID_RE.fullmatch(admin):
                continue
            try:
                await self.slack.dm(admin, text)
                sent = True
            except Exception:
                log.warning("Couldn't send the health alert to %s", admin, exc_info=True)
        return sent

    def snapshot(self) -> dict[str, dict]:
        out = {}
        for name, status in self.status.items():
            out[name] = {"state": status.state, "detail": status.detail,
                         "since": status.failing_since, "ok_at": status.ok_at}
        if self.slack is None:
            out["slack"] = {"state": "off", "detail": "Slack isn't set up.", "since": None, "ok_at": None}
        return out
