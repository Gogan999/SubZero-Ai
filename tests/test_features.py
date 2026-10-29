"""Features that replaced the "Soon" placeholders (docs/FEATURES_TODO.md): each one keeps the approval flow and
the rules for who can see what."""

from __future__ import annotations

import json
import unittest

from subzero_ai.render import explainer_title

from .test_milestone3 import png_bytes
from .helpers import ADMIN, MENTOR, STUDENT, STUDENT_2, Harness


class FeatureTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.ava = self.h.login(STUDENT)
        self.ben = self.h.login(STUDENT_2)
        self.kim = self.h.login(MENTOR)

    def tearDown(self):
        self.h.close()

    def start(self, text, actor=None, **data):
        resp = (actor or self.ava).post("/chats/new", {"text": text, **data})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.h.idle()
        return body["message_id"], int(body["url"].rsplit("/", 1)[1])

    def approve(self, message_id):
        resp = self.kim.post(f"/m/{message_id}/approve")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.h.idle()


class ArtifactsTest(FeatureTest):
    def make_explainer(self):
        mid, cid = self.start("Can you make a simulation of how kP changes the arm?")
        self.approve(mid)
        return self.h.reply_to(mid)["id"], cid

    def test_gallery_lists_explainers_the_viewer_can_see(self):
        self.assertIn("No interactive explainers yet", self.ava.get("/artifacts").text)
        reply_id, cid = self.make_explainer()
        page = self.ava.get("/artifacts").text
        self.assertIn("Arm with a P controller", page)  # the explainer's <title>
        self.assertIn(f'href="/artifacts/{reply_id}/0"', page)
        self.assertIn(f'href="/c/{cid}#m{reply_id}"', page)
        self.assertIn("Arm with a P controller", self.ben.get("/artifacts").text, "an approved team chat")
        self.assertNotIn("Arm with a P controller", self.ben.get("/artifacts?scope=mine").text)
        self.assertNotIn("Arm with a P controller", self.ava.get("/artifacts?q=climber").text)
        self.assertIn("Arm with a P controller", self.ava.get("/artifacts?q=p+controller").text)

        self.h.services.store.make_private(cid)
        self.assertNotIn("Arm with a P controller", self.ben.get("/artifacts").text)
        self.assertIn("Arm with a P controller", self.kim.get("/artifacts").text)
        self.assertIn("Arm with a P controller", self.ava.get("/artifacts?scope=mine").text)

    def test_waiting_questions_make_no_artifacts(self):
        self.start("Can you make a simulation of a flywheel spinning up?")
        self.assertIn("No interactive explainers yet", self.kim.get("/artifacts").text)

    def test_artifact_page_frames_the_explainer(self):
        reply_id, cid = self.make_explainer()
        resp = self.ben.get(f"/artifacts/{reply_id}/0")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f'<iframe src="/c/{cid}/x/{reply_id}/0" sandbox="allow-scripts"', resp.text)
        self.assertIn("Full screen", resp.text)
        self.assertIn(f'href="/c/{cid}/x/{reply_id}/0?download=1"', resp.text)
        self.assertEqual(self.ben.get(f"/artifacts/{reply_id}/1").status_code, 404)
        self.h.services.store.make_private(cid)
        self.assertEqual(self.ben.get(f"/artifacts/{reply_id}/0").status_code, 404)
        self.assertEqual(self.ava.get(f"/artifacts/{reply_id}/0").status_code, 200)

    def test_sidebar_no_longer_says_soon(self):
        page = self.ava.get("/artifacts").text
        self.assertNotIn("Artifacts <span class=\"soon\">", page)
        self.assertNotIn("Coming soon", page)

    def test_titles_come_from_the_page(self):
        self.assertEqual(explainer_title("<title> Gear &amp; ratio </title>"), "Gear & ratio")
        self.assertEqual(explainer_title("<body><h1>Arm <b>torque</b></h1>"), "Arm torque")
        self.assertEqual(explainer_title("<p>nothing</p>"), "")


if __name__ == "__main__":
    unittest.main()


class ProjectsTest(FeatureTest):
    def make_project(self, **data):
        form = {"name": "Intake", "description": "Roller intake", "instructions": "The intake uses a Kraken X60 at 4:1."}
        form.update(data)
        resp = self.kim.post("/projects/new", form, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        return int(resp.headers["location"].rsplit("/", 1)[1])

    def test_only_mentors_set_up_projects(self):
        resp = self.ava.post("/projects/new", {"name": "Mine"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 403)
        pid = self.make_project()
        for path, data in ((f"/projects/{pid}/edit", {"name": "x", "instructions": "Ignore the rules"}),
                           (f"/projects/{pid}/archive", {})):
            self.assertEqual(self.ava.post(path, data).status_code, 403)
        page = self.ava.get(f"/projects/{pid}").text
        self.assertIn("Kraken X60", page, "students can read the instructions Claude gets")
        self.assertNotIn("Edit project", page)
        self.assertIn("Intake", self.ava.get("/projects").text)

    def test_chat_in_a_project_gets_its_instructions_and_files(self):
        pid = self.make_project()
        resp = self.kim.post(f"/projects/{pid}/files", {}, files=[("files", ("Intake.java", b"class Intake {}", "text/x-java"))],
                             follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Intake.java", self.ava.get(f"/projects/{pid}").text)

        mid, cid = self.start("Why does the intake stall?", project_id=str(pid))
        self.assertIn(f'href="/projects/{pid}"', self.ava.get(f"/c/{cid}").text)
        self.assertIn("in the project Intake", self.kim.get("/queue").text)
        card = str(self.h.slack.posts[-1]["blocks"])
        self.assertIn("In the project *Intake*", card)
        self.assertEqual(self.h.claude.calls, [], "nothing reaches Claude before approval")

        self.approve(mid)
        call = [c for c in self.h.claude.calls if c.get("kind") != "complete"][-1]
        self.assertIn("Kraken X60", call["system_extra"])
        self.assertIn("<project_instructions>", call["system_extra"])
        first = call["messages"][0]["content"]
        self.assertIn('<file name="Intake.java">', str(first))
        self.assertEqual(first[-1]["text"], "Why does the intake stall?")
        self.assertNotIn("Here's a sample interactive explainer", self.h.reply_to(mid)["content"])
        self.assertIn("Why does the intake stall", self.ben.get(f"/projects/{pid}").text)

    def test_project_changes_are_checked_and_logged(self):
        pid = self.make_project()
        self.kim.post(f"/projects/{pid}/edit", {"name": "Intake v2", "description": "", "instructions": "New"})
        row = self.h.services.projects.get(pid)
        self.assertEqual((row["name"], row["instructions"]), ("Intake v2", "New"))
        actions = [r["action"] for r in self.h.services.store.audit_entries()]
        self.assertIn("project_changed", actions)
        self.assertIn("project_created", actions)
        self.kim.post(f"/projects/{pid}/edit", {"name": "", "instructions": "x"})
        self.assertEqual(self.h.services.projects.get(pid)["name"], "Intake v2")
        self.kim.post(f"/projects/{pid}/files", {}, files=[("files", ("clip.mp4", b"\0\0\0\x18ftypmp42", "video/mp4"))])
        self.assertEqual(self.h.services.projects.files(pid), [])

    def test_archived_projects_hide_from_students_and_take_no_new_chats(self):
        pid = self.make_project()
        self.kim.post(f"/projects/{pid}/archive")
        self.assertNotIn("Intake", self.ava.get("/projects").text.split("<main")[1])
        self.assertEqual(self.ava.get(f"/projects/{pid}").status_code, 404)
        self.assertEqual(self.kim.get(f"/projects/{pid}").status_code, 200)
        resp = self.ava.post("/chats/new", {"text": "Hi", "project_id": str(pid)})
        self.assertEqual(resp.status_code, 400)

    def test_project_images_are_capped(self):
        pid = self.make_project()
        shots = [("files", (f"shot{i}.png", png_bytes(), "image/png")) for i in range(6)]
        self.kim.post(f"/projects/{pid}/files", {}, files=shots)
        self.assertEqual(self.h.services.projects.files(pid), [], "six images are refused together")
        self.kim.post(f"/projects/{pid}/files", {}, files=shots[:5])
        self.assertEqual(len(self.h.services.projects.files(pid)), 5)
        self.kim.post(f"/projects/{pid}/files", {}, files=shots[5:])
        self.assertEqual(len(self.h.services.projects.files(pid)), 5)

    def test_project_files_are_served_safely(self):
        pid = self.make_project()
        self.kim.post(f"/projects/{pid}/files", {}, files=[("files", ("notes.md", b"# Notes", "text/markdown"))])
        fid = self.h.services.projects.files(pid)[0]["id"]
        resp = self.ava.get(f"/projects/{pid}/files/{fid}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("sandbox", resp.headers["content-security-policy"])
        self.assertEqual(self.ava.get(f"/projects/{pid + 1}/files/{fid}").status_code, 404)
        self.kim.post(f"/projects/{pid}/files/{fid}/remove")
        self.assertEqual(self.ava.get(f"/projects/{pid}/files/{fid}").status_code, 404)


class ProfileTest(FeatureTest):
    def test_appearance_is_saved_and_drawn_on_the_page(self):
        page = self.ava.get("/chats").text
        self.assertIn('<html lang="en">', page)
        self.assertIn('content="light dark"', page)
        self.assertIn('action="/profile/theme"', page)
        self.assertEqual(self.ava.post("/profile/theme", {"theme": "dark"}).json(), {"ok": True})
        page = self.ava.get("/chats").text
        self.assertIn('<html lang="en" data-theme="dark">', page)
        self.assertIn('<meta name="color-scheme" content="dark">', page)
        self.assertIn('value="dark" aria-pressed="true"', page)
        self.assertNotIn("data-theme", self.ben.get("/chats").text.split("<head>")[0], "it's per person")
        self.assertEqual(self.ava.post("/profile/theme", {"theme": "neon"}).status_code, 400)
        resp = self.ava.post("/profile/theme", {"theme": "light", "next": "/artifacts"},
                             headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/artifacts")
        resp = self.ava.post("/profile/theme", {"theme": "light", "next": "//evil.example"},
                             headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/chats")
        css = self.ava.get("/assets/code.css").text
        self.assertIn(':root[data-theme="dark"] .code pre', css)

    def test_preferences(self):
        page = self.ava.get("/profile").text
        self.assertIn("Approved today", page, "students see how many messages they have left")
        self.assertNotIn("Approved today", self.kim.get("/profile").text)
        self.ava.post("/profile", {"default_subteam": "Programming", "theme": "system", "notify_dm": "1"})
        mid, cid = self.start("How do I tune a flywheel?")
        self.assertEqual(self.h.services.store.conversation(cid)["subteam"], "Programming")

        dms = len(self.h.slack.dms)
        self.approve(mid)
        self.assertEqual(len(self.h.slack.dms), dms + 1)
        self.ava.post("/profile", {"default_subteam": "Not a subteam"})  # notify_dm unchecked
        user = self.ava.user
        self.assertEqual((user["default_subteam"], user["notify_dm"]), (None, 0))
        mid, _ = self.start("And a shooter?")
        dms = len(self.h.slack.dms)
        self.approve(mid)
        self.assertEqual(len(self.h.slack.dms), dms, "no Slack message once turned off")


class ComposerToolsTest(FeatureTest):
    def call(self):
        return [c for c in self.h.claude.calls if c.get("kind") != "complete"][-1]

    def test_tools_are_approved_with_the_message(self):
        mid, cid = self.start("What's the stall current of a Kraken X60?", search="1", think="1", style="concise")
        self.assertIn("Asked for: Web search, Extended thinking, Concise style", self.kim.get("/queue").text)
        self.assertIn("Asked for: Web search, Extended thinking, Concise style", str(self.h.slack.posts[-1]["blocks"]))
        self.assertEqual(self.h.claude.calls, [])
        self.approve(mid)
        call = self.call()
        self.assertEqual(call["web_searches"], 5)
        self.assertEqual(call["effort"], "xhigh")
        self.assertIn("Answer concisely", call["system_extra"])
        reply = self.h.reply_to(mid)
        self.assertEqual(reply["web_searches"], 1)
        page = self.ava.get(f"/c/{cid}").text
        self.assertIn("Searched the web (1 search)", page)
        self.assertIn('href="https://docs.wpilib.org/en/stable/" target="_blank" rel="noopener noreferrer nofollow"', page)

    def test_plain_messages_use_no_tools(self):
        mid, _ = self.start("What is a PID loop?")
        self.approve(mid)
        call = self.call()
        self.assertEqual((call["web_searches"], call["effort"], call["system_extra"]), (0, "medium", None))

    def test_research_is_for_mentors_unless_switched_on(self):
        self.assertNotIn('name="research"', self.ava.get("/chats").text)
        self.assertIn('name="research"', self.kim.get("/chats").text)
        mid, _ = self.start("Compare swerve modules", research="1")
        self.approve(mid)
        self.assertEqual(self.call()["web_searches"], 0, "a student can't sneak research in")
        self.h.settings.research_for_students = True
        mid, _ = self.start("Compare swerve modules again", research="1")
        self.approve(mid)
        self.assertEqual(self.call()["web_searches"], 20)
        self.assertIn("Research report", self.call()["system_extra"])

    def test_daily_search_limit_and_switch(self):
        self.h.settings.web_search_daily_limit = 1
        mid, _ = self.start("Search one", search="1")
        self.approve(mid)
        resp = self.ava.post("/chats/new", {"text": "Search two", "search": "1"})
        self.assertEqual(resp.status_code, 429)
        self.assertIn("today's searches", resp.json()["error"])
        self.h.settings.web_search_daily_limit = 50
        self.h.settings.web_search_enabled = False
        self.assertNotIn('name="search"', self.ava.get("/chats").text)
        mid, _ = self.start("Search three", search="1")  # dropped, not refused
        self.approve(mid)
        self.assertEqual(self.call()["web_searches"], 0)

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


class ShareTest(FeatureTest):
    def chat_with_reply(self):
        mid, cid = self.start("How do I tune kP for the arm?")
        self.approve(mid)
        return mid, cid

    def ask(self, actor, cid, audience="my parents"):
        """Asks for a link; returns the error shown, or None."""
        resp = actor.post(f"/c/{cid}/share", {"audience": audience}, follow_redirects=False)
        if resp.status_code == 303:
            return None
        self.assertEqual(resp.status_code, 400, resp.text)
        return resp.text

    def shares(self, cid):
        return self.h.services.shares.for_conversation(cid)

    def anon(self, path):
        self.h.client.cookies.clear()
        return self.h.client.get(path)

    def test_student_link_waits_for_a_mentor(self):
        _, cid = self.chat_with_reply()
        self.assertIn(f'href="/c/{cid}/share"', self.ava.get(f"/c/{cid}").text)
        self.assertIsNone(self.ask(self.ava, cid))
        share = self.shares(cid)[0]
        self.assertEqual(share["status"], "pending")
        self.assertEqual(self.anon(f"/s/{share['token']}").status_code, 404, "not live until approved")
        self.h.idle()
        self.assertTrue(any("for my parents" in p["text"] and "/queue#shares" in p["text"] for p in self.h.slack.posts))

        queue = self.kim.get("/queue").text
        self.assertIn("wants to share", queue)
        preview = self.kim.get(f"/shares/{share['id']}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("This is exactly what they see", preview.text)
        self.assertEqual(self.ben.get(f"/shares/{share['id']}/preview").status_code, 404)
        self.ava.post(f"/shares/{share['id']}/approve", follow_redirects=False)  # a student can't approve
        self.assertEqual(self.shares(cid)[0]["status"], "pending")
        self.kim.post(f"/shares/{share['id']}/approve", follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.shares(cid)[0]["status"], "active")
        self.assertTrue(any("approved your link" in d["text"] for d in self.h.slack.dms))

        resp = self.anon(f"/s/{share['token']}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("How do I tune kP for the arm?", resp.text)
        self.assertIn("noindex", resp.headers["x-robots-tag"])
        self.assertEqual(resp.headers["referrer-policy"], "no-referrer")
        self.assertEqual(self.shares(cid)[0]["views"], 1)

    def test_link_hides_names_files_and_later_messages(self):
        mid, cid = self.chat_with_reply()
        self.ask(self.kim, cid, "a sponsor")
        share = self.shares(cid)[0]
        self.assertEqual(share["status"], "active", "a mentor's link works straight away")
        later = self.ava.send(cid, "Thanks! What about kD?").json()["message_id"]
        self.h.idle()
        text = self.anon(f"/s/{share['token']}").text
        student = self.ava.user["name"]
        self.assertNotIn(student, text)
        self.assertIn(">Student<", text)
        self.assertNotIn("What about kD", text, "pending, and after the link was made")
        self.approve(later)
        self.assertNotIn("What about kD", self.anon(f"/s/{share['token']}").text, "the link is a snapshot")
        self.assertNotIn("/files/", text)

    def test_revoke_expire_and_private(self):
        _, cid = self.chat_with_reply()
        self.ask(self.kim, cid)
        share = self.shares(cid)[0]
        token = share["token"]
        self.assertEqual(self.anon(f"/s/{token}").status_code, 200)
        self.h.services.store.make_private(cid)
        self.assertEqual(self.anon(f"/s/{token}").status_code, 404)
        self.h.services.db.execute("UPDATE conversations SET visibility = 'team' WHERE id = ?", (cid,))
        self.h.services.db.execute("UPDATE shares SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                                   (share["id"],))
        self.assertEqual(self.anon(f"/s/{token}").status_code, 404)
        self.assertIn("Expired", self.ava.get(f"/c/{cid}/share").text)
        self.h.services.db.execute("UPDATE shares SET expires_at = NULL WHERE id = ?", (share["id"],))
        self.ava.post(f"/shares/{share['id']}/revoke", follow_redirects=False)
        self.assertEqual(self.shares(cid)[0]["status"], "revoked")
        self.assertEqual(self.anon(f"/s/{token}").status_code, 404)
        actions = [r["action"] for r in self.h.services.store.audit_entries()]
        self.assertIn("share_created", actions)
        self.assertIn("share_revoked", actions)

    def test_who_can_share(self):
        mid, cid = self.start("A question nobody has approved yet")
        self.assertIn("nothing to share yet", self.ask(self.ava, cid))
        self.approve(mid)
        self.assertEqual(self.ben.get(f"/c/{cid}/share").status_code, 404, "another student's chat")
        self.assertEqual(self.ben.post(f"/c/{cid}/share", {"audience": "x"}).status_code, 404)
        self.assertNotIn(f'href="/c/{cid}/share"', self.ben.get(f"/c/{cid}").text)
        self.assertIn("Say who the link is for", self.ask(self.ava, cid, " "))
        self.h.services.store.make_private(cid)
        self.assertIn("This chat is private, so it can", self.ask(self.ava, cid))
        self.assertEqual(self.shares(cid), [])

    def test_shared_explainer_is_served_only_through_a_live_link(self):
        mid, cid = self.start("Can you make a simulation of how kP changes the arm?")
        self.approve(mid)
        reply_id = self.h.reply_to(mid)["id"]
        self.ask(self.kim, cid)
        token = self.shares(cid)[0]["token"]
        self.assertIn(f"/s/{token}/x/{reply_id}/0", self.anon(f"/s/{token}").text)
        resp = self.anon(f"/s/{token}/x/{reply_id}/0")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("sandbox", resp.headers["content-security-policy"])
        self.assertEqual(self.anon(f"/s/{token}/x/{reply_id}/1").status_code, 404)
        self.assertEqual(self.anon(f"/s/wrong/x/{reply_id}/0").status_code, 404)


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, url="https://api.github.com/"):
        self.status_code, self.body, self.url = status, body, url
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    def json(self):
        return json.loads(self.body)

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]


class FakeWeb:
    """Stands in for requests.Session: answers by URL, and records what was asked."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self):
        return self

    def get(self, url, params=None, headers=None, **kwargs):
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        answer = self.routes.get(url)
        return answer() if callable(answer) else (answer or FakeResponse(404, b"{}", url=url))


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
            "https://docs.google.com/document/d/abcdefghijklmnopqrst/export?format=txt": FakeResponse(
                200, b"Intake design notes", {"content-type": "text/plain",
                                              "content-disposition": 'attachment; filename="Intake notes.txt"'},
                url="https://docs.google.com/document/d/abcdefghijklmnopqrst/export?format=txt"),
            "https://docs.google.com/document/d/privatexxxxxxxxxxxxx/export?format=txt": FakeResponse(
                200, b"<html>Sign in</html>", {"content-type": "text/html"},
                url="https://accounts.google.com/ServiceLogin"),
        })
        self.h.services.github.session_factory = self.web
        self.h.services.drive.session_factory = self.web

    def files_of(self, message_id):
        return self.h.services.attachments.for_message(message_id)

    def test_menu_offers_the_connectors_that_are_set_up(self):
        page = self.ava.get("/chats").text
        self.assertIn('data-open-connector="github"', page)
        self.assertIn('data-open-connector="drive"', page)
        self.assertNotIn("Connectors (GitHub, Drive)", page)
        self.h.services.settings.connector_github_repo = ""
        self.h.services.settings.connector_drive_links = False
        page = self.ava.get("/chats").text
        self.assertNotIn("data-open-connector", page)
        self.assertNotIn("Soon</span></span>", page)

    def test_browse_the_repository(self):
        body = self.ava.get("/connectors/github").json()
        self.assertTrue(body["ok"])
        self.assertEqual([e["name"] for e in body["entries"]], ["src", "build.gradle", "robot.bin"], "folders first")
        self.assertEqual([e["supported"] for e in body["entries"]], [True, True, False])
        self.assertEqual(self.ava.get("/connectors/github?path=src").json()["entries"][0]["path"], "src/Robot.java")
        bad = self.ava.get("/connectors/github?path=../secrets")
        self.assertEqual(bad.status_code, 400)
        self.assertNotIn("Authorization", self.web.calls[0]["headers"], "a public repository needs no token")
        self.h.services.settings.connector_github_token = "github_pat_abc"
        self.ava.get("/connectors/github")
        self.assertEqual(self.web.calls[-1]["headers"]["Authorization"], "Bearer github_pat_abc")

    def test_github_file_is_attached_and_reviewed(self):
        mid, _ = self.start("Why doesn't my robot class compile?", github="src/Robot.java")
        self.assertEqual(self.h.message(mid)["status"], "pending")
        files = self.files_of(mid)
        self.assertEqual([f["name"] for f in files], ["robot-code/src/Robot.java"])
        self.assertEqual(self.h.services.attachments.read(files[0]), b"public class Robot {}\n")
        resp = self.ava.post("/chats/new", {"text": "x", "github": "robot.bin"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("can't be added", resp.json()["error"])
        resp = self.ava.post("/chats/new", {"text": "x", "github": "missing.java"})
        self.assertIn("isn't there", resp.json()["error"])
        resp = self.ava.post("/chats/new", {"text": "x", "github": [f"src/{i}.java" for i in range(6)]})
        self.assertIn("up to 5 files", resp.json()["error"])

    def test_drive_links(self):
        mid, _ = self.start("Summarize these notes",
                            drive="https://docs.google.com/document/d/abcdefghijklmnopqrst/edit?usp=sharing")
        files = self.files_of(mid)
        self.assertEqual([f["name"] for f in files], ["Intake notes.txt"])
        resp = self.ava.post("/chats/new", {"text": "x", "drive": "https://docs.google.com/document/d/privatexxxxxxxxxxxxx/edit"})
        self.assertIn("Anyone with the link", resp.json()["error"])
        resp = self.ava.post("/chats/new", {"text": "x", "drive": "https://evil.example.com/document/d/abcdefghijklmnopqrst"})
        self.assertIn("docs.google.com", resp.json()["error"])
        self.assertFalse(any("evil" in c["url"] for c in self.web.calls), "only Google is ever fetched")
        self.h.services.settings.connector_drive_links = False
        resp = self.ava.post("/chats/new", {"text": "x", "drive": "https://docs.google.com/document/d/abcdefghijklmnopqrst/edit"})
        self.assertIn("switched off", resp.json()["error"])

    def test_admin_sets_up_github(self):
        coach = self.h.login(ADMIN)
        self.h.services.settings.config_path.parent.mkdir(parents=True, exist_ok=True)
        resp = coach.post("/settings/connectors", {"connector_github_repo": "not a repo", "connector_drive_links": "1"},
                          follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.h.services.settings.connector_github_repo, "team/robot-code", "nothing saved")
        self.web.routes["https://api.github.com/repos/team/other/contents/"] = FakeResponse(200, b"[]")
        coach.post("/settings/connectors", {"connector_github_repo": "https://github.com/team/other.git",
                                             "connector_github_token": "github_pat_secret123"}, follow_redirects=False)
        settings = self.h.services.settings
        self.assertEqual((settings.connector_github_repo, settings.connector_github_token, settings.connector_drive_links),
                         ("team/other", "github_pat_secret123", False))
        config = settings.config_path.read_text()
        self.assertIn("CONNECTOR_GITHUB_REPO=team/other", config)
        page = coach.get("/settings").text
        self.assertNotIn("github_pat_secret123", page)
        self.assertIn("Saved</span>", page)
        audit = [r for r in self.h.services.store.audit_entries() if r["action"] == "connectors_changed"]
        self.assertEqual(len(audit), 1)
        self.assertNotIn("secret123", audit[0]["details"])
        self.assertEqual(self.kim.post("/settings/connectors", {"connector_github_repo": ""}).status_code, 403)


def repo_zip() -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("team-robot-code-abc123/build.gradle", "plugins { id 'java' }\n")
        z.writestr("team-robot-code-abc123/src/Robot.java", "public class Robot {\n  int kP = 5;\n}\n")
        z.writestr("team-robot-code-abc123/../evil.txt", "outside")
        script = zipfile.ZipInfo("team-robot-code-abc123/gradlew")
        script.external_attr = 0o755 << 16
        z.writestr(script, "#!/bin/sh\necho BUILD SUCCESSFUL \"$@\"\n")
    return buf.getvalue()


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
        self.act(run["id"], "approve")
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
