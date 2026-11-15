"""Projects, share links, explainers, Drive links, uploads and the per-message tools were removed from the website
in 0.11. These tests check that what is left behaves: old addresses, old settings and old rows are handled quietly,
and every page that still exists draws for the right person."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from subzero_ai.claude import FakeClaude
from subzero_ai.config import load_settings
from subzero_ai.db import Database
from subzero_ai.web import create_app

from .helpers import ADMIN, MENTOR, STUDENT, Harness

REMOVED_KEYS = ("max_tokens_explainer", "max_text_file_kb", "connector_drive_links", "research_for_students",
                # removed with the website chat pages (0.11)
                "subteams", "show_pending_to_team", "show_rejected_to_team", "effort_high", "web_search_enabled",
                "web_search_daily_limit", "long_chat_tokens")


class OldAddressesTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_share_links_say_they_are_gone_and_need_no_sign_in(self):
        for path in ("/s/anything", "/s/" + "x" * 40, "/s/anything/x/3/0"):
            resp = self.h.client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
            self.assertEqual(resp.status_code, 410, path)
            self.assertIn("This link is no longer available.", resp.text)
            self.assertEqual(resp.headers["referrer-policy"], "no-referrer")
            self.assertNotIn("Sign in", resp.text)
        # A real, once-active link row changes nothing: no lookup, no chat data
        sent = self.h.slack_dm("A private question about our arm", user=STUDENT)
        self.h.services.db.execute(
            "INSERT INTO shares (conversation_id, token, upto_message_id, audience, requested_by, status, created_at) "
            "VALUES (?, 'oldtoken123', ?, 'team', ?, 'active', '2026-01-01T00:00:00+00:00')",
            (sent.conversation_id, sent.message_id, self.h.services.store.user_by_slack_id(STUDENT)["id"]))
        self.h.client.cookies.clear()
        resp = self.h.client.get("/s/oldtoken123", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 410)
        self.assertNotIn("private question", resp.text)
        self.assertEqual(self.h.services.db.one("SELECT views FROM shares")["views"], 0)

    def test_removed_pages_are_not_found(self):
        ava = self.h.login(STUDENT)
        for path in ("/connectors/github", "/c/1/share", "/c/1/x/2/0", "/shares/1/preview", "/chats/new"):
            self.assertEqual(ava.get(path).status_code, 404, path)
        # Removed actions have no route (404), or the address only answers GET (405)
        for path in ("/chats/new", "/c/1/messages", "/c/1/model", "/c/1/delete", "/projects/new"):
            self.assertIn(ava.post(path, {"text": "x", "name": "x"}).status_code, (404, 405), path)

    def test_the_sidebar_has_no_removed_links(self):
        for who in (STUDENT, MENTOR, ADMIN):
            page = self.h.login(who).get("/about").text
            for gone in ('href="/projects"', 'href="/artifacts"', 'href="/chats"', 'href="/recents"', "New chat",
                         "Recents", "side-new"):
                self.assertNotIn(gone, page, f"{who} {gone}")

    def test_no_page_has_a_composer_tools_or_file_picker(self):
        sent = self.h.slack_dm("Hello there", user=STUDENT)
        kim = self.h.login(MENTOR)
        for path in ("/about", "/queue", f"/c/{sent.conversation_id}", "/profile"):
            page = kim.get(path).text
            for gone in ('type="file"', 'name="search"', 'name="research"', 'name="think"', 'name="style"',
                         "data-open-connector", "Google Drive", "Add from GitHub", "data-composer", "data-new-chat"):
                self.assertNotIn(gone, page, f"{path} {gone}")


class PagesDrawTest(unittest.TestCase):
    """A smoke test: every page that is left opens for the role it is for, and is closed to the others."""

    def setUp(self):
        self.h = Harness(code_student_access=True)

    def tearDown(self):
        self.h.close()

    def test_pages_by_role(self):
        ava, kim, coach = self.h.login(STUDENT), self.h.login(MENTOR), self.h.login(ADMIN)
        cid = self.h.slack_dm("How do I tune a PID loop?", user=STUDENT).conversation_id
        for actor in (ava, kim, coach):
            for path in ("/code", "/profile", "/about"):
                resp = actor.get(path)
                self.assertEqual(resp.status_code, 200, f"{actor.slack_user_id} {path}")
        for actor in (kim, coach):
            self.assertEqual(actor.get(f"/c/{cid}").status_code, 200, actor.slack_user_id)
        self.assertEqual(ava.get(f"/c/{cid}", follow_redirects=False).status_code, 303, "students have no chat pages")
        for path in ("/queue", "/queue/items"):
            self.assertEqual(kim.get(path).status_code, 200, path)
            self.assertEqual(coach.get(path).status_code, 200, path)
            self.assertEqual(ava.get(path).status_code, 403, path)
        for path in ("/settings", "/audit"):
            self.assertEqual(coach.get(path).status_code, 200, path)
            self.assertEqual(kim.get(path).status_code, 403, path)
            self.assertEqual(ava.get(path).status_code, 403, path)
        self.assertIn("Connectors", coach.get("/settings").text)
        self.assertNotIn("Google Drive links", coach.get("/settings").text)
        self.assertNotIn("connector_drive_links", coach.get("/settings").text)


class RemovedSettingsTest(unittest.TestCase):
    def test_removed_settings_in_config_env_and_the_settings_table_are_ignored_at_start_up(self):
        tmp = Path(tempfile.mkdtemp(prefix="subzero-old-settings-"))
        try:
            (tmp / "config.env").write_text(
                "MAX_TOKENS_EXPLAINER=16000\nMAX_TEXT_FILE_KB=300\nCONNECTOR_DRIVE_LINKS=false\n"
                "RESEARCH_FOR_STUDENTS=true\nSUBTEAMS=Alpha,Beta\nSHOW_PENDING_TO_TEAM=1\nSHOW_REJECTED_TO_TEAM=1\n"
                "EFFORT_HIGH=high\nWEB_SEARCH_ENABLED=0\nWEB_SEARCH_DAILY_LIMIT=5\nLONG_CHAT_TOKENS=1000\n"
                "SOME_UNKNOWN_KEY=whatever\nMAX_TOKENS_CHAT=7000\n", encoding="utf-8")
            settings = load_settings(tmp / "config.env", search_cwd=False, data_dir=tmp, dev_login=True,
                                     slack_team_id="TDEV")
            for key in REMOVED_KEYS:
                self.assertFalse(hasattr(settings, key), key)
            self.assertEqual(settings.max_tokens_chat, 7000, "the other lines still count")
            # The same keys, and an unknown one, in the settings table (saved from the Settings page in 0.10)
            db = Database(settings.db_path)
            for key, value in (*[(k, "1") for k in REMOVED_KEYS], ("some_unknown_key", "x"), ("max_tokens_chat", "6500")):
                db.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
                           (key, value))
            app = create_app(settings, claude=FakeClaude(delay=0), background=False)
            with TestClient(app) as client:
                self.assertEqual(client.get("/healthz").status_code, 200)
                self.assertEqual(client.get("/login").status_code, 200)
            for key in REMOVED_KEYS:
                self.assertFalse(hasattr(settings, key), key)
            self.assertEqual(settings.max_tokens_chat, 6500, "a saved value still wins over config.env")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_settings_page_and_validation_no_longer_know_the_removed_fields(self):
        from subzero_ai import saved_settings, server_settings
        known = set(saved_settings.FIELDS) | set(server_settings.ENV_FIELDS)
        self.assertFalse(known & set(REMOVED_KEYS))
        h = Harness()
        try:
            page = h.login(ADMIN).get("/settings").text
            for gone in REMOVED_KEYS:
                self.assertNotIn(f'name="{gone}"', page)
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
