"""The model lists: what the Settings dropdowns offer, which models are allowed, and what happens when an old chat's
model stops being allowed. Chats started in Slack always use the default model. Offline: the fake AI client from
tests/helpers.py."""

from __future__ import annotations

import json
import unittest

from subzero_ai.model_choices import selectable_models

from .helpers import ADMIN, MENTOR, STUDENT, Harness, pass_model_check

KEY = "sk-ant-test-key-0123456789"
HTML = {"Accept": "text/html"}
SONNET = "anthropic/claude-sonnet-5-5"
OPUS = "anthropic/claude-opus-5-5"
HAIKU = "anthropic/claude-haiku-5-5"
GPT = "openai/gpt-x"


class PickerTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(anthropic_api_key=KEY)  # the Anthropic backend, as in production
        self.ava = self.h.login(STUDENT)
        self.kim = self.h.login(MENTOR)
        self.admin = self.h.login(ADMIN)
        pass_model_check(self.h, SONNET)
        pass_model_check(self.h, OPUS)

    def tearDown(self):
        self.h.close()

    # ---------------------------------------------------------------- helpers
    def conv_model(self, cid: int) -> str:
        return self.h.services.store.conversation(cid)["model"]

    def audit(self, action: str) -> list[dict]:
        return [json.loads(r["details"] or "{}") for r in
                self.h.services.db.all("SELECT details FROM audit_log WHERE action = ? ORDER BY id", (action,))]

    def listed(self, purpose: str = "chat") -> set[str]:
        return {option["id"] for option in selectable_models(self.h.services, purpose=purpose)}

    # ---------------------------------------------------------------- chats started in Slack
    def test_a_chat_started_in_slack_uses_the_default_model(self):
        sent = self.h.slack_dm("Why is my arm slow?", user=STUDENT)
        self.assertEqual(self.conv_model(sent.conversation_id), SONNET)
        self.h.slack_dm("A mentor's own question", user=MENTOR)
        mentor_chat = self.h.services.db.one("SELECT model FROM conversations ORDER BY id DESC")
        self.assertEqual(mentor_chat["model"], SONNET)
        self.h.slack_tap("approve", sent.message_id)
        self.assertEqual(self.h.chat_calls()[0]["model"], SONNET)

    def test_model_without_a_provider_key_is_not_listed(self):
        self.h.settings.ai_backend = "fake"  # so the Anthropic-only rule doesn't hide the OpenAI model
        self.h.settings.ai_providers = ("anthropic", "openai")
        self.h.services.model_info[GPT] = {"id": GPT, "name": "GPT X"}
        pass_model_check(self.h, GPT)
        self.assertNotIn(GPT, self.listed())
        self.h.settings.openai_api_key = "sk-openai-test-key-0123456789"
        self.assertIn(GPT, self.listed())

    def test_anthropic_backend_lists_only_claude_models(self):
        self.h.settings.ai_providers = ("anthropic", "openai")
        self.h.settings.openai_api_key = "sk-openai-test-key-0123456789"
        self.h.services.model_info[GPT] = {"id": GPT, "name": "GPT X"}
        pass_model_check(self.h, GPT)
        self.assertEqual(self.listed(), {SONNET, OPUS})

    def test_judge_lists_claude_models_only(self):
        self.h.settings.ai_backend = "fake"
        self.h.settings.ai_providers = ("anthropic", "openai")
        self.h.settings.openai_api_key = "sk-openai-test-key-0123456789"
        self.h.services.model_info[GPT] = {"id": GPT, "name": "GPT X"}
        self.assertIn(SONNET, self.listed("judge"))
        self.assertNotIn(GPT, self.listed("judge"))

    # ---------------------------------------------------------------- set_model
    def test_configured_default_and_hard_models_need_no_check(self):
        """The default and hard-problem models ran before the check existed, so they stay allowed without one."""
        h = Harness(anthropic_api_key=KEY)  # no safety check has been run
        try:
            settings = h.services.settings
            listed = {o["id"] for o in selectable_models(h.services, purpose="chat")}
            self.assertIn(settings.model_default, listed)
            self.assertIn(settings.model_hard, listed)
            self.assertNotIn(HAIKU, listed)
            from subzero_ai.model_choices import model_block
            self.assertEqual(model_block(settings, h.services.db, settings.model_hard), "")
            self.assertNotEqual(model_block(settings, h.services.db, HAIKU), "")
        finally:
            h.close()

    def test_an_old_chat_moves_to_the_default_when_its_model_stops_passing(self):
        """Chats made before 0.11 could use another model; the rule that checks it again on every reply stays."""
        first = self.h.slack_dm("Hello robot", user=MENTOR)
        cid = first.conversation_id
        self.h.services.db.execute("UPDATE conversations SET model = ? WHERE id = ?", (OPUS, cid))
        self.h.slack_dm("A follow-up question", user=MENTOR, thread_ts=first.thread_ts)
        self.assertEqual(self.audit("model_fallback"), [])  # still passing, so the chat keeps Opus
        pass_model_check(self.h, OPUS, failing="unsafe")  # the newest result is a failure now
        self.h.slack_dm("Another follow-up", user=MENTOR, thread_ts=first.thread_ts)
        self.assertEqual(self.conv_model(cid), SONNET)
        streams = [c for c in self.h.claude.calls if c.get("kind") != "complete"]
        self.assertEqual(streams[-1]["model"], SONNET)
        fallback = self.audit("model_fallback")[-1]
        self.assertEqual((fallback["from"], fallback["to"]), (OPUS, SONNET))

    # ---------------------------------------------------------------- Settings
    def claude_form(self, **changes) -> dict:
        form = {"ai_providers": ["anthropic"], "model_default": SONNET, "model_hard": OPUS, "model_titles": HAIKU,
                "effort_default": "medium", "max_tokens_chat": "8000"}
        form.update(changes)
        return form

    def test_settings_show_dropdowns_and_accept_other(self):
        page = self.admin.get("/settings", headers=HTML).text
        self.assertIn('<select id="f-model_default"', page)
        self.assertIn('<optgroup label="Anthropic (Claude)">', page)
        self.assertIn('value="__other__"', page)
        self.assertIn("not yet safety-checked", page)  # Haiku is listed, marked
        self.assertIn('<select id="f-effort_default"', page)

        # Other with an unchecked id is refused, the same as a typed one would be
        resp = self.admin.post("/settings/claude", self.claude_form(model_default="__other__",
                                                                    model_default_other=HAIKU), headers=HTML)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Run the model safety check on " + HAIKU, resp.text)
        self.assertEqual(self.h.settings.model_default, SONNET)

        # Other with a checked id saves it
        pass_model_check(self.h, HAIKU)
        resp = self.admin.post("/settings/claude", self.claude_form(model_default="__other__",
                                                                    model_default_other=HAIKU), headers=HTML)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.h.settings.model_default, HAIKU)

        # Other with junk is still refused by the same validation
        resp = self.admin.post("/settings/claude", self.claude_form(model_default="__other__",
                                                                    model_default_other="gpt-9"), headers=HTML)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Use provider/model", resp.text)

    def test_choice_fields_refuse_unknown_values(self):
        resp = self.admin.post("/settings/claude", self.claude_form(effort_default="extreme"), headers=HTML)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Pick low, medium, or high.", resp.text)


if __name__ == "__main__":
    unittest.main()
