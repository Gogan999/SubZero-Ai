"""`python -m subzero_ai check` with stand-ins for Slack and Claude."""

from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from subzero_ai import checks
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
        self.assertIn(("anthropic/claude-sonnet-5-5", "medium"), claude.models)
        self.assertIn(("anthropic/claude-haiku-5-5", None), claude.models)
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
        self.assertEqual(by_title["Default model didn't answer (anthropic/claude-sonnet-5-5)"], FAIL)
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

    def test_missing_scope_names_the_scope(self):
        class NoScopeSlack(FakeSlack):
            async def auth_test(self):
                raise SlackError("auth.test", "missing_scope", "groups:read")

        results = asyncio.run(run_checks(self.settings(), slack=NoScopeSlack(), claude=GoodClaude()))
        bad = [r for r in results if r.title == "Slack bot token didn't work"][0]
        self.assertIn("groups:read", bad.detail)
        self.assertIn("reinstall", bad.detail)

    def sandbox_rows(self, results):
        return [r for r in results if "sandbox network" in r.title.lower()]

    def test_no_sandbox_row_unless_hyperv_is_on(self):
        results = asyncio.run(run_checks(self.settings(), slack=FakeSlack(), claude=GoodClaude(),
                                         sandbox_runner=lambda script: "[]"))
        self.assertEqual(self.sandbox_rows(results), [])

    def test_a_locked_down_sandbox_network_is_ok(self):
        settings = self.settings(code_sandbox="hyperv", sandbox_base_image="/srv/base.vhdx")
        seen = []

        def runner(script):
            seen.append(script)
            return "[]"

        results = asyncio.run(run_checks(settings, slack=FakeSlack(), claude=GoodClaude(), sandbox_runner=runner))
        [row] = self.sandbox_rows(results)
        self.assertEqual((row.status, row.title), (OK, "The sandbox network is locked down"))
        self.assertIn("szsandbox-internal", row.detail)
        self.assertIn("Get-VMSwitch -Name 'szsandbox-internal'", seen[0])

    def test_a_problem_in_the_sandbox_network_is_a_failure_with_the_fix(self):
        settings = self.settings(code_sandbox="hyperv", sandbox_base_image="/srv/base.vhdx", sandbox_proxy_port=8080)
        seen = []

        def runner(script):
            seen.append(script)
            return '["forwarding-on","host-gateway"]'

        results = asyncio.run(run_checks(settings, slack=FakeSlack(), claude=GoodClaude(), sandbox_runner=runner))
        [row] = self.sandbox_rows(results)
        self.assertEqual(row.status, FAIL)
        self.assertIn("IP forwarding is on", row.detail)
        self.assertIn("default gateway", row.detail)
        self.assertIn("sandbox-network.ps1 -Action Install", row.detail)
        self.assertIn("'8080'", seen[0])

    def test_a_failing_host_command_is_a_failure_not_a_crash(self):
        settings = self.settings(code_sandbox="hyperv", sandbox_base_image="/srv/base.vhdx")

        def runner(script):
            raise OSError("powershell.exe is missing")

        results = asyncio.run(run_checks(settings, slack=FakeSlack(), claude=GoodClaude(), sandbox_runner=runner))
        [row] = self.sandbox_rows(results)
        self.assertEqual(row.status, FAIL)
        self.assertIn("powershell.exe is missing", row.detail)

    def test_on_a_non_windows_host_the_network_cannot_be_checked(self):
        settings = self.settings(code_sandbox="hyperv", sandbox_base_image="/srv/base.vhdx")
        with mock.patch.object(checks, "os", SimpleNamespace(name="posix")):
            results = asyncio.run(run_checks(settings, slack=FakeSlack(), claude=GoodClaude()))
        [row] = self.sandbox_rows(results)
        self.assertEqual(row.status, WARN)
        self.assertIn("can't be checked on this computer", row.title)

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
