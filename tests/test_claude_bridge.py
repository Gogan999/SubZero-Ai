"""claude-bridge/ models: Claude through a Claude Code login (a Claude subscription), no API key."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from subzero_ai import __main__ as cli
from subzero_ai import model_check, saved_settings
from subzero_ai.ai_backend import capabilities_for
from subzero_ai.checks import FAIL, OK, run_checks
from subzero_ai.claude import FakeClaude
from subzero_ai.config import PROVIDERS, Settings, load_settings, model_provider, normalize_model_id
from subzero_ai.pi_client import (LOGIN_MESSAGE, QUOTA_MESSAGE, child_env, PiClient, bridge_env, bridge_logged_in, claude_cli_path,
                                  error_message, provider_keys)
from subzero_ai.slack_api import FakeSlack

from .helpers import ADMIN, Harness

FAKE = str(Path(__file__).with_name("fake_pi_gateway.py"))
BRIDGE = "claude-bridge/claude-sonnet-5-5"
HTML = {"Accept": "text/html"}


class ModelIdTest(unittest.TestCase):
    def test_provider_is_known_and_labelled(self):
        self.assertIn("claude-bridge", PROVIDERS)
        self.assertEqual(saved_settings.PROVIDER_LABELS["claude-bridge"], "Claude subscription (Claude Code)")
        self.assertIn("claude-bridge", saved_settings.PROVIDER_HELP)

    def test_bridge_id_is_not_rewritten_as_an_anthropic_id(self):
        self.assertEqual(normalize_model_id(BRIDGE), BRIDGE)
        self.assertEqual(model_provider(BRIDGE), "claude-bridge")
        self.assertEqual(normalize_model_id("claude-sonnet-5-5"), "anthropic/claude-sonnet-5-5")

    def test_capabilities_are_not_those_of_the_api(self):
        for backend in ("pi", "anthropic"):
            none = capabilities_for(BRIDGE, backend)
            self.assertEqual((none.web_search, none.tools, none.thinking, none.images, none.code), (False,) * 5)
        info = capabilities_for(BRIDGE, "pi", {"images": True, "reasoning": True})
        self.assertEqual((info.web_search, info.tools, info.thinking, info.images, info.code),
                         (False, False, True, True, False))
        self.assertTrue(capabilities_for("anthropic/claude-sonnet-5-5", "pi").code)
        self.assertTrue(capabilities_for("claude-sonnet-5-5", "pi").tools)


class SettingsTest(unittest.TestCase):
    def settings(self, **kw):
        values = dict(ai_backend="pi", ai_providers=("anthropic",))
        values.update(kw)
        return Settings(**values)

    def form(self, providers):
        return {"ai_providers": "\n".join(providers)}

    def test_turning_it_on_needs_the_pi_backend(self):
        values, errors = saved_settings.validate("claude", self.form(["anthropic", "claude-bridge"]),
                                                 settings=self.settings(ai_backend="anthropic"))
        self.assertIn("AI_BACKEND=pi", errors["ai_providers"])
        self.assertNotIn("ai_providers", values)

    def test_turning_it_on_with_the_pi_backend(self):
        values, errors = saved_settings.validate("claude", self.form(["anthropic", "claude-bridge"]),
                                                 settings=self.settings())
        self.assertNotIn("ai_providers", errors)
        self.assertEqual(values["ai_providers"], ("anthropic", "claude-bridge"))

    def test_bridge_models_follow_the_provider_switch(self):
        self.assertEqual(saved_settings.check_model(BRIDGE, ("anthropic", "claude-bridge"), "pi"), (BRIDGE, ""))
        self.assertIn("isn't turned on", saved_settings.check_model(BRIDGE, ("anthropic",), "pi")[1])
        self.assertIn("pi", saved_settings.check_model(BRIDGE, ("claude-bridge",), "anthropic")[1])

    def test_no_api_key_for_the_bridge(self):
        s = self.settings(ai_providers=("anthropic", "claude-bridge"), anthropic_api_key="sk-ant-x")
        self.assertEqual(provider_keys(s), {"anthropic": "sk-ant-x"})

    def test_the_judge_must_be_an_api_key_model(self):
        s = self.settings(ai_providers=("anthropic", "claude-bridge"), anthropic_api_key="sk-ant-x")
        problem = model_check.judge_problem(s, BRIDGE)
        self.assertIn("API-key model", problem)
        self.assertEqual(model_check.judge_problem(s, "anthropic/claude-haiku-5-5"), "")


class EnvTest(unittest.TestCase):
    def test_bridge_env(self):
        env = bridge_env("/data")
        self.assertEqual(env["SUBZERO_BRIDGE"], "1")
        self.assertEqual(Path(env["CLAUDE_CONFIG_DIR"]), Path("/data/bridge/claude-config"))
        self.assertEqual(Path(env["PI_CODING_AGENT_DIR"]), Path("/data/bridge/pi-agent"))
        self.assertFalse([k for k in env if k.startswith("ANTHROPIC_")])

    def test_client_env_only_when_the_provider_is_on(self):
        off = PiClient.from_settings(Settings(ai_backend="pi", data_dir=Path("/data")))
        self.assertNotIn("SUBZERO_BRIDGE", off.env)
        on = PiClient.from_settings(Settings(ai_backend="pi", ai_providers=("anthropic", "claude-bridge"),
                                             data_dir=Path("/data"), anthropic_api_key="sk-ant-x"))
        self.assertEqual(on.env["SUBZERO_BRIDGE"], "1")
        self.assertEqual(Path(on.env["CLAUDE_CONFIG_DIR"]), Path("/data/bridge/claude-config"))
        self.assertFalse([k for k in on.env if k.startswith("ANTHROPIC_")])
        on.set_keys({"anthropic": "sk-ant-y"})
        self.assertEqual(on.env["SUBZERO_BRIDGE"], "1")  # new keys keep the bridge settings
        on.set_bridge({})
        self.assertFalse([k for k in on.env if k in ("SUBZERO_BRIDGE", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR")])
        self.assertEqual(on.env["SUBZERO_KEY_ANTHROPIC"], "sk-ant-y")
        on.set_bridge(bridge_env("/data"))
        self.assertEqual(on.env["SUBZERO_BRIDGE"], "1")


class ChildEnvTest(unittest.TestCase):
    PARENT = {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_BASE_URL": "http://x",
              "CLAUDE_CODE_OAUTH_TOKEN": "tok", "CLAUDE_CONFIG_DIR": "/elsewhere", "SUBZERO_KEY_OPENAI": "sk-oa"}

    def test_child_env_never_has_anthropic_or_claude_variables(self):
        env = child_env({"anthropic": "sk-ant-real"}, dict(self.PARENT))
        self.assertEqual(sorted(env), ["HOME", "PATH", "SUBZERO_KEY_ANTHROPIC"])

    def test_client_env_with_the_bridge_on_has_no_anthropic_variables(self):
        s = Settings(ai_backend="pi", ai_providers=("anthropic", "claude-bridge"), data_dir=Path("/data"),
                     anthropic_api_key="sk-ant-real", pi_request_timeout=120)
        with mock.patch.dict(os.environ, self.PARENT):
            client = PiClient.from_settings(s)
        self.assertFalse([k for k in client.env if k.startswith("ANTHROPIC_")])
        self.assertEqual(client.env["CLAUDE_CONFIG_DIR"], str(Path("/data/bridge/claude-config")))
        self.assertEqual(client.env["PI_REQUEST_TIMEOUT"], "120")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", client.env)
        self.assertNotIn("SUBZERO_KEY_OPENAI", client.env)


class SetBridgeRestartTest(unittest.IsolatedAsyncioTestCase):
    async def test_set_bridge_restarts_the_gateway_with_the_new_env(self):
        c = PiClient([sys.executable, FAKE], child_env({"anthropic": "sk-old"}), kill_wait=3)
        self.addAsyncCleanup(c.aclose)
        before = (await c.ping())["env"]
        self.assertNotIn("SUBZERO_BRIDGE", before)
        old = c._proc
        c.set_bridge(bridge_env("/data", 90))
        self.assertIsNone(c._proc)
        after = (await c.ping())["env"]
        self.assertEqual((after["SUBZERO_BRIDGE"], after["PI_REQUEST_TIMEOUT"]), ("1", "90"))
        self.assertEqual(after["SUBZERO_KEY_ANTHROPIC"], "sk-old")
        self.assertIsNot(c._proc, old)
        for _ in range(100):
            if old.popen.poll() is not None:
                break
            await asyncio.sleep(0.05)
        self.assertIsNotNone(old.popen.poll(), "the old gateway should have stopped")
        c.set_bridge({})
        self.assertNotIn("SUBZERO_BRIDGE", (await c.ping())["env"])


class ErrorTest(unittest.TestCase):
    def test_quota_error_is_plain(self):
        text = error_message("provider", "Claude quota exceeded — resets at 5pm", BRIDGE)
        self.assertEqual(text, QUOTA_MESSAGE)
        self.assertEqual(text, "The Claude subscription has hit its limit for now. Try again later or ask a mentor "
                               "to switch models.")

    def test_not_logged_in_message(self):
        for detail in ("Not logged in \u00b7 Please run /login", "Claude Code process exited: please run /login"):
            self.assertEqual(error_message("provider", detail, BRIDGE), LOGIN_MESSAGE)
        self.assertIn("claude-login", LOGIN_MESSAGE)
        self.assertNotEqual(error_message("provider", "Not logged in", "anthropic/claude-x"), LOGIN_MESSAGE)

    def test_tool_stop_message_is_kept(self):
        msg = "Claude tried to use a tool, so this reply was stopped."
        self.assertEqual(error_message("provider", msg, BRIDGE), msg)

    def test_other_models_keep_the_generic_message(self):
        self.assertEqual(error_message("provider", "Claude quota exceeded", "anthropic/claude-x"),
                         "The AI service returned an error: Claude quota exceeded")


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def settings(self, **kw):
        values = dict(data_dir=self.tmp, ai_backend="pi", ai_providers=("anthropic", "claude-bridge"),
                      anthropic_api_key="sk-ant-x", pi_gateway_script=str(self.tmp / "gw" / "gateway.mjs"))
        values.update(kw)
        return load_settings(search_cwd=False, **values)

    def install_cli(self, name="claude-agent-sdk-linux-x64", exe="claude"):
        folder = self.tmp / "gw" / "node_modules" / "@anthropic-ai" / name
        folder.mkdir(parents=True)
        (folder / exe).write_text("x")

    def test_finds_the_program_for_each_system(self):
        self.install_cli("claude-agent-sdk-win32-x64", "claude.exe")
        gw = self.tmp / "gw" / "gateway.mjs"
        self.assertEqual(claude_cli_path(gw, platform="win32", machine="AMD64").name, "claude.exe")
        self.assertIsNone(claude_cli_path(gw, platform="linux", machine="x86_64"))
        self.assertIsNone(claude_cli_path(self.tmp / "nowhere" / "gateway.mjs", platform="win32", machine="AMD64"))

    def test_login_runs_the_cli_with_its_own_config_folder(self):
        self.install_cli()
        calls = []

        def fake_run(command, env):
            calls.append((command, env))
            (self.tmp / "bridge" / "claude-config" / ".credentials.json").write_text("{}")
            return SimpleNamespace(returncode=0)

        extra = {"ANTHROPIC_API_KEY": "sk-ant-should-not-pass", "SUBZERO_KEY_OPENAI": "sk-oa", "SUBZERO_BRIDGE": "1",
                 "CLAUDE_CODE_OAUTH_TOKEN": "tok", "CLAUDE_CONFIG_DIR": "/elsewhere", "PATH": os.environ.get("PATH", "")}
        with mock.patch.dict(os.environ, extra):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.claude_login_tool(self.settings(), run=fake_run)
        self.assertEqual(code, 0)
        command, env = calls[0]
        self.assertEqual(command[1:], ["auth", "login", "--claudeai"])
        self.assertEqual(Path(env["CLAUDE_CONFIG_DIR"]), self.tmp / "bridge" / "claude-config")
        self.assertFalse([k for k in env if k.upper().startswith(("ANTHROPIC_", "SUBZERO_"))], sorted(env))
        self.assertEqual([k for k in env if k.upper().startswith("CLAUDE_")], ["CLAUDE_CONFIG_DIR"])
        self.assertIn("PATH", env)
        self.assertIn("Signed in", out.getvalue())
        self.assertNotIn("sk-ant", out.getvalue())

    def test_login_that_does_not_finish_fails(self):
        self.install_cli()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.claude_login_tool(self.settings(), run=lambda command, env: SimpleNamespace(returncode=0))
        self.assertEqual(code, 1)
        self.assertIn("didn't finish", out.getvalue())

    def test_login_without_the_program(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.claude_login_tool(self.settings()), 1)
        self.assertIn("Couldn't find", out.getvalue())

    def test_check_reports_the_login(self):
        class Claude:
            async def ping(self):
                return {"node": "v22", "pi_ai": "1.4.0"}

            async def complete(self, **kwargs):
                return "OK"

        def run():
            slack = FakeSlack(members={"UBOT", "UKIM"})
            return {r.title: r for r in asyncio.run(run_checks(self.settings(), slack=slack, claude=Claude()))}

        results = run()
        self.assertEqual(results["Claude subscription isn't signed in"].status, FAIL)
        self.assertNotIn("No API key for claude-bridge", results)
        (self.tmp / "bridge" / "claude-config").mkdir(parents=True)
        (self.tmp / "bridge" / "claude-config" / ".credentials.json").write_text('{"secret": "x"}')
        self.assertTrue(bridge_logged_in(self.tmp))
        results = run()
        self.assertEqual(results["Claude subscription is signed in (Claude Code login found)"].status, OK)
        self.assertNotIn("secret", "".join(r.detail for r in results.values()))

    def test_check_needs_the_pi_backend(self):
        class Claude:
            async def complete(self, **kwargs):
                return "OK"

        slack = FakeSlack(members={"UBOT"})
        results = {r.title: r for r in asyncio.run(
            run_checks(self.settings(ai_backend="anthropic"), slack=slack, claude=Claude()))}
        self.assertEqual(results["The Claude subscription needs AI_BACKEND=pi"].status, FAIL)


class BridgeClaude(FakeClaude):
    def __init__(self):
        super().__init__(delay=0)
        self.bridge: list[dict] = []
        self.keys: list[dict] = []

    def set_bridge(self, env):
        self.bridge.append(dict(env))

    def set_keys(self, keys):
        self.keys.append(dict(keys))


class AdminPageTest(unittest.TestCase):
    FORM = {"model_default": "anthropic/claude-sonnet-5-5", "model_hard": "anthropic/claude-opus-5-5",
            "model_titles": "anthropic/claude-haiku-5-5", "effort_default": "medium", "effort_high": "high",
            "max_tokens_chat": "8000", "max_tokens_explainer": "16000", "long_chat_tokens": "150000",
            "web_search_daily_limit": "50"}

    def make(self, backend):
        claude = BridgeClaude()
        h = Harness(claude=claude, ai_backend=backend, ai_providers=("anthropic",))
        self.addCleanup(h.close)
        return claude, h, h.login(ADMIN)

    def test_checkbox_and_help_are_shown(self):
        _, _, admin = self.make("pi")
        page = admin.get("/settings").text
        self.assertIn("Claude subscription (Claude Code)", page)
        self.assertIn("python -m subzero_ai claude-login", page)
        self.assertIn("hard-problem fallback", page)

    def test_turning_it_on_updates_the_running_client(self):
        claude, h, admin = self.make("pi")
        resp = admin.post("/settings/claude", {**self.FORM, "ai_providers": ["anthropic", "claude-bridge"]},
                          headers=HTML)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(h.settings.ai_providers, ("anthropic", "claude-bridge"))
        self.assertEqual(claude.bridge[-1]["SUBZERO_BRIDGE"], "1")
        page = admin.get("/settings").text
        self.assertNotIn('name="claude_bridge_api_key"', page)  # no API key box for it
        admin.post("/settings/ai-keys", {"anthropic_api_key": "sk-ant-" + "a" * 30})
        self.assertEqual(len(claude.keys), 1)
        resp = admin.post("/settings/claude", {**self.FORM, "ai_providers": ["anthropic"]}, headers=HTML)
        self.assertEqual(claude.bridge[-1], {})

    def test_refused_without_the_pi_backend(self):
        claude, h, admin = self.make("fake")
        resp = admin.post("/settings/claude", {**self.FORM, "ai_providers": ["anthropic", "claude-bridge"]},
                          headers=HTML)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("AI_BACKEND=pi", resp.text)
        self.assertEqual(h.settings.ai_providers, ("anthropic",))
        self.assertEqual(claude.bridge, [])


if __name__ == "__main__":
    unittest.main()
