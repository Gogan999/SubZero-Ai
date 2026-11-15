"""The approval loop end to end: students message the bot in Slack, mentors decide on the Slack card or on the
website's Review page and read-only chat page. The behavior in the plan's "Roles and approval workflow" section and
the milestone 1 and 2 acceptance tests."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from subzero_ai.claude import FakeClaude, StreamEvent

from .backends import RecordingClaudeClient, RecordingPiClient, add_variants
from .helpers import ADMIN, MENTOR, STUDENT, STUDENT_2, Harness


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.ava = self.h.login(STUDENT)
        self.ben = self.h.login(STUDENT_2)
        self.kim = self.h.login(MENTOR)
        self.threads: dict[int, object] = {}  # chat id -> the Slack message that started it

    def tearDown(self):
        self.h.close()

    # ---------------------------------------------------------------- helpers
    def start(self, text="Why does our arm drift down when the robot is disabled?", actor=None):
        """The person messages the bot in Slack, which starts a chat; returns (message id, chat id)."""
        sent = self.h.slack_dm(text, user=(actor or self.ava).slack_user_id)
        self.assertIsNotNone(sent.message_id, "the message was saved")
        self.threads[sent.conversation_id] = sent
        return sent.message_id, sent.conversation_id

    def say(self, cid, text, actor=None):
        """A follow-up in the chat's Slack thread. Returns what the app did (message_id is None if it refused)."""
        first = self.threads[cid]
        return self.h.slack_dm(text, user=(actor or self.ava).slack_user_id, thread_ts=first.thread_ts)

    def thread_text(self, sent) -> str:
        return "\n".join(p["text"] for p in self.h.slack_thread(sent))

    def card_for(self, message_id):
        for post in self.h.slack.posts:
            actions = [b for b in post["blocks"] or [] if b.get("type") == "actions"]
            if actions and actions[0]["elements"][0]["value"] == str(message_id):
                return post
        return None

    def approve(self, message_id, actor=None, **data):
        resp = (actor or self.kim).post(f"/m/{message_id}/approve", data)
        self.h.idle()
        return resp

    # ------------------------------------------------------------ submitting
    def test_student_message_waits_and_posts_a_card(self):
        mid, cid = self.start()
        self.assertEqual(self.h.message(mid)["status"], "pending")
        self.assertEqual(self.h.claude.calls, [], "Claude must not see a message before a mentor approves it")
        card = self.card_for(mid)
        self.assertIsNotNone(card, "no approval card in Slack")
        self.assertEqual(card["channel"], "CAPPROVALS")
        buttons = [e["action_id"] for e in card["blocks"][-1]["elements"]]
        self.assertEqual(buttons, ["approve", "edit", "reject", "handle_private"])
        self.assertIn("Ava (student)", json.dumps(card["blocks"]))
        # Mentors can open the whole chat on the website; the student's own DMs have no website link
        self.assertIn(f"<http://localhost:8787/c/{cid}|Open the whole chat>", json.dumps(card["blocks"]))
        self.assertNotIn("http", self.thread_text(self.threads[cid]))
        # A mentor sees it marked as waiting, with the review buttons
        page = self.kim.get(f"/c/{cid}").text
        self.assertIn("Waiting for a mentor", page)
        self.assertIn(f"/m/{mid}/approve", page)

    def test_one_waiting_message_per_chat(self):
        mid, cid = self.start()
        sent = self.say(cid, "One more thing")
        self.assertIsNone(sent.message_id)
        self.assertIn("still waiting", self.thread_text(sent))

    def test_students_never_get_chat_pages_or_chat_text(self):
        mid, cid = self.start("Something I want a mentor to see first")
        self.approve(mid)
        for who in (self.ava, self.ben):  # the owner too
            for path in (f"/c/{cid}", f"/c/{cid}?thread=1", "/chats", "/recents", f"/c/{cid}/m/{mid}",
                         f"/c/{cid}/events"):
                resp = who.get(path, follow_redirects=False)
                self.assertNotIn("Something I want a mentor", resp.text, f"{who.slack_user_id} {path}")
                self.assertNotIn("FIRST Robotics", resp.text)
        for who in (self.ava, self.ben):
            resp = who.get(f"/c/{cid}", follow_redirects=False)
            self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/code"), who.slack_user_id)
        # Mentors see everything
        self.assertIn("Something I want a mentor to see first", self.kim.get(f"/c/{cid}").text)

    def test_browser_line_breaks_are_stored_as_newlines(self):
        mid, _ = self.start("line one\r\n\r\nline two\r\n")
        self.assertEqual(self.h.message(mid)["content"], "line one\n\nline two")
        self.approve(mid, text="edited\r\nversion")
        self.assertEqual(self.h.message(mid)["content"], "edited\nversion")

    def test_audit_entries_link_to_the_chat(self):
        mid, cid = self.start()
        self.approve(mid)
        page = self.h.login(ADMIN).get("/audit").text
        self.assertIn(f'href="/c/{cid}#m{mid}"', page)
        self.assertIn(f'href="/c/{cid}"', page)

    def test_huge_messages_are_refused(self):
        sent = self.h.slack_dm("x" * 20001, user=STUDENT)
        self.assertIsNone(sent.message_id)
        self.assertIn("20,000 characters", self.thread_text(sent))

    # ---------------------------------------------------------------- approve
    def test_approve_runs_claude_updates_card_and_answers_in_the_students_thread(self):
        mid, cid = self.start("How do I tune the arm's feedforward?")
        resp = self.approve(mid)
        self.assertEqual(resp.status_code, 200, resp.text)
        msg = self.h.message(mid)
        self.assertEqual(msg["status"], "approved")
        self.assertEqual(msg["reviewer_name"], "Mentor Kim")
        reply = self.h.reply_to(mid)
        self.assertEqual(reply["status"], "complete")
        self.assertEqual(reply["content"], FakeClaude.reply_for("How do I tune the arm's feedforward?"))
        self.assertEqual(reply["model"], "anthropic/claude-sonnet-5-5")
        # Claude got the safety prompt and only the approved text, with no names
        call = self.h.claude.calls[0]
        self.assertIn("high school FIRST Robotics Competition team", call["system"])
        self.assertEqual(call["messages"], [{"role": "user", "content": "How do I tune the arm's feedforward?"}])
        self.assertNotIn("Ava", json.dumps(call["messages"]))
        self.assertEqual(call["effort"], "medium")
        # The card now says who approved it, with no buttons left
        update = [u for u in self.h.slack.updates if u["channel"] == "CAPPROVALS"][-1]
        text = json.dumps(update["blocks"])
        self.assertIn("Approved by <@DEV-mentor-kim>", text)
        self.assertFalse(any(b["type"] == "actions" for b in update["blocks"]))
        # The student is not sent a separate DM: the answer arrives in their thread with the bot
        self.assertEqual(self.h.slack.dms, [])
        thread = self.h.slack_thread(self.threads[cid])
        self.assertEqual(thread[1]["text"], "Thinking…")
        final = [u for u in self.h.slack.updates if u["ts"] == thread[1]["ts"]][-1]["text"]
        self.assertIn("You asked:", final)
        self.assertNotIn("http", final)
        # The chat gets a title, and the reply renders as Markdown with a copy button
        conv = self.h.services.store.conversation(cid)
        self.assertTrue(conv["title"])
        page = self.kim.get(f"/c/{cid}").text
        self.assertIn("Approved by Mentor Kim", page)
        self.assertIn('class="code"', page)
        self.assertIn("data-copy", page)
        self.assertNotIn("<form", page.split('id="main"', 1)[1].replace('<form class="inline-form"', ""),
                         "a read-only page: the only forms are the review forms")

    def test_second_mentor_cannot_act_on_a_handled_message(self):
        mid, _ = self.start()
        self.assertEqual(self.approve(mid).status_code, 200)
        coach = self.h.login(ADMIN)
        for action, data in (("approve", {}), ("reject", {"reason": "no"}), ("hold", {})):
            resp = coach.post(f"/m/{mid}/{action}", data)
            self.assertEqual(resp.status_code, 409, action)
            self.assertEqual(resp.json()["error"], "Already approved by Mentor Kim.")
        self.h.idle()
        self.assertEqual(len(self.h.claude.calls), 2, "one reply and one title, no second reply")

    def test_follow_up_needs_its_own_approval_and_carries_context(self):
        mid, cid = self.start("First question")
        self.approve(mid)
        follow = self.say(cid, "Follow-up question").message_id
        self.assertEqual(self.h.message(follow)["status"], "pending")
        card = self.card_for(follow)
        self.assertIn("Replying to Claude", json.dumps(card["blocks"]))
        self.approve(follow)
        stream_calls = [c for c in self.h.claude.calls if c.get("kind") != "complete"]
        self.assertEqual([m["role"] for m in stream_calls[-1]["messages"]], ["user", "assistant", "user"])
        self.assertEqual(stream_calls[-1]["messages"][-1]["content"], "Follow-up question")

    # ------------------------------------------------------------------ edit
    def test_edit_sends_mentor_version_and_keeps_original(self):
        mid, cid = self.start("pls write my whole auto for me")
        resp = self.approve(mid, text="Help me plan a two-piece autonomous routine. What should I think about first?")
        self.assertEqual(resp.status_code, 200)
        msg = self.h.message(mid)
        self.assertEqual(msg["status"], "edited")
        self.assertEqual(msg["original_content"], "pls write my whole auto for me")
        stream_calls = [c for c in self.h.claude.calls if c.get("kind") != "complete"]
        self.assertEqual(stream_calls[0]["messages"][0]["content"],
                         "Help me plan a two-piece autonomous routine. What should I think about first?")
        audit = self.h.services.db.one("SELECT details FROM audit_log WHERE action = 'message_edited'")
        details = json.loads(audit["details"])
        self.assertEqual(details["before"], "pls write my whole auto for me")
        self.assertIn("two-piece", details["after"])
        # Mentors see the edited version and, folded away, what the student sent; the student is told in their thread
        page = self.kim.get(f"/c/{cid}").text
        self.assertIn("Edited by Mentor Kim", page)
        self.assertIn("Show what was originally sent", page)
        self.assertIn("pls write my whole auto", page)
        self.assertIn("A mentor edited your message before the AI saw it.", self.thread_text(self.threads[cid]))

    def test_edit_with_same_text_counts_as_approve(self):
        mid, _ = self.start("Same text")
        self.approve(mid, text="  Same text ")
        self.assertEqual(self.h.message(mid)["status"], "approved")

    def test_edit_to_empty_is_refused(self):
        mid, _ = self.start()
        self.assertEqual(self.approve(mid, text="   ").status_code, 400)
        self.assertEqual(self.h.message(mid)["status"], "pending")

    # ---------------------------------------------------------------- reject
    def test_reject_needs_a_reason_and_tells_the_student(self):
        mid, cid = self.start("Write my history essay")
        resp = self.kim.post(f"/m/{mid}/reject", {"reason": ""})
        self.assertEqual(resp.status_code, 400)
        resp = self.kim.post(f"/m/{mid}/reject", {"reason": "This tool is for team work. Try asking about the robot."})
        self.assertEqual(resp.status_code, 200)
        self.h.idle()
        msg = self.h.message(mid)
        self.assertEqual(msg["status"], "rejected")
        self.assertEqual(self.h.claude.calls, [])
        self.assertIn("A mentor didn't approve this: This tool is for team work", self.thread_text(self.threads[cid]))
        self.assertEqual(self.h.slack.dms, [], "the reason goes in the student's own thread, not a DM")
        self.assertIn("Rejected by <@DEV-mentor-kim>", json.dumps(self.h.slack.updates[-1]["blocks"]))
        page = self.kim.get(f"/c/{cid}").text
        self.assertIn("Not approved by Mentor Kim", page)
        self.assertIn("This tool is for team work", page)
        # The student can send a revised message right away
        revised = self.say(cid, "How do I document our robot's design for the award?")
        self.assertIsNotNone(revised.message_id)

    # ------------------------------------------------------- handle privately
    def test_handle_privately_hides_chat_and_sends_nothing_to_claude(self):
        first, cid = self.start("Question about the drivetrain")
        self.approve(first)
        follow = self.say(cid, "Something personal").message_id
        dms_before = len(self.h.slack.dms)
        resp = self.kim.post(f"/m/{follow}/hold")
        self.assertEqual(resp.status_code, 200)
        self.h.idle()
        self.assertEqual(self.h.message(follow)["status"], "held")
        self.assertEqual(self.h.services.store.conversation(cid)["visibility"], "private")
        self.assertEqual(len(self.h.slack.dms), dms_before, "Handle privately sends no DM")
        self.assertIsNone(self.h.reply_to(follow))
        self.assertIn("Held privately by <@DEV-mentor-kim>", json.dumps(self.h.slack.updates[-1]["blocks"]))
        # Nobody but mentors can open the chat, and the student is never told the reason in a web page
        self.assertEqual(self.ben.get(f"/c/{cid}", follow_redirects=False).status_code, 303)
        kim_page = self.kim.get(f"/c/{cid}").text
        self.assertIn("Handled privately by Mentor Kim", kim_page)
        self.assertIn("Private: only Ava (student) and mentors can see this chat", kim_page)

    # -------------------------------------------------------------- withdraw
    def test_student_can_withdraw_own_pending_message(self):
        mid, cid = self.start()
        self.assertEqual(self.ben.post(f"/m/{mid}/withdraw").status_code, 403)
        resp = self.ava.post(f"/m/{mid}/withdraw")
        self.assertEqual(resp.status_code, 200)
        self.h.idle()
        self.assertEqual(self.h.message(mid)["status"], "withdrawn")
        self.assertIn("Withdrawn by the student", json.dumps(self.h.slack.updates[-1]["blocks"]))
        self.assertEqual(self.approve(mid).status_code, 409)
        self.assertNotIn(f'id="q{mid}"', self.kim.get("/queue").text)

    # ----------------------------------------------------------------- roles
    def test_students_cannot_review(self):
        mid, _ = self.start()
        for action in ("approve", "reject", "hold"):
            resp = self.ben.post(f"/m/{mid}/{action}", {"reason": "x"})
            self.assertEqual(resp.status_code, 403, action)
        self.assertEqual(self.ben.get("/queue").status_code, 403)
        self.assertEqual(self.ben.get("/audit").status_code, 403)
        self.assertEqual(self.h.message(mid)["status"], "pending")

    def test_mentor_messages_go_straight_to_claude(self):
        mid, _ = self.start("Explain PID to a new programmer", actor=self.kim)
        self.assertEqual(self.h.message(mid)["status"], "auto")
        self.assertEqual(self.h.reply_to(mid)["status"], "complete")
        self.assertEqual([p for p in self.h.slack.posts if p["channel"] == "CAPPROVALS"], [],
                         "no approval card for a mentor's own message")
        audit = self.h.services.db.one("SELECT details FROM audit_log WHERE action = 'message_sent'")
        self.assertEqual(json.loads(audit["details"])["status"], "auto")

    def test_mentors_can_be_required_to_get_approval(self):
        self.h.settings.mentors_need_approval = True
        mid, _ = self.start("A mentor question", actor=self.kim)
        self.assertEqual(self.h.message(mid)["status"], "pending")
        resp = self.kim.post(f"/m/{mid}/approve")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Another mentor", resp.json()["error"])
        coach = self.h.login(ADMIN)
        self.assertEqual(coach.post(f"/m/{mid}/approve").status_code, 200)

    def test_queue_page_lists_waiting_messages(self):
        mid, cid = self.start("Queue me")
        page = self.kim.get("/queue").text
        self.assertIn("Queue me", page)
        self.assertIn(f'id="q{mid}"', page)
        self.assertIn(f"/m/{mid}/approve", page)
        self.assertIn('<span class="count"', page, "the nav shows how many are waiting")
        self.approve(mid)
        self.assertIn("Nothing is waiting", self.kim.get("/queue/items").text)

    def test_audit_log_is_admin_only_and_records_actions(self):
        mid, _ = self.start()
        self.approve(mid)
        self.assertEqual(self.kim.get("/audit").status_code, 403)
        page = self.h.login(ADMIN).get("/audit").text
        for action in ("signed in", "conversation started", "message sent", "message approved"):
            self.assertIn(action, page)

    # ---------------------------------------------------------------- limits
    def test_pending_limit_blocks_a_fourth_waiting_message(self):
        for i in range(3):
            self.start(f"Question {i}")
        sent = self.h.slack_dm("Question 4", user=STUDENT)
        self.assertIsNone(sent.message_id)
        self.assertIn("3 messages waiting", self.thread_text(sent))

    def test_daily_limit(self):
        self.h.settings.limit_approved_per_day = 1
        mid, cid = self.start()
        self.approve(mid)
        sent = self.say(cid, "Another")
        self.assertIsNone(sent.message_id)
        self.assertIn("today's limit of 1", self.thread_text(sent))

    def test_cannot_send_while_claude_is_replying(self):
        mid, cid = self.start()
        self.approve(mid)
        # Pretend the reply is still streaming
        reply = self.h.reply_to(mid)
        self.h.services.db.execute("UPDATE messages SET status = 'streaming' WHERE id = ?", (reply["id"],))
        sent = self.say(cid, "Next")
        self.assertIsNone(sent.message_id)
        self.assertIn("finish replying", self.thread_text(sent))

    # ------------------------------------------------------------- security
    def test_post_without_csrf_token_is_refused(self):
        mid, _ = self.start()
        resp = self.kim.post(f"/m/{mid}/approve", csrf=False)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.h.message(mid)["status"], "pending")

    def test_signed_out_requests(self):
        self.h.client.cookies.clear()
        for path in ("/queue", "/code", "/c/1"):
            resp = self.h.client.get(path, follow_redirects=False)
            self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/login"), path)
        resp = self.h.client.post("/m/1/approve", data={})
        self.assertEqual(resp.status_code, 401)

    def test_message_text_is_escaped(self):
        # Slack sends < and > escaped, so this is what the student typed
        mid, cid = self.start('&lt;script&gt;alert("x")&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;')
        page = self.kim.get(f"/c/{cid}").text
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;", page)

    def test_claude_html_is_not_rendered(self):
        class HtmlClaude(FakeClaude):
            async def stream(self, **kwargs):
                yield StreamEvent("text", text='Hi <script>alert(1)</script> [link](javascript:alert(1))')
        self.h.close()
        self.h = Harness(claude=HtmlClaude(delay=0))
        kim = self.h.login(MENTOR)
        cid = self.h.slack_dm("hello", user=MENTOR).conversation_id
        page = kim.get(f"/c/{cid}").text
        self.assertNotIn("<script>alert(1)", page)
        self.assertNotIn('href="javascript:', page)

    def test_sidebar_links(self):
        mid, cid = self.start("My own question")
        page = self.ava.get("/about").text
        for gone in ('href="/chats"', 'href="/recents"', f'href="/c/{cid}"', "New chat", "Recents", "My own question"):
            self.assertNotIn(gone, page, "students have no chat pages or lists")
        self.assertNotIn('href="/queue"', page, "students don't get mentor tools")
        kim_page = self.kim.get("/about").text
        self.assertIn('href="/queue"', kim_page)
        self.assertIn('href="/code"', kim_page)
        self.assertRegex(kim_page, r'Review\s*<span class="count"[^>]*>1</span>')
        for gone in ('href="/chats"', 'href="/recents"', "New chat", "Recents"):
            self.assertNotIn(gone, kim_page)
        for path in ("/projects", "/artifacts"):
            self.assertEqual(self.ava.get(path, follow_redirects=False).status_code, 303)

    def test_security_headers(self):
        resp = self.ava.get("/about")
        self.assertIn("script-src 'self'", resp.headers["content-security-policy"])
        self.assertEqual(resp.headers["x-frame-options"], "DENY")
        self.assertEqual(resp.headers["x-content-type-options"], "nosniff")

    def test_test_sign_in_is_refused_through_the_tunnel(self):
        resp = self.h.client.get("/auth/dev", headers={"CF-Connecting-IP": "203.0.113.5"})
        self.assertEqual(resp.status_code, 404)
        resp = self.h.client.post("/auth/dev", data={"user": MENTOR}, headers={"X-Forwarded-For": "203.0.113.5"})
        self.assertEqual(resp.status_code, 404)

    def test_test_sign_in_is_off_by_default(self):
        self.h.settings.dev_login = False
        self.assertEqual(self.h.client.get("/auth/dev").status_code, 404)
        self.assertNotIn("/auth/dev", self.h.client.get("/login").text)

    # --------------------------------------------------- deactivated accounts
    def test_people_removed_from_slack_lose_access(self):
        store = self.h.services.store
        user = store.upsert_user(slack_user_id="UGONE", name="Former student", role="student", login=True)
        self.h.slack.users["UGONE"] = {"id": "UGONE", "deleted": True}
        removed = self.h.call(self.h.services.approvals.check_accounts)
        self.assertEqual(removed, 1)
        self.assertEqual(store.user(user["id"])["active"], 0)

    def test_mentor_status_is_rechecked_before_acting(self):
        # A real Slack account that was a mentor, then left #claude-approvals
        store = self.h.services.store
        self.h.slack.members = {"UMENTOR9"}
        user = store.upsert_user(slack_user_id="UMENTOR9", name="Ex Mentor", role="mentor", login=True)
        mid, _ = self.start()
        directory = self.h.services.directory
        self.assertEqual(self.h.call(directory.role_for, "UMENTOR9"), "mentor")
        self.h.slack.members = set()
        # Their session is still cached as mentor, but the action re-checks Slack
        from .helpers import Actor
        cookie = self._session_for(user["id"])
        actor = Actor(self.h, "UMENTOR9", cookie, "csrf-token")
        resp = actor.post(f"/m/{mid}/approve")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.h.message(mid)["status"], "pending")

    def _session_for(self, user_id):
        """Build a signed session cookie the way Starlette does, for a user without a sample sign-in."""
        import base64

        import itsdangerous
        data = base64.b64encode(json.dumps({"uid": user_id, "csrf": "csrf-token"}).encode())
        return itsdangerous.TimestampSigner(str(self.h.settings.session_secret)).sign(data).decode()

class ReplyProblemsTest(unittest.TestCase):
    def tearDown(self):
        self.h.close()

    def test_failed_reply_can_be_retried_without_new_approval(self):
        class FlakyClaude(FakeClaude):
            failures = 1

            async def stream(self, **kwargs):
                if self.failures:
                    self.failures -= 1
                    self.calls.append(kwargs)
                    yield StreamEvent("error", error="Overloaded")
                    return
                async for event in super().stream(**kwargs):
                    yield event

        self.h = Harness(claude=FlakyClaude(delay=0))
        ava, kim = self.h.login(STUDENT), self.h.login(MENTOR)
        mid = self.h.slack_dm("Help with swerve offsets", user=STUDENT).message_id
        kim.post(f"/m/{mid}/approve")
        self.h.idle()
        reply = self.h.reply_to(mid)
        self.assertEqual(reply["status"], "error")
        self.assertEqual(reply["error"], "Overloaded")
        cid = reply["conversation_id"]
        page = kim.get(f"/c/{cid}").text
        self.assertIn("Claude couldn't reply: Overloaded", page)
        self.assertIn(f"/m/{mid}/retry", page)
        other = self.h.login(STUDENT_2)
        self.assertIn(other.post(f"/m/{mid}/retry").status_code, (403, 404))
        resp = ava.post(f"/m/{mid}/retry")
        self.assertEqual(resp.status_code, 200)
        self.h.idle()
        self.assertEqual(self.h.reply_to(mid)["status"], "complete")
        self.assertEqual(ava.post(f"/m/{mid}/retry").status_code, 400, "nothing left to retry")

    def test_mentor_alert_hides_chat_and_alerts_mentors(self):
        self.h = Harness()
        ben, kim = self.h.login(STUDENT_2), self.h.login(MENTOR)
        mid = self.h.slack_dm("ALERT-TEST I've been having a really hard time", user=STUDENT).message_id
        kim.post(f"/m/{mid}/approve")
        self.h.idle()
        reply = self.h.reply_to(mid)
        self.assertEqual(reply["status"], "complete")
        self.assertFalse(reply["content"].startswith("[MENTOR ALERT]"), "the marker line is removed")
        self.assertIn("988", reply["content"])
        cid = reply["conversation_id"]
        self.assertEqual(self.h.services.store.conversation(cid)["visibility"], "private")
        self.assertEqual(ben.get(f"/c/{cid}", follow_redirects=False).status_code, 303)
        alert = [p for p in self.h.slack.posts if p["text"].startswith("Mentor alert")]
        self.assertEqual(len(alert), 1)
        self.assertIn(f"<http://localhost:8787/c/{cid}|Open the whole chat>", json.dumps(alert[0]["blocks"]))
        self.assertIsNotNone(self.h.services.db.one("SELECT 1 FROM audit_log WHERE action = 'mentor_alert'"))
        # The student still gets Claude's reply, pointing them to trusted adults, in their Slack thread
        thread = [u["text"] for u in self.h.slack.updates if u["channel"].startswith("D0")]
        self.assertTrue(any("trusted adult" in t for t in thread), thread)
        self.assertIn("trusted adult", kim.get(f"/c/{cid}").text)

    def test_alert_marker_split_across_chunks(self):
        class SplitClaude(FakeClaude):
            async def stream(self, **kwargs):
                for piece in ("[MENT", "OR ALE", "RT]\nPlease talk", " to a mentor today."):
                    yield StreamEvent("text", text=piece)
        self.h = Harness(claude=SplitClaude(delay=0))
        mid = self.h.slack_dm("hi", user=MENTOR).message_id
        reply = self.h.reply_to(mid)
        self.assertEqual(reply["content"], "Please talk to a mentor today.")
        self.assertEqual(self.h.services.store.conversation(reply["conversation_id"])["visibility"], "private")

    def test_slack_failure_is_logged_and_queue_still_works(self):
        self.h = Harness()
        self.h.slack.fail_posts = True
        kim = self.h.login(MENTOR)
        mid = self.h.slack_dm("Slack is down", user=STUDENT).message_id
        self.assertIsNotNone(self.h.services.db.one("SELECT 1 FROM audit_log WHERE action = 'slack_card_failed'"))
        self.assertIn(f'id="q{mid}"', kim.get("/queue").text)

    def test_reminder_posts_once_in_the_card_thread(self):
        self.h = Harness()
        mid = self.h.slack_dm("Waiting a while", user=STUDENT).message_id
        three_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
        self.h.services.db.execute("UPDATE messages SET created_at = ? WHERE id = ?", (three_hours_ago, mid))
        self.assertEqual(self.h.call(self.h.services.approvals.send_reminders), 1)
        self.assertEqual(self.h.call(self.h.services.approvals.send_reminders), 0)
        reminder = self.h.slack.posts[-1]
        card = next(p for p in self.h.slack.posts if p["channel"] == "CAPPROVALS")
        self.assertEqual(reminder["thread_ts"], card["ts"])
        self.assertIn("3 hours", reminder["text"])

    def test_restart_marks_unfinished_replies_as_failed(self):
        self.h = Harness()
        mid = self.h.slack_dm("hello", user=MENTOR).message_id
        reply = self.h.reply_to(mid)
        self.h.services.db.execute("UPDATE messages SET status = 'streaming' WHERE id = ?", (reply["id"],))
        # Restart the app on the same data folder
        self.h.client.__exit__(None, None, None)
        self.h.client.__enter__()
        self.assertEqual(self.h.services.store.message(reply["id"])["status"], "error")


# The same tests again on the other AI backends (tests/backends.py). Tests that inject behavior by subclassing
# FakeClaude stay fake-only.
class SplitAlertTest(unittest.TestCase):
    """The [MENTOR ALERT] marker arriving in small pieces on the real Anthropic and Pi clients (the fake-only
    version is ReplyProblemsTest.test_alert_marker_split_across_chunks)."""

    def tearDown(self):
        self.h.close()

    def check(self, claude):
        self.h = Harness(claude=claude)
        mid = self.h.slack_dm("ALERT-TEST I feel awful", user=MENTOR).message_id
        reply = self.h.reply_to(mid)
        self.assertNotIn("MENTOR ALERT", reply["content"])
        self.assertTrue(reply["content"].startswith("That sounds like a lot to carry."))
        self.assertEqual(self.h.services.store.conversation(reply["conversation_id"])["visibility"], "private")

    def test_anthropic(self):
        self.check(RecordingClaudeClient(chunk=3))

    def test_pi(self):
        tmp = Path(tempfile.mkdtemp(prefix="subzero-split-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        client = RecordingPiClient(tmp / "calls.jsonl", chunk=3)
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        self.check(client)


add_variants(globals(), FlowTest, ReplyProblemsTest, fake_only={
    "FlowTest": {"test_claude_html_is_not_rendered"},
    "ReplyProblemsTest": {"test_failed_reply_can_be_retried_without_new_approval",
                          "test_alert_marker_split_across_chunks"},
})


if __name__ == "__main__":
    unittest.main()
