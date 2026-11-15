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

REMOVED_KEYS = ("max_tokens_explainer", "max_text_file_kb", "connector_drive_links", "research_for_students")


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
        ava = self.h.login(STUDENT)
        mid = ava.new_chat("A private question about our arm").json()["message_id"]
        cid = self.h.message(mid)["conversation_id"]
        self.h.services.db.execute(
            "INSERT INTO shares (conversation_id, token, upto_message_id, audience, requested_by, status, created_at) "
            "VALUES (?, 'oldtoken123', ?, 'team', ?, 'active', '2026-01-01T00:00:00+00:00')",
            (cid, mid, ava.user["id"]))
        self.h.client.cookies.clear()
        resp = self.h.client.get("/s/oldtoken123", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 410)
        self.assertNotIn("private question", resp.text)
        self.assertEqual(self.h.services.db.one("SELECT views FROM shares")["views"], 0)

    def test_removed_pages_are_not_found(self):
        ava = self.h.login(STUDENT)
        for path in ("/projects", "/projects/1", "/artifacts", "/artifacts/1/0", "/connectors/github",
                     "/c/1/share", "/c/1/x/2/0", "/shares/1/preview"):
            self.assertEqual(ava.get(path).status_code, 404, path)
        self.assertEqual(ava.post("/projects/new", {"name": "x"}).status_code, 404)

    def test_sidebar_has_no_removed_links(self):
        for who in (STUDENT, MENTOR, ADMIN):
            page = self.h.login(who).get("/chats").text
            for gone in ('href="/projects"', 'href="/artifacts"'):
                self.assertNotIn(gone, page, who)

    def test_the_composer_has_no_tools_or_file_picker(self):
        for who in (STUDENT, MENTOR):
            actor = self.h.login(who)
            mid = actor.new_chat("Hello there").json()["message_id"]
            cid = self.h.message(mid)["conversation_id"]
            for path in ("/chats", f"/c/{cid}"):
                page = actor.get(path).text
                for gone in ('type="file"', 'name="search"', 'name="research"', 'name="think"', 'name="style"',
                             "data-open-connector", "Google Drive", "Add from GitHub"):
                    self.assertNotIn(gone, page, f"{who} {path} {gone}")


class PagesDrawTest(unittest.TestCase):
    """A smoke test: every page that is left opens for the role it is for, and is closed to the others."""

    def setUp(self):
        self.h = Harness(code_student_access=True)

    def tearDown(self):
        self.h.close()

    def test_pages_by_role(self):
        ava, kim, coach = self.h.login(STUDENT), self.h.login(MENTOR), self.h.login(ADMIN)
        mid = ava.new_chat("How do I tune a PID loop?").json()["message_id"]
        cid = self.h.message(mid)["conversation_id"]
        self.h.idle()
        for actor in (ava, kim, coach):
            for path in ("/chats", "/recents", f"/c/{cid}", "/code", "/profile", "/about"):
                resp = actor.get(path)
                self.assertEqual(resp.status_code, 200, f"{actor.slack_user_id} {path}")
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
                "RESEARCH_FOR_STUDENTS=true\nSOME_UNKNOWN_KEY=whatever\nMAX_TOKENS_CHAT=7000\n", encoding="utf-8")
            settings = load_settings(tmp / "config.env", search_cwd=False, data_dir=tmp, dev_login=True,
                                     slack_team_id="TDEV")
            for key in REMOVED_KEYS:
                self.assertFalse(hasattr(settings, key), key)
            self.assertEqual(settings.max_tokens_chat, 7000, "the other lines still count")
            # The same four keys, and an unknown one, in the settings table (saved from the Settings page in 0.10)
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
