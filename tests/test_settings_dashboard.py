"""The Settings page: its eight tabs, the config.env sections (AI engine, site, Slack), key removal, connected
accounts, Run checks, Restart, the Code tab, and "Post the cards again": everything an admin can change without editing
files on the server PC."""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from subzero_ai.db import utcnow
from subzero_ai import model_check, saved_settings, server_settings, web_admin
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
        "backup_dir": "", "backup_hour": "2", "check_for_updates": "1",
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
        for anchor in ("accounts", "env-engine", "env-site", "env-slack", "ai-keys", "connectors", "code-engine",
                       "code-auth", "code-advanced", "waiting", "who", "backups", "old-chats"):
            self.assertIn(f'id="{anchor}"', page)
        for f in server_settings.ENV_FIELDS.values():
            self.assertIn(f'name="{f.name}"', page)
        self.assertIn("Kept in config.env only", page)
        self.assertIn('name="model_check_judge"', page)
        self.assertNotIn("Restart to finish saving", page)

    def test_page_is_split_into_sections(self):
        page = self.admin.get("/settings").text
        tabs = re.findall(r'<div class="settings-tab" id="tab-([a-z]+)"', page)
        self.assertEqual(tabs, ["overview", "code", "ai", "review", "people", "slack", "site", "history"])
        self.assertEqual(list(web_admin.TAB_NAMES), tabs)
        names = re.findall(r'<li><a href="#tab-[a-z]+">([^<]+)</a>', page)
        self.assertEqual(names, ["Overview", "Code", "AI and keys", "Slack chat review", "People", "Slack",
                                 "Site and server", "History"])
        for tab in tabs:
            self.assertIn(f'href="#tab-{tab}"', page)
        # Every box sits in exactly one section, in the order the menu promises
        where = {}
        for chunk in page.split('<div class="settings-tab" id="tab-')[1:]:
            tab = chunk.split('"', 1)[0]
            for box in re.findall(r'<section class="panel" id="([a-z-]+)"', chunk):
                self.assertNotIn(box, where, f"{box} appears twice")
                where[box] = tab
        expected = {
            "health": "overview", "waiting": "overview", "accounts": "overview", "checks": "overview",
            "code-engine": "code", "code-auth": "code", "code-token": "code", "code-students": "code",
            "connectors": "code", "code-plan": "code", "code-limits": "code", "code-student-limits": "code",
            "code-classic": "code", "code-advanced": "code",
            "env-engine": "ai", "ai-providers": "ai", "ai-keys": "ai", "claude": "ai", "bridge": "ai",
            "model-check": "ai",
            "review-window": "review", "review-screener": "review", "review-limits": "review",
            "review-always": "review", "prompts": "review",
            "people": "people", "who": "people", "env-slack": "slack", "slack-dms": "slack",
            "env-site": "site", "site-data": "site", "history": "history", "backups": "history",
            "old-chats": "history"}
        self.assertEqual(where, expected)
        # Links in the Connected accounts list all go to a box that is on the page
        for target in set(re.findall(r'<a href="#([a-z-]+)">Change</a>', page)):
            self.assertIn(f'id="{target}"', page)

    def test_old_addresses_still_land_on_the_right_tab(self):
        """Links and bookmarks made before the tabs were reorganised: a name still on the page keeps its box, and a
        name that moved is in the map the page hands to its script."""
        page = self.admin.get("/settings").text
        ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', page))
        old_names = ["tab-overview", "tab-ai", "tab-review", "tab-people", "tab-slack", "tab-site", "tab-history",
                     "health", "accounts", "checks", "restart", "claude", "ai-keys", "bridge", "model-check",
                     "env-engine", "env-site", "env-slack", "limits", "window", "prompts", "prompt-safety",
                     "connectors", "code", "data", "history", "people"]
        tab_of = {}
        for chunk in page.split('<div class="settings-tab" id="tab-')[1:]:
            tab = chunk.split('"', 1)[0]
            for ident in re.findall(r'id="([A-Za-z0-9_-]+)"', chunk):
                tab_of[ident] = tab
        for old in old_names:
            if old == "restart":
                continue  # the banner is outside the tabs, so it shows on every one
            new = old if old in ids else web_admin.LEGACY_ANCHORS.get(old)
            self.assertIsNotNone(new, f"#{old} lost")
            self.assertIn(new, ids, f"#{old} -> #{new} isn't on the page")
        self.assertEqual(tab_of[web_admin.LEGACY_ANCHORS["code"]], "code")
        self.assertEqual(tab_of[web_admin.LEGACY_ANCHORS["limits"]], "review")
        self.assertEqual(tab_of[web_admin.LEGACY_ANCHORS["window"]], "review")
        self.assertEqual(tab_of[web_admin.LEGACY_ANCHORS["data"]], "site")
        self.assertEqual(tab_of["connectors"], "code")
        self.assertEqual(tab_of["accounts"], "overview")
        # The map reaches the script as data, and every target is a box on the page
        data = html.unescape(re.search(r"data-anchors='([^']*)'", page).group(1))
        self.assertEqual(json.loads(data), web_admin.LEGACY_ANCHORS)

    def test_page_helps_phones_and_warns_about_unsaved_edits(self):
        self.post("/settings/env/site", {**SITE, "app_name": "Shop AI"})  # leaves a row in the change history
        page = self.admin.get("/settings").text
        self.assertIn('class="audit stack"', page)
        self.assertIn('data-label="When"', page)
        script = (Path(__file__).resolve().parent.parent / "subzero_ai" / "static" / "app.js").read_text(
            encoding="utf-8")
        self.assertIn("beforeunload", script)
        self.assertIn("Leave without saving?", script)


class SiteTest(Base):
    def test_live_and_restart_settings(self):
        form = {**SITE, "app_name": "Shop AI", "timezone": "America/New_York", "backup_hour": "4"}
        resp = self.post("/settings/env/site", form)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Time zone applies after a restart.", resp.text)
        self.assertIn("Restart to finish saving", resp.text)
        config = self.config()
        self.assertEqual((config["APP_NAME"], config["TIMEZONE"], config["BACKUP_HOUR"]),
                         ("Shop AI", "America/New_York", "4"))
        s = self.h.settings
        self.assertEqual((s.app_name, s.backup_hour), ("Shop AI", 4))  # live right away
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
        self.assertIn("Nothing changed in site and server on the Site and server tab.", resp.text)

    def test_unchanged_value_isnt_rechecked(self):
        self.h.settings.public_url = "http://localhost:8787"  # test mode's address, which a new value can't be
        resp = self.post("/settings/env/site", {**SITE, "public_url": "http://localhost:8787", "app_name": "Shop AI"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.config(), {"APP_NAME": "Shop AI"})

    def test_bad_values_are_refused(self):
        for name, value in (("public_url", "https://ai.yourdomain.org"), ("timezone", "Mars/Olympus"),
                            ("backup_hour", "25"), ("update_repo", "not a repo"),
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
        self.assertIn("Nothing changed in Slack connection on the Slack tab.", resp.text)
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
                "max_tokens_chat": "8000"}

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

    def test_bridge_sign_out_when_the_file_is_in_use(self):
        path = bridge_credentials_path(self.h.settings.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        with mock.patch("pathlib.Path.unlink", side_effect=PermissionError("in use")):
            resp = self.post("/settings/bridge/sign-out", {})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Couldn&#39;t sign out right now", resp.text)
        self.assertTrue(path.exists())


class PanelSaveTest(Base):
    """A box saves only its own fields, and says where it is."""

    def test_a_box_leaves_the_rest_of_its_section_alone(self):
        before = self.h.settings.code_student_access
        resp = self.post("/settings/code", {"_panel": "code-auth", "code_auth": "subscription"})
        self.assertIn("Saved Code sign-in on the Code tab.", resp.text)
        self.assertEqual(self.h.settings.code_auth, "subscription")
        self.assertEqual(self.h.settings.code_student_access, before)  # a missing checkbox isn't "off" here
        self.assertEqual([d["setting"] for d in self.audit()], ["Code sign-in"])
        self.assertIn("Nothing changed in Code sign-in on the Code tab.",
                      self.post("/settings/code", {"_panel": "code-auth", "code_auth": "subscription"}).text)

    def test_errors_in_a_box_are_shown_in_that_box(self):
        resp = self.post("/settings/code", {"_panel": "code-limits", "code_cap_student_usd": "-3",
                                            "code_cap_mentor_usd": "15", "code_max_running": "2",
                                            "code_agent_max_turns": "200", "code_keep_days": "14"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('id="e-code_cap_student_usd"', resp.text)
        self.assertEqual(self.h.settings.code_cap_student_usd, 5.0)

    def test_a_box_for_another_section_is_ignored(self):
        resp = self.post("/settings/limits", {"_panel": "code-auth", "limit_pending_per_student": "2",
                                              "limit_approved_per_day": "25", "reminder_minutes": "120"})
        self.assertIn("Saved limits and review.", resp.text)  # the whole section, as before
        self.assertEqual(self.h.settings.limit_pending_per_student, 2)

    def test_the_slack_tab_holds_the_dm_switch(self):
        resp = self.post("/settings/limits", {"_panel": "slack-dms"})  # unticked
        self.assertIn("Saved Messages to the bot in Slack on the Slack tab.", resp.text)
        self.assertFalse(self.h.settings.slack_dms_enabled)
        self.assertEqual(self.h.settings.limit_pending_per_student, 3)
        page = self.admin.get("/settings").text
        slack_tab = page.split('id="tab-slack"')[1].split('id="tab-site"')[0]
        self.assertIn('name="slack_dms_enabled"', slack_tab)


class CodeTabTest(Base):
    WARNING = ("Team subscription is for testing only. Anthropic's terms don't allow sharing a subscription through "
               "an app, and they can limit the account without notice. Use an API key for regular use.")
    TOKEN = "oat-SECRETSECRETSECRET-1234"

    def code_tab(self) -> str:
        page = self.admin.get("/settings").text
        return page.split('id="tab-code"')[1].split('id="tab-ai"')[0]

    def test_the_code_tab_has_its_settings_in_order(self):
        tab = self.code_tab()
        order = ["code_engine", "code_auth", "code_subscription_token", "code_student_access", "code_sandbox",
                 "sandbox_base_image", "connector_github_repo", "connector_github_branch", "connector_github_token",
                 "code_plan_first_students", "code_web_tools_students", "code_web_tools_mentors", "code_sub_agents",
                 "code_cap_student_usd", "code_cap_mentor_usd", "code_max_running", "code_keep_days",
                 "code_student_max_open", "code_student_daily_sessions", "code_student_free_rein",
                 "code_mentor_free_rein", "code_sandbox_commands", "code_commands", "sandbox_switch",
                 "sandbox_proxy_port", "sandbox_gateway_port", "sandbox_memory_mb", "sandbox_cpus", "sandbox_disk_gb"]
        spots = [tab.index(f'name="{name}"') for name in order]
        self.assertEqual(spots, sorted(spots))
        self.assertIn("Classic engine", tab)
        self.assertIn("fallback", tab)
        self.assertIn(">Check</button>", tab)
        # The classic-only settings say so, and the advanced ones are folded away
        self.assertLess(tab.index("Classic engine only"), tab.index('name="code_commands"'))
        advanced = tab.split('id="code-advanced"')[1]
        self.assertIn('<details class="advanced"', advanced)
        for name in ("sandbox_switch", "sandbox_allowed_hosts", "sandbox_proxy_port", "sandbox_gateway_port",
                     "sandbox_memory_mb", "sandbox_cpus", "sandbox_disk_gb"):
            self.assertIn(f'name="{name}"', advanced)

    def test_the_subscription_warning_is_always_there(self):
        for auth in ("api_key", "subscription"):
            self.h.settings.code_auth = auth
            tab = html.unescape(self.code_tab())
            self.assertIn(self.WARNING, tab, auth)
            self.assertIn('class="warning-line"', tab)
            self.assertNotIn("dismiss", tab.lower())
            # directly under the choice: after the sign-in field, before the token box
            self.assertLess(tab.index('name="code_auth"'), tab.index(self.WARNING))
            self.assertLess(tab.index(self.WARNING), tab.index('name="code_subscription_token"'))

    def test_overview_warns_only_while_the_subscription_is_in_use(self):
        overview = lambda: html.unescape(self.admin.get("/settings").text.split('id="tab-code"')[0])  # noqa: E731
        self.assertNotIn("for testing only", overview())
        self.h.settings.code_auth = "subscription"
        text = overview()
        self.assertIn("Code is signed in with the team subscription, which is for testing only", text)
        self.assertNotIn(self.WARNING, text)  # the short one

    def test_the_token_is_saved_and_never_shown(self):
        resp = self.post("/settings/ai-keys", {"back": "code", "code_subscription_token": self.TOKEN})
        self.assertIn("Saved the team subscription token", resp.text)
        self.assertIn("Code sign-in box, Code tab", resp.text)
        self.assertNotIn(self.TOKEN, resp.text)
        self.assertEqual(self.config()["CODE_SUBSCRIPTION_TOKEN"], self.TOKEN)
        tab = self.code_tab()
        self.assertIn(">Saved</span>", tab)
        self.assertNotIn(self.TOKEN, tab)
        self.assertNotIn(f'value="{self.TOKEN}"', self.admin.get("/settings").text)
        for row in self.h.services.db.all("SELECT details FROM audit_log"):
            self.assertNotIn(self.TOKEN, row["details"] or "")
        self.assertIn('placeholder="Leave empty to keep the saved token"', tab)
        self.post("/settings/ai-keys", {"back": "code", "remove_code_subscription_token": "1"})
        self.assertIn(">Not set</span>", self.code_tab())

    def test_the_check_button_tries_the_repository_without_saving(self):
        calls = []
        self.h.services.github.check = lambda repo, branch, token: calls.append((repo, branch, token))
        resp = self.post("/settings/connectors/check", {"connector_github_repo": "https://github.com/team/robot.git",
                                                         "connector_github_branch": "main"})
        self.assertIn("The check worked", resp.text)
        self.assertEqual(calls, [("team/robot", "main", "")])
        self.assertEqual(self.h.settings.connector_github_repo, "")
        self.assertEqual(self.config(), {})
        self.assertIn("Check failed. Type a repository first.", self.post("/settings/connectors/check", {}).text)
        for who in (STUDENT, MENTOR):
            self.assertEqual(self.h.login(who).post("/settings/connectors/check", {}).status_code, 403)
        self.assertEqual(self.admin.post("/settings/connectors/check", {}, csrf=False).status_code, 403)

    def test_keep_days_is_a_setting_and_drives_the_nightly_clean_up(self):
        field = saved_settings.FIELDS["code_keep_days"]
        self.assertEqual((field.kind, field.minimum, field.maximum), ("int", 1, 365))
        self.assertEqual(self.h.settings.code_keep_days, 14)
        self.assertIn("Delete a finished session's files after this many days", html.unescape(self.code_tab()))
        for bad in ("0", "366", "x"):
            resp = self.post("/settings/code", {"_panel": "code-limits", "code_cap_student_usd": "5",
                                                "code_cap_mentor_usd": "15", "code_max_running": "2",
                                                "code_agent_max_turns": "200", "code_keep_days": bad})
            self.assertEqual(resp.status_code, 400, bad)
        db = self.h.services.db
        admin_id = self.h.services.store.user_by_slack_id(ADMIN)["id"]
        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
        sid = db.execute(
            "INSERT INTO code_sessions (title, task, status, model, repo, created_by, created_at, updated_at) "
            "VALUES ('t', 'x', 'done', 'anthropic/claude-sonnet-5-5', 'a/b', ?, ?, ?)",
            (admin_id, old, old)).lastrowid
        folder = self.h.settings.data_dir / "code" / str(sid)
        folder.mkdir(parents=True)
        (folder / "file.txt").write_text("copy")
        self.h.call(self.h.services.nightly.run)
        self.assertTrue(folder.exists(), "14 days: five days old is kept")
        self.post("/settings/code", {"_panel": "code-limits", "code_cap_student_usd": "5", "code_cap_mentor_usd": "15",
                                     "code_max_running": "2", "code_agent_max_turns": "200", "code_keep_days": "3"})
        self.assertEqual(self.h.settings.code_keep_days, 3)
        self.h.call(self.h.services.nightly.run)
        self.assertFalse(folder.exists(), "3 days: five days old is deleted")
        self.assertIsNotNone(db.one("SELECT 1 FROM code_sessions WHERE id = ?", (sid,)), "the session itself stays")


class OverviewTest(Base):
    def setUp(self):
        super().setUp()
        self.h.slack_people()

    def waiting(self, n: int = 2) -> list[int]:
        ids = []
        for i in range(n):
            ids.append(self.h.slack_dm(f"Question {i}?", user=f"UKID000{i + 1}").message_id)
        return ids

    def test_waiting_row_and_one_restart_banner(self):
        page = self.admin.get("/settings").text
        self.assertIn("Slack messages waiting for a mentor: 0", page)
        self.assertNotIn("Post the cards again", page)
        self.waiting(2)
        page = self.admin.get("/settings").text
        self.assertIn("Slack messages waiting for a mentor: 2", page)
        self.assertIn(", oldest waiting just now", page)
        self.assertIn("Post the cards again", page)
        self.assertEqual(page.count("Restart to finish saving"), 0)
        self.post("/settings/env/site", {**SITE, "timezone": "America/Denver"})
        page = self.admin.get("/settings").text
        self.assertEqual(page.count("Restart to finish saving"), 1)
        self.assertEqual(page.count('action="/settings/restart"'), 1)
        # The banner sits outside every tab, so it shows whichever tab is open
        banner_at = page.index("Restart to finish saving")
        self.assertLess(banner_at, page.index('id="tab-overview"'))

    def test_post_the_cards_again_reposts_only_pending_messages(self):
        first, second = self.waiting(2)
        self.h.slack_tap("approve", second)  # a mentor handles one of them
        posts_before = len(self.h.slack.posts)
        old_card = self.h.services.store.card(first)
        resp = self.post("/settings/repost-cards", {})
        self.assertIn("Posted 1 card again.", resp.text)
        new_posts = self.h.slack.posts[posts_before:]
        self.assertEqual(len(new_posts), 1)
        self.assertIn(f"review_{first}", json.dumps(new_posts[0]["blocks"]))
        self.assertEqual(new_posts[0]["channel"], "CAPPROVALS")
        new_card = self.h.services.store.card(first)
        self.assertNotEqual(new_card["ts"], old_card["ts"])  # the message now follows the new card
        self.assertTrue(any(u["ts"] == old_card["ts"] for u in self.h.slack.updates))  # the old one says so
        self.assertEqual(self.audit("cards_reposted"),
                         [{"posted": 1, "failed": 0, "skipped": 0, "remaining": 0}])
        # Who may approve hasn't changed: a student can't approve, and the card's buttons are the usual ones
        self.h.slack_tap("approve", first, user="UKID0002")
        self.assertEqual(self.h.message(first)["status"], "pending")
        self.h.slack_tap("approve", first)
        self.assertEqual(self.h.message(first)["status"], "approved")

    def test_post_the_cards_again_needs_an_admin_and_csrf(self):
        self.waiting(1)
        for who in (STUDENT, MENTOR):
            self.assertEqual(self.h.login(who).post("/settings/repost-cards", {}).status_code, 403)
        self.assertEqual(self.admin.post("/settings/repost-cards", {}, csrf=False).status_code, 403)
        self.assertEqual(self.audit("cards_reposted"), [])

    def test_reposting_with_nothing_waiting_posts_nothing(self):
        posts = len(self.h.slack.posts)
        resp = self.post("/settings/repost-cards", {})
        self.assertIn("No messages are waiting", resp.text)
        self.assertEqual(len(self.h.slack.posts), posts)

    def test_a_failed_post_is_reported_and_the_old_card_stays(self):
        (first,) = self.waiting(1)
        old = self.h.services.store.card(first)
        self.h.slack.fail_posts = True
        resp = self.post("/settings/repost-cards", {})
        self.assertIn("1 couldn't be posted", html.unescape(resp.text))
        self.assertEqual(self.h.services.store.card(first)["ts"], old["ts"])
        self.assertEqual(self.audit("cards_reposted"),
                         [{"posted": 0, "failed": 1, "skipped": 0, "remaining": 0}])

    def repost(self):
        return self.h.call(self.h.services.approvals.repost_cards)

    def test_a_second_press_posts_nothing(self):
        self.waiting(2)
        posts = len(self.h.slack.posts)
        self.assertEqual(self.repost()["posted"], 2)
        self.assertEqual(len(self.h.slack.posts), posts + 2)
        again = self.repost()  # a double-click, or a press just after
        self.assertTrue(again["busy"])
        self.assertEqual(len(self.h.slack.posts), posts + 2)
        resp = self.post("/settings/repost-cards", {})
        self.assertIn("Wait a few seconds", resp.text)
        self.assertEqual(len(self.audit("cards_reposted")), 0, "the busy press isn't a repost")

    def test_the_press_is_not_run_twice_at_once(self):
        self.waiting(1)
        approvals = self.h.services.approvals
        approvals._reposting = True
        self.assertTrue(self.repost()["busy"])
        approvals._reposting = False

    def test_a_message_decided_or_deleted_mid_run_is_skipped(self):
        first, second, third = self.waiting(3)
        approvals = self.h.services.approvals
        real = approvals._post_card
        calls = []

        async def post(message_id):
            calls.append(message_id)
            if message_id == first:  # while the first card goes out, a mentor decides the second and the third is deleted
                self.h.services.db.execute("UPDATE messages SET status = 'approved' WHERE id = ?", (second,))
                self.h.services.db.execute("DELETE FROM conversations WHERE id = (SELECT conversation_id FROM "
                                           "messages WHERE id = ?)", (third,))
            await real(message_id)
        approvals._post_card = post
        result = self.repost()
        self.assertEqual(calls, [first])
        self.assertEqual((result["posted"], result["skipped"], result["failed"]), (1, 2, 0))

    def test_one_failure_does_not_end_the_run(self):
        first, second = self.waiting(2)
        approvals = self.h.services.approvals
        real = approvals._post_card

        async def post(message_id):
            if message_id == first:
                raise RuntimeError("boom")
            await real(message_id)
        approvals._post_card = post
        result = self.repost()
        self.assertEqual((result["posted"], result["failed"]), (1, 1))

    def test_a_long_list_is_posted_in_batches(self):
        approvals = self.h.services.approvals
        approvals.REPOST_MAX, approvals.REPOST_COOLDOWN = 2, 0
        self.waiting(2)
        user = self.h.services.store.user_by_slack_id("UKID0001")
        for i in range(2):
            cid = self.h.services.db.execute(
                "INSERT INTO conversations (owner_id, title, visibility, model, created_at, updated_at) "
                "VALUES (?, 't', 'team', 'm', 'now', 'now')", (user["id"],)).lastrowid
            self.h.services.db.execute(
                "INSERT INTO messages (conversation_id, author_id, role, content, status, created_at, updated_at) "
                "VALUES (?, ?, 'user', 'x', 'pending', ?, ?)", (cid, user["id"], utcnow(), utcnow()))
        first = self.repost()
        self.assertEqual((first["posted"], first["remaining"]), (2, 2))
        second = self.repost()  # carries on with the rest instead of posting the first two again
        self.assertEqual((second["posted"], second["remaining"]), (2, 0))

    def test_update_notice_says_what_to_do_on_the_server_pc(self):
        self.h.services.db.set_kv("latest_release", json.dumps({"version": "99.0.0", "url": "https://example.com/r",
                                                                  "checked_at": "2026-11-01T00:00:00+00:00"}))
        page = self.admin.get("/settings").text
        self.assertIn("Update available", page)
        self.assertIn("On the server PC, open the SubZero AI folder and run update.cmd.", page)

    def test_mentor_list_warning_shows_when_the_directory_has_one(self):
        self.assertNotIn('id="mentor-warning"', self.admin.get("/settings").text)  # no stale_warning() yet
        self.h.services.directory.stale_warning = lambda: "The mentor list is from last week."
        page = self.admin.get("/settings").text
        self.assertIn('id="mentor-warning"', page)
        self.assertIn("The mentor list is from last week.", page)
        self.h.services.directory.stale_warning = lambda: ""
        self.assertNotIn('id="mentor-warning"', self.admin.get("/settings").text)

    def test_the_gateway_row_is_still_in_the_health_list(self):
        row = {"label": "Code gateway", "state": "Not running", "kind": "bad", "alert": True, "detail": "It stopped."}
        with mock.patch.object(web_admin, "code_gateway_row", return_value=row):
            overview = self.admin.get("/settings").text.split('id="tab-code"')[0]
        self.assertIn("Code gateway", overview)
        self.assertIn("It stopped.", overview)


class PeopleTest(Base):
    def test_who_is_a_mentor_or_admin_right_now(self):
        self.h.slack_people()
        self.h.services.store.upsert_user(slack_user_id="UMENTOR1", name="Coach Lee", role="mentor", team_id="TDEV")
        self.h.settings.admin_slack_ids = frozenset({"U0COACH1", "UADMIN9"})
        self.h.services.store.upsert_user(slack_user_id="UADMIN9", name="Ada Admin", role="admin", team_id="TDEV")
        page = self.admin.get("/settings").text
        people = page.split('id="who"')[1].split("</section>")[0]
        self.assertIn("Ada Admin", people)
        self.assertIn("Coach Lee", people)
        self.assertIn("UMENTOR2 (hasn&#39;t signed in yet)", people)  # in the channel, never signed in
        self.assertLess(people.index("Ada Admin"), people.index("Coach Lee"))  # admins first
        self.assertRegex(people, r"List as of ")
        self.assertIn('data-label="Role"', people)
        self.assertNotIn("<input", people)  # read only
        self.assertNotIn("<form", people)

    def test_when_slack_cant_be_asked_only_admins_are_listed(self):
        async def broken(channel):
            raise RuntimeError("down")
        self.h.slack.channel_members = broken
        self.h.services.directory.invalidate()
        people = self.admin.get("/settings").text.split('id="who"')[1].split("</section>")[0]
        self.assertIn("read the review channel from Slack just now", html.unescape(people))
        self.assertNotIn("List as of", people)


class UnitTest(unittest.TestCase):
    def test_every_setting_has_a_place(self):
        """A new Settings field must go on the Settings page somewhere, or be listed as config.env only."""
        from dataclasses import fields

        from subzero_ai.config import Settings
        from subzero_ai.pi_client import KEY_SETTINGS
        on_page = (set(saved_settings.FIELDS) | set(server_settings.ENV_FIELDS) | set(KEY_SETTINGS.values())
                   | {"connector_github_repo", "connector_github_branch", "connector_github_token"}
                   | {"code_subscription_token"})  # entered in the Code sign-in box on the Code tab
        not_settings = {"config_file", "fake_claude", "slack_team_id"}  # fake_claude is part of the AI engine
        file_only = {"data_dir", "host", "port", "dev_login", "session_secret"}
        missing = {f.name for f in fields(Settings)} - on_page - not_settings - file_only
        self.assertEqual(missing, set())

    def test_every_saved_setting_is_in_exactly_one_box_on_a_tab(self):
        placed = [name for panel in web_admin.PANELS for name in panel.fields]
        self.assertEqual(sorted(placed), sorted(saved_settings.FIELDS))
        self.assertEqual(len(placed), len(set(placed)))
        for panel in web_admin.PANELS:
            self.assertIn(panel.tab, web_admin.TAB_NAMES)
            for name in panel.fields:  # a box saves through the section its fields belong to
                self.assertIn(name, {f.name for f in saved_settings.SECTIONS[panel.section][1]}, name)

    def test_every_setting_is_on_the_rendered_page(self):
        h = Harness(ai_backend="fake")
        try:
            page = h.login(ADMIN).get("/settings").text
            for name in [*saved_settings.FIELDS, *server_settings.ENV_FIELDS]:
                self.assertIn(f'name="{name}"', page, name)
        finally:
            h.close()

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
