"""Chats that scroll inside the window, admins deleting chats, and the cost shown next to token counts."""

from __future__ import annotations

import re
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
