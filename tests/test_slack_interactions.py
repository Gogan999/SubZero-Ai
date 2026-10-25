"""Button taps and modal submissions from the approval cards in Slack."""

from __future__ import annotations

import json
import unittest

from .helpers import STUDENT, Harness

MENTOR_SLACK = "UMENTOR1"
OTHER_MENTOR_SLACK = "UMENTOR2"
STUDENT_SLACK = "USTUDENT9"


def block_action(action_id, message_id, user=MENTOR_SLACK):
    return {"type": "interactive", "envelope_id": "e", "payload": {
        "type": "block_actions", "trigger_id": "trig-1", "user": {"id": user},
        "channel": {"id": "CAPPROVALS"}, "container": {"channel_id": "CAPPROVALS"},
        "actions": [{"action_id": action_id, "value": str(message_id)}]}}


def view_submission(callback_id, message_id, block, text, user=MENTOR_SLACK):
    return {"type": "interactive", "envelope_id": "e", "payload": {
        "type": "view_submission", "user": {"id": user},
        "view": {"callback_id": callback_id, "private_metadata": str(message_id),
                 "state": {"values": {block: {"text": {"type": "plain_text_input", "value": text}}}}}}}


class SlackInteractionsTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.slack.members = {MENTOR_SLACK, OTHER_MENTOR_SLACK, "UBOT"}
        self.h.slack.users[MENTOR_SLACK] = {"id": MENTOR_SLACK, "deleted": False,
                                            "profile": {"display_name": "Coach Lee", "image_72": "https://a.slack-edge.com/x.png"}}
        self.h.slack.users[OTHER_MENTOR_SLACK] = {"id": OTHER_MENTOR_SLACK, "profile": {"real_name": "Sam Ortiz"}}
        self.ava = self.h.login(STUDENT)
        resp = self.ava.new_chat("How should we gear the elevator?")
        self.mid = resp.json()["message_id"]
        self.h.idle()
        self.dispatch = self.h.services.interactions.dispatch

    def tearDown(self):
        self.h.close()

    def tap(self, action_id, user=MENTOR_SLACK):
        ack = self.h.call(self.dispatch, block_action(action_id, self.mid, user))
        self.h.idle()
        return ack

    def submit(self, callback_id, block, text, user=MENTOR_SLACK):
        ack = self.h.call(self.dispatch, view_submission(callback_id, self.mid, block, text, user))
        self.h.idle()
        return ack

    def test_approve_from_slack(self):
        self.assertIsNone(self.tap("approve"))
        msg = self.h.message(self.mid)
        self.assertEqual(msg["status"], "approved")
        self.assertEqual(msg["reviewer_name"], "Coach Lee")
        self.assertEqual(self.h.reply_to(self.mid)["status"], "complete")
        self.assertIn(f"Approved by <@{MENTOR_SLACK}>", json.dumps(self.h.slack.updates[-1]["blocks"]))
        self.assertEqual(self.h.slack.dms[-1]["user"], STUDENT)

    def test_second_tap_on_a_handled_card(self):
        self.tap("approve")
        self.tap("approve", user=OTHER_MENTOR_SLACK)
        self.tap("reject", user=OTHER_MENTOR_SLACK)
        notes = [e["text"] for e in self.h.slack.ephemerals]
        self.assertEqual(notes, ["Already approved by Coach Lee.", "Already approved by Coach Lee."])
        self.assertEqual(self.h.slack.views, [], "no modal for a message that's already handled")
        replies = self.h.services.db.all("SELECT * FROM messages WHERE role = 'assistant'")
        self.assertEqual(len(replies), 1)

    def test_people_outside_the_channel_cannot_review(self):
        self.tap("approve", user=STUDENT_SLACK)
        self.assertEqual(self.h.message(self.mid)["status"], "pending")
        self.assertEqual(self.h.slack.ephemerals[-1]["text"], "Only mentors in this channel can review messages.")
        ack = self.submit("reject_submit", "reason", "no", user=STUDENT_SLACK)
        self.assertEqual(ack["response_action"], "errors")
        self.assertEqual(self.h.message(self.mid)["status"], "pending")

    def test_edit_opens_a_modal_then_approves_the_new_text(self):
        self.tap("edit")
        view = self.h.slack.views[-1]
        self.assertEqual(view["trigger_id"], "trig-1")
        self.assertEqual(view["view"]["callback_id"], "edit_submit")
        self.assertEqual(view["view"]["blocks"][0]["element"]["initial_value"], "How should we gear the elevator?")
        ack = self.submit("edit_submit", "edited", "What gear ratio fits a 20 lb elevator with two NEOs?")
        self.assertEqual(ack, {"response_action": "clear"})
        msg = self.h.message(self.mid)
        self.assertEqual(msg["status"], "edited")
        self.assertEqual(msg["content"], "What gear ratio fits a 20 lb elevator with two NEOs?")
        self.assertEqual(msg["original_content"], "How should we gear the elevator?")

    def test_edit_of_a_very_long_message_points_to_the_app(self):
        self.h.services.db.execute("UPDATE messages SET content = ? WHERE id = ?", ("x" * 4000, self.mid))
        self.tap("edit")
        view = self.h.slack.views[-1]["view"]
        self.assertNotIn("callback_id", view)
        self.assertIn("too long to edit in Slack", json.dumps(view))

    def test_reject_modal_requires_a_reason(self):
        self.tap("reject")
        self.assertEqual(self.h.slack.views[-1]["view"]["callback_id"], "reject_submit")
        ack = self.submit("reject_submit", "reason", "   ")
        self.assertEqual(ack, {"response_action": "errors", "errors": {"reason": "Add a reason the student will see."}})
        ack = self.submit("reject_submit", "reason", "Ask about one mechanism at a time.")
        self.assertEqual(ack, {"response_action": "clear"})
        self.assertEqual(self.h.message(self.mid)["status"], "rejected")
        self.assertEqual(self.h.message(self.mid)["review_note"], "Ask about one mechanism at a time.")

    def test_modal_submitted_after_someone_else_acted(self):
        self.tap("reject")
        self.tap("approve", user=OTHER_MENTOR_SLACK)
        ack = self.submit("reject_submit", "reason", "Too broad")
        self.assertEqual(ack["response_action"], "errors")
        self.assertIn("Already approved by Sam Ortiz", ack["errors"]["reason"])

    def test_handle_privately_from_slack(self):
        self.tap("handle_private")
        self.assertEqual(self.h.message(self.mid)["status"], "held")
        conv = self.h.services.store.conversation(self.h.message(self.mid)["conversation_id"])
        self.assertEqual(conv["visibility"], "private")

    def test_other_envelopes_are_ignored(self):
        self.assertIsNone(self.h.call(self.dispatch, {"type": "events_api", "payload": {}}))
        self.assertIsNone(self.h.call(self.dispatch, {"type": "interactive", "payload": {"type": "shortcut"}}))
        self.assertIsNone(self.h.call(self.dispatch, block_action("approve", "not-a-number")))
        self.assertEqual(self.h.message(self.mid)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
