"""Features that replaced the "Soon" placeholders (docs/FEATURES_TODO.md): each one keeps the approval flow and
the rules for who can see what."""

from __future__ import annotations

import unittest

from subzero_ai.render import explainer_title

from .helpers import MENTOR, STUDENT, STUDENT_2, Harness


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
