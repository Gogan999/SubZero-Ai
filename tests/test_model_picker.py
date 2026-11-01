"""The model picker: what a mentor can choose for a new chat or an existing chat, what the Settings dropdowns offer,
and what happens when a chat's model stops being allowed. Offline: the fake AI client from tests/helpers.py."""

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

    def mentor_chat(self, model: str, text: str = "Hello robot") -> int:
        resp = self.kim.post("/chats/new", {"text": text, "subteam": "Programming", "model": model})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.h.idle()
        return int(resp.json()["url"].rsplit("/", 1)[1])

    def listed(self, purpose: str = "chat") -> set[str]:
        return {option["id"] for option in selectable_models(self.h.services, purpose=purpose)}

    # ---------------------------------------------------------------- New chat
    def test_student_model_field_is_ignored(self):
        resp = self.ava.post("/chats/new", {"text": "Why is my arm slow?", "subteam": "Programming", "model": OPUS})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.h.idle()
        self.assertEqual(self.conv_model(int(resp.json()["url"].rsplit("/", 1)[1])), SONNET)

    def test_mentor_choice_is_stored_on_the_chat(self):
        cid = self.mentor_chat(OPUS)
        self.assertEqual(self.conv_model(cid), OPUS)
        self.assertEqual(self.audit("conversation_started")[-1]["model"], OPUS)
        self.assertIn(f'value="{OPUS}"', self.kim.get("/chats", headers=HTML).text)

    def test_mentor_new_chat_page_offers_checked_models_with_default_selected(self):
        page = self.kim.get("/chats", headers=HTML).text
        self.assertIn('<select id="new-model"', page)
        self.assertIn(f'<option value="{SONNET}" selected>', page)
        self.assertIn(f'<option value="{OPUS}"', page)
        self.assertNotIn("haiku", page.lower())  # never checked, so not offered
        self.assertNotIn('<select id="new-model"', self.ava.get("/chats", headers=HTML).text)

    def test_unchecked_model_is_refused_with_a_plain_note(self):
        # Keep the session cookie the new-chat reply sets, as a browser does, so the note reaches the next page
        self.h.client.cookies.clear()
        resp = self.h.client.post("/chats/new", data={"text": "Hello robot", "subteam": "Programming", "model": HAIKU},
                                  headers={"Cookie": f"subzero_session={self.kim.cookie}",
                                           "X-CSRF-Token": self.kim.csrf, "Accept": "application/json"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.h.idle()
        cid = int(resp.json()["url"].rsplit("/", 1)[1])
        self.assertEqual(self.conv_model(cid), SONNET)
        page = self.h.client.get(f"/c/{cid}", headers=HTML).text
        self.assertIn("Haiku 5.5 can", page)
        self.assertIn("be used for this chat", page)
        self.assertEqual(self.kim.post(f"/c/{cid}/model", {"model": HAIKU}).status_code, 400)

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
        self.assertNotIn("openai", self.kim.get("/chats", headers=HTML).text)

    def test_judge_lists_claude_models_only(self):
        self.h.settings.ai_backend = "fake"
        self.h.settings.ai_providers = ("anthropic", "openai")
        self.h.settings.openai_api_key = "sk-openai-test-key-0123456789"
        self.h.services.model_info[GPT] = {"id": GPT, "name": "GPT X"}
        self.assertIn(SONNET, self.listed("judge"))
        self.assertNotIn(GPT, self.listed("judge"))

    # ---------------------------------------------------------------- set_model
    def test_set_model_uses_the_same_filter(self):
        cid = self.mentor_chat(SONNET)
        self.assertEqual(self.kim.post(f"/c/{cid}/model", {"model": HAIKU}).status_code, 400)  # not checked
        self.assertEqual(self.kim.post(f"/c/{cid}/model", {"model": GPT}).status_code, 400)  # not a listed model
        pass_model_check(self.h, HAIKU)
        self.assertEqual(self.kim.post(f"/c/{cid}/model", {"model": HAIKU}).status_code, 200)
        self.assertEqual(self.conv_model(cid), HAIKU)
        self.assertEqual(self.audit("model_changed")[-1]["to"], HAIKU)
        self.assertEqual(self.ava.post(f"/c/{cid}/model", {"model": OPUS}).status_code, 403)

    def test_chat_moves_to_default_when_its_model_stops_passing(self):
        cid = self.mentor_chat(OPUS)
        self.kim.send(cid, "A follow-up question")
        self.h.idle()
        self.assertEqual(self.audit("model_fallback"), [])  # still passing, so the chat keeps Opus
        pass_model_check(self.h, OPUS, failing="unsafe")  # the newest result is a failure now
        self.kim.send(cid, "Another follow-up")
        self.h.idle()
        self.assertEqual(self.conv_model(cid), SONNET)
        streams = [c for c in self.h.claude.calls if c.get("kind") != "complete"]
        self.assertEqual(streams[-1]["model"], SONNET)
        fallback = self.audit("model_fallback")[-1]
        self.assertEqual((fallback["from"], fallback["to"]), (OPUS, SONNET))

    # ---------------------------------------------------------------- Settings
    def claude_form(self, **changes) -> dict:
        form = {"ai_providers": ["anthropic"], "model_default": SONNET, "model_hard": OPUS, "model_titles": HAIKU,
                "effort_default": "medium", "effort_high": "high", "max_tokens_chat": "8000",
                "max_tokens_explainer": "16000", "long_chat_tokens": "150000", "web_search_daily_limit": "50"}
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
