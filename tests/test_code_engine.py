"""The new Code engine (code_engine.py) end to end, with the fake sandbox and tests/fake_agent_runner.py: nothing needs the
network or a key. Also the migration that adds the engine's columns, the Settings page, and the gateway's callbacks."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from subzero_ai import saved_settings
from subzero_ai.anthropic_gateway import Usage
from subzero_ai.code_engine import AgentEngine, cap_text, patch_script
from subzero_ai.config import Settings
from subzero_ai.db import MIGRATIONS, Database
from subzero_ai.pi_client import bridge_credentials_path
from subzero_ai.prompts import load_safety_prompt
from subzero_ai.sandbox import HyperVSandbox, SandboxError, SandboxHandle, SandboxStream, sandbox_address

from .helpers import ADMIN, MENTOR, STUDENT, CodeStudentTest, Harness

HTML = {"Accept": "text/html"}


def wait_for(check, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


class AgentTest(CodeStudentTest):
    """Sessions on the new engine. `self.sent` is every line the app wrote to a runner, in order."""

    overrides: dict = {"code_engine": "agent", "code_plan_first_students": False}

    def setUp(self):
        super().setUp()
        self.engine: AgentEngine = self.code.agent
        self.sent: list[dict] = []
        self.live: list[int] = []
        original = SandboxStream.write_line

        def spy(stream, text):
            self.sent.append(json.loads(text))
            return original(stream, text)

        patcher = mock.patch.object(SandboxStream, "write_line", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for sid in self.live:  # sessions a test left working must end before the harness goes away
            self.kim.post(f"/code/{sid}/stop", {}, follow_redirects=False)
        if self.live:
            self.h.idle()
        super().tearDown()

    # ---- helpers
    def mentor_start(self, task: str = "Write some notes.") -> int:
        resp = self.kim.post("/code", {"task": task}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        self.h.idle()
        return int(resp.headers["location"].rsplit("/", 1)[1])

    def row(self, sid: int):
        return self.code.get(sid)

    def kinds(self, sid: int) -> list[tuple[str, str | None]]:
        return [(s["kind"], s["tool"]) for s in self.code.steps(sid)]

    def work_file(self, sid: int, name: str = "AGENT_NOTES.md") -> Path:
        return self.root / "x"  # replaced below when a sandbox exists

    def sandbox_dir(self, sid: int) -> Path | None:
        found = sorted(self.root.glob(f"szsandbox-{sid}-*")) if self.root.exists() else []
        return found[0] if found else None

    def starts(self) -> list[dict]:
        return [m for m in self.sent if m["type"] == "start"]

    def post_follow_up(self, actor, sid: int, text: str):
        resp = actor.post(f"/code/{sid}/follow-up", {"text": text}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        self.h.idle()

    def live_run(self, sid: int):
        return self.engine._runs.get(sid)


class MentorSessionTest(AgentTest):
    def test_a_mentor_session_runs_end_to_end(self):
        sid = self.mentor_start("Write some notes.")
        row = self.row(sid)
        self.assertEqual((row["engine"], row["status"], row["error"]), ("agent", "done", None))
        self.assertEqual(row["agent_session_id"].startswith("fake-session-"), True)
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (1200, 300))
        self.assertAlmostEqual(row["cost_usd"], 0.01)
        steps = self.code.steps(sid)
        self.assertEqual([(s["kind"], s["tool"]) for s in steps],
                         [("note", None), ("note", None), ("text", None), ("tool", "Bash"), ("tool", "Write"),
                          ("text", None)])
        self.assertEqual({s["status"] for s in steps}, {"done"})
        self.assertEqual({s["turn"] for s in steps if s["kind"] != "note"}, {0})
        self.assertIn("build.gradle", steps[3]["output"])  # `ls` ran inside the sandbox's copy of the repository
        self.assertEqual(steps[1]["output"][:21], "Made a sandbox for th")
        patch = self.kim.get(f"/code/{sid}/changes.patch")
        self.assertEqual(patch.status_code, 200)
        self.assertIn("+++ b/AGENT_NOTES.md", patch.text)
        self.assertIn("+- Write some notes.", patch.text)
        self.assertNotIn("Robot.java", patch.text)
        page = self.kim.get(f"/code/{sid}").text
        self.assertIn("AGENT_NOTES.md", page)
        self.assertIn("Ran a command", page)
        # one runner started, with what the protocol asks for
        [start] = self.starts()
        self.assertEqual((start["task"], start["plan_first"], start["resume"], start["max_turns"]),
                         ("Write some notes.", False, None, 200))
        self.assertTrue(start["allowed_web"] and start["sub_agents"])
        self.assertEqual(start["model"], "claude-sonnet-5-5")  # the bare Anthropic id
        self.assertTrue(Path(start["cwd"]).is_dir() and start["cwd"].endswith("/work"))

    def test_the_idle_runner_stays_until_closed_and_then_the_sandbox_is_deleted(self):
        sid = self.mentor_start()
        self.assertIsNotNone(self.live_run(sid))
        self.assertIsNotNone(self.manager.live_for_session(sid))
        run = self.live_run(sid)
        self.h.call(self.engine._close_and_settle, sid, run)
        self.assertIsNone(self.manager.live_for_session(sid))
        self.assertNotIn(sid, self.engine._runs)
        self.assertTrue((self.root.parent / "code" / str(sid) / "agent.patch").is_file(), "the patch was kept")

    def test_an_idle_runner_is_closed_after_the_idle_time(self):
        self.engine.idle_seconds = 0.3
        sid = self.mentor_start()
        self.assertTrue(wait_for(lambda: sid not in self.engine._runs and self.manager.live_for_session(sid) is None))
        self.h.idle()
        self.assertEqual(self.row(sid)["status"], "done")

    def test_the_refusal_without_a_sandbox_is_plain_and_the_page_says_so(self):
        self.h.services.settings.code_sandbox = "off"
        resp = self.kim.post("/code", {"task": "Try."}, follow_redirects=True, headers=HTML)
        self.assertIn("switch to the classic engine", resp.text)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM code_sessions")["n"], 0)
        self.assertNotIn("The new Code engine needs the sandbox", self.kim.get("/code").text, "the detail is for admins")
        page = self.h.login(ADMIN).get("/code").text
        self.assertIn("The new Code engine needs the sandbox", page)
        self.assertNotIn('id="code-task"', page, "no broken form")
        self.assertEqual(self.sent, [])

    def test_a_missing_tools_image_fails_with_the_admins_message(self):
        self.manager.backend("fake").tools = "missing"
        sid = self.mentor_start()
        row = self.row(sid)
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("prepare-sandbox-image.cmd", row["error"], "the student-facing text is a plain sentence")
        self.assertIn("An admin needs to finish setting up the sandbox image", row["error"])
        notes = [s["output"] for s in self.code.steps(sid) if s["tool"] == "mentor"]
        self.assertTrue(any("prepare-sandbox-image.cmd" in n for n in notes), notes)
        self.assertEqual(self.sent, [], "nothing reached a runner")
        self.assertIsNone(self.manager.live_for_session(sid), "the sandbox was deleted")

    def test_plain_failure_keeps_the_raw_error_for_mentors_only(self):
        sid = self.mentor_start("AGENT-FAIL please")
        row = self.row(sid)
        self.assertEqual(row["status"], "failed")
        self.assertIn("Claude couldn't finish this task", row["error"])
        self.assertNotIn("could not finish the task", row["error"])
        self.assertIn("The agent could not finish the task.", self.kim.get(f"/code/{sid}").text)
        self.assertEqual(self.engine._slots, set())

    def test_a_crashing_runner_fails_the_session_and_frees_the_place(self):
        sid = self.mentor_start("AGENT-CRASH now")
        row = self.row(sid)
        self.assertEqual(row["status"], "failed")
        self.assertIn("stopped unexpectedly", row["error"])
        self.assertEqual(self.engine._slots, set())
        self.assertNotIn(sid, self.engine._runs)
        self.assertIsNone(self.manager.live_for_session(sid))
        self.assertTrue(all(s["status"] != "running" for s in self.code.steps(sid)))
        again = self.mentor_start("A second, normal session.")  # the place really is free
        self.assertEqual(self.row(again)["status"], "done")

    def test_stop_ends_a_slow_run_promptly(self):
        resp = self.kim.post("/code", {"task": "AGENT-SLOW take your time"}, follow_redirects=False)
        sid = int(resp.headers["location"].rsplit("/", 1)[1])
        self.assertTrue(wait_for(lambda: self.live_run(sid) is not None and self.live_run(sid).driving))
        began = time.monotonic()
        self.assertEqual(self.kim.post(f"/code/{sid}/stop", {}, follow_redirects=False).status_code, 303)
        self.h.idle()
        self.assertLess(time.monotonic() - began, 8)
        self.assertEqual(self.row(sid)["status"], "stopped")
        self.assertEqual(self.engine._slots, set())
        self.assertIsNone(self.manager.live_for_session(sid))
        self.assertIn("code_session_stopped", self.actions())

    def test_todo_helper_and_compact_events_become_the_right_steps(self):
        sid = self.mentor_start("AGENT-TODO AGENT-SUB AGENT-COMPACT")
        steps = self.code.steps(sid)
        todos = [s for s in steps if s["tool"] == "todo"]
        self.assertEqual(len(todos), 1)
        items = json.loads(todos[0]["input"])["items"]
        self.assertEqual([i["content"] for i in items], ["Look around", "Write notes"])
        [helper] = [s for s in steps if s["tool"] == "Agent"]
        [child] = [s for s in steps if s["tool"] == "Read"]
        self.assertEqual(json.loads(child["input"])["_parent"], helper["tool_use_id"])
        self.assertNotIn("_parent", json.loads(helper["input"]))
        self.assertTrue(any(s["kind"] == "note" and s["output"] == "Claude summarised the earlier work to keep going."
                            for s in steps))
        self.assertFalse([s for s in steps if s["tool"] == "TodoWrite"])
        page = self.kim.get(f"/code/{sid}").text
        self.assertIn("Task list", page)
        self.assertIn("Helper: ", page)
        self.assertEqual(self.kim.get(f"/code/{sid}/steps").status_code, 200)


class FollowUpTest(AgentTest):
    def test_a_follow_up_on_a_live_runner_sends_a_message(self):
        sid = self.mentor_start("First job.")
        run = self.live_run(sid)
        self.post_follow_up(self.kim, sid, "Second job.")
        self.assertIs(self.live_run(sid), run, "the same runner took it")
        self.assertEqual([m["type"] for m in self.sent], ["start", "message"])
        self.assertEqual(self.sent[1]["text"], "Second job.")
        self.assertEqual(self.row(sid)["status"], "done")
        steps = self.code.steps(sid)
        self.assertEqual(max(s["turn"] for s in steps), 1)
        patch = self.kim.get(f"/code/{sid}/changes.patch").text
        self.assertIn("+- First job.", patch)
        self.assertIn("+- Second job.", patch)
        self.assertIn("code_follow_up", self.actions())
        self.assertAlmostEqual(self.row(sid)["cost_usd"], 0.02)

    def test_a_follow_up_after_the_runner_was_closed_resumes_the_conversation_in_a_new_sandbox(self):
        sid = self.mentor_start("First job.")
        resume = self.row(sid)["agent_session_id"]
        first_box = self.manager.live_for_session(sid)["name"]
        self.h.call(self.engine._close_and_settle, sid, self.live_run(sid))
        self.assertIsNone(self.manager.live_for_session(sid))
        self.post_follow_up(self.kim, sid, "Second job.")
        starts = self.starts()
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[1]["resume"], resume)
        self.assertEqual(starts[1]["task"], "Second job.")
        self.assertNotEqual(self.manager.live_for_session(sid)["name"], first_box)
        # The new sandbox started from the saved files, and the base commit survived: the patch has both jobs
        patch = self.kim.get(f"/code/{sid}/changes.patch").text
        self.assertIn("+- First job.", patch)
        self.assertIn("+- Second job.", patch)
        self.assertEqual(self.row(sid)["status"], "done")

    def test_a_follow_up_sent_while_a_turn_is_running_is_queued_by_the_runner(self):
        resp = self.kim.post("/code", {"task": "AGENT-SLOW first"}, follow_redirects=False)
        sid = int(resp.headers["location"].rsplit("/", 1)[1])
        self.assertTrue(wait_for(lambda: self.live_run(sid) is not None and self.live_run(sid).driving))
        self.kim.post(f"/code/{sid}/follow-up", {"text": "Then this."}, follow_redirects=False)
        self.assertEqual([m["type"] for m in self.sent], ["start", "message"])
        self.assertEqual(self.row(sid)["status"], "running")
        self.kim.post(f"/code/{sid}/stop", {}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.row(sid)["status"], "stopped")

    def test_the_cap_ends_a_run_and_a_raised_cap_lets_a_follow_up_carry_on(self):
        self.h.services.settings.code_cap_mentor_usd = 0.01  # the stand-in runner's turn costs exactly this
        sid = self.mentor_start("AGENT-FAIL over budget")
        row = self.row(sid)
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["error"], "This session reached its spending cap of $0.01. A mentor can raise the cap in "
                                       "Settings.")
        self.assertEqual(cap_text(5), "This session reached its spending cap of $5.00. A mentor can raise the cap in "
                                      "Settings.")
        resp = self.kim.post(f"/code/{sid}/follow-up", {"text": "More."}, follow_redirects=True, headers=HTML)
        self.assertIn("spending cap", resp.text)
        self.assertEqual(len(self.starts()), 1)
        self.h.services.settings.code_cap_mentor_usd = 5
        self.post_follow_up(self.kim, sid, "More.")
        self.assertEqual(self.row(sid)["status"], "done")
        self.assertEqual(self.row(sid)["cap_usd"], 5)

    def test_restart_recovery_marks_the_session_and_a_follow_up_resumes_it(self):
        sid = self.mentor_start("First job.")
        resume = self.row(sid)["agent_session_id"]
        self.h.call(self.engine._close_and_settle, sid, self.live_run(sid))
        self.db.execute("UPDATE code_sessions SET status = 'running' WHERE id = ?", (sid,))
        from starlette.testclient import TestClient
        from subzero_ai.web import create_app
        with TestClient(create_app(self.h.settings, slack=self.h.slack, claude=self.h.claude, background=False)):
            pass  # starting the app again runs the start-up reset
        row = self.row(sid)
        self.assertEqual(row["status"], "failed")
        self.assertIn("Send a follow-up", row["error"])
        self.assertIn("The app restarted. Send a follow-up to continue.", [s["output"] for s in self.code.steps(sid)])
        self.post_follow_up(self.kim, sid, "Carry on.")
        self.assertEqual(self.row(sid)["status"], "done")
        self.assertEqual(self.starts()[-1]["resume"], resume)


class PlanFirstTest(AgentTest):
    overrides: dict = {"code_engine": "agent", "code_plan_first_students": False, "code_plan_first_mentors": True}

    def plan_step(self, sid: int):
        [step] = [s for s in self.code.steps(sid) if s["tool"] == "plan"]
        return step

    def test_the_plan_waits_for_a_mentor_and_approval_lets_the_work_happen(self):
        sid = self.mentor_start("Make notes.")
        self.assertEqual(self.row(sid)["plan_first"], 1)
        self.assertEqual(self.row(sid)["status"], "waiting")
        step = self.plan_step(sid)
        self.assertEqual(step["status"], "pending")
        self.assertIn("Write notes to AGENT_NOTES.md", json.loads(step["input"])["plan"])
        self.assertEqual(self.engine._slots, set(), "waiting on a plan holds no place")
        self.assertEqual(self.starts()[0]["plan_first"], True)
        box = self.sandbox_dir(sid)
        self.assertFalse((box / "work" / "AGENT_NOTES.md").exists(), "nothing changed before approval")
        page = self.kim.get(f"/code/{sid}").text
        self.assertIn("Approve plan", page)
        self.kim.post(f"/code/steps/{step['id']}/approve", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.row(sid)["status"], "done")
        self.assertEqual(self.plan_step(sid)["status"], "approved")
        self.assertEqual([m["type"] for m in self.sent], ["start", "plan_decision"])
        self.assertEqual(self.sent[1]["approve"], True)
        self.assertIn("+- Make notes.", self.kim.get(f"/code/{sid}/changes.patch").text)
        self.assertIn("code_step_approved", self.actions())

    def test_declining_with_a_note_changes_no_file(self):
        sid = self.mentor_start("Make notes.")
        step = self.plan_step(sid)
        self.kim.post(f"/code/steps/{step['id']}/decline", {"note": "Not that way."}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.plan_step(sid)["status"], "declined")
        self.assertEqual(self.sent[1], {"type": "plan_decision", "approve": False, "note": "Not that way."})
        self.assertEqual(self.row(sid)["status"], "done")
        texts = [s["output"] for s in self.code.steps(sid) if s["kind"] == "text"]
        self.assertIn("Understood, I will not make changes. Note: Not that way.", texts)
        self.assertEqual(self.kim.get(f"/code/{sid}/changes.patch").text, "")
        self.assertIn("code_step_declined", self.actions())

    def test_a_student_cannot_decide_a_plan(self):
        sid = self.mentor_start("Make notes.")
        step = self.plan_step(sid)
        for who in (self.ava, self.ben):
            for action in ("approve", "decline"):
                self.assertEqual(who.post(f"/code/steps/{step['id']}/{action}", {"note": ""}).status_code, 403)
        self.h.idle()
        self.assertEqual(self.plan_step(sid)["status"], "pending")
        self.assertEqual(self.row(sid)["status"], "waiting")

    def test_a_plan_decided_after_the_runner_is_gone_starts_a_new_one_that_resumes(self):
        sid = self.mentor_start("Make notes.")
        resume = self.row(sid)["agent_session_id"]
        run = self.engine._runs.pop(sid)  # as if the app had restarted
        run.stopped = True
        run.stream.terminate()
        step = self.plan_step(sid)
        self.kim.post(f"/code/steps/{step['id']}/approve", {"note": ""}, follow_redirects=False)
        self.h.idle()
        starts = self.starts()
        self.assertEqual(len(starts), 2)
        self.assertEqual((starts[1]["resume"], starts[1]["plan_first"]), (resume, False))
        self.assertIn("approved your plan", starts[1]["task"])
        self.assertEqual(self.row(sid)["status"], "done")
        self.assertIn("+- A mentor approved your plan", self.kim.get(f"/code/{sid}/changes.patch").text)


class QueueTest(AgentTest):
    overrides: dict = {"code_engine": "agent", "code_plan_first_students": False, "code_max_running": 1}

    def test_a_second_session_waits_for_the_first_and_starts_in_order(self):
        first = int(self.kim.post("/code", {"task": "AGENT-SLOW one"}, follow_redirects=False)
                    .headers["location"].rsplit("/", 1)[1])
        self.assertTrue(wait_for(lambda: self.live_run(first) is not None and self.live_run(first).driving))
        second = int(self.kim.post("/code", {"task": "Two."}, follow_redirects=False)
                     .headers["location"].rsplit("/", 1)[1])
        third = int(self.kim.post("/code", {"task": "Three."}, follow_redirects=False)
                    .headers["location"].rsplit("/", 1)[1])
        notes = lambda sid: [s["output"] for s in self.code.steps(sid) if s["kind"] == "note"]  # noqa: E731
        self.assertTrue(wait_for(lambda: "Waiting for a free sandbox. 1 session is ahead of you." in notes(second), 30))
        self.assertTrue(wait_for(lambda: "Waiting for a free sandbox. 2 sessions are ahead of you." in notes(third), 30))
        self.assertEqual((self.row(second)["status"], self.row(third)["status"]), ("preparing", "preparing"))
        self.assertEqual(len(self.starts()), 1, "only the first reached a runner")
        self.kim.post(f"/code/{first}/stop", {}, follow_redirects=False)
        self.h.idle()
        self.assertEqual((self.row(first)["status"], self.row(second)["status"], self.row(third)["status"]),
                         ("stopped", "done", "done"))
        self.assertEqual([m["task"] for m in self.starts()], ["AGENT-SLOW one", "Two.", "Three."])

    def test_a_session_waiting_on_a_plan_gives_its_place_to_the_next(self):
        self.h.services.settings.code_plan_first_mentors = True
        first = self.mentor_start("Plan one.")
        self.assertEqual(self.row(first)["status"], "waiting")
        second = self.mentor_start("Plan two.")  # would wait forever if the first kept its place
        self.assertEqual(self.row(second)["status"], "waiting")
        self.assertEqual(len(self.starts()), 2)


class StudentRuleTest(AgentTest):
    """Rules 5 to 9 and 11, again for the new engine."""

    def test_rule5_nothing_reaches_a_runner_before_a_mentor_approves(self):
        sid, _ = self.ask(self.ava)
        self.assertEqual(self.row(sid)["status"], "awaiting_approval")
        self.assertEqual(self.row(sid)["engine"], "agent")
        self.assertEqual((self.sent, self.engine._runs), ([], {}))
        self.assertEqual(self.db.all("SELECT * FROM sandbox_sessions"), [])
        for actor in (self.ava, self.ben):
            self.assertEqual(actor.post(f"/code/{sid}/approve-request", {"note": ""}).status_code, 403)
        self.h.idle()
        self.assertEqual(self.sent, [])
        self.approve(sid)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.row(sid)["status"], "done")

    def test_rule5_a_declined_request_never_starts(self):
        sid, _ = self.ask(self.ava)
        self.decline(sid, "Not now.")
        self.assertEqual(self.row(sid)["status"], "stopped")
        self.assertEqual((self.sent, self.db.all("SELECT * FROM sandbox_sessions")), ([], []))

    def test_rule5_a_follow_up_waits_for_approval_and_a_declined_one_never_arrives(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        before = len(self.sent)
        self.post_follow_up(self.ava, sid, "Also add a test.")
        self.assertEqual(len(self.sent), before)
        [note] = [s for s in self.code.steps(sid) if s["status"] == "pending"]
        self.assertEqual(self.ava.post(f"/code/steps/{note['id']}/approve-follow-up", {"note": ""}).status_code, 403)
        self.h.idle()
        self.assertEqual(len(self.sent), before)
        self.kim.post(f"/code/steps/{note['id']}/approve-follow-up", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.sent[-1], {"type": "message", "text": "Also add a test."})
        self.post_follow_up(self.ava, sid, "Delete everything.")
        [note2] = [s for s in self.code.steps(sid) if s["status"] == "pending"]
        self.kim.post(f"/code/steps/{note2['id']}/decline-follow-up", {"note": "No."}, follow_redirects=False)
        self.h.idle()
        self.assertNotIn("Delete everything.", json.dumps(self.sent))

    def test_rule6_students_are_refused_without_the_sandbox(self):
        self.h.services.settings.code_sandbox = "off"
        self.ava.post("/code", {"task": "Try it."}, follow_redirects=True)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM code_sessions")["n"], 0)
        self.assertIn("Code isn't ready yet", self.ava.get("/code").text)
        self.assertEqual(self.sent, [])

    def test_rule6_a_running_student_session_is_stopped_when_the_sandbox_goes_off(self):
        sid, _ = self.ask(self.ava)
        self.h.services.settings.code_sandbox = "off"
        self.approve(sid)  # refused: the sandbox is off
        self.assertEqual(self.row(sid)["status"], "awaiting_approval")
        self.assertEqual(self.sent, [])

    def test_rule7_the_patch_is_blocked_until_a_mentor_releases_it(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        self.assertEqual(self.ava.get(f"/code/{sid}/changes.patch").status_code, 403)
        self.assertNotIn("Download patch", self.ava.get(f"/code/{sid}").text)
        self.assertEqual(self.kim.get(f"/code/{sid}/changes.patch").status_code, 200)
        self.assertEqual(self.ava.post(f"/code/{sid}/release", {"note": ""}).status_code, 403)
        self.assertEqual(self.ben.get(f"/code/{sid}/changes.patch").status_code, 404)
        self.release(sid)
        self.h.idle()
        patch = self.ava.get(f"/code/{sid}/changes.patch")
        self.assertEqual(patch.status_code, 200)
        self.assertIn("+++ b/AGENT_NOTES.md", patch.text)
        self.assertEqual(self.ben.get(f"/code/{sid}/changes.patch").status_code, 404, "others stay locked out")
        self.assertEqual(self.ben.get(f"/code/{sid}").status_code, 404)

    def test_rule8_the_screener_note_reaches_the_mentor_and_not_the_student(self):
        sid, _ = self.ask(self.ava, "SCREEN-FLAG a request about the robot.")
        self.assertIn("Screener: Flagged for testing.", self.row(sid)["screen_note"])
        self.assertIn("Screener: Flagged for testing.", self.kim.get(f"/code/{sid}").text)
        self.assertNotIn("Screener:", self.ava.get(f"/code/{sid}").text)
        self.assertEqual(self.sent, [])

    def test_rule9_the_safety_rules_come_first_in_what_the_runner_is_given_and_in_the_gateways_policy(self):
        safety = load_safety_prompt(self.h.settings)
        self.assertGreater(len(safety), 100)
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        self.mentor_start("A mentor's task.")
        starts = self.starts()
        self.assertEqual(len(starts), 2)
        for start in starts:
            self.assertTrue(start["append_system"].startswith(safety))
        self.assertIn("student", starts[0]["append_system"].lower())
        policy = self.engine.policy(str(sid))
        self.assertIn(safety, policy.extra_system)
        self.assertIn("student", policy.extra_system)

    def test_rule11_the_whole_life_of_a_session_is_audited(self):
        sid, _ = self.ask(self.ava, "First task.")
        self.approve(sid)
        self.release(sid)
        second, _ = self.ask(self.ben, "Second task.")
        self.decline(second, "No.")
        resp = self.kim.post("/code", {"task": "AGENT-SLOW stop me"}, follow_redirects=False)
        mentor_sid = int(resp.headers["location"].rsplit("/", 1)[1])
        self.assertTrue(wait_for(lambda: self.live_run(mentor_sid) is not None and self.live_run(mentor_sid).driving))
        self.kim.post(f"/code/{mentor_sid}/stop", {}, follow_redirects=False)
        self.h.idle()
        self.kim.post(f"/code/{sid}/delete", {}, follow_redirects=False)
        self.h.idle()
        actions = self.actions()
        for action in ("code_session_started", "code_student_requested", "code_student_approved",
                       "code_patch_released", "code_student_declined", "code_session_stopped",
                       "code_session_deleted"):
            self.assertIn(action, actions)
        started = json.loads(self.db.one("SELECT details FROM audit_log WHERE action = 'code_session_started'")["details"])
        self.assertEqual((started["engine"], started["plan_first"]), ("agent", False))

    def test_a_student_plan_first_session_waits_for_a_mentor_to_decide(self):
        self.h.services.settings.code_plan_first_students = True
        sid, _ = self.ask(self.ava)
        self.assertEqual(self.row(sid)["plan_first"], 1)
        self.approve(sid)
        self.assertEqual(self.row(sid)["status"], "waiting")
        [step] = [s for s in self.code.steps(sid) if s["tool"] == "plan"]
        self.assertIn("A mentor is reading Claude's plan.", self.ava.get(f"/code/{sid}").text)
        self.assertEqual(self.ava.post(f"/code/steps/{step['id']}/approve", {"note": ""}).status_code, 403)
        self.kim.post(f"/code/steps/{step['id']}/approve", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.row(sid)["status"], "done")

    def test_a_student_never_sees_a_mentors_failure_details(self):
        sid, _ = self.ask(self.ava, "AGENT-FAIL")
        self.approve(sid)
        self.assertEqual(self.row(sid)["status"], "failed")
        self.assertIn("The agent could not finish the task.", self.kim.get(f"/code/{sid}").text)
        page = self.ava.get(f"/code/{sid}").text
        self.assertNotIn("could not finish the task", page)
        self.assertIn("Claude couldn&#39;t finish this task", page)

    def test_sessions_keep_the_engine_they_started_with(self):
        self.h.services.settings.code_engine = "classic"
        classic = self.mentor_start("Classic job.")
        self.assertEqual(self.row(classic)["engine"], "classic")
        self.h.services.settings.code_engine = "agent"
        self.assertEqual(self.row(classic)["engine"], "classic")
        self.assertGreater(self.tool_turns(), 0)
        self.assertEqual(self.sent, [], "the classic session never used a runner")
        modern = self.mentor_start("Agent job.")
        self.assertEqual(self.row(modern)["engine"], "agent")
        sent = len(self.sent)
        self.post_follow_up(self.kim, classic, "More classic work.")
        self.assertEqual(len(self.sent), sent, "the follow-up never reached a runner")
        self.assertEqual(self.row(classic)["engine"], "classic")
        self.assertEqual(len(self.starts()), 1)


class GatewayCallbacksTest(AgentTest):
    def live_session(self) -> tuple[int, str, str]:
        """A session that is still working: (id, sandbox's token, sandbox's address)."""
        resp = self.kim.post("/code", {"task": "AGENT-SLOW hold on"}, follow_redirects=False)
        sid = int(resp.headers["location"].rsplit("/", 1)[1])
        self.assertTrue(wait_for(lambda: self.live_run(sid) is not None and self.live_run(sid).driving))
        box = self.manager.live_for_session(sid)
        handle = self.manager.handle_of(box)
        self.live.append(sid)
        return sid, self.manager._token_for(handle), sandbox_address(box["name"])

    def test_authenticate_accepts_only_the_right_token_from_the_right_address(self):
        sid, token, address = self.live_session()
        auth = self.engine.authenticate
        self.assertEqual(auth(token, address), str(sid))
        self.assertIsNone(auth(token + "x", address))
        self.assertIsNone(auth("", address))
        self.assertIsNone(auth(token, "10.77.0.99"))
        self.assertIsNone(auth(token, ""))
        self.db.execute("UPDATE code_sessions SET engine = 'classic' WHERE id = ?", (sid,))
        self.assertIsNone(auth(token, address), "a classic session never uses the gateway")
        self.db.execute("UPDATE code_sessions SET engine = 'agent', status = 'done' WHERE id = ?", (sid,))
        self.assertIsNone(auth(token, address), "a finished session is refused")
        for state in ("running", "preparing", "waiting"):
            self.db.execute("UPDATE code_sessions SET status = ? WHERE id = ?", (state, sid))
            self.assertEqual(auth(token, address), str(sid), state)
        self.manager.tokens.revoke(self.manager.live_for_session(sid)["name"])
        self.assertIsNone(auth(token, address), "a revoked token is refused")

    def test_policy_carries_the_models_web_setting_cap_and_the_live_total(self):
        student, _ = self.ask(self.ava)  # before the slow session: `ask` waits for background work
        sid, _, _ = self.live_session()
        s = self.h.services.settings
        policy = self.engine.policy(str(sid))
        self.assertEqual(set(policy.models()), {"claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"})
        self.assertTrue(policy.allow_web_tools)
        self.assertEqual((policy.cap_usd, policy.spent()), (15.0, 0.0))
        self.assertIn("a mentor", policy.extra_system)
        s.code_web_tools_mentors = False
        self.assertFalse(self.engine.policy(str(sid)).allow_web_tools)
        self.db.execute("UPDATE code_sessions SET cap_usd = NULL WHERE id = ?", (sid,))
        self.assertIsNone(self.engine.policy(str(sid)).cap_usd)
        self.assertIsNone(self.engine.policy("nonsense"))
        self.assertIsNone(self.engine.policy("99999"))
        self.assertEqual(self.engine.policy(str(student)).cap_usd, 5.0)
        s.code_web_tools_students = False
        self.assertFalse(self.engine.policy(str(student)).allow_web_tools)

    def test_record_adds_to_the_live_total_and_saves_cost_and_tokens(self):
        sid, _, _ = self.live_session()
        policy = self.engine.policy(str(sid))
        usage = Usage(model="claude-sonnet-5-5", input_tokens=100, output_tokens=50, cache_read_tokens=1000,
                      cache_write_tokens=20)
        self.engine.record(str(sid), usage, 0.25)
        self.assertEqual(policy.spent(), 0.25, "the policy reads the live total")
        self.engine.record(str(sid), usage, 0.25)
        self.assertEqual(policy.spent(), 0.5)
        row = self.row(sid)
        self.assertAlmostEqual(row["cost_usd"], 0.5)
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (2 * 1120, 100))
        # a total seeded from the database (after a restart)
        self.engine._spent.clear()
        self.assertAlmostEqual(self.engine.policy(str(sid)).spent(), 0.5)

    def test_the_gateways_numbers_win_over_the_runners(self):
        """When the gateway counted the replies, the turn's own usage isn't added again."""
        original = self.engine._turn_done

        async def counted(sid, run, event):
            self.engine.record(str(sid), Usage(model="claude-sonnet-5-5", input_tokens=7, output_tokens=3), 0.07)
            return await original(sid, run, event)

        with mock.patch.object(self.engine, "_turn_done", counted):
            sid = self.mentor_start("Write some notes.")
        row = self.row(sid)
        self.assertAlmostEqual(row["cost_usd"], 0.07)
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (7, 3))

    def test_credential_by_the_code_sign_in_setting(self):
        s = self.h.services.settings
        s.anthropic_api_key = ""  # the test setup saves a key; start without one
        self.assertIsNone(self.engine.credential(), "no key saved")
        self.assertIn("API key", self.engine.credential_problem())
        s.anthropic_api_key = "sk-ant-TESTKEY"
        credential = self.engine.credential()
        self.assertEqual((credential.kind, credential.secret), ("api_key", "sk-ant-TESTKEY"))
        self.assertNotIn("TESTKEY", repr(credential))
        self.assertEqual(self.engine.credential_problem(), "")
        s.code_auth = "subscription"
        self.assertIsNone(self.engine.credential(), "no token and no login file")
        s.code_subscription_token = "sub-token-1234567890"
        credential = self.engine.credential()
        self.assertEqual((credential.kind, credential.secret), ("subscription", "sub-token-1234567890"))
        s.code_subscription_token = ""
        path = bridge_credentials_path(s.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = {"claudeAiOauth": {"accessToken": "oauth-access", "expiresAt": int((time.time() + 3600) * 1000)}}
        path.write_text(json.dumps(fresh), encoding="utf-8")
        credential = self.engine.credential()
        self.assertEqual((credential.kind, credential.secret), ("subscription", "oauth-access"))
        expired = {"claudeAiOauth": {"accessToken": "oauth-access", "expiresAt": int((time.time() - 5) * 1000)}}
        path.write_text(json.dumps(expired), encoding="utf-8")
        self.assertIsNone(self.engine.credential())
        path.write_text("not json", encoding="utf-8")
        self.assertIsNone(self.engine.credential())
        path.write_text(json.dumps({"claudeAiOauth": {}}), encoding="utf-8")
        self.assertIsNone(self.engine.credential())
        s.code_subscription_token = "sub-token-1234567890"  # a saved token beats the login file
        path.write_text(json.dumps(fresh), encoding="utf-8")
        self.assertEqual(self.engine.credential().secret, "sub-token-1234567890")

    def test_the_gateway_is_wired_to_the_engine_and_not_started_in_test_mode(self):
        gateway = self.h.services.gateway
        self.assertIsNotNone(gateway)
        self.assertFalse(gateway.running)
        self.assertEqual(gateway.address, ("10.77.0.1", 3129))
        self.assertIs(self.engine.gateway, gateway)


class MigrationTest(unittest.TestCase):
    OLD_COLUMNS = ("id, title, task, status, model, repo, ref, transcript, error, input_tokens, output_tokens, created_by, "
                   "created_at, updated_at, owner_is_student, start_approved_by, start_approved_at, screen_note, "
                   "free_rein, patch_released")

    def build_v16(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode = WAL")
        for script in MIGRATIONS[:16]:
            conn.executescript("BEGIN;" + script + "COMMIT;")
        conn.execute("PRAGMA user_version = 16")
        conn.execute("INSERT INTO users (id, slack_user_id, name, role, created_at) VALUES (1, 'U1', 'Kim', 'mentor', "
                     "'2026-01-01T00:00:00+00:00')")
        for sid, status in ((7, "done"), (8, "awaiting_approval")):
            conn.execute("INSERT INTO code_sessions (id, title, task, status, model, repo, ref, transcript, error, "
                         "input_tokens, output_tokens, created_by, created_at, updated_at) VALUES (?, 'Old', 'Old task', "
                         "?, 'anthropic/claude-sonnet-5-5', 'team/robot-code', '', '[]', NULL, 12, 34, 1, "
                         "'2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00')", (sid, status))
        conn.execute("INSERT INTO code_steps (session_id, turn, kind, output, status, created_at) VALUES (7, 0, 'note', "
                     "'Copied', 'done', '2026-01-01T00:00:00+00:00')")
        for key, value in (("code_sandbox", "off"), ("code_student_access", "0"), ("code_engine", "classic"),
                           ("model_default", "anthropic/claude-sonnet-5-5"), ("code_max_running", "1")):
            conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
                         (key, value))
        conn.commit()
        conn.close()

    def test_a_filled_version_16_database_migrates_and_the_defaults_are_enabled(self):
        tmp = Path(tempfile.mkdtemp(prefix="code-v17-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        self.build_v16(tmp / "old.db")
        db = Database(tmp / "old.db")
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))
            self.assertEqual(len(MIGRATIONS), 17)
            columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(code_sessions)")}
        for name, default in (("engine", "'classic'"), ("agent_session_id", None), ("plan_first", "0"),
                              ("cost_usd", "0"), ("cap_usd", None)):
            self.assertIn(name, columns)
            self.assertEqual(columns[name]["dflt_value"], default, name)
        for row in db.all("SELECT * FROM code_sessions"):
            self.assertEqual((row["engine"], row["plan_first"], row["cost_usd"], row["agent_session_id"], row["cap_usd"]),
                             ("classic", 0, 0, None, None))
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM code_steps")["n"], 1)
        # saved choices for the three keys are gone (the new defaults apply); everything else stays
        self.assertEqual(sorted(r["key"] for r in db.all("SELECT key FROM settings")), ["code_max_running", "model_default"])
        [entry] = db.all("SELECT * FROM audit_log WHERE action = 'defaults_enabled_v0_11'")
        self.assertIsNone(entry["actor_id"])
        self.assertEqual(sorted(json.loads(entry["details"])["reset"]),
                         ["code_engine", "code_sandbox", "code_student_access"])
        self.assertRegex(entry["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        # what version 0.10 reads still works
        rows = db.all(f"SELECT {self.OLD_COLUMNS} FROM code_sessions ORDER BY id")
        self.assertEqual([r["id"] for r in rows], [7, 8])
        # and the loaded settings take the new defaults
        settings = Settings()
        from subzero_ai import saved_settings as saved
        saved.load_into(settings, db)
        self.assertEqual((settings.code_engine, settings.code_sandbox, settings.code_student_access, settings.code_max_running),
                         ("agent", "hyperv", True, 1))

    def test_a_fresh_database_migrates_cleanly_and_writes_no_reset_note(self):
        tmp = Path(tempfile.mkdtemp(prefix="code-v17-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        db = Database(tmp / "new.db")
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'defaults_enabled_v0_11'")["n"], 0)
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))

    def test_a_version_16_database_with_nothing_saved_writes_no_note(self):
        tmp = Path(tempfile.mkdtemp(prefix="code-v17-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        self.build_v16(tmp / "old.db")
        conn = sqlite3.connect(tmp / "old.db")
        conn.execute("DELETE FROM settings WHERE key IN ('code_sandbox', 'code_student_access', 'code_engine')")
        conn.commit()
        conn.close()
        db = Database(tmp / "old.db")
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'defaults_enabled_v0_11'")["n"], 0)


NEW_FIELDS = ("code_engine", "code_auth", "code_max_running", "code_plan_first_students", "code_plan_first_mentors",
              "code_web_tools_students", "code_web_tools_mentors", "code_sub_agents", "code_cap_student_usd",
              "code_cap_mentor_usd", "code_agent_max_turns", "sandbox_gateway_port")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(code_sandbox="fake", ai_backend="fake")
        self.admin = self.h.login(ADMIN)
        self.addCleanup(self.h.close)
        self.settings = self.h.services.settings

    def form(self, **changes):
        values = {f.name: saved_settings.current(self.settings, f.name) for f in saved_settings.SECTIONS["code"][1]}
        values.update(changes)
        return values

    def test_the_page_shows_every_new_field_and_the_token_box(self):
        page = self.admin.get("/settings").text
        for name in NEW_FIELDS:
            self.assertIn(f'name="{name}"', page)
            self.assertIn(name, {f.name for f in saved_settings.SECTIONS["code"][1]})
        self.assertIn("Code engine", page)
        self.assertIn("New engine (sandbox only)", page)
        self.assertIn("Team subscription (testing only)", page)
        self.assertIn('name="code_subscription_token"', page)
        self.assertNotIn('name="code_subscription_token" type="text"', page)

    def test_new_defaults(self):
        s = Settings()
        self.assertEqual((s.code_engine, s.code_auth, s.code_max_running, s.code_agent_max_turns),
                         ("agent", "api_key", 2, 200))
        self.assertEqual((s.code_plan_first_students, s.code_plan_first_mentors), (True, False))
        self.assertEqual((s.code_web_tools_students, s.code_web_tools_mentors, s.code_sub_agents), (True, True, True))
        self.assertEqual((s.code_cap_student_usd, s.code_cap_mentor_usd), (5.0, 15.0))
        self.assertEqual((s.code_sandbox, s.code_student_access, s.sandbox_memory_mb, s.sandbox_max_sessions),
                         ("hyperv", True, 6144, 2))
        self.assertEqual(s.sandbox_gateway_port, 3129)
        self.assertEqual(s.code_subscription_token, "")
        self.assertNotIn("code_subscription_token", repr(Settings(code_subscription_token="secret-token-value")))

    def test_values_are_checked(self):
        for name, value in (("code_max_running", "0"), ("code_max_running", "9"), ("code_cap_student_usd", "-1"),
                            ("code_cap_student_usd", "abc"), ("code_cap_student_usd", "nan"),
                            ("code_agent_max_turns", "1"), ("code_engine", "other"), ("code_auth", "password"),
                            ("sandbox_gateway_port", "70000")):
            _, errors = saved_settings.validate("code", self.form(**{name: value}), settings=self.settings)
            self.assertIn(name, errors, (name, value))
        values, errors = saved_settings.validate("code", self.form(code_cap_student_usd="7.50", code_cap_mentor_usd="0",
                                                                    code_engine="classic", code_auth="subscription"),
                                                 settings=self.settings)
        self.assertEqual(errors, {})
        self.assertEqual((values["code_cap_student_usd"], values["code_cap_mentor_usd"], values["code_engine"],
                          values["code_auth"]), (7.5, 0.0, "classic", "subscription"))

    def test_the_student_access_rule_still_ties_to_the_sandbox(self):
        _, errors = saved_settings.validate("code", self.form(code_student_access="1", code_sandbox="off"),
                                            settings=self.settings)
        self.assertIn("code_student_access", errors)

    def test_changing_the_code_sign_in_is_audited(self):
        resp = self.admin.post("/settings/code", self.form(code_auth="subscription", code_cap_student_usd="6"),
                               headers=HTML, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((self.settings.code_auth, self.settings.code_cap_student_usd), ("subscription", 6.0))
        changes = {json.loads(r["details"])["setting"]: json.loads(r["details"]) for r in
                   self.h.services.db.all("SELECT details FROM audit_log WHERE action = 'setting_changed'")}
        self.assertEqual((changes["Code sign-in"]["from"], changes["Code sign-in"]["to"]), ("api_key", "subscription"))
        self.assertEqual(changes["Most one student session may cost, in dollars"]["to"], "6.0")

    def test_the_subscription_token_goes_to_config_env_and_is_never_shown(self):
        token = "oat-SECRETSECRETSECRET-1234"
        resp = self.admin.post("/settings/ai-keys", {"code_subscription_token": token}, headers=HTML,
                               follow_redirects=True)
        self.assertIn("Saved the team subscription token", resp.text)
        self.assertNotIn(token, resp.text)
        self.assertIn("ends in " + token[-4:], resp.text)
        self.assertEqual(self.settings.code_subscription_token, token)
        from subzero_ai.config import parse_env_file
        self.assertEqual(parse_env_file(self.settings.config_path)["CODE_SUBSCRIPTION_TOKEN"], token)
        for row in self.h.services.db.all("SELECT details FROM audit_log"):
            self.assertNotIn(token, row["details"] or "")
        self.assertEqual(len(self.h.services.db.all("SELECT 1 FROM audit_log WHERE action = 'code_token_saved'")), 1)
        bad = self.admin.post("/settings/ai-keys", {"code_subscription_token": "short one"}, headers=HTML,
                              follow_redirects=True)
        self.assertIn("Keys weren&#39;t saved", bad.text)
        self.admin.post("/settings/ai-keys", {"remove_code_subscription_token": "1"}, headers=HTML,
                        follow_redirects=True)
        self.assertEqual(self.settings.code_subscription_token, "")

    def test_the_overview_row_for_the_gateway(self):
        from subzero_ai.web_admin import code_gateway_row
        s = self.h.services
        self.assertIsNone(code_gateway_row(s), "no row unless Hyper-V sandboxes use the new engine")
        s.settings.code_sandbox, s.settings.code_engine, s.settings.ai_backend = "hyperv", "agent", "anthropic"
        row = code_gateway_row(s)
        self.assertEqual((row["state"], row["kind"]), ("Not running", "bad"))
        s.gateway.running = True
        row = code_gateway_row(s)
        self.assertEqual((row["state"], row["kind"]), ("Not signed in", "bad"))
        self.assertIn("API key", row["detail"])
        s.settings.anthropic_api_key = "sk-ant-TESTKEY"
        row = code_gateway_row(s)
        self.assertEqual((row["state"], row["kind"]), ("Working", "ok"))
        self.assertNotIn("TESTKEY", row["detail"])
        s.gateway.running = False


class StreamTest(unittest.TestCase):
    def test_the_hyperv_stream_uses_the_same_login_without_a_time_limit_and_quotes_everything(self):
        opened = []

        class Proc:
            stdin = stdout = stderr = None

        sandbox = HyperVSandbox(base_image="C:/base.vhdx", switch="sw", memory_mb=1024, cpus=1, disk_gb=5,
                                ssh_key="/keys/id", known_dir="/known", popen_ssh=lambda argv: opened.append(argv) or Proc())
        name = "szsandbox-4-s001-abcdef"
        handle = SandboxHandle(name, "hyperv", "now", "later")
        sandbox.open_stream(handle, ["node", "/opt/subzero-runner/runner.mjs"], cwd="work",
                            env={"ANTHROPIC_AUTH_TOKEN": "tok'en; rm -rf /", "ANTHROPIC_BASE_URL": "http://10.77.0.1:3129"})
        [argv] = opened
        self.assertEqual(argv[0], "ssh")
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("UserKnownHostsFile=/known/" + name, argv)
        self.assertEqual(argv[-2], "sandbox@10.77.0.11")
        remote = argv[-1]
        self.assertTrue(remote.startswith("cd /home/sandbox/work && exec env -i "), remote)
        self.assertNotIn("timeout", remote)
        self.assertIn("ANTHROPIC_AUTH_TOKEN='tok'\"'\"'en; rm -rf /'", remote)
        self.assertTrue(remote.endswith("node /opt/subzero-runner/runner.mjs"))

    def test_the_fake_stream_talks_lines(self):
        import sys
        tmp = Path(tempfile.mkdtemp(prefix="stream-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        from subzero_ai.sandbox import FakeSandbox
        box = FakeSandbox(tmp)
        handle = box.create(1)
        (tmp / handle.name / "work").mkdir()
        stream = box.open_stream(handle, [sys.executable, "-u", "-c", "import sys\nfor l in sys.stdin: print(l.upper(), end='')"],
                                 cwd="work", env={})
        stream.write_line("hello")
        self.assertEqual(stream.readline(), b"HELLO\n")
        stream.close_stdin()
        self.assertEqual(stream.wait(5), 0)
        self.assertEqual(stream.readline(), b"")
        again = box.open_stream(handle, [sys.executable, "-c", "import time; time.sleep(30)"], cwd="work", env={})
        box.destroy(handle)  # destroying a sandbox ends what runs in it
        self.assertIsNotNone(again.wait(5))

    def test_the_patch_script_is_a_constant_with_the_exclusions(self):
        sha = "a" * 40
        script = patch_script(sha)
        self.assertIn("--binary", script)
        self.assertIn(f"--cached {sha} --", script, "the patch is compared with a recorded SHA, not a tag")
        self.assertNotIn("subzero-base", script)
        for bad in ("", "subzero-base", "a" * 40 + "; rm -rf /", "A" * 40):
            with self.assertRaises(SandboxError):
                patch_script(bad)
        self.assertIn("exclude,glob", script)


class ReviewFixesTest(AgentTest):
    """Fixes from the G3 review (B1, M1 to M6)."""

    def slow_start(self, task: str) -> int:
        resp = self.kim.post("/code", {"task": task}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        return int(resp.headers["location"].rsplit("/", 1)[1])

    def test_b1_a_follow_up_sent_during_the_end_of_turn_snapshot_is_not_lost(self):
        entered, release = threading.Event(), threading.Event()
        original = self.engine.snapshot

        def slow(sid, handle):
            entered.set()
            release.wait(20)
            return original(sid, handle)

        self.engine.snapshot = slow
        self.addCleanup(release.set)
        sid = self.slow_start("First job.")
        self.assertTrue(entered.wait(15), "the snapshot began")
        self.assertEqual(self.row(sid)["status"], "running")
        self.kim.post(f"/code/{sid}/follow-up", {"text": "Second job."}, follow_redirects=False)
        self.assertEqual([m["type"] for m in self.sent], ["start", "message"])
        self.engine.snapshot = original
        release.set()
        self.h.idle()
        self.assertEqual(self.row(sid)["status"], "done")
        self.assertEqual(len(self.starts()), 1, "the same runner carried on")
        texts = [s["output"] for s in self.code.steps(sid) if s["kind"] == "note" and s["tool"] is None]
        self.assertIn("Second job.", texts)
        self.assertIn("+- Second job.", self.kim.get(f"/code/{sid}/changes.patch").text)
        self.assertAlmostEqual(self.row(sid)["cost_usd"], 0.02)

    def test_d89_a_plan_first_run_that_changes_files_before_approval_is_stopped(self):
        self.h.services.settings.code_plan_first_students = True
        sid, _ = self.ask(self.ava, "AGENT-SNEAK job")
        self.approve(sid)
        row = self.row(sid)
        self.assertEqual(row["status"], "failed")
        self.assertIn("Something went wrong inside the app", row["error"])
        self.assertNotIn("Write", row["error"])
        notes = [s["output"] for s in self.code.steps(sid) if s["tool"] == "mentor"]
        self.assertTrue(any("before the plan was approved" in n for n in notes), notes)
        self.assertEqual([s for s in self.code.steps(sid) if s["tool"] == "Write"], [], "the step was never shown")
        self.assertFalse((self.code.workspace(sid).work / "AGENT_NOTES.md").exists())
        self.assertIsNone(self.live_run(sid))

    def test_d92_a_gateway_cap_refusal_stops_the_session_with_the_cap_text(self):
        original = self.engine._apply
        engine = self.engine

        def apply(sid, run, event):
            if event.get("type") == "turn_done" and event.get("is_error"):
                engine.policy(str(sid)).on_cap()  # the gateway refused a request for the cap during this turn
            return original(sid, run, event)

        with mock.patch.object(self.engine, "_apply", apply):
            sid = self.mentor_start("AGENT-FAIL near the cap")
        row = self.row(sid)
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["error"], cap_text(15.0))

    def test_d92_a_follow_up_near_the_cap_is_refused(self):
        sid = self.mentor_start("First job.")
        self.h.services.settings.code_cap_mentor_usd = 0.1  # less than one typical request
        resp = self.kim.post(f"/code/{sid}/follow-up", {"text": "More."}, follow_redirects=True, headers=HTML)
        self.assertIn("spending cap", resp.text)
        self.assertEqual([m["type"] for m in self.sent], ["start"])

    def test_d92_approving_a_plan_over_the_cap_is_refused_and_the_plan_keeps_waiting(self):
        self.h.services.settings.code_plan_first_students = True
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        [step] = [s for s in self.code.steps(sid) if s["tool"] == "plan"]
        self.h.services.settings.code_cap_student_usd = 0.1
        resp = self.kim.post(f"/code/steps/{step['id']}/approve", {"note": ""}, follow_redirects=True, headers=HTML)
        self.assertIn("spending cap", resp.text)
        self.assertEqual(self.row(sid)["status"], "waiting")
        self.assertEqual(self.db.one("SELECT status FROM code_steps WHERE id = ?", (step["id"],))["status"], "pending")
        self.assertEqual(len(self.starts()), 1)
        self.h.services.settings.code_cap_student_usd = 5
        self.kim.post(f"/code/steps/{step['id']}/approve", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.row(sid)["status"], "done")

    def test_a_mentors_follow_up_on_a_running_student_session_ignores_the_students_open_limit(self):
        self.h.services.settings.code_student_max_open = 1
        self.h.services.settings.code_plan_first_students = False
        resp = self.ava.post("/code", {"task": "AGENT-SLOW student job"}, follow_redirects=False)
        sid = int(resp.headers["location"].rsplit("/", 1)[1])
        self.live.append(sid)
        self.kim.post(f"/code/{sid}/approve-request", {"note": ""}, follow_redirects=False)
        self.assertTrue(wait_for(lambda: self.live_run(sid) is not None and self.live_run(sid).driving))
        self.kim.post(f"/code/{sid}/follow-up", {"text": "Also this."}, follow_redirects=False)
        self.assertEqual([m["type"] for m in self.sent], ["start", "message"])

    def test_background_models_point_at_models_the_gateway_accepts(self):
        sid = self.mentor_start("First job.")
        row, s = self.row(sid), self.h.services.settings
        env = self.engine.model_env(row)
        allowed = set(self.engine.policy(str(sid)).models())
        self.assertEqual(set(env), {"ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                                    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"})
        self.assertTrue(set(env.values()) <= allowed, (env, allowed))
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "claude-haiku-5-5")
        s.model_titles = s.model_screener = ""
        s.model_default = s.model_hard = row["model"]
        env = self.engine.model_env(row)
        self.assertEqual(set(env.values()), {row["model"].split("/")[-1]})
        self.assertTrue(set(env.values()) <= set(self.engine.policy(str(sid)).models()))

    def test_files_windows_cannot_hold_are_left_out_with_a_note(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name in ("aux.txt", "ok.txt", "Dup.txt", "dup.txt"):
                z.writestr(name, "x")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = mock.Mock(root=root, work=root / "work")
            skipped = AgentEngine._save_files(workspace, buf.getvalue())
            self.assertEqual(sorted(skipped), ["aux.txt", "dup.txt"])
            self.assertEqual(sorted(p.name for p in (root / "work").iterdir()), ["Dup.txt", "ok.txt"])
        with mock.patch.object(AgentEngine, "_save_files", return_value=["aux.txt", "nul"]):
            sid = self.mentor_start("First job.")
        self.assertIn("2 files couldn't be copied to the server because of their names.",
                      [s["output"] for s in self.code.steps(sid) if s["kind"] == "note" and s["tool"] is None])
        notes = [s["output"] for s in self.code.steps(sid) if s["tool"] == "mentor"]
        self.assertTrue(any("aux.txt" in n for n in notes), notes)

    def test_m1_an_idle_runner_that_is_closing_is_never_reused(self):
        sid = self.mentor_start("First job.")
        run = self.live_run(sid)
        resume = self.row(sid)["agent_session_id"]
        self.h.call(lambda: self.engine._idle_fire(sid, run))
        self.assertTrue(run.closing)
        self.assertIsNone(self.live_run(sid), "taken out of the table before the close starts")
        self.h.idle()
        self.post_follow_up(self.kim, sid, "Second job.")
        starts = self.starts()
        self.assertEqual(len(starts), 2, "a fresh runner was started")
        self.assertEqual(starts[1]["resume"], resume)
        self.assertEqual(self.row(sid)["status"], "done")

    def test_m3_a_line_over_the_limit_fails_the_run_with_a_plain_message(self):
        with mock.patch.object(SandboxStream, "MAX_LINE", 120):
            sid = self.mentor_start("First job.")
        row = self.row(sid)
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("longer than the limit", row["error"])
        self.assertIn("A mentor can send a follow-up", row["error"])
        notes = [s["output"] for s in self.code.steps(sid) if s["tool"] == "mentor"]
        self.assertTrue(any("longer than the limit" in n for n in notes), notes)
        self.assertIsNone(self.live_run(sid))

    def test_m3_the_event_queue_is_bounded(self):
        sid = self.mentor_start("First job.")
        self.assertEqual(self.live_run(sid).events.maxsize, 1000)

    def test_m4_a_patch_over_the_limit_is_not_kept_and_cannot_be_released(self):
        with mock.patch("subzero_ai.code_engine.MAX_PATCH_BYTES", 10):
            sid, _ = self.ask(self.ava)
            self.approve(sid)
        workspace = self.code.workspace(sid)
        self.assertEqual((workspace.root / "agent.patch").read_bytes(), b"")
        notes = [s["output"] for s in self.code.steps(sid) if s["tool"] == "mentor"]
        self.assertTrue(any("too big" in n for n in notes), notes)
        self.assertEqual(self.engine.patch_problem(sid), "The changes are too big to hand out as a patch. "
                                                         "Download the workspace export instead.")

    def test_m5_a_sandbox_error_never_reaches_the_students_text(self):
        with mock.patch.object(type(self.manager), "create_for_session",
                               side_effect=SandboxError("secret-detail from the host")):
            sid = self.mentor_start("First job.")
        row = self.row(sid)
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("secret-detail", row["error"])
        notes = [s["output"] for s in self.code.steps(sid) if s["tool"] == "mentor"]
        self.assertTrue(any("secret-detail" in n for n in notes), notes)

    def test_m6_the_patch_is_compared_with_a_sha_recorded_on_the_host(self):
        sid = self.mentor_start("First job.")
        sha = (self.code.workspace(sid).root / "base.sha").read_text().strip()
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        box = self.manager.live_for_session(sid)
        backend = self.manager.backend("fake")
        handle = self.manager.handle_of(box)
        # Claude moves the tag to the latest state: the patch must still show everything since the real base
        backend.run(handle, ["sh", "-c", "git add -A && git -c user.name=x -c user.email=x@x commit -qm sneaky && "
                                          "git tag -f subzero-base"], cwd="work", env={}, timeout=60)
        self.post_follow_up(self.kim, sid, "Second job.")
        self.assertIn("+- First job.", self.kim.get(f"/code/{sid}/changes.patch").text)
        self.assertEqual((self.code.workspace(sid).root / "base.sha").read_text().strip(), sha)


if __name__ == "__main__":
    unittest.main()
