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
