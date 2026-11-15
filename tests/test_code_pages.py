"""The Code pages: sidebar and tab title by role, the Code home and its states, "Needs a mentor", every session state as a
student and as a mentor, the diff panel, the transcript, the audit filters and the mentor-list fallback. Offline: the fake
sandbox with tests/fake_agent_runner.py, and rows written straight into the database for the transcript shapes."""

from __future__ import annotations

import html
import json
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from subzero_ai.code_engine import TOO_BIG_FILE
from subzero_ai.config import load_settings
from subzero_ai.db import Database, utcnow
from subzero_ai.diff_view import parse_changes, parse_file
from subzero_ai.roles import MENTOR_LIST_KEY, Directory
from subzero_ai.slack_api import FakeSlack

from .helpers import ADMIN, CodeStudentTest

HTML = {"Accept": "text/html"}


class PagesTest(CodeStudentTest):
    overrides: dict = {"code_engine": "agent", "code_plan_first_students": False}

    def setUp(self):
        super().setUp()
        self.coach = self.h.login(ADMIN)

    def user_id(self, actor) -> int:
        return actor.user["id"]

    def make_session(self, owner, *, status="done", student=None, task="Fix the arm.", engine="agent") -> int:
        student = owner is not self.kim and owner is not self.coach if student is None else student
        now = utcnow()
        sid = self.db.execute(
            "INSERT INTO code_sessions (title, task, status, model, repo, ref, created_by, created_at, updated_at, "
            "owner_is_student, engine) VALUES (?, ?, ?, 'anthropic/claude-sonnet-5-5', 'team/robot-code', 'main', ?, ?, ?, "
            "?, ?)", (task[:60], task, status, self.user_id(owner), now, now, int(student), engine)).lastrowid
        return sid

    def add_step(self, sid, kind="tool", tool=None, tool_input=None, output="", status="done", use_id=None, **extra):
        return self.db.execute(
            "INSERT INTO code_steps (session_id, turn, kind, tool, tool_use_id, input, output, status, created_at, "
            "reviewed_by, review_note) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, kind, tool, use_id, json.dumps(tool_input) if tool_input is not None else None, output, status,
             utcnow(), extra.get("reviewed_by"), extra.get("review_note"))).lastrowid

    def write_patch(self, sid, patch: str) -> None:
        root = self.code.workspace(sid).root
        (root / "work").mkdir(parents=True, exist_ok=True)
        (root / "agent.patch").write_text(patch, encoding="utf-8")

    def page(self, actor, sid) -> str:
        resp = actor.get(f"/code/{sid}", headers=HTML)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def ask_plan_first(self, actor, task="Fix the arm."):
        resp = actor.post("/code", {"task": task, "plan_first": ["0", "1"]}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        self.h.idle()
        return int(resp.headers["location"].rsplit("/", 1)[1])


class ShellTest(PagesTest):
    def test_each_role_sees_its_own_sidebar(self):
        sid, _ = self.ask(self.ava, "Look at the arm.")
        student = self.ava.get("/about").text
        self.assertIn("New session", student)
        self.assertIn("Look at the arm.", student)
        self.assertIn("Waiting", student, "each session shows its status")
        for absent in ("Needs a mentor", ">Review", "Audit log", "Settings"):
            self.assertNotIn(absent, student)
        mentor = self.kim.get("/about").text
        for present in ("New session", "Needs a mentor", "Review", "Look at the arm."):
            self.assertIn(present, mentor)
        for absent in ("Audit log", "Settings"):
            self.assertNotIn(absent, mentor)
        admin = self.coach.get("/about").text
        for present in ("Needs a mentor", "Review", "Audit log", "Settings", "Look at the arm."):
            self.assertIn(present, admin)
        self.assertNotIn("Look at the arm.", self.ben.get("/about").text, "another student's session isn't listed")

    def test_the_tab_title_shows_the_mentors_waiting_count(self):
        title = lambda actor: re.search(r"<title>(.*?)</title>", actor.get("/about").text).group(1)  # noqa: E731
        self.assertTrue(title(self.kim).startswith("How "))
        self.ask(self.ava, "One.")
        self.ask(self.ben, "Two.")
        self.assertTrue(title(self.kim).startswith("(2) "), title(self.kim))
        self.assertFalse(title(self.ava).startswith("("), "students get no count")
        code_title = re.search(r"<title>(.*?)</title>", self.kim.get("/code").text).group(1)
        self.assertTrue(code_title.startswith("(2) Code"), code_title)


class HomeTest(PagesTest):
    def test_the_home_has_one_box_the_repository_and_a_start_button(self):
        page = self.kim.get("/code").text
        self.assertIn("What should Claude do?", page)
        self.assertIn("team/robot-code", page)
        self.assertIn(">Start<", page.replace("</svg> ", ">").replace("\n", ""))
        self.assertIn('name="model"', page, "mentors pick the model")
        student = self.ava.get("/code").text
        self.assertIn("Send to a mentor", student)
        self.assertNotIn('name="model"', student)
        self.assertNotIn("Needs a mentor</h2>", student)

    def test_students_are_told_when_code_is_off(self):
        self.h.services.settings.code_student_access = False
        page = self.ava.get("/code").text
        self.assertIn("A mentor has to turn Code on for students.", page)
        self.assertIn("To chat with Claude, message the bot in Slack.", page)
        self.assertNotIn('id="code-task"', page)

    def test_sandbox_not_ready_is_a_plain_line_and_admins_get_the_detail(self):
        self.h.services.settings.code_sandbox = "off"
        student = self.ava.get("/code").text
        self.assertIn("Code isn't ready yet", student)
        self.assertNotIn("Settings", student.split("<main")[1])
        self.assertNotIn('id="code-task"', student)
        mentor = self.kim.get("/code").text
        self.assertIn("An admin can fix this in Settings.", mentor)
        self.assertNotIn("needs the sandbox", mentor)
        admin = self.coach.get("/code").text
        self.assertIn("needs the sandbox", admin)
        self.assertIn('href="/settings"', admin)

    def test_no_repository_is_a_plain_line_with_a_settings_link_for_admins(self):
        self.h.services.settings.connector_github_repo = ""
        self.assertIn("No repository is set up yet", self.kim.get("/code").text)
        self.assertNotIn('id="code-task"', self.kim.get("/code").text)
        self.assertIn('href="/settings"', self.coach.get("/code").text)
        self.assertNotIn('href="/settings">Open Settings', self.kim.get("/code").text)

    def test_a_limit_shows_its_reason_and_disables_the_button(self):
        self.ask(self.ava, "First.")
        page = self.ava.get("/code").text
        self.assertIn("You can have 1 Code session open at a time", page)
        self.assertRegex(page, r'<button class="button primary" type="submit" disabled>')
        self.assertIn('id="code-task"', page, "the box stays so nothing is lost")

    def test_plan_first_tick_is_stored_for_students_and_mentors(self):
        self.h.services.settings.code_plan_first_students = True
        page = self.ava.get("/code").text
        self.assertRegex(page, r'name="plan_first" value="1"[^>]*checked')
        self.assertIn("Claude writes a plan and waits for a mentor to approve it before changing anything.", page)
        self.assertNotRegex(self.kim.get("/code").text, r'name="plan_first" value="1"[^>]*checked')
        # ticked or not, what the form sends is what is stored
        yes = self.ask_plan_first(self.ava, "Plan me.")
        self.assertEqual(self.code.get(yes)["plan_first"], 1)
        self.approve(yes)
        self.kim.post(f"/code/{yes}/stop", {})
        self.h.idle()
        no = self.kim.post("/code", {"task": "No plan.", "plan_first": "0"}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.code.get(int(no.headers["location"].rsplit("/", 1)[1]))["plan_first"], 0)
        mentor = self.kim.post("/code", {"task": "Plan please.", "plan_first": ["0", "1"]}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.code.get(int(mentor.headers["location"].rsplit("/", 1)[1]))["plan_first"], 1)

    def test_a_form_without_the_field_gets_the_default(self):
        self.h.services.settings.code_plan_first_mentors = True
        sid, _ = self.ask(self.kim, "Default.")
        self.assertEqual(self.code.get(sid)["plan_first"], 1)

    def test_no_plan_first_tick_for_the_classic_engine(self):
        self.h.services.settings.code_engine = "classic"
        page = self.kim.get("/code").text
        self.assertNotIn('name="plan_first"', page)
        self.assertNotIn("Claude writes a plan", page)
        sid = self.make_session(self.kim, status="done", engine="classic")
        self.kim.post("/code", {"task": "Classic.", "plan_first": ["0", "1"]})
        newest = max(self.code.list(), key=lambda r: r["id"])
        self.assertNotEqual(newest["id"], sid)
        self.assertEqual(newest["plan_first"], 0)


class NeedsMentorTest(PagesTest):
    def test_the_list_holds_requests_follow_ups_plans_and_classic_steps(self):
        asked, _ = self.ask(self.ava, "A request to read.")
        done, _ = self.ask(self.ben, "Make notes.")
        self.approve(done)
        self.ben.post(f"/code/{done}/follow-up", {"text": "Also add a test."})
        planned = self.ask_plan_first(self.kim, "A plan to read.")
        self.h.idle()
        self.assertEqual(self.code.get(planned)["status"], "waiting")
        classic = self.make_session(self.kim, status="waiting", engine="classic", task="A step to read.")
        self.add_step(classic, tool="write_file", tool_input={"path": "a.txt"}, output="+x", status="pending")
        self.add_step(classic, tool="run_command", tool_input={"command": "gradlew build"}, status="pending")
        page = self.kim.get("/code").text
        needs = page.split('id="needs"')[1].split("</section>")[0]
        for text in ("A request to read.", "Student request", "Make notes.", "Student follow-up", "A plan to read.",
                     "Plan to approve", "A step to read.", "2 steps to approve"):
            self.assertIn(text, needs)
        self.assertEqual(self.code.count_waiting(), 4)
        self.assertIn('aria-label="4 waiting"', self.kim.get("/about").text)
        self.assertIn(f'href="/code/{asked}"', needs)
        # the partial the home page redraws
        partial = self.kim.get("/code/needs", headers=HTML)
        self.assertEqual(partial.status_code, 200)
        self.assertIn('data-count="4"', partial.text)

    def test_students_never_see_it_and_cannot_open_the_partial(self):
        self.ask(self.ava, "Hidden from students.")
        page = self.ava.get("/code").text
        self.assertNotIn('id="needs"', page)
        self.assertNotIn("Needs a mentor", page)
        self.assertEqual(self.ava.get("/code/needs").status_code, 403)

    def test_an_empty_list_says_so(self):
        self.assertIn("Nothing is waiting for a mentor right now.", self.kim.get("/code").text)


class SessionStateTest(PagesTest):
    def test_a_waiting_request_for_a_student_and_a_mentor(self):
        sid, _ = self.ask(self.ava, "SCREEN-FLAG Read the arm code.")
        student = self.page(self.ava, sid)
        self.assertIn("A mentor will read your request before Claude starts. You can close this page.", student)
        for hidden in ("Screener", "Approve and start", "Delete", "New engine", "Classic"):
            self.assertNotIn(hidden, student)
        self.assertIn('data-poll="slow"', student, "the page refreshes itself while it waits")
        mentor = self.page(self.kim, sid)
        self.assertIn("Screener: Flagged for testing.", mentor)
        self.assertIn("Approve and start", mentor)
        self.assertIn("Decline", mentor)
        self.assertIn("works in the sandbox without step-by-step clicks", mentor)
        self.assertNotIn("show a plan first", mentor)
        self.assertIn("New engine", mentor)
        self.assertIn('data-poll="slow"', mentor, "a mentor's page polls while waiting too")
        self.assertEqual(self.kim.get(f"/code/{sid}/steps").status_code, 200)
        self.assertEqual(self.ava.get(f"/code/{sid}/steps").status_code, 200)

    def test_a_plan_first_request_tells_the_mentor_a_plan_comes_first(self):
        sid = self.ask_plan_first(self.ava)
        self.assertIn("Claude will show a plan first", self.page(self.kim, sid))

    def test_starting_and_the_queue_note(self):
        sid = self.make_session(self.ava, status="preparing")
        self.assertIn("Starting your sandbox. This can take a minute.", self.page(self.ava, sid))
        self.add_step(sid, kind="note", output="Waiting for a free sandbox. 1 session ahead of you.")
        for actor in (self.ava, self.kim):
            page = self.page(actor, sid)
            self.assertIn("Waiting for a free sandbox. 1 session ahead of you.", page)
            self.assertNotIn("Starting your sandbox", page)
        self.assertIn('data-poll="fast"', self.page(self.ava, sid))

    def test_a_plan_to_approve(self):
        sid = self.ask_plan_first(self.ava)
        self.approve(sid)
        self.assertEqual(self.code.get(sid)["status"], "waiting")
        mentor = self.page(self.kim, sid)
        self.assertIn("Claude's plan", mentor)
        self.assertIn("<li>Look around.</li>", mentor, "the plan is rendered as Markdown")
        self.assertIn("Approve plan", mentor)
        self.assertIn("Decline", mentor)
        student = self.page(self.ava, sid)
        self.assertIn("A mentor is reading Claude's plan.", student)
        self.assertNotIn("Approve plan", student)
        self.assertIn('data-poll="slow"', student)
        self.assertEqual(self.kim.get(f"/code/{sid}/steps").status_code, 200)

    def test_working_shows_stop_to_mentors_and_polls_fast(self):
        resp = self.kim.post("/code", {"task": "AGENT-SLOW take your time"}, follow_redirects=False)
        sid = int(resp.headers["location"].rsplit("/", 1)[1])
        for _ in range(100):
            if self.code.get(sid)["status"] == "running" and sid in self.code.agent._runs:
                break
            time.sleep(0.05)
        self.assertEqual(self.code.get(sid)["status"], "running")
        mentor = self.page(self.kim, sid)
        self.assertIn("Claude is working…", mentor)
        self.assertIn("/stop", mentor)
        self.assertIn('data-poll="fast"', mentor)
        self.assertIn("Lost the connection", mentor, "the note for a dropped connection is on the page, hidden")
        self.assertNotRegex(mentor, r'<textarea id="follow-up"[^>]*disabled', "a mentor can send more while it works")
        self.kim.post(f"/code/{sid}/stop", {})
        self.h.idle()

    def test_done_hides_the_changes_from_a_student_until_release(self):
        sid, _ = self.ask(self.ava, "Make notes.")
        self.approve(sid)
        self.assertEqual(self.code.get(sid)["status"], "done")
        student = self.page(self.ava, sid)
        self.assertIn("A mentor is looking at your changes.", student)
        self.assertNotIn("AGENT_NOTES.md</span>", student)
        self.assertNotIn("Download patch", student)
        self.assertIn('data-poll="slow"', student)
        mentor = self.page(self.kim, sid)
        self.assertIn("AGENT_NOTES.md", mentor)
        self.assertIn("+- Make notes.", mentor)
        for button in ("Release to student", "Download patch", "Delete"):
            self.assertIn(button, mentor)
        self.assertNotIn("Stop</button>", mentor)
        self.kim.post(f"/code/{sid}/release", {})
        student = self.page(self.ava, sid)
        self.assertIn("AGENT_NOTES.md", student)
        self.assertIn("+- Make notes.", student)
        self.assertIn("Download patch", student)
        self.assertNotIn("A mentor is looking at your changes", student)
        self.assertIn('data-poll="off"', student, "nothing left to wait for")
        self.assertNotIn("Release to student", self.page(self.kim, sid))

    def test_the_cost_and_the_cap_are_for_mentors(self):
        sid, _ = self.ask(self.ava, "Cost me.")
        self.approve(sid)
        self.assertIn("$0.01 of $5.00", self.page(self.kim, sid))
        self.assertNotIn("$0.01", self.page(self.ava, sid))
        self.h.services.settings.code_auth = "subscription"
        self.assertIn("about $0.01 at API prices", self.page(self.kim, sid))

    def test_a_declined_request_for_a_student_and_a_mentor(self):
        sid, _ = self.ask(self.ava)
        self.decline(sid, "Pick a smaller task first.")
        student = self.page(self.ava, sid)
        self.assertIn("A mentor declined this request", student)
        self.assertIn("Pick a smaller task first.", student)
        self.assertIn("ask again with more detail", student)
        self.assertIn('data-poll="off"', student)
        self.assertIn("Nothing was started", self.page(self.kim, sid))

    def test_a_failed_session_shows_a_friendly_reason_and_hides_raw_errors_from_students(self):
        sid, _ = self.ask(self.ava, "AGENT-FAIL")
        self.approve(sid)
        self.assertEqual(self.code.get(sid)["status"], "failed")
        student = self.page(self.ava, sid)
        self.assertIn("Claude couldn&#39;t finish this task", student)
        self.assertIn("a mentor can send a follow-up to try again", student.lower())
        for hidden in ("could not finish the task", "Details for mentors", "helper program"):
            self.assertNotIn(hidden, student)
        mentor = self.page(self.kim, sid)
        self.assertIn("could not finish the task", mentor)
        self.assertIn("Details for mentors", mentor)
        self.assertIn("<details", mentor.split("Details for mentors")[0].rsplit("<", 1)[0] + "<details")

    def test_a_classic_failure_keeps_the_raw_error_under_a_fold_for_mentors_only(self):
        sid = self.make_session(self.ava, status="failed", engine="classic")
        self.db.execute("UPDATE code_sessions SET error = ? WHERE id = ?", ("KeyError: 'secret detail'", sid))
        self.assertNotIn("secret detail", self.page(self.ava, sid))
        mentor = self.page(self.kim, sid)
        self.assertIn("secret detail", html.unescape(mentor))
        self.assertIn("Details for mentors", mentor)

    def test_a_student_sees_only_their_own_sessions(self):
        sid, _ = self.ask(self.ava, "Mine.")
        self.assertEqual(self.ben.get(f"/code/{sid}").status_code, 404)
        self.assertEqual(self.ben.get(f"/code/{sid}/steps").status_code, 404)
        self.assertNotIn("Mine.", self.ben.get("/code").text)
        for path in ("/stop", "/release", "/delete", "/approve-request"):
            self.assertEqual(self.ava.post(f"/code/{sid}{path}", {}).status_code, 403)

    def test_a_student_follow_up_waiting_is_shown_and_the_screener_note_stays_hidden(self):
        sid, _ = self.ask(self.ava, "Make notes.")
        self.approve(sid)
        self.ava.post(f"/code/{sid}/follow-up", {"text": "SCREEN-FLAG add a test"})
        student = self.page(self.ava, sid)
        self.assertIn("Waiting for a mentor to approve your follow-up", student)
        self.assertNotIn("Screener", student)
        self.assertIn("Screener: Flagged for testing.", self.page(self.kim, sid))
        self.assertIn("Send to Claude", self.page(self.kim, sid))

    def test_the_session_page_keeps_the_follow_up_box_outside_the_part_that_refreshes(self):
        sid, _ = self.ask(self.ava, "Make notes.")
        self.approve(sid)
        partial = self.kim.get(f"/code/{sid}/steps").text
        self.assertNotIn('id="follow-up"', partial)
        self.assertIn("data-composer-state", partial)
        page = self.page(self.kim, sid)
        self.assertEqual(page.count('id="follow-up"'), 1)
        self.assertLess(page.index("data-code-region"), page.index('id="follow-up"'))

    def test_the_release_is_refused_when_the_patch_was_too_big(self):
        sid, _ = self.ask(self.ava, "Make notes.")
        self.approve(sid)
        (self.code.workspace(sid).root / TOO_BIG_FILE).write_text("x")
        from subzero_ai.code_runner import CodeError
        from subzero_ai.store import Viewer
        mentor = self.h.services.store.user(self.user_id(self.kim))
        viewer = Viewer(id=mentor["id"], slack_user_id=mentor["slack_user_id"], name=mentor["name"], role="mentor")
        with self.assertRaises(CodeError) as caught:
            self.code.release_patch(viewer, sid)
        self.assertIn("too big", str(caught.exception))
        page = self.page(self.kim, sid)
        self.assertIn("too big", page)
        self.assertNotIn("Release to student", page)
        self.assertNotIn("Download patch", page)
        self.assertEqual(self.code.get(sid)["patch_released"], 0)


class DiffPanelTest(PagesTest):
    PATCH = (
        "diff --git a/src/Arm.java b/src/Arm.java\nindex 1..2 100644\n--- a/src/Arm.java\n+++ b/src/Arm.java\n"
        "@@ -1,3 +1,3 @@\n class Arm {\n-  double kP = 0.5;\n+  double kP = 0.2; // <script>alert(1)</script>\n }\n"
        "diff --git a/docs/new.md b/docs/new.md\nnew file mode 100644\nindex 0..1\n--- /dev/null\n+++ b/docs/new.md\n"
        "@@ -0,0 +1,2 @@\n+# Notes\n+hello\n"
        "diff --git a/old.txt b/gone.txt\nsimilarity index 100%\nrename from old.txt\nrename to gone.txt\n"
        "diff --git a/logo.png b/logo.png\nnew file mode 100644\nindex 0..1\nBinary files /dev/null and b/logo.png differ\n"
        "diff --git a/dead.txt b/dead.txt\ndeleted file mode 100644\nindex 1..0\n--- a/dead.txt\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-bye\n"
    )

    def test_the_panel_draws_adds_removes_renames_and_binary_files(self):
        sid = self.make_session(self.ava, status="done")
        self.write_patch(sid, self.PATCH)
        page = self.page(self.kim, sid)
        self.assertIn("src/Arm.java", page)
        self.assertIn('<span class="del">-  double kP = 0.5;</span>', page)
        self.assertIn('<span class="add">+# Notes</span>', page)
        self.assertIn("New file", page)
        self.assertIn("Renamed from old.txt", page)
        self.assertIn("gone.txt", page)
        self.assertIn("New binary file", page)
        self.assertIn("logo.png", page)
        self.assertNotIn("Binary files /dev/null", page, "a binary file is named only")
        self.assertIn("Deleted", page)
        self.assertIn("5 files changed", page)

    def test_diff_text_is_escaped(self):
        sid = self.make_session(self.ava, status="done")
        self.write_patch(sid, self.PATCH)
        for actor in (self.kim,):
            page = self.page(actor, sid)
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_a_student_sees_the_diff_only_after_release(self):
        sid = self.make_session(self.ava, status="done")
        self.write_patch(sid, self.PATCH)
        self.assertNotIn("src/Arm.java", self.page(self.ava, sid))
        self.assertNotIn("double kP", self.ava.get(f"/code/{sid}/steps").text)
        self.kim.post(f"/code/{sid}/release", {})
        page = self.page(self.ava, sid)
        self.assertIn("src/Arm.java", page)
        self.assertIn("double kP = 0.2;", page)
        self.assertEqual(self.ava.get(f"/code/{sid}/changes.patch").status_code, 200)

    def test_a_long_diff_starts_folded_and_is_cut(self):
        lines = "".join(f"+line {i}\n" for i in range(900))
        patch = f"diff --git a/big.txt b/big.txt\nnew file mode 100644\n--- /dev/null\n+++ b/big.txt\n@@ -0,0 +1,900 @@\n{lines}"
        [entry], more = parse_changes([{"name": "big.txt", "kind": "added", "diff": patch}])
        self.assertTrue(entry.folded)
        self.assertEqual((entry.added, len(entry.lines), entry.cut > 0, more), (900, 600, True, 0))
        sid = self.make_session(self.ava, status="done")
        self.write_patch(sid, patch)
        page = self.page(self.kim, sid)
        self.assertRegex(page, r'<details class="diff-file">\s*<summary>')
        self.assertIn("not shown here. The download has everything.", page)

    def test_a_removed_line_that_looks_like_a_header_is_still_a_removed_line(self):
        entry = parse_file({"name": "q.sql", "kind": "changed", "diff":
                            "diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,1 @@\n--- a comment\n keep\n"})
        self.assertEqual((entry.removed, entry.added), (1, 0))

    def test_classic_sessions_show_their_changes_too(self):
        sid = self.make_session(self.kim, status="done", engine="classic")
        work = self.code.workspace(sid)
        (work.base).mkdir(parents=True)
        (work.work).mkdir(parents=True)
        (work.base / "a.txt").write_text("one\n")
        (work.work / "a.txt").write_text("two\n")
        page = self.page(self.kim, sid)
        self.assertIn('<span class="add">+two</span>', page)


class TranscriptTest(PagesTest):
    def render(self, steps, actor=None) -> str:
        sid = self.make_session(self.kim, status="done")
        for step in steps:
            self.add_step(sid, **step)
        return self.page(actor or self.kim, sid)

    def test_each_kind_of_step_has_its_own_look(self):
        page = self.render([
            {"tool": "Bash", "tool_input": {"command": "gradlew build", "description": "Build it"},
             "output": "BUILD OK\n" + "\n".join(f"line {i}" for i in range(40))},
            {"tool": "Read", "tool_input": {"file_path": "src/Robot.java"}, "output": "secret file text"},
            {"tool": "Glob", "tool_input": {"pattern": "**/*.java"}, "output": "a.java"},
            {"tool": "Edit", "tool_input": {"file_path": "src/Arm.java", "old_string": "kP = 1", "new_string": "kP = 2"}},
            {"tool": "Write", "tool_input": {"file_path": "NOTES.md", "content": "hello\nworld\n"}},
            {"tool": "WebSearch", "tool_input": {"query": "wpilib pid"}},
            {"tool": "WebFetch", "tool_input": {"url": "https://docs.wpilib.org/pid"}},
            {"tool": "Mystery", "tool_input": {"x": 1}, "output": "odd"},
            {"kind": "note", "output": "Copied 12 files."},
            {"kind": "text", "output": "All **done**."},
        ])
        self.assertIn('<span class="code-prompt" aria-hidden="true">$ </span>gradlew build', page)
        self.assertIn("Build it", page)
        self.assertIn("<summary>Output</summary>", page)
        self.assertIn("Show more", page, "a long output is cut")
        self.assertIn("Read src/Robot.java", page)
        self.assertNotIn("secret file text", page, "a read is one line, no output")
        self.assertIn("Searched for **/*.java", page)
        self.assertIn('<span class="del">- kP = 1</span>', page)
        self.assertIn('<span class="add">+ kP = 2</span>', page)
        self.assertIn("Wrote NOTES.md", page)
        self.assertIn("<summary>The file (2 lines)</summary>", page)
        self.assertIn("Searched the web for wpilib pid", page)
        self.assertIn("Read a web page: https://docs.wpilib.org/pid", page)
        self.assertNotIn('href="https://docs.wpilib.org', page, "no link out of the page")
        self.assertIn("Used Mystery", page)
        self.assertIn("What it was given", page)
        self.assertIn('<p class="code-note">Copied 12 files.</p>', page)
        self.assertIn("All <strong>done</strong>.", page)

    def test_a_helper_holds_its_own_steps(self):
        page = self.render([
            {"tool": "Agent", "tool_input": {"description": "Explore the drive code"}, "output": "Found it.",
             "use_id": "toolu_1"},
            {"tool": "Read", "tool_input": {"file_path": "Drive.java", "_parent": "toolu_1"}},
            {"tool": "Grep", "tool_input": {"pattern": "kP", "_parent": "toolu_1"}},
            {"tool": "Read", "tool_input": {"file_path": "Outside.java"}},
        ])
        self.assertIn("Helper: Explore the drive code", page)
        self.assertIn("2 steps", page)
        group = page.split('class="code-agent"')[1].split("</details>")[0]
        self.assertIn("Read Drive.java", group)
        self.assertIn("Searched for kP", group)
        self.assertNotIn("Outside.java", group)
        self.assertIn("Read Outside.java", page)
        self.assertNotIn("_parent", page)

    def test_the_task_list_updates_in_place(self):
        sid = self.make_session(self.kim, status="running")
        step = self.add_step(sid, tool="todo", tool_input={"items": [
            {"content": "Look around", "status": "in_progress"}, {"content": "Write notes", "status": "pending"}]})
        first = self.page(self.kim, sid)
        self.assertIn("Look around", first)
        self.assertRegex(first, r'<li class="in_progress">')
        self.db.execute("UPDATE code_steps SET input = ? WHERE id = ?", (json.dumps({"items": [
            {"content": "Look around", "status": "completed"}, {"content": "Write notes", "status": "in_progress"}]}),
            step))
        second = self.page(self.kim, sid)
        self.assertRegex(second, r'<li class="completed">')
        self.assertEqual(second.count("Look around"), 1, "one list, not a new one")
        self.assertIn("(completed)", second)

    def test_a_plan_is_a_highlighted_card_and_a_mentors_note_is_folded(self):
        sid = self.make_session(self.kim, status="done")
        self.add_step(sid, tool="plan", tool_input={"plan": "1. Look\n2. **Fix** it"}, status="approved",
                      reviewed_by=self.user_id(self.kim))
        self.add_step(sid, kind="note", tool="mentor", output="The helper program reported a problem: boom")
        page = self.page(self.kim, sid)
        self.assertIn('class="code-step plan approved"', page)
        self.assertIn("<strong>Fix</strong>", page)
        self.assertIn("Approved by Mentor Kim", page)
        self.assertIn("<summary>Details for mentors</summary>", page)

    def test_nothing_from_the_model_is_raw_html(self):
        evil = "<img src=x onerror=alert(1)><script>alert(2)</script>"
        page = self.render([
            {"kind": "text", "output": evil},
            {"tool": "Bash", "tool_input": {"command": evil}, "output": evil},
            {"tool": "Edit", "tool_input": {"file_path": evil, "old_string": evil, "new_string": evil}},
            {"tool": "Write", "tool_input": {"file_path": evil, "content": evil}},
            {"tool": "Odd", "tool_input": {"x": evil}, "output": evil},
            {"tool": "todo", "tool_input": {"items": [{"content": evil, "status": "pending"}]}},
            {"tool": "plan", "tool_input": {"plan": evil}, "status": "approved"},
            {"kind": "note", "output": evil},
        ])
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;alert(2)", page)

    def test_steps_polling_partial_works_in_every_state(self):
        for status in ("awaiting_approval", "preparing", "running", "waiting", "done", "stopped", "failed"):
            sid = self.make_session(self.ava, status=status)
            for actor in (self.ava, self.kim):
                resp = actor.get(f"/code/{sid}/steps", headers=HTML)
                self.assertEqual(resp.status_code, 200, status)
                self.assertIn("data-code-live", resp.text)
                self.assertNotIn("<html", resp.text, "just the part that refreshes")
        sid = self.make_session(self.ava, status="done")
        self.assertIn('data-poll="off"', self.ava.get(f"/code/{sid}/steps").text)


class AuditFilterTest(PagesTest):
    def test_filters_by_who_what_and_date_and_pages(self):
        self.ask(self.ava, "One.")
        self.ask(self.ben, "Two.")
        everything = self.coach.get("/audit").text
        self.assertIn("code student requested", everything)
        who = self.coach.get(f"/audit?who={self.user_id(self.ava)}").text
        self.assertIn("Ava (student)", who.split("<tbody>")[1])
        self.assertNotIn("Ben (student)", who.split("<tbody>")[1])
        what = self.coach.get("/audit?what=signed_in").text.split("<tbody>")[1]
        self.assertIn("signed in", what)
        self.assertNotIn("code student requested", what)
        today = datetime.now(self.h.services.store.tz).date()
        tomorrow = (today + timedelta(days=1)).isoformat()
        self.assertIn("Nothing matches those filters.", self.coach.get(f"/audit?from={tomorrow}").text)
        self.assertIn("code student requested", self.coach.get(f"/audit?from={today}&to={today}").text)
        self.assertIn("Nothing matches those filters.",
                      self.coach.get(f"/audit?to={(today - timedelta(days=1)).isoformat()}").text)
        self.assertEqual(self.coach.get("/audit?from=not-a-date&page=zzz").status_code, 200)
        self.assertEqual(self.ava.get("/audit").status_code, 403)

    def test_paging_keeps_the_filters(self):
        for i in range(130):
            self.db.audit("sample_action", actor_id=self.user_id(self.kim), details={"n": i})
        page = self.coach.get("/audit?what=sample_action").text
        self.assertIn("130 entries match", page)
        self.assertIn("Page 1 of 2", page)
        self.assertIn('href="/audit?what=sample_action&amp;page=2"', page)
        second = self.coach.get("/audit?what=sample_action&page=2").text
        self.assertIn("Page 2 of 2", second)
        self.assertIn('href="/audit?what=sample_action&amp;page=1"', second)


class AboutAndProfileTest(PagesTest):
    def test_about_describes_code_and_names_no_team(self):
        page = self.ava.get("/about").text
        for text in ("What Code does", "What a mentor approves", "What the sandbox is", "patch", "message the bot in Slack",
                     "Mentors can read every Slack chat", "Powered by Claude"):
            self.assertIn(text, page)
        self.assertNotIn("Claude Code", page)

    def test_profile_has_account_and_appearance_only(self):
        page = self.ava.get("/profile").text
        self.assertIn("Your account", page)
        self.assertIn("Appearance", page)
        self.assertNotIn("Today's messages", page)
        self.assertNotIn("Approved today", page)


class MentorListFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = load_settings(search_cwd=False, data_dir=Path(self.tmp.name), slack_approvals_channel="CAPPROVALS",
                                      admin_slack_ids=("UADMIN",))
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.slack = FakeSlack()
        self.slack.members = {"UMENTOR1", "UMENTOR2"}

    def directory(self) -> Directory:  # a new one each time is a restart of the app
        return Directory(self.settings, self.slack, db=self.db)

    def broken(self):
        async def fail(channel):
            raise RuntimeError("slack is down")
        self.slack.channel_members = fail

    def run_async(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_a_good_read_is_saved(self):
        d = self.directory()
        self.assertEqual(self.run_async(d.role_for("UMENTOR1")), "mentor")
        saved = json.loads(self.db.get_kv(MENTOR_LIST_KEY))
        self.assertEqual(saved["ids"], ["UMENTOR1", "UMENTOR2"])
        self.assertIsNone(d.stale_warning())

    def test_after_a_restart_with_slack_down_mentors_keep_their_role(self):
        self.run_async(self.directory().role_for("UMENTOR1"))
        self.broken()
        d = self.directory()
        self.assertEqual(self.run_async(d.role_for("UMENTOR1")), "mentor")
        self.assertEqual(self.run_async(d.role_for("USTUDENT")), "student", "nobody new becomes a mentor")
        self.assertEqual(self.run_async(d.role_for("UADMIN")), "admin")
        self.assertIn("Slack didn't answer", d.stale_warning())
        self.assertIn("saved", d.stale_warning())

    def test_a_list_older_than_a_week_is_not_used(self):
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
        self.db.set_kv(MENTOR_LIST_KEY, json.dumps({"ids": ["UMENTOR1"], "at": old}))
        self.broken()
        d = self.directory()
        self.assertEqual(self.run_async(d.role_for("UMENTOR1")), "student")
        self.assertEqual(self.run_async(d.role_for("UADMIN")), "admin", "admins don't depend on the list")
        self.assertIn("only admins can act as mentors", d.stale_warning())

    def test_no_saved_list_and_slack_down_means_no_mentors(self):
        self.broken()
        d = self.directory()
        self.assertEqual(self.run_async(d.role_for("UMENTOR1")), "student")
        self.assertIsNotNone(d.stale_warning())

    def test_the_warning_goes_away_when_slack_answers_again(self):
        self.run_async(self.directory().role_for("UMENTOR1"))
        working = self.slack.channel_members
        self.broken()
        d = self.directory()
        self.run_async(d.role_for("UMENTOR1"))
        self.assertIsNotNone(d.stale_warning())
        self.slack.channel_members = working
        self.slack.members = {"UMENTOR2"}
        self.assertEqual(self.run_async(d.role_for("UMENTOR1", force=True)), "student")
        self.assertIsNone(d.stale_warning())

    def test_a_broken_saved_list_is_ignored(self):
        self.db.set_kv(MENTOR_LIST_KEY, "not json")
        self.broken()
        self.assertEqual(self.run_async(self.directory().role_for("UMENTOR1")), "student")


if __name__ == "__main__":
    unittest.main()
