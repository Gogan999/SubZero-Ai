"""Claude-only features are offered and allowed only when the chat's model supports them."""

from __future__ import annotations

import unittest
from unittest import mock

from subzero_ai.ai_backend import capabilities_for
from subzero_ai.generation import NO_IMAGES_NOTE

from .helpers import MENTOR, STUDENT, Harness
from .test_milestone3 import png_bytes

CLAUDE = "anthropic/claude-sonnet-5-5"
OTHER = "openrouter/meta-llama/llama-4"
TOOLS = ('name="search"', 'name="research"', 'name="think"')


class MatrixTest(unittest.TestCase):
    def test_claude_models(self):
        c = capabilities_for(CLAUDE, "anthropic")
        self.assertEqual((c.web_search, c.tools, c.thinking, c.images, c.code), (True,) * 5)
        p = capabilities_for(CLAUDE, "pi")
        self.assertEqual((p.web_search, p.tools, p.thinking, p.images, p.code), (False, True, True, True, True))

    def test_other_models_follow_the_gateway_info(self):
        both = capabilities_for(OTHER, "pi", {"images": True, "reasoning": True})
        self.assertEqual((both.web_search, both.tools, both.thinking, both.images, both.code),
                         (False, False, True, True, False))
        neither = capabilities_for(OTHER, "pi", {"images": False, "reasoning": False})
        self.assertEqual((neither.thinking, neither.images), (False, False))

    def test_unknown_other_model_can_do_nothing(self):
        c = capabilities_for(OTHER, "pi")
        self.assertEqual((c.web_search, c.tools, c.thinking, c.images, c.code), (False,) * 5)


class Base(unittest.TestCase):
    def setUp(self):
        self.h = Harness(model_hard=OTHER, ai_providers=("anthropic", "openrouter"),
                         connector_github_repo="team/robot-code")
        self.ava = self.h.login(STUDENT)
        self.kim = self.h.login(MENTOR)

    def tearDown(self):
        self.h.close()

    def chat(self, actor, text="Hello there", **extra):
        resp = actor.post("/chats/new", {"text": text, "subteam": "Programming", **extra})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.h.idle()
        body = resp.json()
        return body["message_id"], int(body["url"].rsplit("/", 1)[1])


class ComposerTest(Base):
    def test_conversation_page_controls_follow_the_model(self):
        _, cid = self.chat(self.kim)
        page = self.kim.get(f"/c/{cid}").text
        for marker in TOOLS:
            self.assertIn(marker, page)
        self.assertNotIn("data-model-note", page)
        self.assertEqual(self.kim.post(f"/c/{cid}/model", {"model": OTHER}).status_code, 200)
        page = self.kim.get(f"/c/{cid}").text
        for marker in TOOLS:
            self.assertNotIn(marker, page)
        self.assertIn("This model can&#39;t search the web, think longer or read images.", page.replace("'", "&#39;"))
        self.assertIn("Response style", page)
        self.assertNotIn(".png", page.split("data-files")[0].rsplit("<input", 1)[1] + "data-files")

    def test_note_lists_only_what_is_missing(self):
        _, cid = self.chat(self.kim)
        self.kim.post(f"/c/{cid}/model", {"model": OTHER})
        self.h.services.model_info[OTHER] = {"id": OTHER, "images": True, "reasoning": False}
        page = self.kim.get(f"/c/{cid}").text.replace("&#39;", "'")
        self.assertIn("This model can't search the web or think longer.", page)
        self.assertIn(".png", page)
        self.assertNotIn('name="think"', page)
        self.h.services.model_info[OTHER] = {"id": OTHER, "images": True, "reasoning": True}
        page = self.kim.get(f"/c/{cid}").text.replace("&#39;", "'")
        self.assertIn("This model can't search the web.", page)
        self.assertIn('name="think"', page)

    def test_new_chat_page_uses_the_default_model(self):
        for marker in TOOLS[:1] + TOOLS[2:]:
            self.assertIn(marker, self.ava.get("/chats").text)
        self.assertIn('name="research"', self.kim.get("/chats").text)
        self.h.services.settings.model_default = OTHER
        page = self.kim.get("/chats").text
        for marker in TOOLS:
            self.assertNotIn(marker, page)
        self.assertIn("Response style", page)


class RefusalTest(Base):
    FORCED = [({"search": "1"}, "Web search"), ({"research": "1"}, "Research"), ({"think": "1"}, "thinking")]

    def test_new_chat_with_a_non_claude_default(self):
        self.h.services.settings.model_default = OTHER
        for extra, word in self.FORCED:
            resp = self.kim.post("/chats/new", {"text": "Hi", "subteam": "Programming", **extra})
            self.assertEqual(resp.status_code, 400)
            self.assertIn(word, resp.json()["error"])
        shot = [("files", ("a.png", png_bytes(), "image/png"))]
        resp = self.kim.post("/chats/new", {"text": "Hi", "subteam": "Programming"}, files=shot)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("can't read images", resp.json()["error"])
        text = [("files", ("a.txt", b"hello", "text/plain"))]
        self.assertEqual(self.kim.post("/chats/new", {"text": "Hi", "subteam": "Programming"}, files=text).status_code, 200)

    def test_follow_up_on_a_non_claude_chat(self):
        _, cid = self.chat(self.kim)
        self.kim.post(f"/c/{cid}/model", {"model": OTHER})
        for extra, word in self.FORCED:
            resp = self.kim.post(f"/c/{cid}/messages", {"text": "More", **extra})
            self.assertEqual(resp.status_code, 400)
            self.assertIn(word, resp.json()["error"])
        shot = [("files", ("a.png", png_bytes(), "image/png"))]
        resp = self.kim.post(f"/c/{cid}/messages", {"text": "More"}, files=shot)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Attach text files instead", resp.json()["error"])
        self.h.services.model_info[OTHER] = {"images": True, "reasoning": True}
        self.assertEqual(self.kim.post(f"/c/{cid}/messages", {"text": "More", "think": "1"}, files=shot).status_code, 200)

    def test_claude_accepts_everything(self):
        _, cid = self.chat(self.kim)
        shot = [("files", ("a.png", png_bytes(), "image/png"))]
        resp = self.kim.post(f"/c/{cid}/messages", {"text": "More", "search": "1", "think": "1"}, files=shot)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.h.idle()
        self.assertEqual(self.kim.post("/chats/new", {"text": "Hi", "subteam": "Programming", "research": "1"}).status_code, 200)


class ModelSwitchTest(Base):
    def test_queued_search_and_think_are_dropped_after_a_switch(self):
        shot = [("files", ("a.png", png_bytes(), "image/png"))]
        resp = self.ava.post("/chats/new", {"text": "Look up this part", "subteam": "Programming", "search": "1",
                                            "think": "1"}, files=shot)
        self.assertEqual(resp.status_code, 200, resp.text)
        mid, cid = resp.json()["message_id"], int(resp.json()["url"].rsplit("/", 1)[1])
        self.assertEqual(self.kim.post(f"/c/{cid}/model", {"model": OTHER}).status_code, 200)
        self.kim.post(f"/m/{mid}/approve", {})
        self.h.idle()
        reply = self.h.reply_to(mid)
        self.assertEqual(reply["status"], "complete")
        call = [c for c in self.h.claude.calls if c.get("kind") != "complete"][-1]
        self.assertEqual(call["model"], OTHER)
        self.assertEqual(call["web_searches"], 0)
        self.assertNotEqual(call["effort"], "xhigh")
        content = call["messages"][-1]["content"]
        self.assertTrue(all(b.get("type") != "image" for b in content))
        self.assertIn(NO_IMAGES_NOTE, [b.get("text") for b in content])

    def test_images_stay_for_a_model_that_reads_them(self):
        self.h.services.model_info[OTHER] = {"images": True}
        shot = [("files", ("a.png", png_bytes(), "image/png"))]
        resp = self.ava.post("/chats/new", {"text": "See this", "subteam": "Programming"}, files=shot)
        mid, cid = resp.json()["message_id"], int(resp.json()["url"].rsplit("/", 1)[1])
        self.kim.post(f"/c/{cid}/model", {"model": OTHER})
        self.kim.post(f"/m/{mid}/approve", {})
        self.h.idle()
        call = [c for c in self.h.claude.calls if c.get("kind") != "complete"][-1]
        self.assertTrue(any(b.get("type") == "image" for b in call["messages"][-1]["content"]))


class CodeModelTest(Base):
    def test_only_claude_models_are_offered_and_accepted(self):
        page = self.kim.get("/code").text
        self.assertIn(CLAUDE.split("/")[1][:6], page)
        self.assertNotIn(f'value="{OTHER}"', page)
        self.h.services.settings.model_default = OTHER
        self.h.services.settings.model_hard = OTHER
        page = self.kim.get("/code").text
        self.assertIn("Code sessions need a Claude model", page)
        self.assertNotIn('name="task"', page)
        with mock.patch.object(self.h.services.code, "tasks"):
            resp = self.kim.post("/code", {"task": "Fix it", "model": OTHER})
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) AS n FROM code_sessions")["n"], 0)
        self.assertIn(resp.status_code, (200, 303))

    def test_runner_refuses_a_non_claude_model(self):
        from subzero_ai.code_runner import CodeError
        viewer = mock.Mock(is_mentor=True, id=1)
        with self.assertRaisesRegex(CodeError, "Claude model"):
            self.h.services.code.start(viewer, "Fix it", OTHER)

    def test_claude_still_starts(self):
        with mock.patch.object(self.h.services.code, "tasks"), \
                mock.patch.object(self.h.services.code, "_prepare_and_run", mock.Mock(return_value=None)):
            self.kim.post("/code", {"task": "Fix it", "model": CLAUDE})
        row = self.h.services.db.one("SELECT model FROM code_sessions")
        self.assertEqual(row["model"], CLAUDE)


if __name__ == "__main__":
    unittest.main()
