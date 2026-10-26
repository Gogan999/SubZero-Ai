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
