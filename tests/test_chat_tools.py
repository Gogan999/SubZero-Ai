"""Chats that scroll inside the window, admins deleting chats, and the cost shown next to token counts."""

from __future__ import annotations

import unittest

from .helpers import ADMIN, MENTOR, STUDENT, STUDENT_2, Harness


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.ava = self.h.login(STUDENT)
        self.ben = self.h.login(STUDENT_2)
        self.kim = self.h.login(MENTOR)
        self.coach = self.h.login(ADMIN)

    def tearDown(self):
        self.h.close()

    def start(self, text, actor=None):
        resp = (actor or self.kim).post("/chats/new", {"text": text})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.h.idle()
        return body["message_id"], int(body["url"].rsplit("/", 1)[1])


class ScrollingChatTest(ChatTest):
    def test_chat_page_scrolls_its_messages_not_the_window(self):
        _, cid = self.start("How do I tune a PID loop?")
        page = self.kim.get(f"/c/{cid}").text
        self.assertRegex(page, r'<body class="[^"]*\bchat-page\b')
        scroller = page.index("data-scroller")
        self.assertLess(scroller, page.index("data-thread"))
        # The composer sits outside the scrolling box, so it stays at the bottom of the window
        self.assertGreater(page.index("data-composer"), page.index("</ol>"))
        # The AI notice is in both places; CSS shows the fixed one on wide screens and the in-thread one on phones
        self.assertEqual(page.count("You're chatting with an AI, not a person."), 2)
        self.assertIn("notice-fixed", page)
        self.assertIn("notice-in-thread", page)

    def test_other_pages_keep_the_normal_layout(self):
        page = self.kim.get("/profile").text
        self.assertNotIn("chat-page", page)
        self.assertEqual(page.count("You're chatting with an AI, not a person."), 1)


class DeleteChatTest(ChatTest):
    def student_chat_with_file(self):
        resp = self.ava.post("/chats/new", {"text": "Why won't my intake spin?"},
                             files=[("files", ("Intake.java", b"class Intake {}", "text/x-java"))])
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.h.idle()
        return body["message_id"], int(body["url"].rsplit("/", 1)[1])

    def delete(self, actor, cid):
        return actor.post(f"/c/{cid}/delete", follow_redirects=False)

    def test_admin_deletes_a_chat_with_its_files_and_an_audit_note(self):
        mid, cid = self.student_chat_with_file()
        files = self.h.services.attachments.for_message(mid)
        self.assertEqual(len(files), 1)
        path = self.h.services.attachments.path(files[0])
        self.assertTrue(path.is_file())
        self.assertTrue(self.h.slack.posts, "the pending message should have a review card in Slack")

        resp = self.delete(self.coach, cid)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/recents")
        self.h.idle()
        self.assertIsNone(self.h.services.store.conversation(cid))
        self.assertIsNone(self.h.message(mid))
        self.assertFalse(path.exists())
        self.assertEqual(self.ava.get(f"/c/{cid}").status_code, 404)

        entry = self.h.services.db.one("SELECT * FROM audit_log WHERE action = 'chat_deleted'")
        self.assertEqual(entry["actor_id"], self.coach.user["id"])
        self.assertEqual(entry["target_id"], cid)
        self.assertIn("Ava (student)", entry["details"])
        self.assertIn('"messages": 1', entry["details"])

        # The waiting review card in Slack loses its buttons and says what happened
        update = self.h.slack.updates[-1]
        self.assertIn("deleted", update["text"])
        self.assertFalse(any(b.get("type") == "actions" for b in update["blocks"]))
        self.assertIn("Chat deleted", str(update["blocks"]))

    def test_deleting_a_chat_with_replies_and_a_share(self):
        _, cid = self.start("How do I tune a PID loop?")
        self.kim.send(cid, "And for an elevator?")
        self.h.idle()
        self.assertGreaterEqual(len(self.h.services.store.messages(cid)), 4)
        self.h.services.db.execute(
            "INSERT INTO shares (conversation_id, token, upto_message_id, audience, requested_by, status, created_at) "
            "VALUES (?, 'x', ?, 'team', ?, 'pending', '2026-01-01T00:00:00+00:00')",
            (cid, self.h.services.store.last_message_id(cid), self.kim.user["id"]))
        self.assertEqual(self.delete(self.coach, cid).status_code, 303)
        self.assertIsNone(self.h.services.store.conversation(cid))
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?",
                                                (cid,))["n"], 0)
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) AS n FROM shares")["n"], 0)

    def test_only_admins_can_delete(self):
        _, cid = self.student_chat_with_file()
        for actor in (self.ava, self.ben, self.kim):
            resp = self.delete(actor, cid)
            self.assertEqual(resp.status_code, 403, actor.slack_user_id)
        self.assertIsNotNone(self.h.services.store.conversation(cid))
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM audit_log WHERE action = 'chat_deleted'"))

    def test_delete_needs_the_csrf_token(self):
        _, cid = self.start("Hello")
        resp = self.coach.post(f"/c/{cid}/delete", csrf=False, follow_redirects=False)
        self.assertEqual(resp.status_code, 403)
        self.assertIsNotNone(self.h.services.store.conversation(cid))

    def test_delete_button_shows_for_admins_only(self):
        _, cid = self.start("Hello")
        self.assertIn(f'action="/c/{cid}/delete"', self.coach.get(f"/c/{cid}").text)
        self.assertIn("data-confirm=\"Delete this chat for everyone?", self.coach.get(f"/c/{cid}").text)
        self.assertNotIn("/delete", self.kim.get(f"/c/{cid}").text)

    def test_deleting_a_missing_chat(self):
        resp = self.delete(self.coach, 999)
        self.assertEqual(resp.status_code, 404)


class PricingTest(unittest.TestCase):
    def test_prices_per_million_tokens(self):
        from subzero_ai import pricing
        # 1M in + 1M out on Sonnet 5.5 is $2 + $10
        self.assertAlmostEqual(pricing.cost_usd("anthropic/claude-sonnet-5-5", input_tokens=1_000_000,
                                                output_tokens=1_000_000), 12.0)
        # Bare Claude ids from older settings work too
        self.assertAlmostEqual(pricing.cost_usd("claude-opus-5-5", input_tokens=1_000_000), 4.0)
        # Cache reads and writes, and web searches
        self.assertAlmostEqual(pricing.cost_usd("anthropic/claude-opus-5-5", cache_read_tokens=1_000_000), 0.20)
        self.assertAlmostEqual(pricing.cost_usd("anthropic/claude-opus-5-5", cache_write_tokens=1_000_000), 5.0)
        self.assertAlmostEqual(pricing.cost_usd("anthropic/claude-sonnet-5-5", web_searches=3), 0.03)

    def test_long_prompt_rate_only_for_one_request(self):
        from subzero_ai import pricing
        model = "anthropic/claude-haiku-5-5"
        self.assertAlmostEqual(pricing.cost_usd(model, input_tokens=100_000), 0.01)
        self.assertAlmostEqual(pricing.cost_usd(model, input_tokens=200_000), 0.10)
        self.assertAlmostEqual(pricing.cost_usd(model, input_tokens=200_000, one_request=False), 0.02)

    def test_unknown_gateway_and_subscription_models(self):
        from subzero_ai import pricing
        self.assertIsNone(pricing.cost_usd("someone/mystery-model", input_tokens=10))
        info = {"openai/gpt-x": {"cost": {"input": 1.5, "output": 6, "cacheRead": 0.15}}}
        self.assertAlmostEqual(pricing.cost_usd("openai/gpt-x", input_tokens=1_000_000, output_tokens=1_000_000,
                                                model_info=info), 7.5)
        self.assertEqual(pricing.cost_usd("claude-bridge/claude-opus-5-5", input_tokens=5000), 0.0)

    def test_labels(self):
        from subzero_ai import pricing
        self.assertEqual(pricing.cost_label(None), "")
        self.assertEqual(pricing.cost_label(0.0), "no API cost")
        self.assertEqual(pricing.cost_label(0.0004), "under $0.001")
        self.assertEqual(pricing.cost_label(0.0042), "about $0.004")
        self.assertEqual(pricing.cost_label(1.234), "about $1.23")
        self.assertEqual(pricing.usage_line(12345, 0.5), "12,345 tokens · about $0.50")
        self.assertEqual(pricing.usage_line(12345, None), "12,345 tokens")


class CostShownTest(ChatTest):
    def test_mentors_see_tokens_and_cost_on_each_reply(self):
        mid, cid = self.start("How do I tune a PID loop?")
        reply = self.h.reply_to(mid)
        self.h.services.db.execute(
            "UPDATE messages SET model = 'anthropic/claude-sonnet-5-5', input_tokens = 2000, output_tokens = 1000, "
            "cache_read_tokens = 0, cache_write_tokens = 0, web_searches = NULL WHERE id = ?", (reply["id"],))
        # 2,000 x $2/M + 1,000 x $10/M = $0.014
        page = self.kim.get(f"/c/{cid}").text
        self.assertIn("3,000 tokens · about $0.01", page)
        self.assertIn('class="msg-usage"', page)
        self.assertIn("3,000 tokens · about $0.01", self.kim.get(f"/c/{cid}/m/{reply['id']}").text)

    def test_students_dont_see_cost(self):
        mid, cid = self.start("How do I tune a PID loop?", actor=self.ava)
        self.assertEqual(self.kim.post(f"/m/{mid}/approve").status_code, 200)
        self.h.idle()
        reply = self.h.reply_to(mid)
        self.assertTrue(reply["output_tokens"], "the pretend Claude should report tokens")
        page = self.ava.get(f"/c/{cid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'id="m{reply["id"]}"', page.text)
        self.assertNotIn("msg-usage", page.text)
        self.assertIn("msg-usage", self.kim.get(f"/c/{cid}").text)

    def test_new_replies_save_cache_write_tokens(self):
        mid, _ = self.start("Hello there")
        reply = self.h.reply_to(mid)
        self.assertIn("cache_write_tokens", reply.keys())

    def test_code_session_shows_cost_next_to_tokens(self):
        from subzero_ai.web_code import session_view
        row = {"id": 1, "title": "t", "task": "x", "status": "done", "owner_name": "Kim", "repo": "r",
               "model": "anthropic/claude-opus-5-5", "ref": "", "error": None, "updated_at": "2026-01-01T00:00:00+00:00",
               "created_at": "2026-01-01T00:00:00+00:00", "input_tokens": 100_000, "output_tokens": 10_000,
               "owner_is_student": 0, "screen_note": None}

        class Row(dict):
            def keys(self):
                return list(super().keys())

        view = session_view(self.h.services, Row(row))
        # 100,000 x $4/M + 10,000 x $20/M = $0.60
        self.assertEqual(view["usage"], "110,000 tokens · about $0.60")


class CacheWriteMigrationTest(unittest.TestCase):
    def test_a_filled_version_15_database_gains_the_column(self):
        import shutil
        import sqlite3
        import tempfile
        from pathlib import Path

        from subzero_ai.db import MIGRATIONS, Database
        tmp = Path(tempfile.mkdtemp(prefix="cache-write-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "old.db"
        conn = sqlite3.connect(path)
        for script in MIGRATIONS[:15]:
            conn.executescript("BEGIN;" + script + "COMMIT;")
        conn.execute("PRAGMA user_version = 15")
        conn.execute("INSERT INTO users (id, slack_user_id, name, role, created_at) VALUES (1, 'U1', 'Kim', 'mentor', "
                     "'2026-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO conversations (id, owner_id, model, created_at, updated_at) VALUES (1, 1, "
                     "'anthropic/claude-sonnet-5-5', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO messages (conversation_id, role, content, status, input_tokens, created_at, "
                     "updated_at) VALUES (1, 'assistant', 'Hi', 'complete', 42, '2026-01-01T00:00:00+00:00', "
                     "'2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        db = Database(path)
        row = db.one("SELECT input_tokens, cache_write_tokens FROM messages")
        self.assertEqual((row["input_tokens"], row["cache_write_tokens"]), (42, None))
        self.assertEqual(db.one("PRAGMA user_version")[0], len(MIGRATIONS))
