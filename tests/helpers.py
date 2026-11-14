"""A test copy of the whole app: temporary data folder, pretend Slack, pretend Claude, sample sign-ins."""

from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import time
import unittest
import zipfile
from dataclasses import dataclass
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from subzero_ai import model_check
from subzero_ai.auth import ISSUER
from subzero_ai.claude import FakeClaude
from subzero_ai.config import load_settings
from subzero_ai.slack_api import FakeSlack
from subzero_ai.web import create_app

from .backends import client_for_active_backend

CSRF_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')

STUDENT = "DEV-student-ava"
STUDENT_2 = "DEV-student-ben"
MENTOR = "DEV-mentor-kim"
ADMIN = "DEV-admin-coach"

# Slack-side people for tests driven through Slack (Harness.slack_people)
SLACK_KID = "UKID0001"
SLACK_KID_2 = "UKID0002"
SLACK_MENTOR = "UMENTOR1"
SLACK_MENTOR_2 = "UMENTOR2"
_slack_ts = [0]


@dataclass
class SlackSent:
    """A student's Slack DM after the app handled it."""

    ts: str
    channel: str
    thread_ts: str
    message_id: int | None
    conversation_id: int | None


def pass_model_check(h: "Harness", model: str, *, failing: str = "") -> None:
    """Record a Model safety check result for a model: every case passes, except the one named `failing`."""
    names = model_check.CHAT_CASES + model_check.SCREENER_CASES
    cases = [model_check.CaseResult(name, name != failing) for name in names]
    model_check.save_result(h.services.db, model_check.CheckResult(model, cases=cases))


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
        # ai_backend="pi" lets build_services make the real client; the others get the offline stand-in
        if claude is None and self.settings.ai_backend != "pi":
            # Test classes that run against another backend (tests/backends.py) supply their own client
            claude = client_for_active_backend() or FakeClaude(delay=0)
        self.claude = claude
        self.app = create_app(self.settings, slack=self.slack, claude=self.claude, background=False)
        self.services = self.app.state.services
        self.claude = self.services.claude
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

    # ---- driving the app through Slack only (the entry points Socket Mode uses)
    def slack_people(self) -> None:
        """Make the fake workspace: two mentors in the approvals channel, a student and a second student outside it."""
        self.slack.members = {SLACK_MENTOR, SLACK_MENTOR_2, "UBOT"}
        self.slack.users[SLACK_KID] = {"id": SLACK_KID, "deleted": False, "profile": {"display_name": "Kid K"}}
        self.slack.users[SLACK_KID_2] = {"id": SLACK_KID_2, "deleted": False, "profile": {"display_name": "Kid L"}}
        self.slack.users[SLACK_MENTOR] = {"id": SLACK_MENTOR, "profile": {"display_name": "Coach Lee"}}
        self.slack.users[SLACK_MENTOR_2] = {"id": SLACK_MENTOR_2, "profile": {"display_name": "Sam Ortiz"}}

    def slack_dm(self, text: str = "How do I tune a PID loop?", *, user: str = SLACK_KID, thread_ts: str | None = None,
                 channel: str | None = None, extra: dict | None = None) -> "SlackSent":
        """The person sends the bot a direct message and the app handles it, as an `message.im` event would.
        Top level starts a chat; `thread_ts` continues the chat in that thread. message_id is None when the app
        didn't save a message (a refusal or a limit)."""
        channel = channel or ("D0" + user)
        _slack_ts[0] += 1
        ts = f"17000{_slack_ts[0]:05d}.000100"
        event = {"type": "message", "channel_type": "im", "channel": channel, "user": user, "text": text, "ts": ts}
        if thread_ts:
            event["thread_ts"] = thread_ts
        event.update(extra or {})
        envelope = {"type": "events_api", "envelope_id": "e", "payload": {"type": "event_callback", "event": event}}
        self.call(self.services.interactions.dispatch, envelope)
        self.idle()
        row = self.services.db.one("SELECT message_id FROM slack_dm_messages WHERE channel = ? AND ts = ?",
                                   (channel, ts))
        message_id = row["message_id"] if row else None
        conversation_id = self.message(message_id)["conversation_id"] if message_id else None
        return SlackSent(ts=ts, channel=channel, thread_ts=thread_ts or ts, message_id=message_id,
                         conversation_id=conversation_id)

    def slack_tap(self, action: str, message_id: int, user: str = SLACK_MENTOR_2):
        """A mentor taps a button on the approval card (approve, reject, edit, hold: "handle_private")."""
        envelope = {"type": "interactive", "envelope_id": "e", "payload": {
            "type": "block_actions", "trigger_id": "trig-1", "user": {"id": user},
            "channel": {"id": "CAPPROVALS"}, "container": {"channel_id": "CAPPROVALS"},
            "actions": [{"action_id": action, "value": str(message_id)}]}}
        ack = self.call(self.services.interactions.dispatch, envelope)
        self.idle()
        return ack

    def slack_submit(self, callback_id: str, message_id: int, block: str, text: str, user: str = SLACK_MENTOR_2):
        """A mentor submits a card's modal (callback_id "reject_submit" with block "reason", or "edit_submit" with
        block "edited")."""
        envelope = {"type": "interactive", "envelope_id": "e", "payload": {
            "type": "view_submission", "user": {"id": user},
            "view": {"callback_id": callback_id, "private_metadata": str(message_id),
                     "state": {"values": {block: {"text": {"type": "plain_text_input", "value": text}}}}}}}
        ack = self.call(self.services.interactions.dispatch, envelope)
        self.idle()
        return ack

    def slack_reject(self, message_id: int, reason: str, user: str = SLACK_MENTOR_2):
        return self.slack_submit("reject_submit", message_id, "reason", reason, user)

    def slack_thread(self, sent: "SlackSent") -> list[dict]:
        """What the app posted in the student's DM thread, oldest first."""
        return [p for p in self.slack.posts if p["channel"] == sent.channel and p["thread_ts"] == sent.thread_ts]

    def chat_calls(self) -> list[dict]:
        """The chat replies the AI client was asked for (not the screener, titles or Code turns)."""
        return [c for c in self.claude.calls if c.get("kind") is None]


# ---- shared by several test modules (moved here from test_auth, test_features and test_code_students)
CLIENT_ID = "1234.5678"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks():
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
    key.update({"kid": "slack-key-1", "alg": "RS256", "use": "sig"})
    return {"keys": [key]}


def id_token(nonce, *, team="TDEV", user="U0AVA", aud=CLIENT_ID, key=PRIVATE_KEY, expires_in=300, **extra):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": user, "aud": aud, "iat": now, "exp": now + expires_in, "nonce": nonce,
              "https://slack.com/team_id": team, "https://slack.com/user_id": user,
              "name": "Ava Martinez", "email": "ava@example.org", **extra}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "slack-key-1"})


class FakeSlackTokenResponse:
    def __init__(self, body):
        self.body = body
        self.content = json.dumps(body).encode()

    def json(self):
        return self.body


class FakeSlackSession:
    """Stands in for requests.Session when the app exchanges the sign-in code."""

    def __init__(self):
        self.token = None
        self.posts = []

    def post(self, url, data=None, timeout=None):
        self.posts.append({"url": url, "data": data})
        if self.token is None:
            return FakeSlackTokenResponse({"ok": False, "error": "invalid_code"})
        return FakeSlackTokenResponse({"ok": True, "access_token": "xoxp-1", "id_token": self.token})


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, url="https://api.github.com/"):
        self.status_code, self.body, self.url = status, body, url
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    def json(self):
        return json.loads(self.body)

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]


class FakeWeb:
    """Stands in for requests.Session: answers by URL, and records what was asked."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self):
        return self

    def get(self, url, params=None, headers=None, **kwargs):
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        answer = self.routes.get(url)
        return answer() if callable(answer) else (answer or FakeResponse(404, b"{}", url=url))


def repo_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("team-robot-code-abc123/build.gradle", "plugins { id 'java' }\n")
        z.writestr("team-robot-code-abc123/src/Robot.java", "public class Robot {\n  int kP = 5;\n}\n")
        z.writestr("team-robot-code-abc123/../evil.txt", "outside")
        script = zipfile.ZipInfo("team-robot-code-abc123/gradlew")
        script.external_attr = 0o755 << 16
        z.writestr(script, "#!/bin/sh\necho BUILD SUCCESSFUL \"$@\"\n")
    return buf.getvalue()


CODE_API = "https://api.github.com/repos/team/robot-code/zipball"


class CodeStudentTest(unittest.TestCase):
    """Students' sessions with the fake sandbox switched on and student access turned on."""

    overrides: dict = {}

    def setUp(self):
        self.h = Harness(connector_github_repo="team/robot-code", code_sandbox="fake", sandbox_min_free_gb=0,
                         code_student_access=True, **self.overrides)
        self.ava = self.h.login(STUDENT)
        self.ben = self.h.login(STUDENT_2)
        self.kim = self.h.login(MENTOR)
        self.h.services.github.session_factory = FakeWeb({CODE_API: FakeResponse(200, repo_zip())})
        self.code = self.h.services.code
        self.db = self.h.services.db
        self.manager = self.h.services.sandboxes
        self.root = self.h.services.settings.data_dir / "sandbox-fake"

    def tearDown(self):
        self.h.close()

    # ---- helpers
    def ask(self, actor, task="Add a notes file and run the build."):
        resp = actor.post("/code", {"task": task}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        location = resp.headers["location"]
        self.h.idle()
        if not location.rsplit("/", 1)[1].isdigit():
            return None, location
        return int(location.rsplit("/", 1)[1]), location

    def approve(self, sid):
        resp = self.kim.post(f"/code/{sid}/approve-request", {"note": ""}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        self.h.idle()

    def decline(self, sid, note=""):
        resp = self.kim.post(f"/code/{sid}/decline-request", {"note": note}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        self.h.idle()

    def tool_turns(self) -> int:
        return sum(1 for c in self.h.claude.calls if c["kind"] == "tool_turn")

    def actions(self) -> list[str]:
        return [row["action"] for row in self.db.all("SELECT action FROM audit_log ORDER BY id")]

    def steps(self, sid, status=None):
        return [s for s in self.code.steps(sid) if s["kind"] == "tool" and (status is None or s["status"] == status)]
