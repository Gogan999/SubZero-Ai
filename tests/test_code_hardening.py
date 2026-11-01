"""Code sessions, hardening: build-file names, Gradle options, streamed output, Stop, size and session limits,
and cleanup of stuck sessions. Offline: the command tests run small Python or sh programs."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from subzero_ai import code_runner
from subzero_ai.code_runner import (
    MAX_OUTPUT_CHARS,
    ToolProblem,
    Workspace,
    _OutputTail,
    is_build_file,
    parse_command,
    prune_workspaces,
    run_program,
    stop_command,
)
from subzero_ai.db import utcnow

from .helpers import MENTOR, STUDENT, Harness
from .test_features import FakeResponse, FakeWeb, repo_zip


class BuildFileNameTest(unittest.TestCase):
    def test_build_files_and_scripts_are_refused_whatever_the_spelling(self):
        for name in ("gradlew.bat.", "build.gradle ", "GRADLEW.BAT", "sub\\build.gradle", "gradlew", "gradlew.bat",
                     "settings.gradle.kts", "app/Build.Groovy", "tools/run.CMD", "tools/x.ps1", "scripts/deploy.sh",
                     "a.bat.", "build.gradle::$DATA", "gradle/wrapper/gradle-wrapper.properties", "gradle.properties",
                     "buildSrc/A.java", ".mvn/extensions.xml", "Vendordeps/Phoenix.json", ".wpilib/x.json",
                     "libs/x.jar", "src/../build.gradle", "../evil.java", "src/..."):
            self.assertTrue(is_build_file(name), name)

    def test_ordinary_robot_code_is_allowed(self):
        for name in ("src/Robot.java", "NOTES.md", "src/gradle_helper.java", "docs/gradle", "README.md",
                     "src/Robot.java.", "src/Robot.java/", "src/gradlewish/Helper.java"):
            self.assertFalse(is_build_file(name), name)


class GradleOptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="code-args-"))
        (self.tmp / "gradlew").write_text("#!/bin/sh\n")
        (self.tmp / "gradlew").chmod(0o755)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_loading_other_scripts_or_build_files_is_refused(self):
        for command in ("gradlew -I init.gradle", "gradlew -Ifoo.gradle", "gradlew --init-script x.gradle",
                        "gradlew --init-script=x.gradle", "gradlew -c s.gradle", "gradlew -cs.gradle",
                        "gradlew --settings-file=x", "gradlew --settings-file x", "gradlew -b build.gradle",
                        "gradlew -bbuild.gradle", "gradlew --build-file x", "gradlew --build-file=x"):
            with self.assertRaises(ToolProblem, msg=command) as caught:
                parse_command(command, ("gradlew",), self.tmp)
            self.assertIn("can't pass", str(caught.exception))

    def test_normal_gradle_flags_still_work(self):
        self.assertEqual(parse_command("gradlew build", ("gradlew",), self.tmp)[1:], ["build"])
        self.assertEqual(parse_command("gradlew test --info --continue", ("gradlew",), self.tmp)[1:],
                         ["test", "--info", "--continue"])
        self.assertEqual(parse_command("gradlew -x test -q", ("gradlew",), self.tmp)[1:], ["-x", "test", "-q"])


class OutputTest(unittest.TestCase):
    def test_tail_keeps_only_the_end(self):
        tail = _OutputTail(MAX_OUTPUT_CHARS * 4)
        for _ in range(200):
            tail.add(b"x" * 65536)
        tail.add(b"THE END")
        self.assertLessEqual(len(tail.data), MAX_OUTPUT_CHARS * 4, "memory stays bounded")
        text = tail.text()
        self.assertTrue(text.startswith("(…earlier output cut)\n"))
        self.assertTrue(text.endswith("THE END"))
        self.assertLessEqual(len(text), MAX_OUTPUT_CHARS + 30)

    def test_short_output_is_untouched(self):
        tail = _OutputTail(MAX_OUTPUT_CHARS * 4)
        tail.add("héllo\n".encode())
        self.assertEqual(tail.text(), "héllo\n")

    def test_a_chatty_command_is_streamed_and_cut(self):
        code = "import sys; sys.stdout.write('a' * 3000000 + 'FINISHED')"
        with tempfile.TemporaryDirectory(prefix="code-out-") as tmp:
            out = run_program([sys.executable, "-c", code], Path(tmp), 60)
        self.assertTrue(out.startswith("(…earlier output cut)"))
        self.assertTrue(out.endswith("FINISHED\n(Exit code 0.)"))
        self.assertLess(len(out), MAX_OUTPUT_CHARS + 100)

    def test_timeout_still_kills_the_command(self):
        with tempfile.TemporaryDirectory(prefix="code-slow-") as tmp:
            started = time.monotonic()
            out = run_program([sys.executable, "-c", "import time; time.sleep(30)"], Path(tmp), 1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("Stopped after 1 seconds", out)


class StopTest(unittest.TestCase):
    def test_stop_kills_a_running_command_and_its_children(self):
        child_file = Path(tempfile.mkdtemp(prefix="code-child-")) / "pid"
        code = ("import subprocess, sys, time\n"
                f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"open({str(child_file)!r}, 'w').write(str(p.pid))\n"
                "time.sleep(60)\n")
        result: dict[str, str] = {}
        worker = threading.Thread(target=lambda: result.update(
            out=run_program([sys.executable, "-c", code], Path(tempfile.gettempdir()), 120, 4242)))
        worker.start()
        for _ in range(500):
            if 4242 in code_runner._RUNNING and child_file.exists() and child_file.read_text():
                break
            time.sleep(0.02)
        self.assertTrue(stop_command(4242))
        worker.join(20)
        self.assertFalse(worker.is_alive(), "the command returned after Stop")
        self.assertIn("Stopped by a mentor", result["out"])
        self.assertNotIn(4242, code_runner._RUNNING)
        self.assertFalse(stop_command(4242), "nothing left to stop")
        if os.name != "nt" and child_file.exists():
            child = int(child_file.read_text())
            for _ in range(100):
                try:
                    os.kill(child, 0)
                except OSError:
                    break
                time.sleep(0.05)
            else:
                self.fail("the child process is still running")


class SlotTest(unittest.TestCase):
    def test_a_third_command_waits_then_is_refused(self):
        slots = code_runner._COMMAND_SLOTS
        with tempfile.TemporaryDirectory(prefix="code-slots-") as tmp:
            held = 0
            try:
                while held < code_runner.MAX_RUNNING_COMMANDS:
                    self.assertTrue(slots.acquire(timeout=1))
                    held += 1
                with mock.patch.object(code_runner, "COMMAND_WAIT_SECONDS", 0.2):
                    with self.assertRaises(ToolProblem) as caught:
                        run_program([sys.executable, "-c", "print(1)"], Path(tmp), 30)
                self.assertIn("Too many commands", str(caught.exception))
            finally:
                for _ in range(held):
                    slots.release()
            self.assertEqual(run_program([sys.executable, "-c", "print(1)"], Path(tmp), 30).splitlines()[0], "1",
                             "slots are free again")


class SizeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="code-size-"))
        (self.root / "work" / "src").mkdir(parents=True)
        (self.root / "work" / "src" / "a.java").write_text("x" * 100)
        (self.root / "base").mkdir()
        (self.root / "base" / "a.java").write_text("x" * 100)
        self.space = Workspace(self.root)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_full_session_refuses_more_writes_and_commands(self):
        self.assertEqual(self.space.size_bytes(), 200)
        with mock.patch.object(code_runner, "MAX_SESSION_BYTES", 250):
            self.space.check_room()
            with self.assertRaises(ToolProblem) as caught:
                self.space.apply("write_file", {"path": "src/B.java", "content": "y" * 100})
            self.assertIn("MB limit", str(caught.exception))
            self.assertFalse((self.root / "work" / "src" / "B.java").exists(), "nothing written")
            with self.assertRaises(ToolProblem):
                self.space.check_room(60)  # 200 bytes now, so 260 would be over
        with mock.patch.object(code_runner, "MAX_SESSION_BYTES", 1000):
            self.assertIn("Saved", self.space.apply("write_file", {"path": "src/B.java", "content": "y" * 100}))


class SessionLimitTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(connector_github_repo="team/robot-code")
        self.ava = self.h.login(STUDENT)
        self.kim = self.h.login(MENTOR)
        self.web = FakeWeb({"https://api.github.com/repos/team/robot-code/zipball": FakeResponse(200, repo_zip())})
        self.h.services.github.session_factory = self.web
        self.code = self.h.services.code

    def tearDown(self):
        self.h.close()

    def begin(self, task="Add a notes file and run the build."):
        resp = self.kim.post("/code", {"task": task}, follow_redirects=False)
        self.h.idle()
        return resp

    def count_sessions(self):
        return self.code.db.one("SELECT COUNT(*) AS n FROM code_sessions")["n"]

    def test_a_mentor_has_five_open_sessions_at_most(self):
        ids = []
        for _ in range(code_runner.MAX_OPEN_SESSIONS):
            resp = self.begin()
            self.assertEqual(resp.status_code, 303, resp.text)
            ids.append(int(resp.headers["location"].rsplit("/", 1)[1]))
        self.assertEqual(self.code.get(ids[0])["status"], "waiting")
        resp = self.kim.post("/code", {"task": "one more"}, follow_redirects=True)
        self.assertIn("5 Code sessions waiting or running", resp.text)
        self.assertEqual(self.count_sessions(), code_runner.MAX_OPEN_SESSIONS)

        self.kim.post(f"/code/{ids[0]}/stop", follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.begin().status_code, 303, "a slot is free once a session stops")

    def test_stop_kills_the_running_command_and_marks_the_step(self):
        sid = int(self.begin().headers["location"].rsplit("/", 1)[1])
        work = self.code.workspace(sid).work
        now = utcnow()
        step = self.code.db.execute(
            "INSERT INTO code_steps (session_id, turn, kind, tool, tool_use_id, input, output, status, created_at) "
            "VALUES (?, 9, 'tool', 'run_command', 'toolu_stop', '{}', 'gradlew build', 'running', ?)",
            (sid, now)).lastrowid
        self.code._set(sid, status="running")
        result: dict[str, str] = {}
        worker = threading.Thread(target=lambda: result.update(out=run_program(
            [sys.executable, "-c", "import time; time.sleep(60)"], work, 120, sid)))
        worker.start()
        for _ in range(500):
            if sid in code_runner._RUNNING:
                break
            time.sleep(0.02)
        self.kim.post(f"/code/{sid}/stop", follow_redirects=False)
        worker.join(20)
        self.assertFalse(worker.is_alive())
        self.assertIn("Stopped by a mentor", result["out"])
        self.assertEqual(self.code.get(sid)["status"], "stopped")
        row = self.code.db.one("SELECT status, review_note FROM code_steps WHERE id = ?", (step,))
        self.assertEqual(row["status"], "declined")
        self.assertIn("stopped while this ran", row["review_note"])

    def test_stuck_sessions_are_stopped_and_their_copies_deleted_after_three_days(self):
        stuck = int(self.begin().headers["location"].rsplit("/", 1)[1])
        recent = int(self.begin().headers["location"].rsplit("/", 1)[1])
        folder = self.code.workspace(stuck).root
        self.code.db.execute("UPDATE code_sessions SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                             (stuck,))
        data_dir = self.h.services.settings.data_dir
        self.assertEqual(prune_workspaces(self.code.db, data_dir), 1)
        self.assertFalse(folder.exists())
        row = self.code.get(stuck)
        self.assertIsNotNone(row, "the row is kept")
        self.assertEqual(row["status"], "stopped")
        self.assertIn("3 days", row["error"])
        self.assertEqual(self.code.get(recent)["status"], "waiting", "a recent session is left alone")
        self.assertTrue(self.code.workspace(recent).root.exists())
        self.assertIn("no activity for 3 days", self.kim.get(f"/code/{stuck}").text)
        self.assertEqual(prune_workspaces(self.code.db, data_dir), 0, "nothing more to do")


if __name__ == "__main__":
    unittest.main()
