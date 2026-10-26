"""A test copy of the whole app: temporary data folder, pretend Slack, pretend Claude, sample sign-ins."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from starlette.testclient import TestClient

from subzero_ai.claude import FakeClaude
from subzero_ai.config import load_settings
from subzero_ai.slack_api import FakeSlack
from subzero_ai.web import create_app

CSRF_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')

STUDENT = "DEV-student-ava"
STUDENT_2 = "DEV-student-ben"
MENTOR = "DEV-mentor-kim"
ADMIN = "DEV-admin-coach"


class Actor:
    """One signed-in person. Each request carries that person's session cookie."""

    def __init__(self, harness: Harness, slack_user_id: str, cookie: str, csrf: str):
        self.h = harness
        self.slack_user_id = slack_user_id
        self.cookie = cookie
        self.csrf = csrf

    def _send(self, method: str, path: str, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Cookie"] = f"subzero_session={self.cookie}"
        self.h.client.cookies.clear()
        try:
            return self.h.client.request(method, path, headers=headers, **kwargs)
        finally:
            self.h.client.cookies.clear()

    def get(self, path: str, **kwargs):
        return self._send("GET", path, **kwargs)

    def post(self, path: str, data: dict | None = None, *, csrf: bool = True, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json")
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        return self._send("POST", path, data=data or {}, headers=headers, **kwargs)

    # Shortcuts for the chat API
    def new_chat(self, text: str, subteam: str = "Programming"):
        return self.post("/chats/new", {"text": text, "subteam": subteam})

    def send(self, conversation_id: int, text: str):
        return self.post(f"/c/{conversation_id}/messages", {"text": text})

    @property
    def user(self):
        return self.h.services.store.user_by_slack_id(self.slack_user_id)


class Harness:
    def __init__(self, *, slack: FakeSlack | None = None, claude=None, use_slack: bool = True, **overrides):
        self.tmp = Path(tempfile.mkdtemp(prefix="subzero-test-"))
        options = dict(
            data_dir=self.tmp, dev_login=True, public_url="http://localhost:8787",
            slack_approvals_channel="CAPPROVALS", slack_team_id="TDEV", timezone="America/Chicago",
            # Most tests check that every student message waits; the approval window tests turn it on.
            approval_window_minutes=0,
        )
        options.update(overrides)
        self.settings = load_settings(search_cwd=False, **options)
        self.slack = slack if slack is not None else (FakeSlack() if use_slack else None)
        self.claude = claude if claude is not None else FakeClaude(delay=0)
        self.app = create_app(self.settings, slack=self.slack, claude=self.claude, background=False)
        self.services = self.app.state.services
        self.client = TestClient(self.app)
        self.client.__enter__()

    def close(self) -> None:
        try:
            self.client.__exit__(None, None, None)
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, slack_user_id: str) -> Actor:
        self.client.cookies.clear()
        resp = self.client.post("/auth/dev", data={"user": slack_user_id})
        assert resp.status_code == 200, resp.text
        cookie = self.client.cookies.get("subzero_session")
        match = CSRF_RE.search(resp.text)
        assert cookie and match, "sign-in didn't return a session"
        self.client.cookies.clear()
        return Actor(self, slack_user_id, cookie, match.group(1))

    def idle(self) -> None:
        """Wait for background jobs (Claude replies, Slack posts) to finish."""
        self.client.portal.call(self.services.tasks.wait_idle)

    def call(self, func, *args):
        """Run an async function inside the app's event loop."""
        return self.client.portal.call(func, *args)

    def message(self, message_id: int):
        return self.services.store.message(message_id)

    def reply_to(self, message_id: int):
        return self.services.store.reply_for(message_id)
