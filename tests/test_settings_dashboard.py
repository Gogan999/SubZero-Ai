"""The Settings page's config.env sections (AI engine, site, Slack), key removal, connected accounts, Run checks and
Restart: everything an admin can change without editing files on the mini PC."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest import mock

from subzero_ai import model_check, server_settings
from subzero_ai.config import parse_env_file
from subzero_ai.pi_client import bridge_credentials_path
from subzero_ai.slack_api import FakeSlack, SlackError

from .helpers import ADMIN, MENTOR, STUDENT, Harness

HTML = {"Accept": "text/html"}
BOT = "xoxb-1111-2222-SECRETBOTTOKEN"
APP = "xapp-1-A111-SECRETAPPTOKEN99"
CLIENT_SECRET = "SECRETCLIENTSECRET0123456789"
OPENAI_KEY = "sk-openaiSECRETSECRET123456"

SITE = {"public_url": "https://ai.team.org", "app_name": "SubZero AI", "timezone": "America/Chicago",
        "max_text_file_kb": "200", "backup_dir": "", "backup_hour": "2", "check_for_updates": "1",
        "update_repo": "Gogan999/SubZero-Ai"}
ENGINE = {"ai_backend": "fake", "pi_request_timeout": "300", "pi_idle_timeout": "120", "pi_node": "",
          "pi_gateway_script": "", "anthropic_base_url": "https://api.anthropic.com"}
SLACK = {"slack_client_id": "123.456", "slack_approvals_channel": "CAPPROVALS", "allow_slack_guests": ""}


class Base(unittest.TestCase):
    overrides: dict = {}

    def setUp(self):
        self.h = Harness(**{"ai_backend": "fake", **self.overrides})
        self.h.settings.public_url = SITE["public_url"]  # after the app is built, so cookies still work over http
        self.admin = self.h.login(ADMIN)

    def tearDown(self):
        self.h.close()

    def config(self) -> dict[str, str]:
        return parse_env_file(self.h.settings.config_path)

    def post(self, path: str, form: dict, **kwargs):
        return self.admin.post(path, form, headers=HTML, follow_redirects=True, **kwargs)

    def audit(self, action: str = "setting_changed") -> list[dict]:
        return [json.loads(r["details"] or "{}") for r in
                self.h.services.db.all("SELECT details FROM audit_log WHERE action = ? ORDER BY id", (action,))]


class AccessTest(Base):
    def test_admin_only_and_csrf(self):
        routes = [("/settings/env/site", SITE), ("/settings/env/slack", SLACK), ("/settings/env/engine", ENGINE),
                  ("/settings/restart", {}), ("/settings/checks", {}), ("/settings/bridge/sign-out", {})]
        for path, form in routes:
            for who in (STUDENT, MENTOR):
                self.assertEqual(self.h.login(who).post(path, form).status_code, 403, (path, who))
            self.assertEqual(self.admin.post(path, form, csrf=False).status_code, 403, path)
        self.assertEqual(self.config(), {})
        self.assertEqual(self.audit(), [])

    def test_unknown_section(self):
        self.assertEqual(self.admin.post("/settings/env/nope", {}, headers=HTML).status_code, 404)

    def test_page_lists_every_section(self):
        page = self.admin.get("/settings").text
        for anchor in ("accounts", "env-engine", "env-site", "env-slack", "ai-keys", "connectors"):
            self.assertIn(f'id="{anchor}"', page)
        for f in server_settings.ENV_FIELDS.values():
            self.assertIn(f'name="{f.name}"', page)
        self.assertIn("Kept in config.env only", page)
        self.assertIn('name="model_check_judge"', page)
        self.assertNotIn("Restart to finish saving", page)


class SiteTest(Base):
    def test_live_and_restart_settings(self):
        form = {**SITE, "app_name": "Shop AI", "timezone": "America/New_York", "max_text_file_kb": "300",
                "backup_hour": "4"}
        resp = self.post("/settings/env/site", form)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Time zone applies after a restart.", resp.text)
        self.assertIn("Restart to finish saving", resp.text)
        config = self.config()
        self.assertEqual((config["APP_NAME"], config["TIMEZONE"], config["MAX_TEXT_FILE_KB"], config["BACKUP_HOUR"]),
                         ("Shop AI", "America/New_York", "300", "4"))
        s = self.h.settings
        self.assertEqual((s.app_name, s.max_text_file_kb, s.backup_hour), ("Shop AI", 300, 4))  # live right away
        self.assertEqual(s.timezone, "America/Chicago")  # waits for the restart
        self.assertEqual(self.h.services.pending_restart, {"timezone": "America/New_York"})
        self.assertIn('value="America/New_York"', resp.text)
        changes = {d["setting"]: (d["from"], d["to"]) for d in self.audit()}
        self.assertEqual(changes["App name"], ("SubZero AI", "Shop AI"))
        self.assertEqual(changes["Time zone"], ("America/Chicago", "America/New_York"))
        # Saving the old value again clears the restart note
        self.post("/settings/env/site", {**form, "timezone": "America/Chicago"})
        self.assertEqual(self.h.services.pending_restart, {})

    def test_nothing_changed(self):
        self.post("/settings/env/site", {**SITE, "public_url": "http://localhost:8787"})  # refused: not https
        self.assertEqual(self.config(), {})
        resp = self.post("/settings/env/site", SITE)
        self.assertIn("Nothing changed in site and server.", resp.text)

    def test_unchanged_value_isnt_rechecked(self):
        self.h.settings.public_url = "http://localhost:8787"  # test mode's address, which a new value can't be
        resp = self.post("/settings/env/site", {**SITE, "public_url": "http://localhost:8787", "app_name": "Shop AI"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.config(), {"APP_NAME": "Shop AI"})

    def test_bad_values_are_refused(self):
        for name, value in (("public_url", "https://ai.yourdomain.org"), ("timezone", "Mars/Olympus"),
                            ("max_text_file_kb", "lots"), ("backup_hour", "25"), ("update_repo", "not a repo"),
                            ("backup_dir", "relative/folder"), ("app_name", "x" * 41), ("app_name", "")):
            resp = self.post("/settings/env/site", {**SITE, name: value})
            self.assertEqual(resp.status_code, 400, name)
            self.assertIn('aria-invalid="true"', resp.text)
            self.assertEqual(self.config(), {}, name)
        self.assertEqual(self.audit(), [])

    def test_environment_variable_locks_a_field(self):
        with mock.patch.dict(os.environ, {"TIMEZONE": "America/Denver"}):
            page = self.admin.get("/settings").text
            self.assertIn("Set by the TIMEZONE environment variable", page)
            self.post("/settings/env/site", {**SITE, "timezone": "America/New_York", "app_name": "Shop AI"})
        self.assertNotIn("TIMEZONE", self.config())
        self.assertEqual(self.config()["APP_NAME"], "Shop AI")


class EngineTest(Base):
    def test_backend_waits_for_restart_and_turns_off_fake(self):
        resp = self.post("/settings/env/engine", {**ENGINE, "ai_backend": "anthropic", "pi_request_timeout": "600"})
        self.assertIn("after a restart", resp.text)
        config = self.config()
        self.assertEqual((config["AI_BACKEND"], config["FAKE_CLAUDE"], config["PI_REQUEST_TIMEOUT"]),
                         ("anthropic", "0", "600"))
        self.assertEqual(self.h.settings.ai_backend, "fake")
        self.assertEqual(set(self.h.services.pending_restart), {"ai_backend", "pi_request_timeout"})

    @mock.patch.dict(os.environ, {}, clear=False)
    def test_bad_engine_values(self):
        os.environ.pop("ANTHROPIC_BASE_URL", None)  # some machines set it, which locks the field
        for name, value in (("ai_backend", "skynet"), ("pi_idle_timeout", "1"), ("pi_node", "C:/no/such/node.exe"),
                            ("anthropic_base_url", "http://evil.example.com")):
            resp = self.post("/settings/env/engine", {**ENGINE, name: value})
            self.assertEqual(resp.status_code, 400, name)
        self.assertEqual(self.config(), {})


class EngineModelsTest(Base):
    overrides = {"ai_providers": ("anthropic", "openai"), "model_hard": "openai/gpt-x"}

    def test_anthropic_engine_refused_while_another_company_is_in_use(self):
        resp = self.post("/settings/env/engine", {**ENGINE, "ai_backend": "anthropic"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("openai/gpt-x", resp.text)
        self.assertEqual(self.config(), {})


class RefusingSlack(FakeSlack):
    async def auth_test(self):
        raise SlackError("auth.test", "invalid_auth")


class SlackTest(Base):
    def setUp(self):
        super().setUp()
        self.h.services.slack_factory = lambda token: FakeSlack()

    def form(self, **extra):
        return {**SLACK, "slack_bot_token": BOT, "slack_app_token": APP, "slack_client_secret": CLIENT_SECRET,
                **extra}

    def test_tokens_are_checked_saved_and_never_shown(self):
        resp = self.post("/settings/env/slack", self.form(allow_slack_guests="1"))
        self.assertEqual(resp.status_code, 200)
        for secret in (BOT, APP, CLIENT_SECRET):
            self.assertNotIn(secret, resp.text)
        config = self.config()
        self.assertEqual((config["SLACK_BOT_TOKEN"], config["SLACK_APP_TOKEN"], config["SLACK_CLIENT_SECRET"]),
                         (BOT, APP, CLIENT_SECRET))
        self.assertIn("ends in " + BOT[-4:], resp.text)
        self.assertTrue(self.h.settings.allow_slack_guests)  # live
        self.assertEqual(self.h.settings.slack_client_secret, CLIENT_SECRET)  # live: sign-in reads it per request
        self.assertNotEqual(self.h.settings.slack_bot_token, BOT)  # waits for the restart
        self.assertIn("slack_bot_token", self.h.services.pending_restart)
        self.assertEqual(self.h.services.pending_restart["slack_bot_token"], "")
        for row in self.h.services.db.all("SELECT details FROM audit_log"):
            for secret in (BOT, APP, CLIENT_SECRET):
                self.assertNotIn(secret, row["details"] or "")
        changes = {d["setting"]: (d["from"], d["to"]) for d in self.audit()}
        self.assertEqual(changes["Bot token"], ("none", "saved"))

    def test_empty_boxes_keep_saved_tokens(self):
        self.post("/settings/env/slack", self.form())
        resp = self.post("/settings/env/slack", {**SLACK, "slack_bot_token": "", "slack_app_token": ""})
        self.assertIn("Nothing changed in Slack connection.", resp.text)
        self.assertEqual(self.config()["SLACK_BOT_TOKEN"], BOT)

    def test_wrong_prefix_and_refused_tokens(self):
        resp = self.post("/settings/env/slack", self.form(slack_bot_token="xoxp-user-token-not-a-bot"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("starts with xoxb-", resp.text)
        self.h.services.slack_factory = lambda token: RefusingSlack()
        resp = self.post("/settings/env/slack", self.form())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("invalid_auth", resp.text)
        self.assertNotIn(BOT, resp.text)
        self.assertEqual(self.config(), {})

    def test_other_workspace_is_refused(self):
        self.h.services.slack_factory = lambda token: FakeSlack(team_id="TOTHER")
        resp = self.post("/settings/env/slack", self.form())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("different Slack workspace", resp.text)
        self.assertEqual(self.config(), {})

    def test_channel_must_be_visible(self):
        class NoChannel(FakeSlack):
            async def channel_members(self, channel):
                raise SlackError("conversations.members", "not_in_channel")
        self.h.settings.slack_bot_token = BOT
        self.h.services.slack_factory = lambda token: NoChannel()
        resp = self.post("/settings/env/slack", {**SLACK, "slack_approvals_channel": "CNEW1"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not_in_channel", resp.text)
        self.h.services.slack_factory = lambda token: FakeSlack()
        self.post("/settings/env/slack", {**SLACK, "slack_approvals_channel": "CNEW1"})
        self.assertEqual(self.h.settings.slack_approvals_channel, "CNEW1")  # live
        resp = self.post("/settings/env/slack", {**SLACK, "slack_approvals_channel": "#general"})
        self.assertEqual(resp.status_code, 400)


class KeysTest(Base):
    overrides = {"ai_providers": ("anthropic", "openai"), "openai_api_key": OPENAI_KEY,
                 "anthropic_api_key": "sk-ant-SECRETSECRETSECRET1234"}

    def test_last_four_shown_and_unused_key_removed(self):
        page = self.admin.get("/settings").text
        self.assertIn("ends in " + OPENAI_KEY[-4:], page)
        self.assertNotIn(OPENAI_KEY, page)
        resp = self.post("/settings/ai-keys", {"remove_openai": "1"})
        self.assertIn("Removed the key for OpenAI.", resp.text)
        self.assertEqual(self.h.settings.openai_api_key, "")
        self.assertEqual(self.config()["OPENAI_API_KEY"], "")
        self.assertEqual(self.audit("ai_key_removed"), [{"providers": ["openai"]}])

    def test_key_in_use_cant_be_removed(self):
        resp = self.post("/settings/ai-keys", {"remove_anthropic": "1"})
        self.assertIn("still needed by the default model", resp.text)
        self.assertEqual(self.config(), {})

    def test_turned_off_provider_key_can_still_be_removed(self):
        self.h.settings.ai_providers = ("anthropic",)
        page = self.admin.get("/settings").text
        self.assertIn("OpenAI is turned off. You can remove its saved key.", page)
        self.post("/settings/ai-keys", {"remove_openai": "1"})
        self.assertEqual(self.h.settings.openai_api_key, "")


class JudgeTest(Base):
    overrides = {"ai_providers": ("anthropic", "openai")}

    def form(self, judge: str) -> dict:
        s = self.h.settings
        return {"ai_providers": list(s.ai_providers), "model_default": s.model_default, "model_hard": s.model_hard,
                "model_titles": s.model_titles, "model_check_judge": judge, "effort_default": "medium",
                "effort_high": "high", "max_tokens_chat": "8000", "max_tokens_explainer": "16000",
                "long_chat_tokens": "150000", "web_search_daily_limit": "50"}

    def test_judge_must_be_claude(self):
        resp = self.post("/settings/claude", self.form("openai/gpt-x"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("has to be a Claude model", resp.text)
        resp = self.post("/settings/claude", self.form("anthropic/claude-opus-5-5"))
        self.assertIn("Saved Claude settings.", resp.text)
        self.assertEqual(self.h.settings.model_check_judge, "anthropic/claude-opus-5-5")
        self.assertEqual(model_check.judge_problem(self.h.settings), "")


class RestartTest(Base):
    def test_without_a_service(self):
        resp = self.post("/settings/restart", {})
        self.assertIn("can&#39;t restart itself", resp.text)

    def test_restart_is_requested_and_logged(self):
        calls = []
        self.h.app.state.request_restart = lambda: calls.append(1)
        self.post("/settings/env/site", {**SITE, "timezone": "America/Denver"})
        resp = self.post("/settings/restart", {})
        self.assertIn("Restarting", resp.text)
        self.assertIn('http-equiv="refresh"', resp.text)
        self.h.call(asyncio.sleep, 1.2)
        self.assertEqual(calls, [1])
        self.assertEqual(self.audit("app_restarted"), [{"settings": ["Time zone"]}])


class AccountsTest(Base):
    def test_accounts_and_checks(self):
        page = self.admin.get("/settings").text
        self.assertIn("Connected accounts", page)
        self.assertIn("Cloudflare Tunnel", page)
        resp = self.post("/settings/checks", {})
        self.assertIn("Checks started.", resp.text)
        self.h.idle()
        self.assertEqual(self.h.services.checks_running, "")
        page = self.admin.get("/settings").text
        self.assertIn("Last run", page)
        self.assertIn("Using the pretend Claude", page)
        self.assertEqual(len(self.audit("checks_done")), 1)

    def test_bridge_sign_out(self):
        resp = self.post("/settings/bridge/sign-out", {})
        self.assertIn("isn&#39;t signed in", resp.text)
        path = bridge_credentials_path(self.h.settings.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        page = self.admin.get("/settings").text
        self.assertIn("Sign out of the Claude subscription", page)
        resp = self.post("/settings/bridge/sign-out", {})
        self.assertIn("Signed out of the Claude subscription", resp.text)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.audit("bridge_signed_out")), 1)


class UnitTest(unittest.TestCase):
    def test_every_setting_has_a_place(self):
        """A new Settings field must go on the Settings page somewhere, or be listed as config.env only."""
        from dataclasses import fields

        from subzero_ai import saved_settings
        from subzero_ai.config import Settings
        from subzero_ai.pi_client import KEY_SETTINGS
        on_page = (set(saved_settings.FIELDS) | set(server_settings.ENV_FIELDS) | set(KEY_SETTINGS.values())
                   | {"connector_github_repo", "connector_github_branch", "connector_github_token",
                      "connector_drive_links"})
        not_settings = {"config_file", "fake_claude", "slack_team_id"}  # fake_claude is part of the AI engine
        file_only = {"data_dir", "host", "port", "dev_login", "session_secret"}
        missing = {f.name for f in fields(Settings)} - on_page - not_settings - file_only
        self.assertEqual(missing, set())

    def test_secret_hint(self):
        self.assertEqual(server_settings.secret_hint("xoxb-123456789abc"), "ends in 9abc")
        self.assertEqual(server_settings.secret_hint("short"), "")

    def test_env_updates_turn_fake_off(self):
        self.assertEqual(server_settings.env_updates({"ai_backend": "pi", "check_for_updates": False}),
                         {"AI_BACKEND": "pi", "CHECK_FOR_UPDATES": "0", "FAKE_CLAUDE": "0"})

    def test_config_env_is_private(self):
        if os.name == "nt":
            self.skipTest("Windows uses the data folder's permissions")
        h = Harness()
        try:
            from subzero_ai.config import update_env_file
            update_env_file(h.settings.config_path, {"X": "1"})
            self.assertEqual(h.settings.config_path.stat().st_mode & 0o777, 0o600)
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
