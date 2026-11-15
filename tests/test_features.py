"""Profile, the GitHub connector and Code (the features left from docs/FEATURES_TODO.md): each one keeps the
approval flow and the rules for who can see what."""

from __future__ import annotations

import json
import unittest

from .helpers import ADMIN, MENTOR, STUDENT, STUDENT_2, FakeResponse, FakeWeb, Harness, repo_zip


class FeatureTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.ava = self.h.login(STUDENT)
        self.ben = self.h.login(STUDENT_2)
        self.kim = self.h.login(MENTOR)

    def tearDown(self):
        self.h.close()

    def start(self, text, actor=None):
        """A person messages the bot in Slack; returns (message id, chat id)."""
        sent = self.h.slack_dm(text, user=(actor or self.ava).slack_user_id)
        return sent.message_id, sent.conversation_id

    def approve(self, message_id):
        resp = self.kim.post(f"/m/{message_id}/approve")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.h.idle()


if __name__ == "__main__":
    unittest.main()


class ProfileTest(FeatureTest):
    def test_appearance_is_saved_and_drawn_on_the_page(self):
        page = self.ava.get("/about").text
        self.assertIn('<html lang="en">', page)
        self.assertIn('content="light dark"', page)
        self.assertIn('action="/profile/theme"', page)
        self.assertEqual(self.ava.post("/profile/theme", {"theme": "dark"}).json(), {"ok": True})
        page = self.ava.get("/about").text
        self.assertIn('<html lang="en" data-theme="dark">', page)
        self.assertIn('<meta name="color-scheme" content="dark">', page)
        self.assertIn('value="dark" aria-pressed="true"', page)
        self.assertNotIn("data-theme", self.ben.get("/about").text.split("<head>")[0], "it's per person")
        self.assertEqual(self.ava.post("/profile/theme", {"theme": "neon"}).status_code, 400)
        resp = self.ava.post("/profile/theme", {"theme": "light", "next": "/profile"},
                             headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/profile")
        resp = self.ava.post("/profile/theme", {"theme": "light", "next": "//evil.example"},
                             headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/code")
        css = self.ava.get("/assets/code.css").text
        self.assertIn(':root[data-theme="dark"] .code pre', css)

    def test_profile_is_appearance_only(self):
        page = self.ava.get("/profile").text
        self.assertIn("Approved today", page, "students see how many messages they have left")
        self.assertNotIn("Approved today", self.kim.get("/profile").text)
        for gone in ("default_subteam", "notify_dm", "subteam", "Slack messages when a mentor"):
            self.assertNotIn(gone, page)
        self.assertIn('name="theme"', page)
        resp = self.ava.post("/profile", {"theme": "dark", "default_subteam": "Programming", "notify_dm": "0"},
                             follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        user = self.ava.user
        self.assertEqual(user["theme"], "dark")
        # The old preferences can't be changed any more: the "DM me when a mentor decides" setting keeps its default
        self.assertEqual((user["default_subteam"], user["notify_dm"]), (None, 1))

    def test_the_slack_decision_dm_still_goes_out_for_a_message_with_no_thread(self):
        mid, _ = self.start("How do I tune a flywheel?")
        self.h.services.db.execute("DELETE FROM slack_dm_messages WHERE message_id = ?", (mid,))
        dms = len(self.h.slack.dms)
        self.approve(mid)
        self.assertEqual(len(self.h.slack.dms), dms + 1)
        self.assertNotIn("http", self.h.slack.dms[-1]["text"])


class RepliesWithoutToolsTest(FeatureTest):
    def call(self):
        return [c for c in self.h.claude.calls if c.get("kind") != "complete"][-1]

    def test_plain_messages_use_no_tools(self):
        mid, _ = self.start("What is a PID loop?")
        self.approve(mid)
        call = self.call()
        self.assertEqual((call["web_searches"], call["effort"], call["system_extra"]), (0, "medium", None))

    def test_stream_parser_reads_searches_and_sources(self):
        from subzero_ai.claude import ClaudeClient, parse_sse
        lines = []
        for event in (
            {"type": "content_block_start", "index": 0, "content_block": {"type": "server_tool_use", "name": "web_search", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"query": "Kraken'}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": ' X60"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "web_search_tool_result", "content": [
                {"type": "web_search_result", "url": "https://example.com/x60", "title": "X60 specs"}]}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9, "server_tool_use": {"web_search_requests": 1}}},
        ):
            lines += ["data: " + __import__("json").dumps(event), ""]
        events = list(parse_sse(lines))
        self.assertEqual([(e.kind, e.text) for e in events][:1], [("search", "Kraken X60")])
        self.assertEqual(events[1].sources, [{"url": "https://example.com/x60", "title": "X60 specs"}])
        self.assertEqual(events[2].usage["web_searches"], 1)
        body = ClaudeClient.request_body(model="m", system="s", messages=[], max_tokens=10, web_searches=5)
        self.assertEqual(body["tools"], [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}])
        self.assertNotIn("tools", ClaudeClient.request_body(model="m", system="s", messages=[], max_tokens=10))


REPO_ROOT = [{"name": "src", "path": "src", "type": "dir", "size": 0},
             {"name": "build.gradle", "path": "build.gradle", "type": "file", "size": 1200},
             {"name": "robot.bin", "path": "robot.bin", "type": "file", "size": 99}]
API = "https://api.github.com/repos/team/robot-code/contents/"


class ConnectorsTest(FeatureTest):
    def setUp(self):
        self.h = Harness(connector_github_repo="team/robot-code")
        self.ava = self.h.login(STUDENT)
        self.kim = self.h.login(MENTOR)
        self.web = FakeWeb({
            API: FakeResponse(200, json.dumps(REPO_ROOT).encode()),
            API + "src": FakeResponse(200, json.dumps([{"name": "Robot.java", "path": "src/Robot.java", "type": "file",
                                                        "size": 40}]).encode()),
            API + "src/Robot.java": FakeResponse(200, b"public class Robot {}\n"),
        })
        self.h.services.github.session_factory = self.web

    def test_admin_sets_up_github(self):
        coach = self.h.login(ADMIN)
        self.h.services.settings.config_path.parent.mkdir(parents=True, exist_ok=True)
        resp = coach.post("/settings/connectors", {"connector_github_repo": "not a repo"},
                          follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.h.services.settings.connector_github_repo, "team/robot-code", "nothing saved")
        self.web.routes["https://api.github.com/repos/team/other/contents/"] = FakeResponse(200, b"[]")
        coach.post("/settings/connectors", {"connector_github_repo": "https://github.com/team/other.git",
                                             "connector_github_token": "github_pat_secret123"}, follow_redirects=False)
        settings = self.h.services.settings
        self.assertEqual((settings.connector_github_repo, settings.connector_github_token),
                         ("team/other", "github_pat_secret123"))
        config = settings.config_path.read_text()
        self.assertIn("CONNECTOR_GITHUB_REPO=team/other", config)
        page = coach.get("/settings").text
        self.assertNotIn("github_pat_secret123", page)
        self.assertIn("Saved</span>", page)
        audit = [r for r in self.h.services.store.audit_entries() if r["action"] == "connectors_changed"]
        self.assertEqual(len(audit), 1)
        self.assertNotIn("secret123", audit[0]["details"])
        self.assertEqual(self.kim.post("/settings/connectors", {"connector_github_repo": ""}).status_code, 403)


class CodeTest(FeatureTest):
    def setUp(self):
        self.h = Harness(connector_github_repo="team/robot-code")
        self.ava = self.h.login(STUDENT)
        self.kim = self.h.login(MENTOR)
        self.web = FakeWeb({"https://api.github.com/repos/team/robot-code/zipball": FakeResponse(200, repo_zip())})
        self.h.services.github.session_factory = self.web
        self.code = self.h.services.code

    def begin(self, task="Add a notes file and run the build."):
        resp = self.kim.post("/code", {"task": task}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        self.h.idle()
        return int(resp.headers["location"].rsplit("/", 1)[1])

    def pending(self, sid):
        return [s for s in self.code.steps(sid) if s["status"] == "pending"]

    def act(self, step_id, kind, note=""):
        self.kim.post(f"/code/steps/{step_id}/{kind}", {"note": note}, follow_redirects=False)
        self.h.idle()

    def test_mentors_only(self):
        self.assertEqual(self.ava.get("/code").status_code, 403)
        self.assertEqual(self.ava.post("/code", {"task": "x"}).status_code, 403)
        self.h.services.settings.connector_github_repo = ""
        self.assertIn("Connect the repository first", self.kim.get("/code").text)

    def test_every_change_and_command_waits_for_a_mentor(self):
        sid = self.begin()
        session = self.code.get(sid)
        self.assertEqual(session["status"], "waiting")
        work = self.code.workspace(sid).work
        self.assertTrue((work / "src" / "Robot.java").is_file())
        self.assertFalse((work.parent / "evil.txt").exists(), "zip paths can't climb out")
        done = [s["tool"] for s in self.code.steps(sid) if s["kind"] == "tool" and s["status"] == "done"]
        self.assertEqual(done, ["list_files", "read_file"], "reading needs no approval")
        [write] = self.pending(sid)
        self.assertEqual(write["tool"], "write_file")
        self.assertFalse((work / "NOTES.md").exists(), "nothing is written before approval")
        page = self.kim.get(f"/code/{sid}").text
        self.assertIn("Wants to write NOTES.md", page)
        self.assertIn("+# Notes", page)
        self.assertIn("Approve change", page)

        self.act(write["id"], "approve")
        self.assertTrue((work / "NOTES.md").is_file())
        [run] = self.pending(sid)
        self.assertEqual(run["tool"], "run_command")
        self.assertIn("code Claude changed in this session (NOTES.md)", self.kim.get(f"/code/{sid}").text)
        import asyncio

        from subzero_ai import code_runner
        threads = []
        original = code_runner.run_program

        def watched(*args):
            try:
                asyncio.get_running_loop()
                threads.append("event loop")
            except RuntimeError:
                threads.append("worker")
            return original(*args)

        code_runner.run_program = watched
        try:
            self.act(run["id"], "approve")
        finally:
            code_runner.run_program = original
        self.assertEqual(threads, ["worker"], "a build never blocks the app")
        session = self.code.get(sid)
        self.assertEqual(session["status"], "done")
        ran = self.code.db.one("SELECT * FROM code_steps WHERE id = ?", (run["id"],))
        self.assertIn("BUILD SUCCESSFUL build", ran["output"])
        self.assertIn("Exit code 0", ran["output"])

        patch = self.kim.get(f"/code/{sid}/changes.patch")
        self.assertEqual(patch.status_code, 200)
        self.assertIn("+++ b/NOTES.md", patch.text)
        self.assertNotIn("Robot.java", patch.text)
        actions = [r["action"] for r in self.h.services.store.audit_entries()]
        for action in ("code_session_started", "code_step_approved", "code_patch_downloaded"):
            self.assertIn(action, actions)
        # A follow-up carries on in the same copy
        self.kim.post(f"/code/{sid}/follow-up", {"text": "Thanks, that is all."}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.code.get(sid)["status"], "done")
        self.assertIn("Thanks, that is all.", self.kim.get(f"/code/{sid}").text)

    def test_declined_step_goes_back_to_claude(self):
        sid = self.begin()
        [write] = self.pending(sid)
        self.act(write["id"], "decline", "Don't add files at the root.")
        work = self.code.workspace(sid).work
        self.assertFalse((work / "NOTES.md").exists())
        transcript = json.loads(self.code.get(sid)["transcript"])
        last = next(block for m in transcript if m["role"] == "user" and isinstance(m["content"], list)
                    for block in m["content"] if block.get("tool_use_id") == write["tool_use_id"])
        self.assertTrue(last["is_error"])
        self.assertIn("Don't add files at the root.", last["content"])
        self.assertEqual(self.code.db.one("SELECT status FROM code_steps WHERE id = ?", (write["id"],))["status"],
                         "declined")
        self.act(write["id"], "approve")  # already handled: nothing changes
        self.assertFalse((work / "NOTES.md").exists())

    def test_stop_and_failures(self):
        sid = self.begin()
        self.kim.post(f"/code/{sid}/stop", follow_redirects=False)
        self.assertEqual(self.code.get(sid)["status"], "stopped")
        self.assertEqual(self.pending(sid), [])
        failed = self.begin("CODE-ERROR please")
        self.assertEqual(self.code.get(failed)["status"], "failed")
        self.assertIn("Simulated failure", self.kim.get(f"/code/{failed}").text)
        self.web.routes.clear()
        broken = self.begin()
        self.assertEqual(self.code.get(broken)["status"], "failed")

    def test_old_copies_are_deleted_and_slow_commands_stopped(self):
        import time

        from subzero_ai.code_runner import prune_workspaces, run_program
        sid = self.begin()
        self.kim.post(f"/code/{sid}/stop", follow_redirects=False)
        folder = self.code.workspace(sid).root
        self.assertEqual(prune_workspaces(self.code.db, self.h.services.settings.data_dir), 0, "too recent")
        self.code.db.execute("UPDATE code_sessions SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (sid,))
        self.assertEqual(prune_workspaces(self.code.db, self.h.services.settings.data_dir), 1)
        self.assertFalse(folder.exists())
        self.assertIn("deleted after 14 days", self.kim.get(f"/code/{sid}").text)
        self.assertEqual(self.kim.get(f"/code/{sid}/changes.patch").status_code, 404)
        script = folder.parent / "slow.sh"
        script.write_text("sleep 30 &\nsleep 30\n")
        started = time.monotonic()
        out = run_program(["sh", str(script)], folder.parent, 1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("Stopped after 1 seconds", out)

    def test_commands_and_paths_are_fenced_in(self):
        from subzero_ai.code_runner import ToolProblem, Workspace, parse_command
        sid = self.begin()
        work = self.code.workspace(sid).work
        self.assertEqual(parse_command("gradlew build", ("gradlew",), work)[1:], ["build"])
        self.assertEqual(parse_command("./gradlew test", ("gradlew",), work)[1:], ["test"])
        for bad in ("rm -rf /", "gradlew build; rm -rf /", "gradlew build && curl x", "../gradlew build",
                    "python -c 1", "gradlew $(whoami)"):
            with self.assertRaises(ToolProblem, msg=bad):
                parse_command(bad, ("gradlew",), work)
        space = Workspace(work.parent)
        for bad in ("../base/build.gradle", "/etc/passwd", "src/../../x"):
            with self.assertRaises(ToolProblem, msg=bad):
                space.path(bad)
        self.assertIn("kP = 5", space.read_file("src/Robot.java"))
        self.assertIn("src/Robot.java:2", space.search(r"kP\s*="))
        with self.assertRaises(ToolProblem):
            space.preview("edit_file", {"path": "src/Robot.java", "old_text": "missing", "new_text": "x"})
        # Build files run with the app's permissions, so Claude can't change them
        for name in ("build.gradle", "settings.gradle", "sub/build.gradle.kts", "gradlew", "gradlew.bat",
                     "gradle.properties", "gradle/wrapper/gradle-wrapper.properties", "buildSrc/A.java",
                     "vendordeps/Phoenix.json", ".gradle/init.gradle", "tools/run.ps1", "libs/x.jar"):
            with self.assertRaises(ToolProblem, msg=name):
                space.preview("write_file", {"path": name, "content": "x"})
            with self.assertRaises(ToolProblem, msg=name):
                space.apply("write_file", {"path": name, "content": "x"})
        self.assertIn("+x", space.preview("write_file", {"path": "src/Gradle.java", "content": "x"}))
