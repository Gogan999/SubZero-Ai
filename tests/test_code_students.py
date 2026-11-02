"""Students' Code sessions (M6): a mentor approves each request, then the sandbox runs free-rein steps without clicks.
Everything runs offline with the fake sandbox and the scripted Claude, so nothing here reaches a real VM or site."""

from __future__ import annotations

import html
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from subzero_ai import sandbox, saved_settings
from subzero_ai.code_runner import build_system_prompt
from subzero_ai.db import MIGRATIONS, Database

from .helpers import ADMIN, MENTOR, STUDENT, STUDENT_2, Harness
from .test_features import FakeResponse, FakeWeb, repo_zip

API = "https://api.github.com/repos/team/robot-code/zipball"


class CodeStudentTest(unittest.TestCase):
    """Students' sessions with the fake sandbox switched on and student access turned on."""

    overrides: dict = {}

    def setUp(self):
        self.h = Harness(connector_github_repo="team/robot-code", code_sandbox="fake", sandbox_min_free_gb=0,
                         code_student_access=True, **self.overrides)
        self.ava = self.h.login(STUDENT)
        self.ben = self.h.login(STUDENT_2)
        self.kim = self.h.login(MENTOR)
        self.h.services.github.session_factory = FakeWeb({API: FakeResponse(200, repo_zip())})
        self.code = self.h.services.code
        self.db = self.h.services.db
        self.manager = self.h.services.sandboxes
        self.root = self.h.services.settings.data_dir / "sandbox-fake"

    def tearDown(self):
        self.h.close()

    # ---- helpers
    def ask(self, actor, task="Add a notes file and run the build."):
        resp = actor.post("/code", {"task": task}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        location = resp.headers["location"]
        self.h.idle()
        if not location.rsplit("/", 1)[1].isdigit():
            return None, location
        return int(location.rsplit("/", 1)[1]), location

    def approve(self, sid):
        resp = self.kim.post(f"/code/{sid}/approve-request", {"note": ""}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        self.h.idle()

    def decline(self, sid, note=""):
        resp = self.kim.post(f"/code/{sid}/decline-request", {"note": note}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        self.h.idle()

    def tool_turns(self) -> int:
        return sum(1 for c in self.h.claude.calls if c["kind"] == "tool_turn")

    def actions(self) -> list[str]:
        return [row["action"] for row in self.db.all("SELECT action FROM audit_log ORDER BY id")]

    def steps(self, sid, status=None):
        return [s for s in self.code.steps(sid) if s["kind"] == "tool" and (status is None or s["status"] == status)]


class StudentAccessTest(CodeStudentTest):
    def test_students_see_no_code_when_access_is_off(self):
        self.h.services.settings.code_student_access = False
        self.assertNotIn('href="/code"', self.ava.get("/chats").text)
        self.assertEqual(self.ava.get("/code").status_code, 403)
        self.assertEqual(self.ava.post("/code", {"task": "x"}).status_code, 403)
        self.assertIn('href="/code"', self.kim.get("/chats").text, "mentors keep Code")
        self.h.services.settings.code_student_access = True
        self.assertIn('href="/code"', self.ava.get("/chats").text)
        self.assertEqual(self.ava.get("/code").status_code, 200)

    def test_a_student_request_waits_for_a_mentor_and_makes_no_sandbox(self):
        sid, _ = self.ask(self.ava)
        session = self.code.get(sid)
        self.assertEqual(session["status"], "awaiting_approval")
        self.assertEqual(session["owner_is_student"], 1)
        self.assertEqual(session["free_rein"], 0, "free rein is decided when a mentor approves")
        self.assertEqual(self.tool_turns(), 0, "Claude hasn't started")
        self.assertEqual(self.db.all("SELECT * FROM sandbox_sessions"), [], "no sandbox before approval")
        self.assertFalse(self.code.workspace(sid).ready(), "no copy of the repository before approval")
        self.assertFalse(self.root.exists() and any(self.root.iterdir()))
        self.assertIn("asked for a Code session", self.h.slack.posts[-1]["text"], "mentors hear about it in Slack")
        self.assertEqual(self.h.slack.posts[-1]["channel"], "CAPPROVALS")
        self.assertIn("Student requests", self.kim.get("/code").text)
        self.assertIn(f'href="/code/{sid}"', self.kim.get("/code").text)
        self.assertIn("Approve and start", self.kim.get(f"/code/{sid}").text)
        self.assertIn("code_student_requested", self.actions())

    def test_mentor_approval_makes_the_sandbox_and_starts_claude(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        self.assertEqual(self.code.get(sid)["status"], "done")
        self.assertEqual(self.code.get(sid)["free_rein"], 1)
        self.assertIsNotNone(self.code.get(sid)["start_approved_by"])
        self.assertGreater(self.tool_turns(), 0)
        [record] = self.db.all("SELECT * FROM sandbox_sessions WHERE code_session_id = ?", (sid,))
        self.assertEqual(record["state"], "destroyed", "the sandbox is deleted when the session is done")
        self.assertIn("code_student_approved", self.actions())
        self.assertIn("sandbox_created", self.actions())

    def test_a_declined_request_stops_with_the_note_and_makes_no_sandbox(self):
        sid, _ = self.ask(self.ava)
        self.decline(sid, "Pick a smaller task first.")
        session = self.code.get(sid)
        self.assertEqual(session["status"], "stopped")
        self.assertIn("Pick a smaller task first.", session["error"])
        self.assertEqual(self.tool_turns(), 0)
        self.assertEqual(self.db.all("SELECT * FROM sandbox_sessions"), [])
        page = self.ava.get(f"/code/{sid}").text
        self.assertIn("Pick a smaller task first.", page)
        self.assertIn("code_student_declined", self.actions())

    def test_free_rein_runs_steps_without_clicks_for_a_student(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        self.assertEqual(self.steps(sid, "pending"), [], "no step waits for a click")
        self.assertEqual([s["tool"] for s in self.steps(sid, "done")],
                         ["list_files", "read_file", "write_file", "run_command"])
        ran = self.steps(sid, "done")[-1]
        self.assertIn("BUILD SUCCESSFUL", ran["output"])
        self.assertNotIn("code_step_approved", self.actions(), "nothing was clicked")
        with zipfile.ZipFile(self.code.workspace(sid).root / "sandbox-export.zip") as archive:
            self.assertIn("NOTES.md", archive.namelist(), "the write ran inside the sandbox")
        self.assertTrue(any("sandbox" in (s["output"] or "") for s in self.code.steps(sid) if s["kind"] == "note"))

    def test_without_free_rein_a_student_step_waits_for_a_mentor(self):
        self.h.services.settings.code_student_free_rein = False
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        self.assertEqual(self.code.get(sid)["status"], "waiting")
        [write] = self.steps(sid, "pending")
        self.assertIn("Waiting for a mentor to look at this step", self.ava.get(f"/code/{sid}").text)
        self.assertNotIn("Approve change", self.ava.get(f"/code/{sid}").text, "students never see approve buttons")
        self.kim.post(f"/code/steps/{write['id']}/approve", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertIn("code_step_approved", self.actions())

    def test_mentor_sessions_keep_the_clicks_unless_free_rein_is_on(self):
        self.kim.post("/code", {"task": "Add a notes file and run the build."}, follow_redirects=False)
        self.h.idle()
        [mine] = [r for r in self.code.list() if r["owner_is_student"] == 0]
        self.assertEqual(self.code.get(mine["id"])["status"], "waiting")
        self.assertEqual(len(self.steps(mine["id"], "pending")), 1, "mentor's write waits for a click")
        self.h.services.settings.code_mentor_free_rein = True
        self.kim.post("/code", {"task": "Second task, free rein."}, follow_redirects=False)
        self.h.idle()
        newest = max(self.code.list(), key=lambda r: r["id"])
        self.assertEqual(newest["free_rein"], 1)
        self.assertEqual(self.code.steps(newest["id"]) and [s for s in self.steps(newest["id"], "pending")], [])


class StudentRefusalTest(CodeStudentTest):
    def test_sandbox_off_refuses_a_student_with_a_plain_message(self):
        self.h.services.settings.code_sandbox = "off"
        resp = self.ava.post("/code", {"task": "Try it."}, follow_redirects=True)
        self.assertIn("Code for students needs the sandbox turned on", html.unescape(resp.text))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM code_sessions")["n"], 0)
        self.assertIn("Code isn't ready yet", self.ava.get("/code").text)

    def test_a_failed_network_check_refuses_a_student(self):
        with mock.patch.object(sandbox.FakeSandbox, "check_network",
                               side_effect=sandbox.SandboxRefused("The sandbox network isn't ready yet.")):
            resp = self.ava.post("/code", {"task": "Try it."}, follow_redirects=True)
        self.assertIn("The sandbox network isn&#39;t ready yet.", resp.text)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM code_sessions")["n"], 0)

    def test_turning_the_sandbox_off_stops_a_student_run_before_anything_runs_on_the_computer(self):
        self.h.services.settings.code_student_free_rein = False
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        [write] = self.steps(sid, "pending")
        self.h.services.settings.code_sandbox = "off"
        self.kim.post(f"/code/steps/{write['id']}/approve", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.code.get(sid)["status"], "failed")
        self.assertIn("The sandbox is off", self.code.get(sid)["error"])

    def test_the_sandbox_being_off_does_not_stop_a_mentor_session(self):
        self.h.services.settings.code_sandbox = "off"
        self.kim.post("/code", {"task": "Add a notes file and run the build."}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(max(self.code.list(), key=lambda r: r["id"])["status"], "waiting",
                         "mentors keep the host's per-step clicks")


class StudentLimitsTest(CodeStudentTest):
    overrides = {"code_student_max_open": 1, "code_student_daily_sessions": 2}

    def test_one_open_session_at_a_time(self):
        self.ask(self.ava)
        resp = self.ava.post("/code", {"task": "A second task."}, follow_redirects=True)
        self.assertIn("You can have 1 Code session open at a time", resp.text)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM code_sessions")["n"], 1)

    def test_a_daily_limit_counts_declined_requests_too(self):
        self.h.services.settings.code_student_max_open = 5
        for text in ("First task.", "Second task."):
            sid, _ = self.ask(self.ava, text)
            self.decline(sid, "No.")
        resp = self.ava.post("/code", {"task": "Third task."}, follow_redirects=True)
        self.assertIn("You&#39;ve used your 2 Code requests for today", resp.text)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM code_sessions")["n"], 2)

    def test_the_limit_is_per_student(self):
        self.h.services.settings.code_student_max_open = 5
        self.ask(self.ava, "Ava's task.")
        sid, _ = self.ask(self.ben, "Ben's task.")
        self.assertIsNotNone(sid)

    def test_student_turns_are_capped_separately(self):
        self.h.services.settings.code_student_max_turns = 2
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        session = self.code.get(sid)
        self.assertEqual(session["status"], "stopped")
        self.assertIn("Stopped after 2 replies", session["error"])

    def test_student_command_timeout_has_its_own_setting(self):
        sid, _ = self.ask(self.ava)
        self.h.services.settings.code_student_command_timeout = 45
        self.h.services.settings.code_command_timeout = 900
        self.assertEqual(self.code._timeout(sid), 45)


class StudentPrivacyTest(CodeStudentTest):
    def test_a_student_sees_only_their_own_sessions(self):
        sid, _ = self.ask(self.ava, "Ava secret task here.")
        self.assertEqual(self.ben.get(f"/code/{sid}").status_code, 404)
        self.assertEqual(self.ben.get(f"/code/{sid}/steps").status_code, 404)
        self.assertEqual(self.ben.post(f"/code/{sid}/follow-up", {"text": "Hi"},
                                       follow_redirects=False).status_code, 303)
        self.assertNotIn("Ava secret task here.", self.ben.get("/code").text)
        self.assertIn("Ava secret task here.", self.ava.get("/code").text)
        self.assertIn("Ava secret task here.", self.kim.get("/code").text, "mentors see every session")
        self.assertEqual(self.code.get(sid)["status"], "awaiting_approval", "Ben's follow-up didn't reach Ava's")
        self.assertEqual(self.ben.get(f"/code/{sid}/changes.patch").status_code, 404)

    def test_a_student_cannot_open_a_mentor_session(self):
        self.kim.post("/code", {"task": "Mentor only."}, follow_redirects=False)
        self.h.idle()
        mentor_sid = max(self.code.list(), key=lambda r: r["id"])["id"]
        self.assertEqual(self.ava.get(f"/code/{mentor_sid}").status_code, 404)
        self.assertEqual(self.ava.get(f"/code/{mentor_sid}/changes.patch").status_code, 404)

    def test_students_cannot_approve_stop_delete_release_or_extend(self):
        sid, _ = self.ask(self.ava)
        for action in ("approve-request", "decline-request", "stop", "release", "extend", "delete"):
            self.assertEqual(self.ava.post(f"/code/{sid}/{action}", {"note": ""}).status_code, 403, action)
        self.assertEqual(self.code.get(sid)["status"], "awaiting_approval")
        self.assertIsNotNone(self.code.get(sid), "a student can't delete")

    def test_the_patch_is_not_downloadable_until_a_mentor_releases_it(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        blocked = self.ava.get(f"/code/{sid}/changes.patch")
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("A mentor is looking at your changes", blocked.text)
        self.assertIn("Waiting for a mentor to look at your changes", self.ava.get(f"/code/{sid}").text)
        self.assertNotIn("Download patch", self.ava.get(f"/code/{sid}").text, "no download before release")
        self.assertNotIn("changes-title", self.ava.get(f"/code/{sid}").text, "no file list before release")
        self.assertEqual(self.kim.get(f"/code/{sid}/changes.patch").status_code, 200, "mentors always can")
        self.kim.post(f"/code/{sid}/release", {"note": ""}, follow_redirects=False)
        self.h.idle()
        patch = self.ava.get(f"/code/{sid}/changes.patch")
        self.assertEqual(patch.status_code, 200)
        self.assertIn("+++ b/NOTES.md", patch.text)
        self.assertIn("code_patch_released", self.actions())

    def test_a_student_sees_steps_and_outputs_but_not_the_proxy_login(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        page = self.ava.get(f"/code/{sid}").text
        self.assertIn("Ran a command", page)
        self.assertIn("BUILD SUCCESSFUL", page)


class StudentFollowUpTest(CodeStudentTest):
    def finished(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        self.assertEqual(self.code.get(sid)["status"], "done")
        return sid

    def test_a_student_follow_up_waits_for_a_mentor(self):
        sid = self.finished()
        before = self.tool_turns()
        self.ava.post(f"/code/{sid}/follow-up", {"text": "Also add a test."}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.tool_turns(), before, "Claude doesn't see it yet")
        self.assertEqual(self.code.get(sid)["status"], "done")
        [note] = [s for s in self.code.steps(sid) if s["status"] == "pending"]
        self.assertEqual(note["output"], "Also add a test.")
        self.assertIn("Waiting for a mentor to approve your follow-up", self.ava.get(f"/code/{sid}").text)
        self.kim.post(f"/code/steps/{note['id']}/approve-follow-up", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertGreater(self.tool_turns(), before, "approved, it carries on")
        self.assertIn("code_student_followup_approved", self.actions())
        self.assertIn("Also add a test.", self.kim.get(f"/code/{sid}").text)

    def test_a_declined_follow_up_never_reaches_claude(self):
        sid = self.finished()
        before = self.tool_turns()
        self.ava.post(f"/code/{sid}/follow-up", {"text": "Delete everything."}, follow_redirects=False)
        [note] = [s for s in self.code.steps(sid) if s["status"] == "pending"]
        self.kim.post(f"/code/steps/{note['id']}/decline-follow-up", {"note": "No."}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.tool_turns(), before)
        self.assertEqual(self.code.db.one("SELECT status FROM code_steps WHERE id = ?", (note["id"],))["status"],
                         "declined")
        self.assertIn("code_student_followup_declined", self.actions())

    def test_a_second_follow_up_waits_behind_the_first(self):
        sid = self.finished()
        self.ava.post(f"/code/{sid}/follow-up", {"text": "One."}, follow_redirects=False)
        resp = self.ava.post(f"/code/{sid}/follow-up", {"text": "Two."}, follow_redirects=True)
        self.assertIn("Your last request is still waiting for a mentor", resp.text)


class StudentSandboxLifetimeTest(CodeStudentTest):
    def live_student_session(self):
        self.h.services.settings.code_student_free_rein = False
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        self.assertIsNotNone(self.manager.live_for_session(sid), "the sandbox is live while a step waits")
        return sid

    def test_a_student_sees_how_long_the_workspace_lasts(self):
        sid = self.live_student_session()
        self.assertIn("This workspace is deleted in 48 hours", self.ava.get(f"/code/{sid}").text)
        self.assertNotIn("Give the sandbox more time", self.ava.get(f"/code/{sid}").text)

    def test_only_mentors_can_extend_and_it_is_capped(self):
        sid = self.live_student_session()
        self.assertEqual(self.ava.post(f"/code/{sid}/extend", {"note": ""}).status_code, 403)
        self.kim.post(f"/code/{sid}/extend", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertIn("This workspace is deleted in 96 hours", self.ava.get(f"/code/{sid}").text)
        for _ in range(3):
            self.kim.post(f"/code/{sid}/extend", {"note": ""}, follow_redirects=False)
        resp = self.kim.post(f"/code/{sid}/extend", {"note": ""}, follow_redirects=True)
        self.assertIn("reached its longest time", resp.text)
        self.assertIn("sandbox_extended", self.actions())


class StudentScreenerAndPromptTest(CodeStudentTest):
    def test_the_screener_flags_a_request_for_the_mentor_and_it_still_waits(self):
        sid, _ = self.ask(self.ava, "SCREEN-FLAG a request about the robot.")
        session = self.code.get(sid)
        self.assertEqual(session["status"], "awaiting_approval",
                         "a flag never starts anything, and never skips a mentor")
        self.assertIn("Screener: Flagged for testing.", session["screen_note"])
        self.assertIn("Screener: Flagged for testing.", self.kim.get(f"/code/{sid}").text)
        self.assertNotIn("Screener:", self.ava.get(f"/code/{sid}").text, "the note is for mentors")

    def test_a_student_run_is_told_it_is_in_a_sandbox_without_clicks(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        system = next(c["system"] for c in self.h.claude.calls if c["kind"] == "tool_turn")
        self.assertIn("inside a sandbox", system)
        self.assertIn("with no mentor clicking", system)
        self.assertIn("cannot reach", system.replace("can't", "cannot")) if "cannot reach" in system else None
        self.assertIn("pip", system, "Claude is told it may install packages through the proxy")
        self.assertIn("high school robotics student", system)

    def test_the_host_prompt_still_asks_for_clicks(self):
        text = build_system_prompt(student=False, sandboxed=False, free_rein=False, commands=("gradlew",))
        self.assertIn("shown to a mentor, who approves or declines it", text)
        self.assertIn("gradlew.bat", text)
        self.assertNotIn("inside a sandbox", text)
        sandbox_text = build_system_prompt(student=True, sandboxed=True, free_rein=False, commands=("pip",))
        self.assertIn("reaches package and code sites on an allow-list", sandbox_text)
        self.assertIn("shown to a mentor", sandbox_text)


class StudentDeleteTest(CodeStudentTest):
    def test_a_mentor_deletes_a_waiting_request_and_its_record_goes(self):
        sid, _ = self.ask(self.ava)
        resp = self.kim.post(f"/code/{sid}/delete", {"note": ""}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.h.idle()
        self.assertIsNone(self.code.get(sid))
        self.assertIn("code_session_deleted", self.actions())

    def test_deleting_a_finished_session_removes_its_copies_and_sandbox_record(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        folder = self.code.workspace(sid).root
        self.assertTrue(folder.exists())
        self.kim.post(f"/code/{sid}/delete", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertFalse(folder.exists())
        self.assertIsNone(self.code.get(sid))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM code_steps WHERE session_id = ?", (sid,))["n"], 0)
        self.assertIsNone(self.db.one("SELECT code_session_id FROM sandbox_sessions LIMIT 1")["code_session_id"])

    def test_a_live_sandbox_is_deleted_with_its_session(self):
        self.h.services.settings.code_student_free_rein = False
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        box = self.manager.live_for_session(sid)
        self.assertIsNotNone(box)
        self.kim.post(f"/code/{sid}/delete", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertFalse((self.root / box["name"]).exists())
        self.assertIsNone(self.code.get(sid))


class SettingsValidationTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(connector_github_repo="team/robot-code", code_sandbox="fake")
        self.settings = self.h.services.settings

    def tearDown(self):
        self.h.close()

    def form(self, **overrides):
        values = {f.name: saved_settings.current(self.settings, f.name) for f in saved_settings.SECTIONS["code"][1]}
        values.update(overrides)
        return values

    def test_defaults_are_off_and_safe(self):
        fresh = Harness()
        try:
            s = fresh.services.settings
            self.assertFalse(s.code_student_access)
            self.assertTrue(s.code_student_free_rein)
            self.assertFalse(s.code_mentor_free_rein)
            self.assertEqual((s.code_student_max_open, s.code_student_daily_sessions, s.code_student_max_turns,
                              s.code_student_command_timeout), (1, 3, 40, 600))
        finally:
            fresh.close()

    def test_students_need_a_sandbox_to_be_switched_on(self):
        values, errors = saved_settings.validate("code", self.form(code_student_access="1", code_sandbox="off"),
                                                 settings=self.settings, db=self.h.services.db)
        self.assertIn("code_student_access", errors)
        self.assertNotIn("code_student_access", values)
        values, errors = saved_settings.validate("code", self.form(code_student_access="1", code_sandbox="hyperv"),
                                                 settings=self.settings, db=self.h.services.db)
        self.assertNotIn("code_student_access", errors)
        self.assertTrue(values["code_student_access"])

    def test_limits_are_checked_and_saved(self):
        _, errors = saved_settings.validate("code", self.form(code_student_max_open="0"), settings=self.settings)
        self.assertIn("code_student_max_open", errors)
        _, errors = saved_settings.validate("code", self.form(code_student_max_turns="500"), settings=self.settings)
        self.assertIn("code_student_max_turns", errors)
        _, errors = saved_settings.validate("code", self.form(code_student_command_timeout="5"), settings=self.settings)
        self.assertIn("code_student_command_timeout", errors)
        values, errors = saved_settings.validate("code", self.form(code_student_daily_sessions="7"),
                                                 settings=self.settings)
        self.assertEqual(errors, {})
        self.assertEqual(values["code_student_daily_sessions"], 7)

    def test_settings_page_shows_the_student_fields(self):
        admin = self.h.login(ADMIN)
        page = admin.get("/settings").text
        for name in ("code_student_access", "code_student_free_rein", "code_mentor_free_rein",
                     "code_student_max_open", "code_student_daily_sessions"):
            self.assertIn(f'name="{name}"', page)

    def test_every_new_setting_is_on_the_settings_page_and_documented(self):
        names = {f.name for f in saved_settings.SECTIONS["code"][1]}
        for name in ("code_student_access", "code_student_free_rein", "code_mentor_free_rein",
                     "code_sandbox_commands", "code_student_max_open", "code_student_daily_sessions",
                     "code_student_max_turns", "code_student_command_timeout"):
            self.assertIn(name, names)
        doc = (Path(__file__).resolve().parent.parent / "docs" / "SETTINGS.md").read_text(encoding="utf-8")
        for key in ("CODE_STUDENT_ACCESS", "CODE_STUDENT_FREE_REIN", "CODE_MENTOR_FREE_REIN",
                    "CODE_STUDENT_MAX_OPEN", "CODE_STUDENT_DAILY_SESSIONS", "CODE_STUDENT_MAX_TURNS",
                    "CODE_STUDENT_COMMAND_TIMEOUT", "CODE_SANDBOX_COMMANDS"):
            self.assertIn(key, doc)


class MigrationFifteenTest(unittest.TestCase):
    def test_version_14_data_survives_the_rebuild_of_code_sessions(self):
        tmp = Path(tempfile.mkdtemp(prefix="code-migration-"))
        try:
            path = tmp / "old.db"
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode = WAL")
            for script in MIGRATIONS[:14]:
                conn.executescript("BEGIN;" + script + "COMMIT;")
            conn.execute("PRAGMA user_version = 14")
            conn.execute("INSERT INTO users (id, slack_user_id, name, role, created_at) VALUES (1, 'U1', 'Kim', "
                         "'mentor', '2026-01-01T00:00:00+00:00')")
            conn.execute("INSERT INTO code_sessions (id, title, task, status, model, repo, ref, transcript, "
                         "error, input_tokens, output_tokens, created_by, created_at, updated_at) "
                         "VALUES (7, 'Old', 'Old task', "
                         "'done', 'anthropic/claude-sonnet-5-5', 'team/robot-code', '', '[]', NULL, 12, 34, 1, "
                         "'2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00')")
            conn.execute("INSERT INTO code_steps (session_id, turn, kind, tool, input, output, status, created_at) "
                         "VALUES (7, 0, 'note', NULL, NULL, 'Copied 3 files', 'done', '2026-01-01T00:00:00+00:00')")
            conn.execute("INSERT INTO sandbox_sessions (code_session_id, backend, name, state, created_at, expires_at) "
                         "VALUES (7, 'fake', 'szsandbox-7-s000-abc', 'destroyed', '2026-01-01T00:00:00+00:00', "
                         "'2026-01-03T00:00:00+00:00')")
            conn.commit()
            conn.close()

            db = Database(path)
            session = db.one("SELECT * FROM code_sessions WHERE id = 7")
            self.assertEqual((session["title"], session["input_tokens"], session["output_tokens"]), ("Old", 12, 34))
            self.assertEqual((session["owner_is_student"], session["free_rein"], session["patch_released"]), (0, 0, 0))
            self.assertIsNone(session["start_approved_by"])
            self.assertEqual(db.one("SELECT COUNT(*) AS n FROM code_steps WHERE session_id = 7")["n"], 1,
                             "the steps were not cascaded away by the rebuild")
            self.assertEqual(db.one("SELECT code_session_id FROM sandbox_sessions")["code_session_id"], 7)
            with db.connect() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 15)
                indexes = [row["name"] for row in conn.execute("PRAGMA index_list(code_sessions)")]
                self.assertIn("idx_code_sessions_owner", indexes)
                conn.execute("UPDATE code_sessions SET status = 'awaiting_approval' WHERE id = 7")  # now allowed
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("UPDATE code_sessions SET status = 'nonsense' WHERE id = 7")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
