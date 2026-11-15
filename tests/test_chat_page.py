"""The website after the chat pages were removed: students get no chat pages, mentors and admins read a chat on a
read-only page with the review buttons, old addresses lead to Code with a one-time note, and sign-in lands on Code."""

from __future__ import annotations

import re
import unittest

from .helpers import ADMIN, MENTOR, STUDENT, STUDENT_2, Harness

NOTE = "Chats have moved to Slack. Message the bot there."
HTML = {"Accept": "text/html"}


class ChatPageTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(code_student_access=True)
        self.ava, self.ben = self.h.login(STUDENT), self.h.login(STUDENT_2)
        self.kim, self.coach = self.h.login(MENTOR), self.h.login(ADMIN)
        self.h.slack_people()
        # A chat from Slack: an approved first message with its reply, then a follow-up that is still waiting
        self.first = self.h.slack_dm("How do I tune the arm feedforward?", user=STUDENT)
        self.cid = self.first.conversation_id
        self.h.slack_tap("approve", self.first.message_id)
        self.follow = self.h.slack_dm("And what about the D term?", user=STUDENT, thread_ts=self.first.thread_ts)
        self.h.add_old_attachment(self.first.message_id, "Arm.java")

    def tearDown(self):
        self.h.close()

    def main(self, who, path=None, **kwargs):
        return who.get(path or f"/c/{self.cid}", headers=HTML, **kwargs).text.split('id="main"', 1)[1]

    # ------------------------------------------------------------ students
    def test_students_are_sent_to_code_with_a_note_and_never_get_the_chat(self):
        for who in (self.ava, self.ben):
            resp = who.get(f"/c/{self.cid}", headers=HTML, follow_redirects=False)
            self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/code"), who.slack_user_id)
            for text in ("feedforward", "D term", "Arm.java"):
                self.assertNotIn(text, resp.text)
        # The note shows once on the Code page the redirect leads to
        browser = self.h.browser(self.ava)
        browser.get(f"/c/{self.cid}", headers=HTML, follow_redirects=False)
        first = browser.get("/code", headers=HTML)
        self.assertEqual(first.status_code, 200)
        self.assertIn(NOTE, first.text)
        self.assertNotIn(NOTE, browser.get("/code", headers=HTML).text, "the note shows only once")

    def test_a_chat_that_does_not_exist_gives_a_student_the_same_answer(self):
        resp = self.ava.get("/c/99999", headers=HTML, follow_redirects=False)
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/code"))
        self.assertEqual(self.kim.get("/c/99999", headers=HTML).status_code, 404)

    def test_the_note_also_shows_when_student_code_access_is_off(self):
        self.h.settings.code_student_access = False
        browser = self.h.browser(self.ava)
        browser.get(f"/c/{self.cid}", headers=HTML, follow_redirects=False)
        page = browser.get("/code", headers=HTML)  # today's plain page for this case
        self.assertEqual(page.status_code, 403)
        self.assertIn(NOTE, page.text)
        self.assertNotIn(NOTE, browser.get("/code", headers=HTML).text)

    def test_signed_out_people_sign_in_first(self):
        self.h.client.cookies.clear()
        resp = self.h.client.get(f"/c/{self.cid}", follow_redirects=False)
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/login"))
        self.assertNotIn("feedforward", resp.text)

    def test_no_chat_text_comes_out_of_any_other_route_for_a_student(self):
        for path in ("/queue", "/queue/items", "/queue/events", "/files/1", f"/c/{self.cid}/events",
                     f"/c/{self.cid}/m/{self.first.message_id}", "/audit", "/settings"):
            resp = self.ava.get(path, headers=HTML, follow_redirects=False)
            self.assertNotIn("feedforward", resp.text, path)
            self.assertNotIn("D term", resp.text, path)
        self.assertEqual(self.ava.get("/files/1", follow_redirects=False).status_code, 404)

    def test_a_student_cannot_approve_or_reject(self):
        for who in (self.ava, self.ben):
            for action, data in (("approve", {}), ("reject", {"reason": "x"}), ("hold", {})):
                self.assertEqual(who.post(f"/m/{self.follow.message_id}/{action}", data).status_code, 403, action)
        self.assertEqual(self.h.message(self.follow.message_id)["status"], "pending")

    # ------------------------------------------------------------- mentors
    def test_mentors_and_admins_read_the_whole_chat_with_no_way_to_write(self):
        for who in (self.kim, self.coach):
            page = self.main(who)
            for text in ("How do I tune the arm feedforward?", "And what about the D term?", "Approved by Sam Ortiz",
                         "Waiting for a mentor", "Arm.java"):
                self.assertIn(text, page, f"{who.slack_user_id} {text}")
            self.assertIn('href="/files/', page, "old attached files are listed with links")
            self.assertNotIn("<textarea id=\"reply-text\"", page)
            self.assertNotIn("data-composer", page)
            self.assertNotIn('name="model"', page)
            self.assertNotIn("/delete", page)
            self.assertNotIn("/messages", page)
            self.assertNotIn('action="/c/', page, "no form posts to a chat address")
            self.assertNotIn("Recents", page)
            self.assertNotIn("/recents", page)

    def test_review_buttons_appear_only_on_the_waiting_message(self):
        page = self.main(self.kim)
        mid = self.follow.message_id
        for action in ("approve", "reject", "hold"):
            self.assertIn(f"/m/{mid}/{action}", page)
        self.assertIn(f'data-form-action="/m/{mid}/approve"', page, "Edit posts to approve with the new text")
        for action in ("approve", "reject", "hold"):
            self.assertNotIn(f"/m/{self.first.message_id}/{action}", page)
        # A mentor whose own message is waiting can't review it
        self.h.settings.mentors_need_approval = True
        own = self.h.slack_dm("A mentor's own question", user=MENTOR)
        self.assertNotIn(f"/m/{own.message_id}/approve", self.main(self.kim, f"/c/{own.conversation_id}"))
        self.assertIn(f"/m/{own.message_id}/approve", self.main(self.coach, f"/c/{own.conversation_id}"))

    def test_deciding_on_the_chat_page_goes_through_the_same_routes_as_the_review_page(self):
        queue = self.kim.get("/queue", headers=HTML).text
        page = self.main(self.kim)
        for needle in (f"/m/{self.follow.message_id}/approve", f"/m/{self.follow.message_id}/reject",
                       f"/m/{self.follow.message_id}/hold"):
            self.assertIn(needle, queue)
            self.assertIn(needle, page)
        # Edit, from the chat page, then Reject and Handle privately on two more waiting messages
        resp = self.kim.post(f"/m/{self.follow.message_id}/approve", {"text": "What does the D term do on an arm?"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.h.idle()
        self.assertEqual(self.h.message(self.follow.message_id)["status"], "edited")
        third = self.h.slack_dm("A third question", user=STUDENT, thread_ts=self.first.thread_ts)
        self.assertEqual(self.kim.post(f"/m/{third.message_id}/reject", {"reason": "One thing at a time"}).status_code, 200)
        fourth = self.h.slack_dm("A fourth question", user=STUDENT, thread_ts=self.first.thread_ts)
        self.assertEqual(self.kim.post(f"/m/{fourth.message_id}/hold").status_code, 200)
        page = self.main(self.kim)
        for text in ("Edited by Mentor Kim", "Not approved by Mentor Kim", "One thing at a time",
                     "Handled privately by Mentor Kim"):
            self.assertIn(text, page)
        self.assertNotIn("/approve", page.replace("/queue", ""), "no waiting message is left to review")

    def test_the_page_can_refresh_itself_with_only_the_messages(self):
        part = self.kim.get(f"/c/{self.cid}?thread=1", headers=HTML).text
        self.assertTrue(part.lstrip().startswith('<li class="msg person'), part[:80])
        self.assertNotIn("<html", part)
        self.assertEqual(self.ava.get(f"/c/{self.cid}?thread=1", follow_redirects=False).status_code, 303)
        script = self.kim.get("/static/app.js").text
        self.assertIn("/queue/events", script)
        self.assertIn("?thread=1", script)

    def test_the_review_page_links_each_item_to_its_chat(self):
        queue = self.kim.get("/queue", headers=HTML).text
        self.assertIn(f'href="/c/{self.cid}"', queue)
        self.assertRegex(queue, r'Review\s*<span class="count"[^>]*>1</span>')

    def test_a_private_chat_is_still_readable_by_mentors(self):
        self.h.slack_tap("handle_private", self.follow.message_id)
        self.assertIn("Private: only", self.main(self.kim))
        self.assertEqual(self.ben.get(f"/c/{self.cid}", follow_redirects=False).status_code, 303)

    # -------------------------------------------------------- removed routes
    def test_removed_chat_routes_are_gone(self):
        for who in (self.ava, self.kim, self.coach):
            for path in ("/chats/new", f"/c/{self.cid}/messages", f"/c/{self.cid}/model", f"/c/{self.cid}/delete"):
                self.assertIn(who.post(path, {"text": "x", "model": "x"}).status_code, (404, 405), path)
            for path in (f"/c/{self.cid}/events", f"/c/{self.cid}/m/{self.first.message_id}", "/chats/new"):
                self.assertEqual(who.get(path).status_code, 404, path)

    def test_the_queue_stream_still_needs_a_mentor(self):
        self.assertEqual(self.ava.get("/queue/events").status_code, 403)


class OldAddressesTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(code_student_access=True)

    def tearDown(self):
        self.h.close()

    def test_old_addresses_lead_to_code_with_the_note_shown_once(self):
        for who in (STUDENT, MENTOR, ADMIN):
            browser = self.h.browser(self.h.login(who))
            for path in ("/chats", "/recents", "/projects", "/projects/3", "/projects/3/files/1", "/artifacts",
                         "/artifacts/4/0"):
                resp = browser.get(path, headers=HTML, follow_redirects=False)
                self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/code"), f"{who} {path}")
                page = browser.get("/code", headers=HTML).text
                self.assertIn(NOTE, page, f"{who} {path}")
                self.assertNotIn(NOTE, browser.get("/code", headers=HTML).text)

    def test_old_addresses_with_a_query_string_still_work(self):
        ava = self.h.login(STUDENT)
        resp = ava.get("/chats?scope=mine&q=arm", headers=HTML, follow_redirects=False)
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/code"))

    def test_old_addresses_need_no_sign_in_to_answer_and_leave_no_chat_data(self):
        self.h.client.cookies.clear()
        resp = self.h.client.get("/recents", follow_redirects=False)
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/code"))
        resp = self.h.client.get("/code", follow_redirects=False)
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/login"))

    def test_the_old_address_post_routes_do_not_exist(self):
        ava = self.h.login(STUDENT)
        self.assertIn(ava.post("/projects", {"name": "x"}).status_code, (404, 405))
        self.assertIn(ava.post("/artifacts/1", {}).status_code, (404, 405))

    def test_the_home_page_and_sign_in_lead_to_code(self):
        self.assertEqual(self.h.client.get("/", follow_redirects=False).headers["location"], "/login")
        ava = self.h.login(STUDENT)
        self.assertEqual(ava.get("/", follow_redirects=False).headers["location"], "/code")
        self.assertEqual(ava.get("/login", follow_redirects=False).headers["location"], "/code")
        for who in (STUDENT, MENTOR, ADMIN):
            self.h.client.cookies.clear()
            resp = self.h.client.post("/auth/dev", data={"user": who}, follow_redirects=False)
            self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/code"), who)

    def test_a_student_with_code_access_off_still_gets_a_plain_page(self):
        self.h.settings.code_student_access = False
        ava = self.h.login(STUDENT)
        resp = ava.get("/code", headers=HTML)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Code is for mentors", resp.text)
        self.assertNotIn("/chats", resp.text)
        self.assertIn('href="/code"', resp.text)  # the button and the app name lead back here, not to /chats

    def test_error_pages_link_to_code(self):
        kim = self.h.login(MENTOR)
        page = kim.get("/c/12345", headers=HTML).text
        self.assertIn("Chat not found", page)
        self.assertNotIn("/chats", page)
        self.assertRegex(page, r'<a class="button" href="/code">')

    def test_no_template_or_script_links_to_removed_pages(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "subzero_ai"
        for path in list((root / "templates").glob("*.html")) + [root / "static" / "app.js"]:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r'["\'(]/(?:chats|recents)\b', path.name)


class PagesForEveryRoleTest(unittest.TestCase):
    """A smoke test: the pages that are left draw for each role, with the right sidebar."""

    def setUp(self):
        self.h = Harness(code_student_access=True)
        self.cid = self.h.slack_dm("A question", user=STUDENT).conversation_id

    def tearDown(self):
        self.h.close()

    def test_every_page_draws(self):
        pages = {STUDENT: ["/code", "/about", "/profile"], MENTOR: ["/code", "/queue", f"/c/{self.cid}", "/about", "/profile"],
                 ADMIN: ["/code", "/queue", f"/c/{self.cid}", "/settings", "/audit", "/about", "/profile"]}
        for who, paths in pages.items():
            actor = self.h.login(who)
            for path in paths:
                resp = actor.get(path, headers=HTML)
                self.assertEqual(resp.status_code, 200, f"{who} {path}")
                self.assertNotIn("You're chatting with an AI", resp.text, "no chat disclaimer on every page")
                self.assertEqual(len(re.findall(r'class="sidebar"', resp.text)), 1)
