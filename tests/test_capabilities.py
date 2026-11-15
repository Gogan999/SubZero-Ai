"""What each model can do (the capability matrix) and which models Code accepts."""

from __future__ import annotations

import unittest
from unittest import mock

from subzero_ai.ai_backend import capabilities_for

from .helpers import MENTOR, STUDENT, Harness, pass_model_check

CLAUDE = "anthropic/claude-sonnet-5-5"
OTHER = "openrouter/meta-llama/llama-4"


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
        # The fake backend runs any provider's model, and OTHER is keyed and passes the safety check, so it is a
        # model Settings could offer (Code still refuses it: only Claude models run Code sessions)
        self.h = Harness(ai_backend="fake", model_hard=OTHER, ai_providers=("anthropic", "openrouter"),
                         openrouter_api_key="sk-or-test-key-0123456789", connector_github_repo="team/robot-code")
        self.ava = self.h.login(STUDENT)
        self.kim = self.h.login(MENTOR)
        pass_model_check(self.h, OTHER)

    def tearDown(self):
        self.h.close()


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
