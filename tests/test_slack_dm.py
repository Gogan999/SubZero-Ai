"""Messaging the bot in Slack: DMs go through the approval flow and the reply comes back in the thread."""

from __future__ import annotations

import json
import unittest

from subzero_ai.claude import FakeClaude
from subzero_ai.slack_dm import CHUNK_LIMIT, _hard_split, escape, replace_explainers, to_slack

from .helpers import Harness
from .test_slack_interactions import block_action

MENTOR_SLACK = "UMENTOR1"
OTHER_MENTOR = "UMENTOR2"
KID = "UKID0001"
DM = "D0KID"
_counter = [0]


def next_ts() -> str:
    _counter[0] += 1
    return f"17000{_counter[0]:05d}.000100"


def dm_event(text="How do I tune a PID loop?", *, user=KID, ts=None, thread_ts=None, channel=DM, **extra):
    event = {"type": "message", "channel_type": "im", "channel": channel, "user": user, "text": text,
             "ts": ts or next_ts()}
    if thread_ts:
        event["thread_ts"] = thread_ts
    event.update(extra)
    return {"type": "events_api", "envelope_id": "e", "payload": {"type": "event_callback", "event": event}}


class DMTestBase(unittest.TestCase):
    harness_options: dict = {}

    def setUp(self):
        self.h = self.make_harness()
        self.slack = self.h.slack
        self.slack.members = {MENTOR_SLACK, OTHER_MENTOR, "UBOT"}
        self.slack.users[KID] = {"id": KID, "deleted": False, "profile": {"display_name": "Kid K"}}
        self.slack.users[MENTOR_SLACK] = {"id": MENTOR_SLACK, "profile": {"display_name": "Coach Lee"}}
        self.slack.users[OTHER_MENTOR] = {"id": OTHER_MENTOR, "profile": {"display_name": "Sam Ortiz"}}
        self.store = self.h.services.store

    def make_harness(self):
        return Harness(**self.harness_options)

    def tearDown(self):
        self.h.close()

    def send(self, text="How do I tune a PID loop?", **kwargs):
        event = dm_event(text, **kwargs)
        ack = self.h.call(self.h.services.interactions.dispatch, event)
        self.assertIsNone(ack)
        self.h.idle()
        return event["payload"]["event"]["ts"]

    def tap(self, action, message_id, user=OTHER_MENTOR):
        self.h.call(self.h.services.interactions.dispatch, block_action(action, message_id, user))
        self.h.idle()

    def thread_posts(self, thread_ts):
        return [p for p in self.slack.posts if p["thread_ts"] == thread_ts and p["channel"] == DM]

    def message_for(self, ts, channel=DM):
        row = self.h.services.db.one("SELECT message_id FROM slack_dm_messages WHERE channel = ? AND ts = ?",
                                     (channel, ts))
        return row["message_id"] if row else None


class DirectMessageFlowTest(DMTestBase):
    def test_dm_waits_for_approval_then_reply_comes_back_in_thread(self):
        ts = self.send()
        mid = self.message_for(ts)
        self.assertEqual(self.h.message(mid)["status"], "pending")
        self.assertIsNone(self.h.reply_to(mid))
        card = self.store.card(mid)
        self.assertIsNotNone(card, "the mentor approval card is posted")
        self.assertEqual(card["channel"], "CAPPROVALS")
        notes = self.thread_posts(ts)
        self.assertEqual(len(notes), 1)
        self.assertIn("A mentor will check this before the AI answers", notes[0]["text"])
        self.assertIn("also on the website", notes[0]["text"])
        self.assertIn("/c/", notes[0]["text"])
        self.assertNotIn("SubZero Robotics", notes[0]["text"])

        self.tap("approve", mid)
        self.assertEqual(self.h.message(mid)["status"], "approved")
        reply = self.h.reply_to(mid)
        self.assertEqual(reply["status"], "complete")
        posts = self.thread_posts(ts)
        self.assertEqual(posts[1]["text"], "Thinking…")
        placeholder = posts[1]["ts"]
        finals = [u for u in self.slack.updates if u["ts"] == placeholder]
        self.assertEqual(len(finals), 1)
        self.assertIn("You asked:", finals[-1]["text"])
        self.assertIn("```", finals[-1]["text"])
        self.assertEqual(self.slack.dms, [], "the decision is posted in the thread, not as a separate DM")
        # the approval card still updates
        self.assertTrue(any(u["channel"] == "CAPPROVALS" for u in self.slack.updates))
        # the student's account was made from Slack and owns the chat
        conv = self.store.conversation(self.h.message(mid)["conversation_id"])
        self.assertEqual(conv["owner_slack_id"], KID)
        self.assertEqual(conv["owner_name"], "Kid K")

    def test_follow_up_in_thread_continues_chat_and_new_message_starts_new_chat(self):
        ts = self.send()
        first = self.message_for(ts)
        self.tap("approve", first)
        follow_ts = self.send("And what about the D term?", thread_ts=ts)
        second = self.message_for(follow_ts)
        self.assertEqual(self.h.message(second)["conversation_id"], self.h.message(first)["conversation_id"])
        self.assertEqual(self.h.message(second)["status"], "pending")
        notes = [p["text"] for p in self.thread_posts(ts)]
        self.assertEqual(sum("also on the website" in n for n in notes), 1, "the website line comes only once")
        self.tap("approve", second)
        self.assertEqual(self.h.reply_to(second)["status"], "complete")
        self.assertEqual(sum(p["text"] == "Thinking…" for p in self.thread_posts(ts)), 2)

        other = self.send("A different question")
        third = self.message_for(other)
        self.assertNotEqual(self.h.message(third)["conversation_id"], self.h.message(first)["conversation_id"])
        self.assertTrue(self.thread_posts(other), "the new chat has its own thread")

    def test_reply_in_a_thread_the_bot_did_not_start(self):
        ts = self.send("hello again", thread_ts="1600000000.000100")
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages WHERE role = 'user'"))
        self.assertIn("Send a new message", self.thread_posts("1600000000.000100")[-1]["text"])
        self.assertIsNone(self.message_for(ts), "nothing was submitted")

    def test_thread_start_event_with_own_thread_ts_is_top_level(self):
        ts = next_ts()
        self.send(ts=ts, thread_ts=ts)
        self.assertIsNotNone(self.store.conversation_in_thread(DM, ts))

    def test_rejection_reaches_only_the_student_in_the_thread(self):
        ts = self.send()
        mid = self.message_for(ts)
        self.h.call(self.h.services.interactions.dispatch, {
            "type": "interactive", "envelope_id": "e", "payload": {
                "type": "view_submission", "user": {"id": OTHER_MENTOR},
                "view": {"callback_id": "reject_submit", "private_metadata": str(mid),
                         "state": {"values": {"reason": {"text": {"value": "Ask about the arm <first>"}}}}}}})
        self.h.idle()
        self.assertEqual(self.h.message(mid)["status"], "rejected")
        posts = self.thread_posts(ts)
        self.assertEqual(posts[-1]["text"], "A mentor didn't approve this: Ask about the arm &lt;first&gt;")
        self.assertEqual(self.slack.dms, [])
        self.assertTrue(all(p["channel"] == DM for p in self.slack.posts if p["channel"] != "CAPPROVALS"))
        self.assertIsNone(self.h.reply_to(mid))
        self.assertIn("Rejected", json.dumps(self.slack.updates[-1]["blocks"]))

    def test_edit_is_noted_in_thread(self):
        ts = self.send("original words")
        mid = self.message_for(ts)
        self.h.call(self.h.services.interactions.dispatch, {
            "type": "interactive", "envelope_id": "e", "payload": {
                "type": "view_submission", "user": {"id": OTHER_MENTOR},
                "view": {"callback_id": "edit_submit", "private_metadata": str(mid),
                         "state": {"values": {"edited": {"text": {"value": "better words"}}}}}}})
        self.h.idle()
        self.assertEqual(self.h.message(mid)["status"], "edited")
        self.assertTrue(any("edited your message" in p["text"] for p in self.thread_posts(ts)))
        self.assertEqual(self.h.reply_to(mid)["status"], "complete")

    def test_website_message_still_gets_the_usual_dm(self):
        ava = self.h.login("DEV-student-ava")
        mid = ava.new_chat("A website question").json()["message_id"]
        self.h.idle()
        self.tap("approve", mid)
        self.assertEqual(self.slack.dms[-1]["user"], "DEV-student-ava")
        self.assertFalse([p for p in self.slack.posts if p["channel"] == DM])

    def test_mentor_alert_hides_chat_and_alert_text_has_no_marker(self):
        ts = self.send("ALERT-TEST I feel awful")
        mid = self.message_for(ts)
        self.tap("approve", mid)
        conv_id = self.h.message(mid)["conversation_id"]
        self.assertEqual(self.store.conversation(conv_id)["visibility"], "private")
        alerts = [p for p in self.slack.posts if p["channel"] == "CAPPROVALS" and "Mentor alert" in p["text"]]
        self.assertEqual(len(alerts), 1)
        placeholder = self.thread_posts(ts)[1]["ts"]
        final = [u for u in self.slack.updates if u["ts"] == placeholder][-1]["text"]
        self.assertNotIn("MENTOR ALERT", final)
        self.assertIn("988", final)

    def test_mentor_dm_skips_approval_like_the_web(self):
        self.slack.users[MENTOR_SLACK]["profile"] = {"display_name": "Coach Lee"}
        ts = self.send("Mentor question", user=MENTOR_SLACK)
        mid = self.message_for(ts)
        self.assertEqual(self.h.message(mid)["status"], "auto")
        self.assertEqual(self.h.reply_to(mid)["status"], "complete")
        self.assertEqual(self.store.card(mid), None)
        self.assertFalse(any("A mentor will check" in p["text"] for p in self.thread_posts(ts)))
        self.assertEqual(self.thread_posts(ts)[0]["text"], "Thinking…")

    def test_failed_reply_says_so_and_links_to_the_website(self):
        ts = self.send("ERROR-TEST please")
        mid = self.message_for(ts)
        self.tap("approve", mid)
        self.assertEqual(self.h.reply_to(mid)["status"], "error")
        placeholder = self.thread_posts(ts)[1]["ts"]
        final = [u for u in self.slack.updates if u["ts"] == placeholder][-1]["text"]
        self.assertIn("couldn't finish that reply", final)
        self.assertIn("http://localhost:8787/c/", final)

    def test_explainer_reply_links_to_the_website(self):
        ts = self.send("Make me an interactive simulation of an arm")
        mid = self.message_for(ts)
        self.tap("approve", mid)
        placeholder = self.thread_posts(ts)[1]["ts"]
        final = "\n".join(u["text"] for u in self.slack.updates if u["ts"] == placeholder)
        extra = "\n".join(p["text"] for p in self.thread_posts(ts)[2:])
        text = final + extra
        self.assertNotIn("<html", text.lower())
        self.assertNotIn("explainer\n", text)
        self.assertIn("interactive explainer. Open it on the website>", text)

    def test_sources_listed_at_the_end(self):
        chunks = self.h.services.dms.reply_chunks(
            "Answer", json.dumps([{"url": "https://docs.wpilib.org/", "title": "WPILib <docs>"}]), "http://x/c/1")
        self.assertEqual(chunks, ["Answer\n\nSources:\n• <https://docs.wpilib.org/|WPILib &lt;docs&gt;>"])

    def test_approval_hook_failure_does_not_stop_the_reply(self):
        async def boom(*args):
            raise RuntimeError("hook broke")

        self.h.services.runner.on_reply = boom
        self.h.services.runner.on_reply_done = boom
        ava = self.h.login("DEV-student-ava")
        mid = ava.new_chat("A website question").json()["message_id"]
        self.h.idle()
        self.tap("approve", mid)
        self.assertEqual(self.h.reply_to(mid)["status"], "complete")


class ReplyPostingTest(DMTestBase):
    def approve_new(self, text="Question"):
        ts = self.send(text)
        self.tap("approve", self.message_for(ts))
        return ts

    def test_failed_update_falls_back_to_a_new_post(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("cannot edit")

        self.slack.update_message = broken
        ts = self.send()
        mid = self.message_for(ts)
        self.tap("approve", mid)  # the card update fails too; the reply must still arrive
        texts = [p["text"] for p in self.thread_posts(ts)]
        self.assertTrue(any("You asked:" in t for t in texts))

    def test_one_failed_chunk_does_not_drop_the_rest(self):
        real = self.slack.post_message
        calls = []

        async def flaky(channel, text, blocks=None, thread_ts=None):
            calls.append(text)
            if text == "second chunk":
                raise RuntimeError("nope")
            return await real(channel, text, blocks, thread_ts)

        self.slack.post_message = flaky
        ts = self.send()
        self.h.services.dms.reply_chunks = staticmethod(lambda *a: ["first chunk", "second chunk", "third chunk"])
        self.tap("approve", self.message_for(ts))
        self.assertEqual([t for t in calls if t.endswith("chunk")], ["second chunk", "third chunk"])
        self.assertIn("first chunk", [u["text"] for u in self.slack.updates])
        self.assertIn("third chunk", [p["text"] for p in self.thread_posts(ts)])

    def test_state_machine_for_late_placeholder(self):
        """The placeholder arrives after the answer was posted: it is edited to point to the answer."""
        import asyncio
        from subzero_ai import slack_dm

        dms = self.h.services.dms
        ts = self.send()
        mid = self.message_for(ts)
        conv = self.h.message(mid)["conversation_id"]
        real = self.slack.post_message
        gate = asyncio.Event()

        async def slow(channel, text, blocks=None, thread_ts=None):
            if text == "Thinking…":
                await gate.wait()
            return await real(channel, text, blocks, thread_ts)

        self.slack.post_message = slow
        # a finished reply for the message
        with self.h.services.db.transaction() as c:
            reply_id = self.store.add_message(c, conversation_id=conv, author_id=None, role="assistant",
                                              content="The answer", status="complete", reply_to=mid)

        async def scenario():
            starter = asyncio.ensure_future(dms.on_reply(conv, mid, reply_id))
            await asyncio.sleep(0)
            state = dms._replies[reply_id]
            state.posted.set()  # pretend the wait for the placeholder timed out
            await dms.on_reply_done(conv, mid, reply_id)
            gate.set()
            await starter
            return state

        state = self.h.call(scenario)
        self.assertTrue(state.done)
        self.assertIsNone(state.ticker, "no ticker for a placeholder that came too late")
        self.assertEqual(self.slack.posts[-1]["text"], "Thinking…")
        self.assertIn("The answer", [p["text"] for p in self.thread_posts(ts)][-2])
        self.assertEqual(self.slack.updates[-1]["text"], "(The answer is below.)")
        self.assertEqual(slack_dm.UPDATE_SECONDS, 3.0)

    def test_thread_link_exists_when_the_reply_starts_for_a_new_chat(self):
        seen = []
        original = self.h.services.runner.on_reply

        async def spy(conversation_id, user_message_id, reply_id):
            seen.append((self.store.slack_thread(conversation_id) is not None,
                         self.store.dm_message(user_message_id) is not None))
            await original(conversation_id, user_message_id, reply_id)

        self.h.services.runner.on_reply = spy
        ts = self.send("Mentor question", user=MENTOR_SLACK)  # a mentor's message starts the reply at once
        self.assertEqual(seen, [(True, True)])
        self.assertEqual(self.thread_posts(ts)[0]["text"], "Thinking…")

    def test_failed_thread_post_keeps_the_normal_dm(self):
        ts = self.send()
        mid = self.message_for(ts)
        self.slack.fail_posts = True
        self.h.call(self.h.services.interactions.dispatch, {
            "type": "interactive", "envelope_id": "e", "payload": {
                "type": "view_submission", "user": {"id": OTHER_MENTOR},
                "view": {"callback_id": "reject_submit", "private_metadata": str(mid),
                         "state": {"values": {"reason": {"text": {"value": "not now"}}}}}}})
        self.h.idle()
        self.slack.fail_posts = False
        self.assertEqual(self.slack.dms[-1]["user"], KID)


class StreamingUpdatesTest(DMTestBase):
    def make_harness(self):
        return Harness(claude=FakeClaude(delay=0.02))

    def test_placeholder_is_updated_while_streaming(self):
        self.h.services.dms.update_seconds = 0.05
        ts = self.send()
        self.tap("approve", self.message_for(ts))
        placeholder = self.thread_posts(ts)[1]["ts"]
        updates = [u["text"] for u in self.slack.updates if u["ts"] == placeholder]
        self.assertGreater(len(updates), 1, "partial text is shown before the final one")
        self.assertLess(len(updates[0]), len(updates[-1]))


class IgnoredEventsTest(DMTestBase):
    def assert_ignored(self, event):
        before = len(self.slack.posts)
        ack = self.h.call(self.h.services.interactions.dispatch, event)
        self.h.idle()
        self.assertIsNone(ack)
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))
        self.assertEqual(len(self.slack.posts), before)

    def test_ignored_events(self):
        self.assert_ignored(dm_event(user="UBOT"))  # the bot's own message
        self.assert_ignored(dm_event(bot_id="B123"))
        self.assert_ignored(dm_event(subtype="message_changed"))
        self.assert_ignored(dm_event(subtype="message_deleted"))
        self.assert_ignored(dm_event(subtype="bot_message"))
        self.assert_ignored(dm_event("   "))
        self.assert_ignored(dm_event(channel_type="channel"))
        event = dm_event()
        del event["payload"]["event"]["user"]
        self.assert_ignored(event)
        other = dm_event()
        other["payload"]["event"]["type"] = "app_mention"
        self.assert_ignored(other)

    def test_same_event_delivered_twice_is_submitted_once(self):
        event = dm_event()
        for _ in range(3):
            self.h.call(self.h.services.interactions.dispatch, event)
            self.h.idle()
        rows = self.h.services.db.all("SELECT * FROM messages WHERE role = 'user'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len([p for p in self.slack.posts if p["channel"] == "CAPPROVALS"]), 1)
        self.assertEqual(len(self.thread_posts(event["payload"]["event"]["ts"])), 1)

    def test_duplicate_of_a_refused_message_is_refused_once(self):
        self.h.settings.slack_dms_enabled = False
        event = dm_event()
        for _ in range(2):
            self.h.call(self.h.services.interactions.dispatch, event)
            self.h.idle()
        self.assertEqual(len(self.slack.posts), 1)


class RefusalsTest(DMTestBase):
    def test_guest_is_refused(self):
        self.slack.users["UGUEST"] = {"id": "UGUEST", "is_restricted": True, "profile": {"real_name": "Guest"}}
        ts = self.send(user="UGUEST")
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))
        self.assertIsNone(self.store.user_by_slack_id("UGUEST"))
        self.assertIn("only members of the team's Slack workspace", self.thread_posts(ts)[0]["text"])

    def test_deleted_account_and_other_workspace_are_refused(self):
        self.slack.users["UGONE"] = {"id": "UGONE", "deleted": True}
        self.slack.users["UOTHER"] = {"id": "UOTHER", "team_id": "TELSE", "profile": {"real_name": "Outsider"}}
        for user in ("UGONE", "UOTHER"):
            ts = self.send(user=user)
            self.assertIn("only members", self.thread_posts(ts)[0]["text"])
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))

    def test_deactivated_account_is_refused(self):
        row = self.store.upsert_user(slack_user_id=KID, name="Kid K", role="student")
        self.store.deactivate_user(row["id"])
        ts = self.send()
        self.assertIn("only members", self.thread_posts(ts)[0]["text"])
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))

    def test_slack_lookup_failure_is_refused(self):
        async def broken(user_id):
            raise RuntimeError("slack down")

        original = self.slack.user_info
        self.slack.user_info = broken
        event = dm_event()
        ts = event["payload"]["event"]["ts"]
        self.h.call(self.h.services.interactions.dispatch, event)
        self.h.idle()
        self.assertEqual([p["text"] for p in self.thread_posts(ts)],
                         ["Something went wrong on my side. Try again in a minute."])
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))
        # Slack's retry of the same message is handled now that Slack works again
        self.slack.user_info = original
        self.h.call(self.h.services.interactions.dispatch, event)
        self.h.idle()
        self.assertIsNotNone(self.message_for(ts))

    def test_dms_turned_off(self):
        self.h.settings.slack_dms_enabled = False
        ts = self.send()
        self.assertEqual(self.thread_posts(ts)[0]["text"],
                         "Messaging the bot is turned off. Use the website: http://localhost:8787/")
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))

    def test_files_are_refused(self):
        ts = self.send("Look at this", subtype="file_share", files=[{"id": "F1"}])
        self.assertEqual(self.thread_posts(ts)[0]["text"],
                         "I can't take files in Slack yet. Attach them on the website instead: "
                         "http://localhost:8787/")
        ts2 = self.send("", files=[{"id": "F2"}])
        self.assertIn("can't take files", self.thread_posts(ts2)[0]["text"])
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM messages"))

    def test_limit_message_is_posted(self):
        self.h.settings.limit_pending_per_student = 1
        self.send("first")
        ts = self.send("second")
        self.assertIn("1 messages waiting", self.thread_posts(ts)[0]["text"])

    def test_setting_is_on_by_default_and_editable(self):
        from subzero_ai import saved_settings
        self.assertTrue(self.h.settings.slack_dms_enabled)
        self.assertEqual(saved_settings.FIELDS["slack_dms_enabled"].label, "Let people message the bot in Slack")


class SameRulesAsTheWebTest(DMTestBase):
    harness_options = {"approval_window_minutes": 5}

    def test_approval_window_applies_to_dm_follow_ups(self):
        ts = self.send()
        self.tap("approve", self.message_for(ts))
        follow = self.send("Next question", thread_ts=ts)
        mid = self.message_for(follow)
        self.assertEqual(self.h.message(mid)["status"], "grace")
        self.assertEqual(self.h.reply_to(mid)["status"], "complete")
        self.assertFalse(any("A mentor will check" in p["text"] for p in self.thread_posts(ts)[2:]))
        # a new top-level message is a new chat, so it waits again
        fresh = self.message_for(self.send("Brand new chat"))
        self.assertEqual(self.h.message(fresh)["status"], "pending")

    def test_screener_flag_makes_dm_follow_up_wait(self):
        ts = self.send()
        self.tap("approve", self.message_for(ts))
        follow = self.send("SCREEN-FLAG something odd", thread_ts=ts)
        mid = self.message_for(follow)
        self.assertEqual(self.h.message(mid)["status"], "pending")
        self.assertTrue(self.h.message(mid)["screen_note"])
        self.assertIsNone(self.h.reply_to(mid))
        self.assertIsNotNone(self.store.card(mid))


class FormattingTest(unittest.TestCase):
    def test_bold_and_headings(self):
        self.assertEqual(to_slack("**Hi** and __there__"), ["*Hi* and *there*"])
        self.assertEqual(to_slack("## A heading\ntext"), ["*A heading*\ntext"])
        self.assertEqual(to_slack("# **Big** one #"), ["*Big one*"])
        self.assertEqual(to_slack("- one\n* two"), ["• one\n• two"])

    def test_links_and_escaping(self):
        self.assertEqual(to_slack("See [the docs](https://a.b/c?x=1&y=2) for 1 < 2 & 3 > 2"),
                         ["See <https://a.b/c?x=1&amp;y=2|the docs> for 1 &lt; 2 &amp; 3 &gt; 2"])
        self.assertEqual(to_slack("[bold **x**](https://u.v)"), ["<https://u.v|bold **x**>"])
        self.assertEqual(to_slack("[x](javascript:alert(1))"), ["[x](javascript:alert(1))"])
        self.assertEqual(escape("a&b<c>"), "a&amp;b&lt;c&gt;")

    def test_code_is_left_alone(self):
        text = "**bold**\n```java\nint a = **b**; // # not a heading [x](https://y.z)\nList<String> l;\n```\n**after**"
        out = to_slack(text)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0], "*bold*\n```\nint a = **b**; // # not a heading [x](https://y.z)\n"
                                 "List&lt;String&gt; l;\n```\n*after*")
        self.assertEqual(to_slack("Use `**x**` and **y**"), ["Use `**x**` and *y*"])

    def test_unclosed_code_block(self):
        self.assertEqual(to_slack("```\nstill **going**"), ["```\nstill **going**\n```"])

    def test_chunking_on_line_boundaries(self):
        lines = [f"line {i} " + "x" * 90 for i in range(100)]
        chunks = to_slack("\n".join(lines))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= CHUNK_LIMIT for c in chunks))
        self.assertEqual("\n".join(chunks).split("\n"), lines)

    def test_code_block_not_split_when_it_fits(self):
        code = "```\n" + "\n".join("c" * 90 for _ in range(20)) + "\n```"
        text = "\n".join("p" * 90 for _ in range(30)) + "\n" + code
        chunks = to_slack(text)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[1], code)

    def test_huge_code_block_is_split_with_fences(self):
        code = "\n".join("c" * 99 for _ in range(100))
        chunks = to_slack("```python\n" + code + "\n```")
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), CHUNK_LIMIT)
            self.assertTrue(chunk.startswith("```\n") and chunk.endswith("\n```"))
        self.assertEqual("\n".join(c[4:-4] for c in chunks), code)

    def test_single_long_line_is_cut(self):
        chunks = to_slack("y" * (CHUNK_LIMIT * 2 + 5))
        self.assertEqual([len(c) for c in chunks], [CHUNK_LIMIT, CHUNK_LIMIT, 5])

    def test_italics_stay_italics(self):
        self.assertEqual(to_slack("*slanted* and _also_ and **bold** and snake_case_name"),
                         ["_slanted_ and _also_ and *bold* and snake_case_name"])
        self.assertEqual(to_slack("**bold *and* more**"), ["*bold _and_ more*"])
        self.assertEqual(to_slack("2 * 3 * 4"), ["2 * 3 * 4"])

    def test_bars_in_link_text_and_quotes(self):
        self.assertEqual(to_slack("[a|b](https://u.v)"), ["<https://u.v|a¦b>"])
        self.assertEqual(to_slack("> quoted **x** <b>\nplain"), ["> quoted *x* &lt;b&gt;\nplain"])

    def test_hard_split_keeps_tags_and_entities_whole(self):
        for text in ("a" * (CHUNK_LIMIT - 2) + "<https://x.y/zzzz|link>", "a" * (CHUNK_LIMIT - 3) + "&amp;&amp; tail"):
            chunks = to_slack(text if text.startswith("a") else text)
            self.assertTrue(all(len(c) <= CHUNK_LIMIT for c in chunks))
            for chunk in chunks:
                self.assertEqual(chunk.count("<"), chunk.count(">"))
                self.assertNotRegex(chunk, r"&[a-z]{0,3}$")
        raw = "a" * (CHUNK_LIMIT - 2) + "<https://x.y|link>" + "b" * 10
        self.assertEqual("".join(_hard_split(raw, CHUNK_LIMIT)), raw)
        self.assertTrue(_hard_split(raw, CHUNK_LIMIT)[1].startswith("<https"))
        ent = "a" * (CHUNK_LIMIT - 2) + "&amp;"
        self.assertTrue(_hard_split(ent, CHUNK_LIMIT)[1].startswith("&amp;"))

    def test_empty(self):
        self.assertEqual(to_slack(""), [])

    def test_replace_explainers(self):
        text = "Before\n\n```explainer\n<html>big</html>\n```\n\nAfter ```java\nx\n```"
        out = replace_explainers(text, "http://site/c/4")
        self.assertNotIn("<html>", out)
        self.assertIn("(http://site/c/4)", out)
        self.assertIn("Before", out)
        kept = replace_explainers("```java\nint x;\n```", "http://site/c/4")
        self.assertEqual(kept, "```java\nint x;\n```")


if __name__ == "__main__":
    unittest.main()


class PruneTest(DMTestBase):
    def test_old_message_ids_are_pruned(self):
        from datetime import datetime, timedelta, timezone

        from subzero_ai import maintenance

        ts = self.send()
        self.assertEqual(maintenance.prune_dm_messages(self.h.services.db), 0)
        later = datetime.now(timezone.utc) + timedelta(days=8)
        self.assertEqual(maintenance.prune_dm_messages(self.h.services.db, now=later), 1)
        self.assertIsNone(self.message_for(ts))
        self.assertIsNotNone(self.h.services.db.one("SELECT 1 FROM messages"), "the chat itself stays")

    def test_deleting_a_chat_keeps_the_dedupe_row(self):
        ts = self.send()
        mid = self.message_for(ts)
        self.h.services.db.execute("DELETE FROM conversations WHERE id = ?", (self.h.message(mid)["conversation_id"],))
        row = self.h.services.db.one("SELECT * FROM slack_dm_messages WHERE ts = ?", (ts,))
        self.assertIsNotNone(row)
        self.assertIsNone(row["message_id"])
