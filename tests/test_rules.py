"""The rules that must always hold, each proven without any website chat page.

Rules 1-4, 9 (replies) and 10 (messaging) are driven only through Slack: direct-message events and approval-card
taps, the same entry points Socket Mode uses, plus direct service calls for time-based things. Rules 5-8 and 11 use the
Code and settings pages, which stay. Rule 10 sign-in uses the sign-in routes.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from subzero_ai import maintenance, saved_settings
from subzero_ai.auth import SlackLogin
from subzero_ai.prompts import load_safety_prompt

from .backends import add_variants
from .helpers import (ADMIN, CLIENT_ID, SLACK_KID, SLACK_KID_2, SLACK_MENTOR, SLACK_MENTOR_2, CodeStudentTest, Harness,
                      id_token, jwks)
from .helpers import FakeSlackSession as FakeSession

HTML = {"Accept": "text/html"}


def audit_actions(h: Harness) -> list[str]:
    return [row["action"] for row in h.services.db.all("SELECT action FROM audit_log ORDER BY id")]


def audit_entry(h: Harness, action: str):
    return h.services.db.one("SELECT * FROM audit_log WHERE action = ? ORDER BY id DESC LIMIT 1", (action,))


class SlackRuleTest(unittest.TestCase):
    """A Slack workspace with a student, a second student and two mentors; the student talks to the bot by DM."""

    options: dict = {}

    def setUp(self):
        super().setUp()
        self.h = Harness(**self.options)
        self.h.slack_people()
        self.slack = self.h.slack
        self.addCleanup(self.h.close)

    def cards(self):
        return [p for p in self.slack.posts if p["channel"] == "CAPPROVALS"
                and any(b.get("type") == "actions" for b in p["blocks"] or [])]

    def age(self, message_id: int, minutes: float) -> None:
        """Pretend a message was sent and approved this many minutes ago."""
        then = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        self.h.services.db.execute("UPDATE messages SET created_at = ?, reviewed_at = CASE WHEN reviewed_at IS NULL "
                                   "THEN NULL ELSE ? END WHERE id = ?", (then, then, message_id))

    def status(self, sent) -> str:
        return self.h.message(sent.message_id)["status"]

    def approved_chat(self, text="How do I zero our swerve modules?", user=SLACK_KID):
        sent = self.h.slack_dm(text, user=user)
        self.h.slack_tap("approve", sent.message_id)
        self.assertEqual(self.status(sent), "approved")
        return sent

    def asked_claude(self, text: str) -> bool:
        return any(text in json.dumps(c["messages"]) for c in self.h.chat_calls())


# ======================================================================================================== rule 1
class Rule01ReachesClaudeOnlyAfterApproval(SlackRuleTest):
    """Rule 1: A student's DM reaches Claude only after a mentor approves it, or inside an open approval window
    when the screener passes it."""

    options = {"approval_window_minutes": 5}

    def test_a_dm_is_held_and_claude_is_not_called(self):
        sent = self.h.slack_dm("Secret plan for the arm")
        self.assertEqual(self.status(sent), "pending")
        self.assertIsNone(self.h.reply_to(sent.message_id))
        self.assertEqual(self.h.chat_calls(), [])
        self.assertEqual(len(self.cards()), 1, "a mentor card is posted")
        self.assertIn("A mentor will check this", self.h.slack_thread(sent)[0]["text"])

    def test_a_mentor_tap_on_approve_calls_claude_and_the_reply_is_posted_in_the_thread(self):
        sent = self.h.slack_dm("Why does the arm drift?")
        self.assertEqual(self.h.chat_calls(), [])
        self.h.slack_tap("approve", sent.message_id)
        self.assertEqual(self.status(sent), "approved")
        [call] = self.h.chat_calls()
        self.assertEqual(call["messages"][-1]["content"], "Why does the arm drift?")
        self.assertEqual(self.h.reply_to(sent.message_id)["status"], "complete")
        thread = self.h.slack_thread(sent)
        self.assertEqual(thread[1]["text"], "Thinking…")
        finals = [u for u in self.slack.updates if u["ts"] == thread[1]["ts"]]
        self.assertIn("You asked:", finals[-1]["text"])

    def test_only_mentors_can_approve(self):
        sent = self.h.slack_dm("Let me approve myself")
        self.h.slack_tap("approve", sent.message_id, user=SLACK_KID)
        self.h.slack_tap("approve", sent.message_id, user=SLACK_KID_2)
        self.assertEqual(self.status(sent), "pending")
        self.assertEqual(self.h.chat_calls(), [])

    def test_a_non_mentor_cannot_use_any_card_button_or_modal(self):
        sent = self.h.slack_dm("Let me review myself")
        for user in (SLACK_KID, SLACK_KID_2):
            for action in ("approve", "edit", "reject", "handle_private"):
                self.h.slack_tap(action, sent.message_id, user=user)
            self.assertEqual(self.slack.views, [], "no modal opens for a non-mentor")
            for callback, block in (("edit_submit", "edited"), ("reject_submit", "reason")):
                ack = self.h.slack_submit(callback, sent.message_id, block, "Sneaky words", user=user)
                self.assertEqual(ack["response_action"], "errors")
        self.assertTrue(self.slack.ephemerals)
        self.assertTrue(all(e["text"] == "Only mentors in this channel can review messages."
                            for e in self.slack.ephemerals))
        self.assertEqual(self.status(sent), "pending")
        self.assertEqual(self.h.message(sent.message_id)["content"], "Let me review myself")
        self.assertEqual(self.h.chat_calls(), [])

    def test_a_mentor_removed_from_the_channel_loses_the_approve_button_at_once(self):
        other = self.h.slack_dm("Warms the role cache", user=SLACK_KID_2)
        self.h.slack_tap("approve", other.message_id)  # the mentor's role is cached from here on
        self.assertEqual(self.status(other), "approved")
        sent = self.h.slack_dm("Waiting for someone who is gone")
        self.slack.members.discard(SLACK_MENTOR_2)
        self.h.slack_tap("approve", sent.message_id, user=SLACK_MENTOR_2)
        self.assertEqual(self.status(sent), "pending")
        self.assertFalse(self.asked_claude("Waiting for someone who is gone"))
        self.assertEqual(self.slack.ephemerals[-1]["text"], "Only mentors in this channel can review messages.")

    def test_after_a_mentor_edit_claude_sees_only_the_mentors_words(self):
        sent = self.h.slack_dm("Original wording with a private detail")
        self.h.slack_submit("edit_submit", sent.message_id, "edited", "A mentor's cleaner wording")
        self.assertEqual(self.status(sent), "edited")
        calls = self.h.chat_calls()
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.asked_claude("Original wording"))
        self.assertEqual(calls[-1]["messages"][-1]["content"], "A mentor's cleaner wording")

    def test_a_dm_with_a_file_is_not_saved_and_never_reaches_claude(self):
        sent = self.h.slack_dm("Look at my file", extra={"files": [{"id": "F1", "name": "Arm.java"}]})
        self.assertIsNone(sent.message_id)
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))
        self.assertEqual(self.h.chat_calls(), [])
        self.assertEqual(self.cards(), [])
        self.assertIn("can't take files", self.h.slack_thread(sent)[0]["text"])

    def test_the_window_restarts_from_the_last_window_message_and_then_ages_out(self):
        first = self.approved_chat()
        follow = self.h.slack_dm("Next question", thread_ts=first.thread_ts)
        self.age(first.message_id, 30)
        self.age(follow.message_id, 4)
        again = self.h.slack_dm("Still chatting", thread_ts=first.thread_ts)
        self.assertEqual(self.status(again), "grace", "a window message keeps the window open")
        self.age(first.message_id, 60)
        self.age(follow.message_id, 30)
        self.age(again.message_id, 6)
        late = self.h.slack_dm("Back after a break", thread_ts=first.thread_ts)
        self.assertEqual(self.status(late), "pending", "the window aged out")
        self.assertFalse(self.asked_claude("Back after a break"))

    def test_only_one_message_per_chat_waits_at_a_time(self):
        first = self.h.slack_dm("First, waiting")
        second = self.h.slack_dm("Second, too soon", thread_ts=first.thread_ts)
        self.assertIsNone(second.message_id)
        self.assertIn("still waiting for a mentor", self.h.slack_thread(first)[-1]["text"])
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) AS n FROM messages")["n"], 1)
        self.assertEqual(self.h.chat_calls(), [])

    def test_nobody_can_continue_someone_elses_thread(self):
        first = self.approved_chat("Ava's private chat")
        same_channel = self.h.slack_dm("Ben butts in", user=SLACK_KID_2, thread_ts=first.thread_ts,
                                       channel=first.channel)
        own_channel = self.h.slack_dm("Ben guesses a thread", user=SLACK_KID_2, thread_ts=first.thread_ts)
        for sent in (same_channel, own_channel):
            self.assertIsNone(sent.message_id)
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) AS n FROM messages WHERE role = 'user'")["n"], 1)
        self.assertEqual(len(self.h.chat_calls()), 1)
        self.assertFalse(self.asked_claude("Ben"))

    def test_with_the_screener_off_the_window_passes_and_says_so_in_the_audit_log(self):
        self.h.settings.screener_enabled = False
        first = self.approved_chat()
        follow = self.h.slack_dm("SCREEN-FLAG not checked", thread_ts=first.thread_ts)
        self.assertEqual(self.status(follow), "grace")
        entry = audit_entry(self.h, "message_auto_approved")
        self.assertEqual(entry["target_id"], follow.message_id)
        self.assertEqual(json.loads(entry["details"])["screener"], "turned off")

    def test_a_mentors_own_message_cannot_be_approved_by_themselves(self):
        self.h.settings.mentors_need_approval = True
        sent = self.h.slack_dm("A mentor's own question", user=SLACK_MENTOR)
        self.assertEqual(self.status(sent), "pending")
        self.h.slack_tap("approve", sent.message_id, user=SLACK_MENTOR)
        self.assertEqual(self.status(sent), "pending")
        self.assertEqual(self.slack.ephemerals[-1]["text"], "Another mentor needs to approve your message.")
        self.assertEqual(self.h.chat_calls(), [])
        self.h.slack_tap("approve", sent.message_id, user=SLACK_MENTOR_2)
        self.assertEqual(self.status(sent), "approved")
        self.assertEqual(len(self.h.chat_calls()), 1)

    def test_inside_an_open_window_a_safe_follow_up_is_answered_without_a_tap(self):
        first = self.approved_chat()
        follow = self.h.slack_dm("What offset goes in the config?", thread_ts=first.thread_ts)
        self.assertEqual(self.status(follow), "grace")
        self.assertEqual(self.h.reply_to(follow.message_id)["status"], "complete")
        self.assertEqual(len(self.h.chat_calls()), 2)
        self.assertEqual(self.h.chat_calls()[-1]["messages"][-1]["content"], "What offset goes in the config?")
        self.assertEqual(len(self.cards()), 1, "no second card: no mentor tap needed")
        screens = [c for c in self.h.claude.calls if "Reply with one line: SAFE" in c.get("system", "")]
        self.assertEqual(len(screens), 1, "the screener checked it")

    def test_the_first_message_of_a_chat_is_always_held_even_with_a_window(self):
        self.approved_chat()
        other = self.h.slack_dm("A brand new question")
        self.assertEqual(self.status(other), "pending")
        self.assertFalse(self.asked_claude("A brand new question"))

    def test_inside_an_open_window_a_flagged_message_is_held(self):
        first = self.approved_chat()
        flagged = self.h.slack_dm("SCREEN-FLAG something odd", thread_ts=first.thread_ts)
        self.assertEqual(self.status(flagged), "pending")
        self.assertEqual(self.h.message(flagged.message_id)["screen_note"], "Flagged for testing.")
        self.assertFalse(self.asked_claude("SCREEN-FLAG"))
        self.assertEqual(len(self.cards()), 2, "it goes to the mentors with a card")
        self.assertIn("the screener flagged it", json.dumps(self.cards()[-1]["blocks"]))

    def test_when_the_window_is_closed_the_follow_up_is_held(self):
        first = self.approved_chat()
        self.age(first.message_id, 6)
        follow = self.h.slack_dm("One more thing", thread_ts=first.thread_ts)
        self.assertEqual(self.status(follow), "pending")
        self.assertFalse(self.asked_claude("One more thing"))

    def test_with_the_window_turned_off_the_follow_up_is_held(self):
        self.h.settings.approval_window_minutes = 0
        first = self.approved_chat()
        follow = self.h.slack_dm("Follow-up", thread_ts=first.thread_ts)
        self.assertEqual(self.status(follow), "pending")

    def test_a_screener_error_fails_closed(self):
        first = self.approved_chat()
        failed = self.h.slack_dm("SCREEN-ERROR please", thread_ts=first.thread_ts)
        self.assertEqual(self.status(failed), "pending")
        self.assertIn("couldn't check it", self.h.message(failed.message_id)["screen_note"])
        self.assertFalse(self.asked_claude("SCREEN-ERROR"))


# ======================================================================================================== rule 2
class Rule02RejectedOrHeldNeverReachesClaude(SlackRuleTest):
    """Rule 2: A rejected or held message never reaches Claude, and only the student sees the reason.

    The reason is posted in the student's own DM thread. The one other place it appears is the handled card in the
    approvals channel, which mentors see by design; it is never a new post there and never goes to anyone else."""

    def test_a_rejected_message_never_reaches_claude_and_only_the_student_sees_the_reason(self):
        sent = self.h.slack_dm("Help with the arm")
        other = self.h.slack_dm("Question from Ben", user=SLACK_KID_2)
        ack = self.h.slack_tap("reject", sent.message_id)
        self.assertIsNone(ack)
        self.assertEqual(self.slack.views[-1]["view"]["callback_id"], "reject_submit", "the card asks for a reason")
        self.assertEqual(self.h.slack_reject(sent.message_id, "Ask about one mechanism at a time."),
                         {"response_action": "clear"})
        self.assertEqual(self.status(sent), "rejected")
        self.assertEqual(self.h.chat_calls(), [])
        self.assertIsNone(self.h.reply_to(sent.message_id))
        self.assertEqual(self.h.slack_thread(sent)[-1]["text"],
                         "A mentor didn't approve this: Ask about one mechanism at a time.")
        reason_posts = [p for p in self.slack.posts if "one mechanism" in p["text"]]
        self.assertEqual([(p["channel"], p["thread_ts"]) for p in reason_posts], [(sent.channel, sent.thread_ts)])
        self.assertFalse([p for p in self.slack.posts if p["channel"] == "CAPPROVALS" and "one mechanism" in p["text"]])
        self.assertFalse([p for p in self.slack.posts if p["channel"] == other.channel and "one mechanism" in p["text"]])
        self.assertEqual(self.slack.dms, [], "no other person is messaged")
        shown = [u for u in self.slack.updates if "one mechanism" in json.dumps(u["blocks"])]
        self.assertEqual({u["channel"] for u in shown}, {"CAPPROVALS"}, "only the handled card, for mentors")

    def test_a_reason_is_required_and_a_rejected_message_cannot_be_approved_later(self):
        sent = self.h.slack_dm("Help with the arm")
        ack = self.h.slack_reject(sent.message_id, "   ")
        self.assertEqual(ack["response_action"], "errors")
        self.assertEqual(self.status(sent), "pending")
        self.h.slack_reject(sent.message_id, "Not for this chat.")
        self.h.slack_tap("approve", sent.message_id, user=SLACK_MENTOR)
        self.assertEqual(self.status(sent), "rejected")
        self.assertEqual(self.h.chat_calls(), [])

    def test_a_message_handled_privately_never_reaches_claude_and_tells_nobody_in_public(self):
        sent = self.h.slack_dm("Something personal")
        self.h.slack_tap("handle_private", sent.message_id)
        self.assertEqual(self.status(sent), "held")
        self.assertEqual(self.h.chat_calls(), [])
        self.assertIsNone(self.h.reply_to(sent.message_id))
        conv = self.h.services.store.conversation(sent.conversation_id)
        self.assertEqual(conv["visibility"], "private", "the team can't see it")
        self.assertEqual([p["text"] for p in self.h.slack_thread(sent)][1:], [], "nothing more is said in the thread")
        self.assertEqual(self.slack.dms, [])
        self.h.slack_tap("approve", sent.message_id, user=SLACK_MENTOR)
        self.assertEqual(self.status(sent), "held")
        self.assertEqual(self.h.chat_calls(), [])

    def test_a_rejection_in_a_chat_does_not_let_the_next_message_skip_review(self):
        self.h.settings.approval_window_minutes = 5
        first = self.approved_chat()
        self.h.settings.approval_window_minutes = 0
        second = self.h.slack_dm("Something off", thread_ts=first.thread_ts)
        self.h.slack_reject(second.message_id, "Not for this chat.")
        self.h.settings.approval_window_minutes = 5
        third = self.h.slack_dm("Try again", thread_ts=first.thread_ts)
        self.assertEqual(self.status(third), "pending")
        self.assertFalse(self.asked_claude("Something off") or self.asked_claude("Try again"))


    def test_after_a_rejection_the_rejected_words_never_reach_claude_later(self):
        first = self.h.slack_dm("Rejected secret wording")
        self.h.slack_reject(first.message_id, "Not for this chat.")
        again = self.h.slack_dm("Try again", thread_ts=first.thread_ts)
        self.assertEqual(self.status(again), "pending")
        self.h.slack_tap("approve", again.message_id)
        calls = self.h.chat_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["messages"][-1]["content"], "Try again")
        self.assertFalse(self.asked_claude("Rejected secret wording"))

    def test_after_private_handling_the_held_words_never_reach_claude_later(self):
        first = self.h.slack_dm("Held personal wording")
        self.h.slack_tap("handle_private", first.message_id)
        again = self.h.slack_dm("A calmer question", thread_ts=first.thread_ts)
        self.assertEqual(self.status(again), "pending")
        self.h.slack_tap("approve", again.message_id)
        self.assertEqual(len(self.h.chat_calls()), 1)
        self.assertFalse(self.asked_claude("Held personal wording"))

# ======================================================================================================== rule 3
class Rule03MentorAlertPostsToApprovals(SlackRuleTest):
    """Rule 3: A MENTOR ALERT posts to the approvals channel."""

    options = {"approval_window_minutes": 5}

    def alerts(self):
        return [p for p in self.slack.posts if "Mentor alert" in p["text"]]

    def test_an_alert_reply_posts_to_the_approvals_channel_and_hides_the_chat(self):
        sent = self.h.slack_dm("ALERT-TEST I feel awful")
        self.assertEqual(self.alerts(), [], "nothing before the message is approved")
        self.h.slack_tap("approve", sent.message_id)
        [alert] = self.alerts()
        self.assertEqual(alert["channel"], "CAPPROVALS")
        self.assertEqual(self.h.services.store.conversation(sent.conversation_id)["visibility"], "private")
        self.assertIn("mentor_alert", audit_actions(self.h))
        thread = self.h.slack_thread(sent)
        final = [u for u in self.slack.updates if u["ts"] == thread[1]["ts"]][-1]["text"]
        self.assertNotIn("MENTOR ALERT", final)
        self.assertIn("988", final, "the student still gets the caring reply")

    def test_an_ordinary_reply_posts_no_alert(self):
        sent = self.h.slack_dm("How do I tune a PID loop?")
        self.h.slack_tap("approve", sent.message_id)
        self.assertEqual(self.h.reply_to(sent.message_id)["status"], "complete")
        self.assertEqual(self.alerts(), [])
        self.assertNotIn("mentor_alert", audit_actions(self.h))
        self.assertEqual(self.h.services.store.conversation(sent.conversation_id)["visibility"], "team")

    def test_an_alert_inside_the_window_still_reaches_the_mentors(self):
        first = self.approved_chat()
        alert = self.h.slack_dm("ALERT-TEST I'm having a rough week", thread_ts=first.thread_ts)
        self.assertEqual(self.status(alert), "grace")
        [post] = self.alerts()
        self.assertEqual(post["channel"], "CAPPROVALS")
        after = self.h.slack_dm("Anyway", thread_ts=first.thread_ts)
        self.assertEqual(self.status(after), "pending", "an alert closes the window")


# ======================================================================================================== rule 4
class Rule04RemindersAndLimits(SlackRuleTest):
    """Rule 4: Waiting messages trigger reminders; per-student limits and the always-review list apply."""

    def test_a_waiting_message_gets_one_reminder_in_the_card_thread(self):
        sent = self.h.slack_dm("Waiting a while")
        self.assertEqual(self.h.call(self.h.services.approvals.send_reminders), 0, "too soon")
        self.age(sent.message_id, 3 * 60)
        self.assertEqual(self.h.call(self.h.services.approvals.send_reminders), 1)
        self.assertEqual(self.h.call(self.h.services.approvals.send_reminders), 0, "only one reminder")
        reminder = self.slack.posts[-1]
        self.assertEqual(reminder["channel"], "CAPPROVALS")
        self.assertEqual(reminder["thread_ts"], self.cards()[0]["ts"])
        self.assertIn("Still waiting for a mentor", reminder["text"])
        self.assertIn("3 hours", reminder["text"])

    def test_reminders_follow_the_setting_and_skip_handled_messages(self):
        self.h.settings.reminder_minutes = 30
        waiting = self.h.slack_dm("Still waiting")
        done = self.h.slack_dm("Already handled", user=SLACK_KID_2)
        self.h.slack_tap("approve", done.message_id)
        self.age(waiting.message_id, 45)
        self.age(done.message_id, 45)
        self.assertEqual(self.h.call(self.h.services.approvals.send_reminders), 1)

    def test_the_waiting_limit_applies_to_dms(self):
        self.h.settings.limit_pending_per_student = 2
        first = self.h.slack_dm("One")
        self.h.slack_dm("Two")
        third = self.h.slack_dm("Three")
        self.assertIsNone(third.message_id, "nothing was saved")
        self.assertIn("You have 2 messages waiting for review", self.h.slack_thread(third)[0]["text"])
        self.assertEqual(len(self.cards()), 2)
        self.assertFalse(self.asked_claude("Three"))
        other = self.h.slack_dm("Ben's question", user=SLACK_KID_2)
        self.assertIsNotNone(other.message_id, "the limit is per student")
        self.h.slack_tap("approve", first.message_id)
        again = self.h.slack_dm("Three again")
        self.assertIsNotNone(again.message_id, "room again once a mentor acts")

    def test_the_daily_approved_limit_applies_to_dms(self):
        self.h.settings.limit_approved_per_day = 1
        self.approved_chat("First")
        second = self.h.slack_dm("Second")
        self.assertIsNone(second.message_id)
        self.assertIn("reached today's limit of 1 approved messages", self.h.slack_thread(second)[0]["text"])
        self.assertEqual(len(self.cards()), 1)
        other = self.h.slack_dm("Ben's question", user=SLACK_KID_2)
        self.assertIsNotNone(other.message_id, "the limit is per student")

    def test_window_messages_count_toward_the_daily_approved_limit(self):
        self.h.settings.approval_window_minutes = 5
        first = self.approved_chat("First")
        follow = self.h.slack_dm("Window follow-up", thread_ts=first.thread_ts)
        self.assertEqual(self.status(follow), "grace")
        self.h.settings.limit_approved_per_day = 2
        third = self.h.slack_dm("Third")
        self.assertIsNone(third.message_id)
        self.assertIn("reached today's limit of 2 approved messages", self.h.slack_thread(third)[0]["text"])
        blocked = self.h.slack_dm("Third, in the chat", thread_ts=first.thread_ts)
        self.assertIsNone(blocked.message_id)
        self.assertFalse(self.asked_claude("Third"))

    def test_the_always_review_list_forces_review_inside_an_open_window(self):
        self.h.settings.approval_window_minutes = 5
        self.h.settings.always_review_slack_ids = frozenset({SLACK_KID})
        first = self.approved_chat("Ava's first question")
        follow = self.h.slack_dm("Ava's follow-up", thread_ts=first.thread_ts)
        self.assertEqual(self.status(follow), "pending")
        self.assertFalse(self.asked_claude("Ava's follow-up"))
        self.assertEqual(len(self.cards()), 2)
        ben_first = self.approved_chat("Ben's first question", user=SLACK_KID_2)
        ben_follow = self.h.slack_dm("Ben's follow-up", user=SLACK_KID_2, thread_ts=ben_first.thread_ts)
        self.assertEqual(self.status(ben_follow), "grace", "others on the same window skip review")


# ======================================================================================================== rule 9 (DMs)
class Rule09SafetyRulesInDMReplies(SlackRuleTest):
    """Rule 9: The safety rules are in the system prompt of every DM reply and every Code session."""

    options = {"approval_window_minutes": 5}

    def test_every_dm_reply_carries_the_safety_rules(self):
        safety = load_safety_prompt(self.h.settings)
        self.assertGreater(len(safety), 100)
        first = self.approved_chat()
        follow = self.h.slack_dm("A window follow-up", thread_ts=first.thread_ts)
        self.assertEqual(self.status(follow), "grace")
        edited = self.h.slack_dm("A third one that a mentor edits")
        self.h.slack_submit("edit_submit", edited.message_id, "edited", "A mentor's better wording")
        calls = self.h.chat_calls()
        self.assertEqual(len(calls), 3, "approved, window and edited replies")
        for call in calls:
            self.assertIn(safety, call["system"])


# ======================================================================================================== rule 10
class Rule10OutsidersCannotMessage(SlackRuleTest):
    """Rule 10: Guests, bots and people outside the workspace can neither sign in nor message the bot."""

    def assert_refused(self, user: str, **extra):
        sent = self.h.slack_dm("Hello bot", user=user, **extra)
        self.assertIsNone(sent.message_id)
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))
        self.assertIsNone(self.h.services.store.user_by_slack_id(user))
        self.assertEqual(self.h.chat_calls(), [])
        self.assertEqual(self.cards(), [])
        return sent

    def test_guests_are_refused(self):
        self.slack.users["UGUEST"] = {"id": "UGUEST", "is_restricted": True, "profile": {"real_name": "Guest"}}
        self.slack.users["USINGLE"] = {"id": "USINGLE", "is_ultra_restricted": True, "profile": {"real_name": "Single"}}
        for user in ("UGUEST", "USINGLE"):
            sent = self.assert_refused(user)
            self.assertIn("only members of the team's Slack workspace", self.h.slack_thread(sent)[0]["text"])

    def test_bots_are_refused(self):
        self.slack.users["UBOTTY"] = {"id": "UBOTTY", "is_bot": True, "profile": {"real_name": "Botty"}}
        self.assert_refused("UBOTTY")
        event = {"type": "events_api", "envelope_id": "e", "payload": {"type": "event_callback", "event": {
            "type": "message", "channel_type": "im", "channel": "D0BOT", "user": SLACK_KID, "bot_id": "B1",
            "text": "I am a bot message", "ts": "1700099999.000100"}}}
        self.h.call(self.h.services.interactions.dispatch, event)
        self.h.idle()
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))
        self.assert_refused("UBOT")  # the bot itself

    def test_people_outside_the_workspace_and_deleted_accounts_are_refused(self):
        self.slack.users["UOTHER"] = {"id": "UOTHER", "team_id": "TELSE", "profile": {"real_name": "Outsider"}}
        self.slack.users["UGONE"] = {"id": "UGONE", "deleted": True}
        for user in ("UOTHER", "UGONE"):
            self.assert_refused(user)

    def test_a_deactivated_account_is_refused(self):
        row = self.h.services.store.upsert_user(slack_user_id=SLACK_KID, name="Kid K", role="student")
        self.h.services.store.deactivate_user(row["id"])
        sent = self.h.slack_dm("Let me back in")
        self.assertIsNone(sent.message_id)
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))

    def test_when_slack_cannot_say_who_it_is_the_message_is_refused(self):
        async def broken(user_id):
            raise RuntimeError("slack down")

        self.slack.user_info = broken
        sent = self.h.slack_dm("Hello?")
        self.assertIsNone(sent.message_id)
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))
        self.assertEqual(self.h.chat_calls(), [])


class Rule10OutsidersCannotSignIn(unittest.TestCase):
    """Rule 10 (sign-in): guests, bots and people outside the workspace can't sign in."""

    def setUp(self):
        self.h = Harness(slack_client_id=CLIENT_ID, slack_client_secret="secret")
        self.addCleanup(self.h.close)
        self.session = FakeSession()
        self.h.services.login = SlackLogin(self.h.settings, session=self.session, fetch_jwks=jwks)
        self.client = self.h.client
        self.client.cookies.clear()

    def attempt(self, user: str, **token_extra):
        self.client.cookies.clear()
        resp = self.client.get("/auth/login", follow_redirects=False)
        from urllib.parse import parse_qs, urlparse
        q = {k: v[0] for k, v in parse_qs(urlparse(resp.headers["location"]).query).items()}
        self.session.token = id_token(q["nonce"], user=user, **token_extra)
        page = self.client.get(f"/auth/slack/callback?code=abc&state={q['state']}")
        self.assertIsNone(self.h.services.store.user_by_slack_id(user))
        self.assertEqual(self.client.get("/code", follow_redirects=False).status_code, 303, "not signed in")
        return page

    def test_guests_and_bots_cannot_sign_in(self):
        for user, info in [("U0GUEST", {"is_restricted": True}), ("U0SCG", {"is_ultra_restricted": True}),
                           ("U0BOT", {"is_bot": True})]:
            with self.subTest(user=user):
                self.h.slack.users[user] = {"id": user, "profile": {}, **info}
                self.assertIn("Guest accounts can&#39;t sign in", self.attempt(user).text)

    def test_another_workspace_cannot_sign_in(self):
        page = self.attempt("U0OUT", team="TOTHER")
        self.assertIn("Sign in with the team&#39;s Slack workspace", page.text)

    def test_nobody_signs_in_while_the_workspace_is_unknown(self):
        self.h.settings.slack_team_id = ""

        async def broken_auth_test():
            raise ConnectionError("Slack is down")

        self.h.slack.auth_test = broken_auth_test
        self.assertIn("couldn&#39;t reach Slack", self.client.get("/auth/login").text)
        self.assertIsNone(self.h.services.store.user_by_slack_id("U0AVA"))
        self.assertEqual(self.client.get("/code", follow_redirects=False).status_code, 303)


# ======================================================================================================== rule 5
class Rule05CodeNeedsMentorApproval(CodeStudentTest):
    """Rule 5: A student Code session never starts without a mentor's approval, and neither does a follow-up."""

    def test_a_request_does_not_start_until_a_mentor_approves(self):
        sid, _ = self.ask(self.ava)
        self.assertEqual(self.code.get(sid)["status"], "awaiting_approval")
        self.assertEqual(self.tool_turns(), 0)
        self.assertEqual(self.db.all("SELECT * FROM sandbox_sessions"), [])
        self.approve(sid)
        self.assertGreater(self.tool_turns(), 0)

    def test_students_cannot_approve_their_own_request(self):
        sid, _ = self.ask(self.ava)
        for actor in (self.ava, self.ben):
            self.assertEqual(actor.post(f"/code/{sid}/approve-request", {"note": ""}).status_code, 403)
        self.h.idle()
        self.assertEqual(self.code.get(sid)["status"], "awaiting_approval")
        self.assertEqual(self.tool_turns(), 0)

    def test_a_declined_request_never_starts(self):
        sid, _ = self.ask(self.ava)
        self.decline(sid, "Not now.")
        self.assertEqual(self.code.get(sid)["status"], "stopped")
        self.assertEqual(self.tool_turns(), 0)
        self.assertEqual(self.db.all("SELECT * FROM sandbox_sessions"), [])

    def test_a_flagged_request_still_waits(self):
        sid, _ = self.ask(self.ava, "SCREEN-FLAG a request about the robot.")
        self.assertEqual(self.code.get(sid)["status"], "awaiting_approval")
        self.assertEqual(self.tool_turns(), 0)

    def test_a_follow_up_does_not_reach_claude_until_a_mentor_approves_it(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        before = self.tool_turns()
        self.ava.post(f"/code/{sid}/follow-up", {"text": "Also add a test."}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.tool_turns(), before)
        [note] = [s for s in self.code.steps(sid) if s["status"] == "pending"]
        self.assertEqual(self.ava.post(f"/code/steps/{note['id']}/approve-follow-up", {"note": ""}).status_code, 403)
        self.h.idle()
        self.assertEqual(self.tool_turns(), before)
        self.kim.post(f"/code/steps/{note['id']}/approve-follow-up", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertGreater(self.tool_turns(), before)

    def test_a_declined_follow_up_never_reaches_claude(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        before = self.tool_turns()
        self.ava.post(f"/code/{sid}/follow-up", {"text": "Delete everything."}, follow_redirects=False)
        [note] = [s for s in self.code.steps(sid) if s["status"] == "pending"]
        self.kim.post(f"/code/steps/{note['id']}/decline-follow-up", {"note": "No."}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.tool_turns(), before)
        turns = [c for c in self.h.claude.calls if c["kind"] == "tool_turn"]
        self.assertNotIn("Delete everything.", str([c["messages"] for c in turns]))


# ======================================================================================================== rule 6
class Rule06CodeNeedsSandbox(CodeStudentTest):
    """Rule 6: Student Code sessions run only with the sandbox on."""

    def test_with_the_sandbox_off_a_student_is_refused_and_nothing_is_made(self):
        self.h.services.settings.code_sandbox = "off"
        self.ava.post("/code", {"task": "Try it."}, follow_redirects=True)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM code_sessions")["n"], 0)
        self.assertEqual(self.tool_turns(), 0)
        self.assertIn("Code isn't ready yet", self.ava.get("/code").text)

    def test_an_approved_student_session_runs_inside_a_sandbox(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        [record] = self.db.all("SELECT * FROM sandbox_sessions WHERE code_session_id = ?", (sid,))
        self.assertIsNotNone(record)
        self.assertIn("sandbox_created", self.actions())

    def test_turning_the_sandbox_off_stops_a_waiting_student_step(self):
        self.h.services.settings.code_student_free_rein = False
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        [write] = self.steps(sid, "pending")
        self.h.services.settings.code_sandbox = "off"
        self.kim.post(f"/code/steps/{write['id']}/approve", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.code.get(sid)["status"], "failed")
        self.assertIn("The sandbox is off", self.code.get(sid)["error"])

    def test_student_access_cannot_be_switched_on_without_a_sandbox(self):
        settings = self.h.services.settings
        values = {f.name: saved_settings.current(settings, f.name) for f in saved_settings.SECTIONS["code"][1]}
        values.update(code_student_access="1", code_sandbox="off")
        _, errors = saved_settings.validate("code", values, settings=settings, db=self.db)
        self.assertIn("code_student_access", errors)


# ======================================================================================================== rule 7
class Rule07PatchAndPrivacy(CodeStudentTest):
    """Rule 7: A student can download a patch only after a mentor releases it, and sees only their own sessions."""

    def test_the_patch_is_blocked_until_a_mentor_releases_it(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        blocked = self.ava.get(f"/code/{sid}/changes.patch")
        self.assertEqual(blocked.status_code, 403)
        self.assertNotIn("Download patch", self.ava.get(f"/code/{sid}").text)
        self.assertEqual(self.kim.get(f"/code/{sid}/changes.patch").status_code, 200)
        self.assertEqual(self.ava.post(f"/code/{sid}/release", {"note": ""}).status_code, 403)
        self.assertEqual(self.ava.get(f"/code/{sid}/changes.patch").status_code, 403, "a student can't release")
        self.kim.post(f"/code/{sid}/release", {"note": ""}, follow_redirects=False)
        self.h.idle()
        patch = self.ava.get(f"/code/{sid}/changes.patch")
        self.assertEqual(patch.status_code, 200)
        self.assertIn("+++ b/NOTES.md", patch.text)

    def test_another_student_cannot_get_a_released_patch(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        self.kim.post(f"/code/{sid}/release", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.ben.get(f"/code/{sid}/changes.patch").status_code, 404)

    def test_a_student_sees_only_their_own_sessions(self):
        sid, _ = self.ask(self.ava, "Ava secret task here.")
        self.assertEqual(self.ben.get(f"/code/{sid}").status_code, 404)
        self.assertEqual(self.ben.get(f"/code/{sid}/steps").status_code, 404)
        self.assertNotIn("Ava secret task here.", self.ben.get("/code").text)
        self.assertIn("Ava secret task here.", self.ava.get("/code").text)
        self.kim.post("/code", {"task": "Mentor only."}, follow_redirects=False)
        self.h.idle()
        mentor_sid = max(self.code.list(), key=lambda r: r["id"])["id"]
        self.assertEqual(self.ava.get(f"/code/{mentor_sid}").status_code, 404)
        self.assertEqual(self.ava.get(f"/code/{mentor_sid}/changes.patch").status_code, 404)


# ======================================================================================================== rule 8
class Rule08ScreenerNoteReachesMentor(CodeStudentTest):
    """Rule 8: The screener's note reaches the mentor on student Code requests."""

    def test_a_flagged_request_shows_the_note_to_the_mentor_and_not_the_student(self):
        sid, _ = self.ask(self.ava, "SCREEN-FLAG a request about the robot.")
        self.assertIn("Screener: Flagged for testing.", self.code.get(sid)["screen_note"])
        self.assertIn("Screener: Flagged for testing.", self.kim.get(f"/code/{sid}").text)
        self.assertNotIn("Screener:", self.ava.get(f"/code/{sid}").text)

    def test_a_follow_up_note_reaches_the_mentor_too(self):
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        self.ava.post(f"/code/{sid}/follow-up", {"text": "SCREEN-FLAG also do this"}, follow_redirects=False)
        self.h.idle()
        [note] = [s for s in self.code.steps(sid) if s["status"] == "pending"]
        page = self.kim.get(f"/code/{sid}").text
        self.assertIn("Flagged for testing.", page)
        self.assertIn(note["output"], page)

    def test_a_request_that_passes_has_no_note(self):
        sid, _ = self.ask(self.ava)
        self.assertFalse(self.code.get(sid)["screen_note"])


# ======================================================================================================== rule 9 (Code)
class Rule09SafetyRulesInCodeSessions(CodeStudentTest):
    """Rule 9: The safety rules are in the system prompt of every DM reply and every Code session."""

    def test_every_code_turn_starts_with_the_safety_rules(self):
        safety = load_safety_prompt(self.h.settings)
        self.assertGreater(len(safety), 100)
        sid, _ = self.ask(self.ava)
        self.approve(sid)
        turns = [c for c in self.h.claude.calls if c["kind"] == "tool_turn"]
        self.assertGreater(len(turns), 1)
        for call in turns:
            self.assertTrue(call["system"].startswith(safety))
        self.kim.post("/code", {"task": "A mentor's task."}, follow_redirects=False)
        self.h.idle()
        for call in [c for c in self.h.claude.calls if c["kind"] == "tool_turn"]:
            self.assertIn(safety, call["system"])


# ======================================================================================================== rule 11
class Rule11EverythingIsAudited(CodeStudentTest):
    """Rule 11: Every approval, rejection, release, setting change and deletion is written to the audit log."""

    def setUp(self):
        super().setUp()
        self.h.slack_people()
        self.coach = self.h.login(ADMIN)

    def last_actor(self, action: str):
        return audit_entry(self.h, action)["actor_id"]

    def test_slack_approval_edit_rejection_and_private_handling_are_logged(self):
        mentor = self.h.services.store
        approved = self.h.slack_dm("To approve")
        edited = self.h.slack_dm("To edit")
        rejected = self.h.slack_dm("To reject")
        held = self.h.slack_dm("To hold", user=SLACK_KID_2)
        self.assertNotIn("message_approved", audit_actions(self.h))
        self.h.slack_tap("approve", approved.message_id)
        self.h.slack_submit("edit_submit", edited.message_id, "edited", "Better words")
        self.h.slack_reject(rejected.message_id, "Because.")
        self.h.slack_tap("handle_private", held.message_id)
        mentor_id = mentor.user_by_slack_id(SLACK_MENTOR_2)["id"]
        for action, sent in (("message_approved", approved), ("message_edited", edited),
                             ("message_rejected", rejected), ("message_held", held)):
            entry = audit_entry(self.h, action)
            self.assertIsNotNone(entry, action)
            self.assertEqual((entry["target_type"], entry["target_id"], entry["actor_id"]),
                             ("message", sent.message_id, mentor_id), action)
        self.assertEqual(json.loads(audit_entry(self.h, "message_rejected")["details"])["reason"], "Because.")

    def test_window_passes_and_screener_flags_are_logged(self):
        self.h.settings.approval_window_minutes = 5
        first = self.h.slack_dm("First question")
        self.h.slack_tap("approve", first.message_id)
        passed = self.h.slack_dm("A safe follow-up", thread_ts=first.thread_ts)
        entry = audit_entry(self.h, "message_auto_approved")
        self.assertEqual(entry["target_id"], passed.message_id)
        details = json.loads(entry["details"])
        self.assertEqual((details["window_minutes"], details["screener"], details["conversation"]),
                         (5, "passed", passed.conversation_id))
        flagged = self.h.slack_dm("SCREEN-FLAG something odd", thread_ts=first.thread_ts)
        entry = audit_entry(self.h, "message_flagged_by_screener")
        self.assertEqual(entry["target_id"], flagged.message_id)
        self.assertEqual(json.loads(entry["details"])["reason"], "Flagged for testing.")

    def test_deleting_chats_is_logged(self):
        files_dir = self.h.services.attachments.files_dir
        sent = self.h.slack_dm("A chat to delete")
        details = maintenance.delete_chat(self.db, files_dir, sent.conversation_id, actor_id=self.kim.user["id"])
        self.assertIsNotNone(details)
        entry = audit_entry(self.h, "chat_deleted")
        self.assertEqual((entry["target_type"], entry["target_id"], entry["actor_id"]),
                         ("conversation", sent.conversation_id, self.kim.user["id"]))
        old = self.h.slack_dm("An old chat", user=SLACK_KID_2)
        later = datetime.now(timezone.utc) + timedelta(days=40)
        self.assertEqual(maintenance.delete_old_chats(self.db, files_dir, 30, now=later), 1)
        entry = audit_entry(self.h, "old_chats_deleted")
        self.assertEqual(json.loads(entry["details"]), {"chats": 1, "older_than_days": 30})
        self.assertIsNone(self.h.services.store.conversation(old.conversation_id))

    def test_code_approval_and_decline_and_release_are_logged(self):
        first, _ = self.ask(self.ava, "First task.")
        self.approve(first)
        self.assertEqual(self.last_actor("code_student_approved"), self.kim.user["id"])
        self.kim.post(f"/code/{first}/release", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.last_actor("code_patch_released"), self.kim.user["id"])
        self.ava.post(f"/code/{first}/follow-up", {"text": "More."}, follow_redirects=False)
        [note] = [s for s in self.code.steps(first) if s["status"] == "pending"]
        self.kim.post(f"/code/steps/{note['id']}/approve-follow-up", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.last_actor("code_student_followup_approved"), self.kim.user["id"])
        self.ava.post(f"/code/{first}/follow-up", {"text": "Even more."}, follow_redirects=False)
        [note2] = [s for s in self.code.steps(first) if s["status"] == "pending"]
        self.kim.post(f"/code/steps/{note2['id']}/decline-follow-up", {"note": "No."}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.last_actor("code_student_followup_declined"), self.kim.user["id"])
        second, _ = self.ask(self.ben, "Second task.")
        self.decline(second, "No.")
        self.assertEqual(self.last_actor("code_student_declined"), self.kim.user["id"])

    def test_a_setting_change_is_logged_with_before_and_after(self):
        resp = self.coach.post("/settings/limits", {
            "limit_pending_per_student": "1", "limit_approved_per_day": "25", "reminder_minutes": "120",
            "show_pending_to_team": "1"}, headers=HTML)
        self.assertEqual(resp.status_code, 200)
        entries = self.h.services.db.all("SELECT * FROM audit_log WHERE action = 'setting_changed'")
        by_setting = {json.loads(e["details"])["setting"]: e for e in entries}
        entry = by_setting["Waiting messages per student"]
        self.assertEqual(entry["actor_id"], self.coach.user["id"])
        self.assertEqual(json.loads(entry["details"])["to"], "1")

    def test_a_deletion_is_logged(self):
        sid, _ = self.ask(self.ava)
        self.kim.post(f"/code/{sid}/delete", {"note": ""}, follow_redirects=False)
        self.h.idle()
        self.assertIsNone(self.code.get(sid))
        entry = audit_entry(self.h, "code_session_deleted")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["actor_id"], self.kim.user["id"])


# The Slack-driven rules run again on the other AI backends, as test_flow does.
add_variants(globals(), Rule01ReachesClaudeOnlyAfterApproval, Rule02RejectedOrHeldNeverReachesClaude,
             Rule03MentorAlertPostsToApprovals, Rule04RemindersAndLimits, Rule09SafetyRulesInDMReplies,
             Rule10OutsidersCannotMessage)


if __name__ == "__main__":
    unittest.main()
