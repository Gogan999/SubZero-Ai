"""Deleting chats (the maintenance function), the price list, and the cost mentors see on a reply."""

from __future__ import annotations

import unittest

from subzero_ai import maintenance

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
        """Someone messages the bot in Slack (a mentor's own message goes straight through)."""
        sent = self.h.slack_dm(text, user=(actor or self.kim).slack_user_id)
        return sent.message_id, sent.conversation_id


class DeleteChatTest(ChatTest):
    """The Delete chat button is gone, but maintenance.delete_chat stays: the archive feature uses it."""

    def delete(self, cid, actor=None):
        s = self.h.services
        return maintenance.delete_chat(s.db, s.attachments.files_dir, cid, actor_id=(actor or self.coach).user["id"])

    def student_chat_with_file(self):
        sent = self.h.slack_dm("Why won't my intake spin?", user=STUDENT)
        self.h.add_old_attachment(sent.message_id, "Intake.java", b"class Intake {}")  # a file from 0.10
        return sent.message_id, sent.conversation_id

    def test_deleting_a_chat_removes_its_files_and_leaves_an_audit_note(self):
        mid, cid = self.student_chat_with_file()
        files = self.h.services.attachments.for_message(mid)
        self.assertEqual(len(files), 1)
        path = self.h.services.attachments.path(files[0])
        self.assertTrue(path.is_file())

        deleted = self.delete(cid)
        self.assertIsNotNone(deleted)
        self.assertIsNone(self.h.services.store.conversation(cid))
        self.assertIsNone(self.h.message(mid))
        self.assertFalse(path.exists())
        self.assertEqual(self.coach.get(f"/c/{cid}").status_code, 404)

        entry = self.h.services.db.one("SELECT * FROM audit_log WHERE action = 'chat_deleted'")
        self.assertEqual(entry["actor_id"], self.coach.user["id"])
        self.assertEqual(entry["target_id"], cid)
        self.assertIn('"messages": 1', entry["details"])

    def test_deleting_a_chat_with_replies_and_an_old_share_row(self):
        first = self.h.slack_dm("How do I tune a PID loop?", user=MENTOR)
        self.h.slack_dm("And for an elevator?", user=MENTOR, thread_ts=first.thread_ts)
        cid = first.conversation_id
        self.assertGreaterEqual(len(self.h.services.store.messages(cid)), 4)
        self.h.services.db.execute(
            "INSERT INTO shares (conversation_id, token, upto_message_id, audience, requested_by, status, created_at) "
            "VALUES (?, 'x', ?, 'team', ?, 'pending', '2026-01-01T00:00:00+00:00')",
            (cid, self.h.services.store.last_message_id(cid), self.kim.user["id"]))
        self.assertIsNotNone(self.delete(cid))
        self.assertIsNone(self.h.services.store.conversation(cid))
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?",
                                                (cid,))["n"], 0)
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) AS n FROM shares")["n"], 0)

    def test_deleting_a_missing_chat(self):
        self.assertIsNone(self.delete(999))
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM audit_log WHERE action = 'chat_deleted'"))


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

    def test_students_get_no_chat_page_so_no_cost(self):
        mid, cid = self.start("How do I tune a PID loop?", actor=self.ava)
        self.assertEqual(self.kim.post(f"/m/{mid}/approve").status_code, 200)
        self.h.idle()
        self.assertTrue(self.h.reply_to(mid)["output_tokens"], "the pretend Claude should report tokens")
        page = self.ava.get(f"/c/{cid}", follow_redirects=False)
        self.assertEqual(page.status_code, 303)
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
