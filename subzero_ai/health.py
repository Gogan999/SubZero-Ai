"""Keeps track of whether Slack and the Claude API are working, for the admin page and /healthz,
and sends admins a Slack DM when either has been failing for 15 minutes (and again when it recovers).

Slack is probed every few minutes, which is free. Claude is judged by real replies, and only while a call
has just failed is it probed with a one-token request, so one stray error clears itself within minutes
instead of looking like an outage until the next student asks something."""

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
CLAUDE_PROBE_SECONDS = 5 * 60
# How many failures in a row count as "not working": one Slack probe, but two Claude calls, since a single
# busy (529) or empty reply happens now and then.
FAILURES_NEEDED = {"slack": 1, "claude": 2}
NAMES = {"slack": "Slack", "claude": "the Claude API"}


@dataclass
class Status:
    ok_at: float | None = None
    failing_since: float | None = None
    detail: str = ""
    alerted: bool = False
    failures: int = 0  # in a row
    needed: int = 1

    @property
    def failing(self) -> bool:
        return self.failing_since is not None and self.failures >= self.needed

    @property
    def state(self) -> str:
        if self.failing:
            return "failing"
        return "ok" if self.ok_at is not None else "unknown"


class Health:
    def __init__(self, settings: Settings, slack, claude=None, *, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.slack = slack
        self.claude = claude
        self.clock = clock
        self.status = {name: Status(needed=FAILURES_NEEDED[name]) for name in NAMES}
        self.socket_client = None  # set when Socket Mode starts
        self._probed_at: float | None = None
        self._claude_probed_at: float | None = None

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        status = self.status[name]
        now = self.clock()
        if ok:
            status.ok_at, status.failing_since, status.detail, status.failures = now, None, "", 0
        else:
            status.detail = detail[:300]
            status.failures += 1
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
        await self._probe_claude(now)
        await self._send_alerts()

    async def _probe_claude(self, now: float) -> None:
        """While Claude's last call failed, check it now and then with a one-token request (a tiny fraction
        of a cent), so it can recover, or be confirmed down, without waiting for a student."""
        if self.claude is None or self.status["claude"].failing_since is None:
            return
        if self._claude_probed_at is not None and now - self._claude_probed_at < CLAUDE_PROBE_SECONDS:
            return
        self._claude_probed_at = now
        try:
            await self.claude.complete(model=self.settings.model_titles, system="Reply with OK.",
                                       messages=[{"role": "user", "content": "Status check"}], max_tokens=1)
        except Exception as exc:
            self.record("claude", False, f"A status check didn't get through: {exc}")
        else:
            self.record("claude", True)

    async def _send_alerts(self) -> None:
        now = self.clock()
        for name, status in self.status.items():
            if status.failing and not status.alerted:
                if now - status.failing_since >= ALERT_AFTER_SECONDS:
                    minutes = int((now - status.failing_since) // 60)
                    text = (f"{self.settings.app_name}: calls to {NAMES[name]} have been failing for {minutes} "
                            f"minutes. {status.detail} Check the admin page: {self.settings.url('/settings')}")
                    if await self._dm_admins(text):
                        status.alerted = True
            elif not status.failing and status.alerted:
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
