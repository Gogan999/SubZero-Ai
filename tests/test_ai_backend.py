"""The AI backend seam: factory, capabilities, unavailable client, `check` and setup page wiring."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from subzero_ai.ai_backend import (BACKENDS, Capabilities, UnavailableClient, capabilities_for, make_client,
                                   resolve_backend)
from subzero_ai.checks import FAIL, WARN, run_checks
from subzero_ai.claude import ClaudeClient, ClaudeError, FakeClaude
from subzero_ai.config import load_settings, parse_env_file
from subzero_ai.slack_api import FakeSlack

from .helpers import Harness


def settings_with(**overrides):
    return load_settings(search_cwd=False, data_dir=Path(tempfile.gettempdir()), **overrides)


class FactoryTest(unittest.TestCase):
    def test_default_is_anthropic(self):
        s = settings_with(anthropic_api_key="sk-x", anthropic_base_url="https://proxy.example/")
        self.assertEqual(s.ai_backend, "anthropic")
        client = make_client(s)
        self.assertIsInstance(client, ClaudeClient)
        self.assertEqual(client.api_key, "sk-x")
        self.assertEqual(client.base_url, "https://proxy.example")
        self.assertEqual(client.max_retries, 2)
        self.assertEqual(make_client(s, max_retries=1).max_retries, 1)

    def test_fake(self):
        self.assertIsInstance(make_client(settings_with(ai_backend="fake")), FakeClaude)
        self.assertIsInstance(make_client(settings_with(ai_backend=" Fake ")), FakeClaude)

    def test_fake_claude_flag_wins(self):
        s = settings_with(ai_backend="anthropic", fake_claude=True)
        self.assertEqual(resolve_backend(s), "fake")
        self.assertIsInstance(make_client(s), FakeClaude)

    def test_case_and_whitespace(self):
        self.assertIsInstance(make_client(settings_with(ai_backend="  Anthropic ")), ClaudeClient)
        self.assertEqual(resolve_backend(settings_with(ai_backend=" PI ")), "pi")

    def test_pi_and_unknown_are_unavailable(self):
        for value in ("pi", "xyz"):
            self.assertIsInstance(make_client(settings_with(ai_backend=value)), UnavailableClient)
        self.assertIn("isn't built yet", make_client(settings_with(ai_backend="pi")).message)
        message = make_client(settings_with(ai_backend="xyz")).message
        self.assertIn("AI_BACKEND=xyz", message)
        self.assertIn("anthropic, pi or fake", message)
        self.assertEqual(BACKENDS, ("anthropic", "pi", "fake"))


class UnavailableClientTest(unittest.TestCase):
    def test_stream_reports_error_and_stops(self):
        async def run():
            return [e async for e in UnavailableClient("nope").stream(
                model="m", system="s", messages=[], max_tokens=10)]

        events = asyncio.run(run())
        self.assertEqual([(e.kind, e.error) for e in events], [("error", "nope")])

    def test_complete_and_tool_turn_raise(self):
        client = UnavailableClient("nope")
        with self.assertRaisesRegex(ClaudeError, "nope"):
            asyncio.run(client.complete(model="m", system="s", messages=[], max_tokens=10))
        with self.assertRaisesRegex(ClaudeError, "nope"):
            asyncio.run(client.tool_turn(model="m", system="s", messages=[], tools=[], max_tokens=10))


class CapabilitiesTest(unittest.TestCase):
    def test_claude_ids(self):
        everything = Capabilities(True, True, True, True)
        self.assertEqual(capabilities_for("claude-sonnet-5-5"), everything)
        self.assertEqual(capabilities_for("anthropic/claude-opus-5-5"), everything)

    def test_other_models(self):
        nothing = Capabilities(False, False, False, False)
        self.assertEqual(capabilities_for("openai/gpt-x"), nothing)
        self.assertEqual(capabilities_for(""), nothing)

    def test_frozen(self):
        with self.assertRaises(Exception):
            capabilities_for("claude-x").tools = False  # type: ignore[misc]

    def test_clients_have_the_protocol_methods(self):
        for client in (FakeClaude(), ClaudeClient("k"), UnavailableClient("x")):
            for name in ("stream", "complete", "tool_turn"):
                self.assertTrue(callable(getattr(client, name)), (type(client), name))


class CheckTest(unittest.TestCase):
    def run_check(self, **overrides):
        values = dict(public_url="https://ai.example.org", anthropic_api_key="sk-x", slack_bot_token="xoxb-1",
                      slack_app_token="xapp-1", slack_client_id="1.2", slack_client_secret="s",
                      slack_approvals_channel="CAPPROVALS", admin_slack_ids=frozenset({"U1"}))
        values.update(overrides)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        settings = load_settings(search_cwd=False, data_dir=tmp, **values)
        results = asyncio.run(run_checks(settings, slack=FakeSlack(members={"UBOT"}), claude=None))
        return {r.title: r for r in results}

    def test_unknown_backend_fails(self):
        results = self.run_check(ai_backend="xyz")
        found = results["AI_BACKEND isn't a known backend"]
        self.assertEqual(found.status, FAIL)
        self.assertIn("AI_BACKEND=xyz", found.detail)

    def test_pi_fails(self):
        found = self.run_check(ai_backend="pi")["The AI backend can't be used"]
        self.assertEqual(found.status, FAIL)
        self.assertIn("isn't built yet", found.detail)

    def test_fake_warns(self):
        for overrides in ({"ai_backend": "fake"}, {"fake_claude": True}):
            results = self.run_check(**overrides)
            found = [r for r in results.values() if r.title.startswith("Using the pretend Claude")]
            self.assertEqual([r.status for r in found], [WARN])


class SetupPageTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(slack_approvals_channel="", dev_login=False, ai_backend="fake")
        self.h.services.claude_factory = lambda key: FakeClaude(delay=0)
        page = self.h.client.get("/setup")
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    def tearDown(self):
        self.h.close()

    def save_key(self):
        resp = self.h.client.post("/setup/claude", data={"csrf": self.csrf, "anthropic_api_key": "sk-ant-good"})
        self.assertIn("The key works", resp.text)

    def test_fake_backend_switches_to_anthropic(self):
        self.save_key()
        self.assertEqual(self.h.settings.ai_backend, "anthropic")
        saved = parse_env_file(self.h.settings.config_path)
        self.assertEqual(saved["AI_BACKEND"], "anthropic")
        self.assertEqual(saved["FAKE_CLAUDE"], "0")

    def test_pi_backend_is_left_alone(self):
        self.h.settings.ai_backend = "pi"
        self.save_key()
        self.assertEqual(self.h.settings.ai_backend, "pi")
        self.assertNotIn("AI_BACKEND", parse_env_file(self.h.settings.config_path))


if __name__ == "__main__":
    unittest.main()
