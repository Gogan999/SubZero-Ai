"""`python -m subzero_ai check` with stand-ins for Slack and Claude."""

from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from subzero_ai.checks import FAIL, OK, WARN, print_report, run_checks
from subzero_ai.config import load_settings
from subzero_ai.slack_api import FakeSlack, SlackError


class GoodClaude:
    def __init__(self):
        self.models = []

    async def complete(self, *, model, **kwargs):
        self.models.append((model, kwargs.get("effort")))
        return "OK"


class BadClaude:
    async def complete(self, **kwargs):
        raise RuntimeError("invalid x-api-key")


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def settings(self, **overrides):
        values = dict(data_dir=self.tmp, public_url="https://ai.example.org", anthropic_api_key="sk-x",
                      slack_bot_token="xoxb-1", slack_app_token="xapp-1", slack_client_id="1.2",
                      slack_client_secret="s", slack_approvals_channel="CAPPROVALS", admin_slack_ids=frozenset({"U1"}))
        values.update(overrides)
        return load_settings(search_cwd=False, **values)

    def statuses(self, results):
        return {r.title: r.status for r in results}

    def test_everything_working(self):
        slack = FakeSlack(members={"UBOT", "UKIM", "ULEE"})
        claude = GoodClaude()
        results = asyncio.run(run_checks(self.settings(), slack=slack, claude=claude))
        failing = [r for r in results if r.status == FAIL]
        self.assertEqual(failing, [])
        self.assertIn(("claude-sonnet-5-5", "medium"), claude.models)
        self.assertIn(("claude-haiku-5-5", None), claude.models)
        self.assertIn("Approvals channel found: 2 mentors", self.statuses(results))
        out = io.StringIO()
        self.assertEqual(print_report(results, out), 0)
        self.assertIn("Everything needed is working", out.getvalue())

    def test_reports_problems(self):
        class NoChannelSlack(FakeSlack):
            async def channel_members(self, channel):
                raise SlackError("conversations.members", "not_in_channel")

        settings = self.settings(slack_app_token="", slack_client_id="", admin_slack_ids=frozenset(),
                                 public_url="http://ai.example.org")
        results = asyncio.run(run_checks(settings, slack=NoChannelSlack(), claude=BadClaude()))
        by_title = self.statuses(results)
        self.assertEqual(by_title["PUBLIC_URL should start with https://"], FAIL)
        self.assertEqual(by_title["Default model didn't answer (claude-sonnet-5-5)"], FAIL)
        self.assertEqual(by_title["The bot can't see the approvals channel"], FAIL)
        self.assertEqual(by_title["No app-level token (SLACK_APP_TOKEN)"], FAIL)
        self.assertEqual(by_title["Sign in with Slack isn't set up"], FAIL)
        self.assertEqual(by_title["No admins named"], WARN)
        out = io.StringIO()
        self.assertEqual(print_report(results, out), 1)
        self.assertIn("problems to fix", out.getvalue())

    def test_bad_slack_token(self):
        class BadTokenSlack(FakeSlack):
            async def auth_test(self):
                raise SlackError("auth.test", "invalid_auth")

        results = asyncio.run(run_checks(self.settings(), slack=BadTokenSlack(), claude=GoodClaude()))
        bad = [r for r in results if r.title == "Slack bot token didn't work"][0]
        self.assertEqual(bad.status, FAIL)
        self.assertIn("Copy it again", bad.detail)

    def test_test_mode(self):
        settings = self.settings(fake_claude=True, dev_login=True, slack_bot_token="", public_url="http://localhost:8787")
        results = asyncio.run(run_checks(settings))
        by_title = self.statuses(results)
        self.assertEqual(by_title["Using the pretend Claude (FAKE_CLAUDE=1)"], WARN)
        self.assertEqual(by_title["Slack isn't connected"], WARN)
        self.assertEqual(by_title["The app is only reachable on this PC"], WARN)
        self.assertEqual(by_title["Test sign-in is on (DEV_LOGIN=1)"], WARN)
        self.assertNotIn(FAIL, by_title.values())
        self.assertIn(OK, by_title.values())


if __name__ == "__main__":
    unittest.main()
