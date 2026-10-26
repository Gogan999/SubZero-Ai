"""Milestone 4: the Settings page, setup page, recovery sign-in, backups and restore, retention, health
checks, update checks, and the Windows release build's pure parts."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from subzero_ai import __main__ as cli
from subzero_ai import backup, maintenance, saved_settings, setup_flow
from subzero_ai.claude import ClaudeError, FakeClaude
from subzero_ai.config import load_settings, parse_env_file, update_env_file
from subzero_ai.db import Database
from subzero_ai.generation import load_system_prompt
from subzero_ai.health import Health
from subzero_ai.slack_api import FakeSlack

from .helpers import ADMIN, MENTOR, STUDENT, Harness

ROOT = Path(__file__).resolve().parent.parent
HTML = {"Accept": "text/html"}
TUNNEL = {"cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "203.0.113.9"}


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="subzero-m4-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------- config.env
class ConfigFileTest(TempDirTest):
    def test_update_keeps_notes_and_order_and_appends_new_keys(self):
        path = self.tmp / "config.env"
        path.write_text("# Address\nPUBLIC_URL=https://old.example\n\n# Slack\nSLACK_BOT_TOKEN=\n", encoding="utf-8")
        update_env_file(path, {"SLACK_BOT_TOKEN": "xoxb-1", "ANTHROPIC_API_KEY": "sk-ant-1"})
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Address\nPUBLIC_URL=https://old.example\n\n# Slack\nSLACK_BOT_TOKEN=xoxb-1\n"))
        self.assertEqual(parse_env_file(path)["ANTHROPIC_API_KEY"], "sk-ant-1")
        self.assertFalse((self.tmp / "config.env.tmp").exists())

    def test_update_refuses_line_breaks(self):
        with self.assertRaises(ValueError):
            update_env_file(self.tmp / "config.env", {"PUBLIC_URL": "https://a\nDEV_LOGIN=1"})

    def test_subteams_from_config_env(self):
        (self.tmp / "config.env").write_text("SUBTEAMS=Programming, Build , Strategy\n", encoding="utf-8")
        settings = load_settings(search_cwd=False, data_dir=self.tmp)
        self.assertEqual(settings.subteams, ("Programming", "Build", "Strategy"))


# --------------------------------------------------------------------------- Settings page
class SettingsValuesTest(TempDirTest):
    def test_validation(self):
        values, errors = saved_settings.validate("claude", {
            "model_default": "claude-fable-5-1", "model_hard": "gpt-4", "model_titles": "claude-haiku-5-5",
            "effort_default": "extreme", "effort_high": "high", "max_tokens_chat": "8,000",
            "max_tokens_explainer": "10", "long_chat_tokens": "150000"})
        self.assertIn("Fable", errors["model_default"])
        self.assertIn("model ID", errors["model_hard"])
        self.assertIn("effort_default", errors)
        self.assertIn("max_tokens_explainer", errors)
        self.assertEqual(values["max_tokens_chat"], 8000)

    def test_admins_cant_remove_themselves(self):
        _, errors = saved_settings.validate("people", {"subteams": "Programming", "admin_slack_ids": "U0OTHER1"},
                                            actor_slack_id="U0ME1234")
        self.assertIn("yourself", errors["admin_slack_ids"])
        _, errors = saved_settings.validate("people", {"subteams": "", "admin_slack_ids": "nope"})
        self.assertIn("subteams", errors)
        self.assertIn("Slack member ID", errors["admin_slack_ids"])

    def test_saved_values_survive_a_restart_and_win_over_config_env(self):
        (self.tmp / "config.env").write_text("LIMIT_APPROVED_PER_DAY=10\n", encoding="utf-8")
        settings = load_settings(search_cwd=False, data_dir=self.tmp)
        db = Database(settings.db_path)
        saved_settings.save(db, settings, {"limit_approved_per_day": 40, "subteams": ("A", "B")}, None)
        fresh = load_settings(search_cwd=False, data_dir=self.tmp)
        self.assertEqual(fresh.limit_approved_per_day, 10)
        saved_settings.load_into(fresh, Database(fresh.db_path))
        self.assertEqual(fresh.limit_approved_per_day, 40)
        self.assertEqual(fresh.subteams, ("A", "B"))


class SettingsPageTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.coach = self.h.login(ADMIN)

    def tearDown(self):
        self.h.close()

    def test_only_admins(self):
        self.assertEqual(self.h.login(MENTOR).get("/settings").status_code, 403)
        self.assertEqual(self.h.login(STUDENT).get("/settings").status_code, 403)
        self.assertEqual(self.h.login(MENTOR).post("/settings/limits", {"limit_pending_per_student": "9"}).status_code,
                         403)
        page = self.coach.get("/settings").text
        for text in ("Health and backups", "Limits and review", "System prompt", "Change history", "Back up now"):
            self.assertIn(text, page)

    def test_limits_apply_without_a_restart(self):
        resp = self.coach.post("/settings/limits", {
            "limit_pending_per_student": "1", "limit_approved_per_day": "25", "reminder_minutes": "120",
            "show_pending_to_team": "1"}, headers=HTML)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Saved limits and review.", resp.text)
        ava = self.h.login(STUDENT)
        first = ava.new_chat("How do I tune a PID loop?")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(ava.new_chat("And feedforward?").status_code, 429)
        self.assertTrue(self.h.services.store.show_pending_to_team, "visibility settings reach the store")
        entry = self.h.services.db.one("SELECT details FROM audit_log WHERE action = 'setting_changed' LIMIT 1")
        self.assertIn("Waiting messages per student", entry["details"])

    def test_bad_values_are_shown_and_not_saved(self):
        resp = self.coach.post("/settings/claude", {"model_default": "claude-mythos-1", "model_hard": "claude-opus-5-5",
                                                    "model_titles": "claude-haiku-5-5", "effort_default": "medium",
                                                    "effort_high": "high", "max_tokens_chat": "8000",
                                                    "max_tokens_explainer": "16000", "long_chat_tokens": "150000"},
                               headers=HTML)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("kept off", resp.text)
        self.assertEqual(self.h.settings.model_default, "claude-sonnet-5-5")
        # The error is tied to its field for screen readers
        field = re.search(r'<input id="f-model_default"[^>]*>', resp.text).group(0)
        self.assertIn('aria-describedby="h-model_default e-model_default"', field)
        self.assertIn('aria-invalid="true"', field)
        self.assertIn('id="e-model_default"', resp.text)

    def test_subteams(self):
        self.coach.post("/settings/people", {"subteams": "Software\nMechanical", "admin_slack_ids": ""}, headers=HTML)
        self.assertNotEqual(self.h.settings.subteams, ("Software", "Mechanical"), "admins can't be emptied")
        resp = self.coach.post("/settings/people", {"subteams": "Software\nMechanical", "admin_slack_ids": "U0COACH1"},
                               headers=HTML)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.h.settings.subteams, ("Software", "Mechanical"))
        self.assertIn("<option >Software</option>", self.h.login(STUDENT).get("/recents").text.replace("  ", ""))

    def test_prompt_layers(self):
        resp = self.coach.post("/settings/prompts/team", {"text": "## Team context\n- Robot code is Java."},
                               headers=HTML)
        self.assertIn("It applies to the next reply", resp.text)
        self.assertIn("Robot code is Java.", load_system_prompt(self.h.settings))
        self.assertIn("Changed from built-in", resp.text)
        entry = self.h.services.db.one("SELECT details FROM audit_log WHERE action = 'prompt_changed'")
        self.assertIn("Robot code is Java.", json.loads(entry["details"])["after"])
        self.coach.post("/settings/prompts/team", {"reset": "1"}, headers=HTML)
        self.assertFalse((self.h.settings.data_dir / "prompts" / "team.md").exists())
        self.assertNotIn("Robot code is Java.", load_system_prompt(self.h.settings))
        self.assertEqual(self.coach.post("/settings/prompts/other", {"text": "x"}, headers=HTML).status_code, 404)

    def test_back_up_now(self):
        resp = self.coach.post("/settings/backup", {}, headers=HTML)
        self.assertIn("Backed up to subzero-", resp.text)
        self.assertEqual(len(backup.list_backups(self.h.settings)), 1)

    def test_csrf(self):
        resp = self.coach.post("/settings/limits", {"limit_pending_per_student": "9"}, csrf=False)
        self.assertEqual(resp.status_code, 403)

    def test_healthz(self):
        body = self.h.client.get("/healthz").json()
        self.assertEqual(set(body), {"ok", "version", "setup_complete", "slack", "socket_mode", "claude",
                                     "last_backup_at", "last_backup_ok"})


# --------------------------------------------------------------------------- setup page
class FailingClaude(FakeClaude):
    async def complete(self, **kwargs) -> str:
        raise ClaudeError("invalid x-api-key")


class SetupPageTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(slack_approvals_channel="", dev_login=False)
        self.fake = FakeSlack(members={"UBOT", "U0KIM111"},
                              channels=[{"id": "G0APPROVE", "name": "claude-approvals"}],
                              users={"U0COACH1": {"id": "U0COACH1", "profile": {"real_name": "Pat Coach"}},
                                     "U0GUEST1": {"id": "U0GUEST1", "is_restricted": True}})
        self.h.services.slack_factory = lambda token: self.fake
        self.h.services.claude_factory = lambda key: FakeClaude(delay=0)
        page = self.h.client.get("/setup")
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    def tearDown(self):
        self.h.close()

    def step(self, name, **data):
        return self.h.client.post(f"/setup/{name}", data={"csrf": self.csrf, **data})

    def test_whole_setup(self):
        self.assertEqual(self.step("address", public_url="https://ai.yourdomain.org").status_code, 400)
        resp = self.step("address", public_url="ai.team-example.net")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("api.slack.com/apps?new_app=1&amp;manifest_json=", resp.text)
        self.assertIn("https://ai.team-example.net/auth/slack/callback", resp.text)

        self.assertIn("starts with xoxb-", self.step("slack", slack_bot_token="nope").text)
        resp = self.step("slack", slack_bot_token="xoxb-1", slack_app_token="xapp-1", slack_client_id="123.456",
                         slack_client_secret="s3cret")
        self.assertIn("Connected to the Test workspace", resp.text)
        self.assertIn("#claude-approvals", resp.text)
        self.assertNotIn("s3cret", resp.text, "secrets are never shown again")

        self.assertEqual(self.step("channel", channel="bad").status_code, 400)
        self.assertIn("1 mentor in it", self.step("channel", channel="G0APPROVE").text)

        self.assertIn("starts with sk-ant-", self.step("claude", anthropic_api_key="abc").text)
        self.h.services.claude_factory = lambda key: FailingClaude()
        self.assertIn("invalid x-api-key", self.step("claude", anthropic_api_key="sk-ant-bad").text)
        self.h.services.claude_factory = lambda key: FakeClaude(delay=0)
        self.assertIn("The key works", self.step("claude", anthropic_api_key="sk-ant-good").text)

        self.assertIn("guest", self.step("admins", admins="U0GUEST1").text)
        self.assertIn("Pat Coach", self.step("admins", admins="U0COACH1").text)

        resp = self.step("finish")
        self.assertEqual(resp.status_code, 200)
        code = re.search(r'class="recovery-code">([A-Z0-9-]+)<', resp.text).group(1)
        self.assertTrue(setup_flow.check_recovery_code(self.h.services.db, code))
        self.assertTrue(setup_flow.setup_complete(self.h.services.db))
        self.assertEqual(self.h.client.get("/setup").status_code, 404, "setup locks once it's finished")

        saved = parse_env_file(self.h.settings.config_path)
        self.assertEqual(saved["PUBLIC_URL"], "https://ai.team-example.net")
        self.assertEqual(saved["SLACK_BOT_TOKEN"], "xoxb-1")
        self.assertEqual(saved["SLACK_APPROVALS_CHANNEL"], "G0APPROVE")
        self.assertEqual(saved["ANTHROPIC_API_KEY"], "sk-ant-good")
        self.assertEqual(self.h.settings.admin_slack_ids, frozenset({"U0COACH1"}))

    def test_finish_needs_every_step(self):
        self.assertIn("Finish the steps", self.step("finish").text)
        self.assertFalse(setup_flow.setup_complete(self.h.services.db))

    def test_only_on_this_pc(self):
        self.assertEqual(self.h.client.get("/setup", headers=TUNNEL).status_code, 404)
        resp = self.h.client.post("/setup/address", data={"csrf": self.csrf, "public_url": "https://a.team.net"},
                                  headers=TUNNEL)
        self.assertEqual(resp.status_code, 404)

    def test_a_pasted_line_break_is_a_step_error(self):
        self.step("address", public_url="ai.team-example.net")
        resp = self.step("slack", slack_bot_token="xoxb-1\n2", slack_app_token="xapp-1", slack_client_id="1",
                         slack_client_secret="s")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("line break", resp.text)

    def test_needs_the_form_token(self):
        resp = self.h.client.post("/setup/address", data={"public_url": "https://a.team.net"})
        self.assertEqual(resp.status_code, 403)

    def test_login_page_points_to_setup_on_this_pc_only(self):
        self.assertIn('href="/setup"', self.h.client.get("/login").text)
        self.assertNotIn('href="/setup"', self.h.client.get("/login", headers=TUNNEL).text)

    def test_existing_config_env_locks_setup(self):
        tmp = Path(tempfile.mkdtemp(prefix="subzero-m4-"))
        try:
            settings = load_settings(search_cwd=False, data_dir=tmp, public_url="https://ai.team-example.net",
                                     slack_bot_token="xoxb-1", slack_app_token="xapp-1", slack_client_id="1",
                                     slack_client_secret="2", slack_approvals_channel="G1", anthropic_api_key="sk-ant-1",
                                     admin_slack_ids=frozenset({"U0COACH1"}))
            from subzero_ai.web import build_services
            services = build_services(settings, slack=FakeSlack(), claude=FakeClaude(delay=0))
            self.assertTrue(setup_flow.setup_complete(services.db))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_manifest_link(self):
        settings = self.h.settings
        link = setup_flow.create_app_link(setup_flow.slack_manifest(settings, "https://ai.team-example.net/"))
        manifest = json.loads(parse_qs(urlparse(link).query)["manifest_json"][0])
        self.assertEqual(manifest["oauth_config"]["redirect_urls"], ["https://ai.team-example.net/auth/slack/callback"])
        self.assertTrue(manifest["settings"]["socket_mode_enabled"])
        yaml_text = (ROOT / "slack-manifest.yaml").read_text(encoding="utf-8")
        for scope in manifest["oauth_config"]["scopes"]["bot"] + manifest["oauth_config"]["scopes"]["user"]:
            self.assertIn(f"- {scope}", yaml_text, "the setup link and slack-manifest.yaml must ask for the same scopes")
        self.assertNotRegex(json.dumps(manifest), r"5690|SubZero Robotics")


# --------------------------------------------------------------------------- recovery sign-in
class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(dev_login=False)
        self.code = setup_flow.new_recovery_code(self.h.services.db)
        setup_flow.mark_setup_complete(self.h.services.db)
        page = self.h.client.get("/auth/recovery")
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    def tearDown(self):
        self.h.close()

    def test_code_signs_in_an_admin_on_this_pc_only(self):
        bad = self.h.client.post("/auth/recovery", data={"csrf": self.csrf, "code": "AAAAA-BBBBB-CCCCC-DDDDD"})
        self.assertEqual(bad.status_code, 400)
        resp = self.h.client.post("/auth/recovery", data={"csrf": self.csrf, "code": self.code.lower().replace("-", " ")})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Health and backups", resp.text)
        # The same session through the tunnel is treated as signed out
        self.assertEqual(self.h.client.get("/settings", headers=TUNNEL).status_code, 200)
        self.assertIn("Sign in", self.h.client.get("/settings", headers=TUNNEL).text)
        self.assertNotIn("Health and backups", self.h.client.get("/settings", headers=TUNNEL).text)
        self.assertEqual(self.h.client.get("/auth/recovery", headers=TUNNEL).status_code, 404)

    def test_a_new_code_replaces_the_old_one(self):
        newer = setup_flow.new_recovery_code(self.h.services.db)
        self.assertFalse(setup_flow.check_recovery_code(self.h.services.db, self.code))
        self.assertTrue(setup_flow.check_recovery_code(self.h.services.db, newer))


# --------------------------------------------------------------------------- backups and restore
class BackupTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.ava = self.h.login(STUDENT)
        self.settings = self.h.settings

    def tearDown(self):
        self.h.close()

    def chat(self, text):
        resp = self.ava.post("/chats/new", {"text": text, "subteam": "Programming"},
                             files=[("files", ("notes.txt", b"kP = 0.2\n", "text/plain"))])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.h.idle()
        return int(resp.json()["url"].rsplit("/", 1)[1])

    def test_backup_and_restore_round_trip(self):
        first = self.chat("First question")
        (self.settings.data_dir / "prompts").mkdir(exist_ok=True)
        (self.settings.data_dir / "prompts" / "team.md").write_text("Team layer v1", encoding="utf-8")
        made = backup.make_backup(self.settings, now=datetime(2026, 10, 25, 2, 0, 0))
        with zipfile.ZipFile(made) as zf:
            names = zf.namelist()
        self.assertIn("subzero.db", names)
        self.assertIn("prompts/team.md", names)
        self.assertTrue(any(n.startswith("files/") for n in names))
        self.assertFalse(any("config.env" in n for n in names), "secrets stay out of backups")

        second = self.chat("Second question")
        (self.settings.data_dir / "prompts" / "team.md").write_text("Team layer v2", encoding="utf-8")
        result = backup.restore_backup(self.settings, made, now=datetime(2026, 10, 26, 9, 0, 0))
        self.assertIn("before-restore", result["safety_backup"].name)
        store = self.h.services.store
        self.assertIsNotNone(store.conversation(first))
        self.assertIsNone(store.conversation(second))
        self.assertEqual((self.settings.data_dir / "prompts" / "team.md").read_text(encoding="utf-8"), "Team layer v1")
        files = self.h.services.attachments.for_conversation(first)
        row = next(iter(files.values()))[0]
        self.assertEqual(self.h.services.attachments.read(row), b"kP = 0.2\n")

    def test_bad_backups_are_refused(self):
        not_backup = self.settings.backups_path / "subzero-20260101-000000.zip"
        with zipfile.ZipFile(not_backup, "w") as zf:
            zf.writestr("hello.txt", "hi")
        with self.assertRaises(backup.BackupError):
            backup.check_backup(not_backup)
        sneaky = self.settings.backups_path / "subzero-20260102-000000.zip"
        made = backup.make_backup(self.settings)
        shutil.copy(made, sneaky)
        with zipfile.ZipFile(sneaky, "a") as zf:
            zf.writestr("../outside.txt", "nope")
        with self.assertRaises(backup.BackupError):
            backup.check_backup(sneaky)
        # A backup from a newer version of the app
        newer = self.settings.backups_path / "subzero-20260103-000000.zip"
        db_file = Path(tempfile.mkdtemp()) / "subzero.db"
        conn = sqlite3.connect(db_file)
        conn.execute("PRAGMA user_version = 99")
        conn.close()
        with zipfile.ZipFile(newer, "w") as zf:
            zf.write(db_file, "subzero.db")
            zf.writestr("backup.json", "{}")
        with self.assertRaisesRegex(backup.BackupError, "newer version"):
            backup.check_backup(newer)

    def test_prune_keeps_two_weeks_and_always_the_newest(self):
        now = datetime(2026, 10, 25, 3, 0, 0)
        for days in (0, 5, 13, 15, 30):
            backup.make_backup(self.settings, now=now - timedelta(days=days))
        removed = backup.prune_backups(self.settings, 14, now=now)
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(backup.list_backups(self.settings)), 3)
        # Even a very old newest backup is kept
        removed = backup.prune_backups(self.settings, 14, now=now + timedelta(days=100))
        self.assertEqual([b.name for b in backup.list_backups(self.settings)], ["subzero-20261025-030000.zip"])

    def test_find_backup(self):
        with self.assertRaises(backup.BackupError):
            backup.find_backup(self.settings, "latest")
        made = backup.make_backup(self.settings)
        self.assertEqual(backup.find_backup(self.settings, "latest"), made)
        self.assertEqual(backup.find_backup(self.settings, made.name), made)

    def test_command_line(self):
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertEqual(cli.run_tool("backup", "pre-update", self.settings), 0)
            self.assertEqual(cli.run_tool("backups", None, self.settings), 0)
            with mock.patch.object(cli, "app_is_running", return_value=True):
                self.assertEqual(cli.run_tool("restore", "latest", self.settings), 1)
            with mock.patch.object(cli, "app_is_running", return_value=False):
                self.assertEqual(cli.run_tool("restore", "latest", self.settings), 0)
                self.assertEqual(cli.run_tool("restore", "missing.zip", self.settings), 1)
            self.assertEqual(cli.run_tool("recovery-code", None, self.settings), 0)
            setup_flow.mark_setup_complete(self.h.services.db)
            self.assertEqual(cli.run_tool("reset-setup", None, self.settings), 0)
        text = "\n".join(out)
        self.assertIn("pre-update.zip", text)
        self.assertIn("Stop it first", text)
        self.assertIn("Couldn't find the backup", text)
        self.assertFalse(setup_flow.setup_complete(self.h.services.db))
        code = re.search(r"([A-Z2-9]{5}-){3}[A-Z2-9]{5}", text).group(0)
        self.assertTrue(setup_flow.check_recovery_code(self.h.services.db, code))


class NightlyTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.s = self.h.services

    def tearDown(self):
        self.h.close()

    def test_backup_due(self):
        tz = ZoneInfo("America/Chicago")
        now = datetime(2026, 10, 25, 1, 30, tzinfo=tz)
        last_night = datetime(2026, 10, 24, 2, 5, tzinfo=tz).astimezone(timezone.utc).isoformat()
        self.assertTrue(maintenance.backup_due(None, now, 2))
        self.assertFalse(maintenance.backup_due(last_night, now, 2))
        self.assertTrue(maintenance.backup_due(last_night, now.replace(hour=2, minute=1), 2))

    def test_run_records_success_and_failure(self):
        name = self.h.call(self.s.nightly.run)
        self.assertTrue(name.startswith("subzero-"))
        self.assertEqual(self.s.db.get_kv("last_backup_ok"), "1")
        self.assertFalse(self.h.call(self.s.nightly.tick), "already backed up tonight")
        blocker = self.h.tmp / "not-a-folder"
        blocker.write_text("x")
        self.h.settings.backup_dir = str(blocker)
        self.assertIsNone(self.h.call(self.s.nightly.run))
        self.assertEqual(self.s.db.get_kv("last_backup_ok"), "0")
        page = self.h.login(ADMIN).get("/settings").text
        self.assertIn("Failed", page)
        self.assertIn('class="count alert"', page)

    def test_old_chats_are_deleted_with_their_files(self):
        ava = self.h.login(STUDENT)
        old = ava.post("/chats/new", {"text": "Old", "subteam": "Programming"},
                       files=[("files", ("old.txt", b"old", "text/plain"))]).json()
        new = ava.new_chat("New").json()
        old_id, new_id = (int(x["url"].rsplit("/", 1)[1]) for x in (old, new))
        self.h.idle()
        row = next(iter(self.s.attachments.for_conversation(old_id).values()))[0]
        path = self.h.settings.data_dir / "files" / row["path"]
        self.assertTrue(path.exists())
        long_ago = (datetime.now(timezone.utc) - timedelta(days=800)).isoformat()
        self.s.db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (long_ago, old_id))
        self.assertEqual(maintenance.delete_old_chats(self.s.db, self.h.settings.data_dir / "files", 0), 0)
        self.assertEqual(maintenance.delete_old_chats(self.s.db, self.h.settings.data_dir / "files", 730), 1)
        self.assertIsNone(self.s.store.conversation(old_id))
        self.assertIsNotNone(self.s.store.conversation(new_id))
        self.assertFalse(path.exists())
        self.assertIsNone(self.s.db.one("SELECT id FROM messages WHERE conversation_id = ?", (old_id,)))


# --------------------------------------------------------------------------- health checks
class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class HealthTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.settings = load_settings(search_cwd=False, data_dir=self.tmp, admin_slack_ids=frozenset({"U0COACH1",
                                                                                                       "DEV-admin"}))
        self.slack = FakeSlack()
        self.clock = Clock()
        self.health = Health(self.settings, self.slack, clock=self.clock)

    def run_check(self):
        asyncio.run(self.health.check())

    def test_admins_get_one_dm_after_15_minutes_and_one_when_it_recovers(self):
        self.health.record("claude", False, "Couldn't reach Claude (ConnectionError).")
        self.assertEqual(self.health.snapshot()["claude"]["state"], "unknown", "one failed reply isn't an outage")
        self.health.record("claude", False, "Couldn't reach Claude (ConnectionError).")
        self.run_check()
        self.assertEqual(self.slack.dms, [])
        self.clock.now += 14 * 60
        self.run_check()
        self.assertEqual(self.slack.dms, [])
        self.clock.now += 61
        self.run_check()
        self.run_check()
        self.assertEqual(len(self.slack.dms), 1)
        self.assertEqual(self.slack.dms[0]["user"], "U0COACH1")
        self.assertIn("the Claude API have been failing for 15 minutes", self.slack.dms[0]["text"])
        self.health.record("claude", True)
        self.run_check()
        self.assertEqual(len(self.slack.dms), 2)
        self.assertIn("working again", self.slack.dms[1]["text"])
        self.assertEqual(self.health.snapshot()["claude"]["state"], "ok")

    def test_slack_probe_and_socket_connection(self):
        self.run_check()
        self.assertEqual(self.health.snapshot()["slack"]["state"], "ok")
        self.slack.fail_auth = True
        self.clock.now += 301
        self.run_check()
        self.assertEqual(self.health.snapshot()["slack"]["state"], "failing")
        self.slack.fail_auth = False

        class Socket:
            connected = asyncio.Event()

        self.health.socket_client = Socket()
        self.run_check()
        self.assertIn("button taps", self.health.snapshot()["slack"]["detail"])

    def test_one_stray_failure_clears_itself(self):
        class Probe:
            def __init__(self):
                self.calls, self.fail = [], False

            async def complete(self, **kwargs):
                self.calls.append(kwargs)
                if self.fail:
                    raise ClaudeError("overloaded")
                return "OK"

        probe = Probe()
        self.health.claude = probe
        self.run_check()
        self.assertEqual(probe.calls, [], "no probes while Claude is fine")
        self.health.record("claude", False, "Claude returned an empty reply.")
        self.run_check()
        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(probe.calls[0]["max_tokens"], 1)
        self.assertEqual(self.health.snapshot()["claude"]["state"], "ok")
        # A real outage: the probe fails too, so it counts as not working and admins hear about it
        probe.fail = True
        self.health.record("claude", False, "Couldn't reach Claude.")
        self.clock.now += 301
        self.run_check()
        self.assertEqual(self.health.snapshot()["claude"]["state"], "failing")
        self.clock.now += 15 * 60
        self.run_check()
        self.assertTrue(any("Claude API" in dm["text"] for dm in self.slack.dms))

    def test_replies_feed_the_claude_status(self):
        h = Harness()
        try:
            kim = h.login(MENTOR)
            for _ in range(2):
                kim.new_chat("ERROR-TEST please")
                h.idle()
            self.assertEqual(h.services.health.snapshot()["claude"]["state"], "failing")
            kim.new_chat("A normal question")
            h.idle()
            self.assertEqual(h.services.health.snapshot()["claude"]["state"], "ok")
        finally:
            h.close()


# --------------------------------------------------------------------------- updates and packaging
class UpdateCheckTest(TempDirTest):
    def test_update_available(self):
        settings = load_settings(search_cwd=False, data_dir=self.tmp)
        db = Database(settings.db_path)
        self.assertIsNone(maintenance.update_available(db))
        asyncio.run(maintenance.check_for_update(settings, db, fetch=lambda repo: {"version": "99.0.0", "url": "u"}))
        self.assertEqual(maintenance.update_available(db)["version"], "99.0.0")
        asyncio.run(maintenance.check_for_update(settings, db, fetch=lambda repo: {"version": "0.0.1", "url": "u"}))
        self.assertIsNone(maintenance.update_available(db))

        def broken(repo):
            raise OSError("offline")

        self.assertIsNone(asyncio.run(maintenance.check_for_update(settings, db, fetch=broken)))
        self.assertGreater(maintenance.version_tuple("0.10.0"), maintenance.version_tuple("0.9.9"))


class DataFolderCheckTest(unittest.TestCase):
    def test_open_folder_is_flagged(self):
        from subzero_ai.checks import data_folder_open
        locked = ("C:\\ProgramData\\SubZeroAI BUILTIN\\Administrators:(OI)(CI)(F)\n"
                  "  NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n  NT SERVICE\\SubZeroAI:(OI)(CI)(M)\n")
        self.assertEqual(data_folder_open("C:\\ProgramData\\SubZeroAI", locked), "")
        opened = locked + "  BUILTIN\\Users:(OI)(CI)(RX)\n"
        self.assertIn("install.cmd", data_folder_open("C:\\ProgramData\\SubZeroAI", opened))


class PackagingTest(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_windows", ROOT / "packaging" / "build_windows.py")
        self.build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.build)

    def test_embedded_python_path_file(self):
        self.assertEqual(self.build.pth_contents("3.13.17"),
                         "python313.zip\n.\nLib\\site-packages\n..\\app\nimport site\n")

    def test_windows_hashes_match_the_lock_file(self):
        lock = self.build.pinned(ROOT / "packaging" / "requirements-lock.txt")
        windows = self.build.pinned(ROOT / "packaging" / "requirements-windows.txt")
        self.assertEqual(lock, windows, "run python packaging/build_windows.py --write-hashes")
        text = (ROOT / "packaging" / "requirements-windows.txt").read_text()
        self.assertEqual(text.count("--hash=sha256:"), len(lock))

    def test_lock_file_pins_everything_in_requirements(self):
        lock = {line.split("==")[0].lower().replace("_", "-") for line in
                (ROOT / "packaging" / "requirements-lock.txt").read_text().splitlines()
                if line and not line.startswith("#")}
        for line in (ROOT / "requirements.txt").read_text().splitlines():
            line = line.split("#")[0].strip()
            if not line:
                continue
            name = re.split(r"[<>=;\[ ]", line, maxsplit=1)[0].lower().replace("_", "-")
            self.assertIn(name, lock)
        for line in (ROOT / "packaging" / "requirements-lock.txt").read_text().splitlines():
            if line and not line.startswith("#"):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9.]*$")

    def test_version_matches_the_release_scripts(self):
        self.assertRegex(self.build.app_version(), r"^\d+\.\d+\.\d+$")
        for script in (ROOT / "packaging" / "windows").rglob("*"):
            if script.suffix in (".ps1", ".cmd"):
                text = script.read_text(encoding="utf-8")
                self.assertTrue(text.isascii(), f"{script.name} must be ASCII for Windows PowerShell 5.1")
                self.assertNotRegex(text, r"5690|SubZero Robotics")

    def test_cmd_files_explain_running_from_inside_the_zip(self):
        # Opening a .cmd from inside the zip in Explorer copies just that file to a temp folder, so the
        # script it runs isn't there. Each one says to extract the zip instead of failing in PowerShell.
        for cmd in (ROOT / "packaging" / "windows" / "cmd").glob("*.cmd"):
            text = cmd.read_text(encoding="utf-8")
            script = re.search(r'-File "%~dp0(scripts\\[a-z-]+\.ps1)"', text).group(1)
            self.assertIn(f'if exist "%~dp0{script}" goto run', text)
            self.assertIn("Extract All", text)
            self.assertLess(text.index("goto run"), text.index("powershell"))


if __name__ == "__main__":
    unittest.main()
