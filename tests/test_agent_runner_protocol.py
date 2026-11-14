"""Protocol contract of tests/fake_agent_runner.py (the D67 JSON-lines runner protocol)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake_agent_runner.py"


class Runner:
    """The fake runner as a subprocess with line-based helpers."""

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.p = subprocess.Popen([sys.executable, str(FAKE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, cwd=cwd)

    def send(self, **msg) -> None:
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def start(self, task: str, **over) -> None:
        msg = dict(task=task, model="m", cwd=self.cwd, plan_first=False, allowed_web=False, sub_agents=False,
                   max_turns=None, append_system="")
        msg.update(over)
        self.send(type="start", **msg)

    def read(self) -> dict:
        line = self.p.stdout.readline()
        if not line:
            raise AssertionError("runner closed stdout; stderr: " + self.p.stderr.read())
        return json.loads(line)

    def until(self, kind: str) -> list[dict]:
        seen = []
        while True:
            ev = self.read()
            seen.append(ev)
            if ev["type"] == kind:
                return seen

    def close(self) -> None:
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait()
        for f in (self.p.stdin, self.p.stdout, self.p.stderr):
            if f and not f.closed:
                try:
                    f.close()
                except OSError:
                    pass

    def finish(self) -> int:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        code = self.p.wait(timeout=10)
        self.p.stdout.close()
        self.p.stderr.close()
        return code


class FakeRunnerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def runner(self) -> Runner:
        r = Runner(self.dir)
        self.addCleanup(r.close)
        return r

    def notes(self) -> str:
        p = Path(self.dir) / "AGENT_NOTES.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_version_flag(self):
        out = subprocess.run([sys.executable, str(FAKE), "--version"], capture_output=True, text=True, check=True)
        v = json.loads(out.stdout)
        self.assertEqual(set(v), {"runner_version", "sdk_version"})

    def test_ready_first_and_default_script(self):
        r = self.runner()
        ready = r.read()
        self.assertEqual(ready["type"], "ready")
        self.assertIn("runner_version", ready)
        r.start("make notes")
        evs = r.until("turn_done")
        self.assertEqual([e["type"] for e in evs],
                         ["session", "text", "tool_start", "tool_end", "tool_start", "tool_end", "text", "turn_done"])
        self.assertEqual([e["name"] for e in evs if e["type"] == "tool_start"], ["Bash", "Write"])
        self.assertEqual(evs[-2]["text"], "Done.")
        done = evs[-1]
        self.assertEqual(done["usage"], {"input_tokens": 1200, "output_tokens": 300,
                                         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        self.assertEqual(done["cost_usd"], 0.01)
        self.assertFalse(done["is_error"])
        self.assertEqual(self.notes(), "- make notes\n")
        self.assertEqual(r.finish(), 0)

    def test_follow_up_message_appends_and_stays_alive(self):
        r = self.runner()
        r.read()
        r.start("first")
        r.until("turn_done")
        r.send(type="message", text="second")
        evs = r.until("turn_done")
        self.assertFalse(evs[0]["type"] == "session")  # same session, no new session event
        self.assertEqual(self.notes(), "- first\n- second\n")
        self.assertEqual(r.finish(), 0)

    def test_resume_returns_same_session_id(self):
        r = self.runner()
        r.read()
        r.start("go", resume="abc-123")
        self.assertEqual(r.read(), {"type": "session", "session_id": "abc-123"})
        r.until("turn_done")
        self.assertEqual(r.finish(), 0)

    def test_fail(self):
        r = self.runner()
        r.read()
        r.start("AGENT-FAIL please")
        done = r.until("turn_done")[-1]
        self.assertTrue(done["is_error"])
        self.assertTrue(done["error"])
        self.assertEqual(self.notes(), "")
        self.assertEqual(r.finish(), 0)

    def test_crash(self):
        r = self.runner()
        r.read()
        r.start("AGENT-CRASH")
        seen = []
        while True:
            line = r.p.stdout.readline()
            if not line:
                break
            seen.append(json.loads(line)["type"])
        self.assertEqual(r.p.wait(timeout=10), 3)
        self.assertNotIn("turn_done", seen)
        r.p.stdout.close()
        r.p.stderr.close()

    def test_todo_sub_compact(self):
        r = self.runner()
        r.read()
        r.start("AGENT-TODO AGENT-SUB AGENT-COMPACT")
        evs = r.until("turn_done")
        kinds = [e["type"] for e in evs]
        self.assertIn("todo", kinds)
        self.assertIn("compact", kinds)
        todo = next(e for e in evs if e["type"] == "todo")
        self.assertEqual(todo["items"][0]["content"], "Look around")
        starts = {e["id"]: e for e in evs if e["type"] == "tool_start"}
        child = next(e for e in starts.values() if e.get("parent_id"))
        self.assertEqual(starts[child["parent_id"]]["name"], "Agent")
        ends = {e["id"] for e in evs if e["type"] == "tool_end"}
        self.assertEqual(ends, set(starts))  # every tool_start has a tool_end
        self.assertEqual(r.finish(), 0)

    def test_slow_honours_stop(self):
        r = self.runner()
        r.read()
        r.start("AGENT-SLOW")
        r.read()  # session
        t0 = time.monotonic()
        r.send(type="stop")
        done = r.until("turn_done")[-1]
        self.assertTrue(done["is_error"])
        self.assertEqual(done["error"], "stopped")
        self.assertEqual(r.p.wait(timeout=10), 0)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(self.notes(), "")
        r.p.stdout.close()
        r.p.stderr.close()

    def test_plan_approve(self):
        r = self.runner()
        r.read()
        r.start("do work", plan_first=True)
        evs = r.until("plan")
        self.assertEqual(evs[-1]["type"], "plan")
        self.assertTrue(evs[-1]["plan"])
        self.assertEqual(evs[-2]["name"], "ExitPlanMode")
        self.assertEqual(evs[-2]["id"], evs[-1]["id"])
        self.assertEqual(self.notes(), "")  # nothing changed before the decision
        r.send(type="plan_decision", approve=True, note="")
        rest = r.until("turn_done")
        self.assertFalse(rest[-1]["is_error"])
        self.assertEqual(self.notes(), "- do work\n")
        self.assertEqual(r.finish(), 0)

    def test_plan_decline(self):
        r = self.runner()
        r.read()
        r.start("do work", plan_first=True)
        r.until("plan")
        r.send(type="plan_decision", approve=False, note="use tabs")
        rest = r.until("turn_done")
        self.assertEqual(rest[0]["type"], "tool_end")
        self.assertTrue(rest[0]["is_error"])
        self.assertIn("use tabs", " ".join(e.get("text", "") for e in rest))
        self.assertFalse(rest[-1]["is_error"])
        self.assertEqual(self.notes(), "")
        r.send(type="message", text="ok now do it")  # still alive for a follow-up
        r.until("turn_done")
        self.assertEqual(self.notes(), "- ok now do it\n")
        self.assertEqual(r.finish(), 0)

    def test_stop_when_idle_and_eof(self):
        r = self.runner()
        r.read()
        r.start("x")
        r.until("turn_done")
        r.send(type="stop")
        self.assertEqual(r.p.wait(timeout=10), 0)
        r.p.stdout.close()
        r.p.stderr.close()
        r2 = self.runner()
        r2.read()
        self.assertEqual(r2.finish(), 0)  # EOF before start

    def test_queued_messages_keep_order_and_eof_interrupts(self):
        r = self.runner()
        r.read()
        r.start("AGENT-SLOW one")
        r.send(type="message", text="two")
        r.send(type="message", text="three")
        r.p.stdin.close()  # EOF means stop: the slow turn is interrupted
        evs = []
        while True:
            line = r.p.stdout.readline()
            if not line:
                break
            evs.append(json.loads(line))
        self.assertEqual(r.p.wait(timeout=10), 0)
        self.assertEqual(evs[-1]["error"], "stopped")
        self.assertEqual(sum(e["type"] == "turn_done" for e in evs), 1)
        self.assertEqual(self.notes(), "")

    def test_queued_followups_handled_in_order(self):
        r = self.runner()
        r.read()
        r.start("one")
        r.send(type="message", text="two")
        r.send(type="message", text="three")
        for _ in range(3):
            r.until("turn_done")
        self.assertEqual(self.notes(), "- one\n- two\n- three\n")
        self.assertEqual(r.finish(), 0)

    def test_malformed_line_keeps_running(self):
        r = self.runner()
        r.read()
        r.p.stdin.write("not json\n")
        r.p.stdin.flush()
        self.assertEqual(r.read()["type"], "error")
        r.start("after")
        r.until("turn_done")
        self.assertEqual(r.finish(), 0)


class SdkPinTest(unittest.TestCase):
    def test_sdk_version_pin_matches_pi_gateway_lock(self):
        pkg = json.loads((ROOT / "sandbox_runner" / "package.json").read_text(encoding="utf-8"))
        pinned = pkg["dependencies"]["@anthropic-ai/claude-agent-sdk"]
        self.assertRegex(pinned, r"^\d+\.\d+\.\d+$")  # exact, no range
        lock = json.loads((ROOT / "pi_gateway" / "package-lock.json").read_text(encoding="utf-8"))
        locked = lock["packages"]["node_modules/@anthropic-ai/claude-agent-sdk"]["version"]
        self.assertEqual(pinned, locked)
        own = json.loads((ROOT / "sandbox_runner" / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(own["packages"]["node_modules/@anthropic-ai/claude-agent-sdk"]["version"], pinned)
        self.assertIn("node_modules/@anthropic-ai/claude-agent-sdk-linux-x64", own["packages"])


if __name__ == "__main__":
    unittest.main()
