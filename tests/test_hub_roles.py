"""SubZero Hub's core: module migrations, roles picked on the People page, and role permissions."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from subzero_ai.config import load_settings
from subzero_ai.db import Database
from subzero_ai.hub.modules import Module, migrate_modules
from subzero_ai.hub.permissions import Grants, PermissionChangeError, default_permissions, role_label
from subzero_ai.roles import Directory
from subzero_ai.slack_api import FakeSlack
from subzero_ai.store import Viewer

from .helpers import ADMIN, MENTOR, STUDENT, STUDENT_2, Harness



class TempDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hub-test-"))
        self.db = Database(self.tmp / "app.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ModuleMigrationTest(TempDbTest):
    def test_a_new_database_has_the_hub_tables(self):
        columns = [r["name"] for r in self.db.all("PRAGMA table_info(users)")]
        self.assertIn("assigned_role", columns)
        self.assertEqual(self.db.one("SELECT version FROM hub_module_migrations WHERE module = 'hub'")["version"], 1)

    def test_migrating_again_changes_nothing(self):
        Database(self.db.path)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM hub_module_migrations WHERE module = 'hub'")["n"], 1)

    def test_a_module_gets_only_the_migrations_it_has_not_run(self):
        class Extra(Module):
            key = "extra"
            migrations = ("CREATE TABLE extra_a (id INTEGER);", "CREATE TABLE extra_b (id INTEGER);")

        from subzero_ai.hub import modules
        original = modules.installed
        modules.installed = lambda: [Extra()]
        try:
            with self.db.connect() as conn:
                conn.executescript("BEGIN; CREATE TABLE extra_a (id INTEGER);"
                                   "INSERT INTO hub_module_migrations VALUES ('extra', 1); COMMIT;")
                migrate_modules(conn)
        finally:
            modules.installed = original
        self.assertIsNotNone(self.db.one("SELECT name FROM sqlite_master WHERE name = 'extra_b'"))
        self.assertEqual(self.db.one("SELECT version FROM hub_module_migrations WHERE module = 'extra'")["version"], 2)


class GrantsTest(TempDbTest):
    def setUp(self):
        super().setUp()
        self.grants = Grants(self.db)

    def test_defaults(self):
        self.assertEqual(self.grants.for_role("admin"), frozenset({"hub.manage", "ai.mentor"}))
        self.assertEqual(self.grants.for_role("mentor"), frozenset({"ai.mentor"}))
        self.assertEqual(self.grants.for_role("lead"), frozenset())
        self.assertEqual(self.grants.for_role("student"), frozenset())
        self.assertEqual(self.grants.for_role("nobody"), frozenset())

    def test_changes_are_saved_and_a_return_to_the_default_removes_the_row(self):
        self.assertTrue(self.grants.set("lead", "ai.mentor", True, actor_id=None))
        self.assertTrue(Grants(self.db).allows("lead", "ai.mentor"))
        self.assertFalse(self.grants.set("lead", "ai.mentor", True, actor_id=None))
        self.assertTrue(self.grants.set("mentor", "ai.mentor", False, actor_id=None))
        self.assertFalse(self.grants.allows("mentor", "ai.mentor"))
        self.grants.set("lead", "ai.mentor", False, actor_id=None)
        self.grants.set("mentor", "ai.mentor", True, actor_id=None)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM hub_role_permissions")["n"], 0)
        actions = [r["action"] for r in self.db.all("SELECT action FROM audit_log")]
        self.assertEqual(actions.count("permission_changed"), 4)

    def test_refused_changes(self):
        for role, key, allowed in (("admin", "ai.mentor", False), ("student", "ai.mentor", True),
                                   ("student", "hub.manage", True), ("lead", "no.such", True),
                                   ("ghost", "ai.mentor", True)):
            with self.subTest(role=role, key=key), self.assertRaises(PermissionChangeError):
                self.grants.set(role, key, allowed, actor_id=None)

    def test_a_saved_grant_for_a_never_role_is_ignored(self):
        self.db.execute("INSERT INTO hub_role_permissions VALUES ('student', 'ai.mentor', 1, 'x')")
        self.assertFalse(Grants(self.db).allows("student", "ai.mentor"))


class ViewerTest(unittest.TestCase):
    def test_role_defaults_without_saved_permissions(self):
        self.assertTrue(Viewer(1, "U1", "Kim", "mentor").is_mentor)
        self.assertFalse(Viewer(1, "U1", "Kim", "mentor").is_admin)
        self.assertTrue(Viewer(1, "U1", "Coach", "admin").can("anything.at.all"))
        self.assertFalse(Viewer(1, "U1", "Lee", "lead").is_mentor)
        self.assertEqual(default_permissions("student"), frozenset())

    def test_given_permissions_win(self):
        lead = Viewer(1, "U1", "Lee", "lead", permissions=frozenset({"ai.mentor"}))
        self.assertTrue(lead.is_mentor)
        self.assertFalse(Viewer(1, "U1", "Kim", "mentor", permissions=frozenset()).is_mentor)

    def test_labels(self):
        self.assertEqual([role_label(r) for r in ("admin", "mentor", "lead", "student")],
                         ["Coach", "Mentor", "Lead", "Student"])
        self.assertEqual(Viewer(1, "U1", "Coach", "admin").role_label, "Coach")


class DirectoryTest(TempDbTest):
    def setUp(self):
        super().setUp()
        self.slack = FakeSlack(members={"UCHANNEL"})
        self.settings = load_settings(search_cwd=False, data_dir=self.tmp, slack_approvals_channel="CAPPROVALS",
                                      admin_slack_ids="USETTINGS")
        self.directory = Directory(self.settings, self.slack, db=self.db)
        for sid in ("USETTINGS", "UCHANNEL", "UPLAIN"):
            self.db.execute("INSERT INTO users (slack_user_id, name, created_at) VALUES (?, ?, 'x')", (sid, sid))

    def assign(self, sid: str, role: str | None) -> None:
        self.db.execute("UPDATE users SET assigned_role = ? WHERE slack_user_id = ?", (role, sid))

    def role(self, sid: str) -> str:
        return asyncio.run(self.directory.role_for(sid))

    def test_order(self):
        self.assertEqual(self.role("UPLAIN"), "student")
        self.assertEqual(self.role("UCHANNEL"), "mentor")
        self.assign("UPLAIN", "lead")
        self.assertEqual(self.role("UPLAIN"), "lead")
        self.assign("UCHANNEL", "student")
        self.assertEqual(self.role("UCHANNEL"), "mentor")
        self.assign("UCHANNEL", "admin")
        self.assertEqual(self.role("UCHANNEL"), "admin")
        self.assign("USETTINGS", "student")
        self.assertEqual(self.role("USETTINGS"), "admin")
        self.assertEqual(self.role("UNEVERSEEN"), "student")

    def test_channel_mentors_can_be_turned_off(self):
        self.settings.mentors_from_channel = False
        self.assertEqual(self.role("UCHANNEL"), "student")
        self.assign("UCHANNEL", "mentor")
        self.assertEqual(self.role("UCHANNEL"), "mentor")

    def test_an_unknown_saved_role_is_ignored(self):
        self.assign("UPLAIN", "wizard")
        self.assertEqual(self.role("UPLAIN"), "student")


class PagesTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.coach = self.h.login(ADMIN)
        self.kim = self.h.login(MENTOR)
        self.ava = self.h.login(STUDENT)
        self.h.login(STUDENT_2)

    def tearDown(self):
        self.h.close()

    def set_role(self, actor, who, role):
        return actor.post(f"/people/{who.user['id']}/role", {"role": role}, follow_redirects=False)

    def test_only_coaches_open_the_pages(self):
        for path in ("/people", "/permissions"):
            with self.subTest(path=path):
                self.assertEqual(self.coach.get(path).status_code, 200)
                self.assertEqual(self.kim.get(path).status_code, 403)
                self.assertEqual(self.ava.get(path).status_code, 403)
        self.assertEqual(self.set_role(self.kim, self.ava, "lead").status_code, 403)

    def test_people_lists_everyone_with_their_role(self):
        page = self.coach.get("/people").text
        for name in ("Ava (student)", "Ben (student)", "Mentor Kim", "Head coach"):
            self.assertIn(name, page)
        self.assertIn("Your own role", page)
        self.assertIn('href="/people"', page)

    def test_a_coach_picks_a_role(self):
        resp = self.set_role(self.coach, self.ava, "lead")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.ava.user["assigned_role"], "lead")
        self.assertEqual(self.ava.user["role"], "lead")
        self.assertIn("Lead", self.ava.get("/profile").text)
        audit = self.h.services.db.one("SELECT * FROM audit_log WHERE action = 'role_assigned'")
        self.assertIsNotNone(audit)
        self.set_role(self.coach, self.ava, "")
        self.assertIsNone(self.ava.user["assigned_role"])
        self.assertEqual(self.ava.user["role"], "student")

    def test_refused_role_changes(self):
        self.set_role(self.coach, self.coach, "student")
        self.assertIsNone(self.coach.user["assigned_role"])
        self.set_role(self.coach, self.ava, "wizard")
        self.assertIsNone(self.ava.user["assigned_role"])

    def test_a_coach_made_on_the_people_page_can_manage_people(self):
        self.set_role(self.coach, self.kim, "admin")
        self.assertEqual(self.kim.get("/people").status_code, 200)

    def test_giving_leads_the_mentor_permission(self):
        self.set_role(self.coach, self.ava, "lead")
        self.assertEqual(self.ava.get("/queue").status_code, 403)
        resp = self.coach.post("/permissions", {"lead:ai.mentor": "1", "mentor:ai.mentor": "1"},
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.ava.get("/queue").status_code, 200)
        self.coach.post("/permissions", {"mentor:ai.mentor": "1"})
        self.assertEqual(self.ava.get("/queue").status_code, 403)

    def test_students_never_get_the_mentor_permission(self):
        self.coach.post("/permissions", {"student:ai.mentor": "1", "mentor:ai.mentor": "1"})
        self.assertEqual(self.ava.get("/queue").status_code, 403)
        page = self.coach.get("/permissions").text
        self.assertIn('name="student:ai.mentor" value="1" aria-label="Student:', page)

    def test_unticking_a_mentor_permission_takes_mentor_powers_away(self):
        self.coach.post("/permissions", {})
        self.assertEqual(self.kim.get("/queue").status_code, 403)
        self.assertEqual(self.coach.get("/queue").status_code, 200)


if __name__ == "__main__":
    unittest.main()
