"""SubZero Hub's Slack router: DMs offered to modules before the AI, module button taps, and the Home tab."""

from __future__ import annotations

import unittest

from subzero_ai.hub import modules
from subzero_ai.hub.modules import Module, NavItem

from .helpers import STUDENT, Harness
from .test_slack_dm import DM, KID, DMTestBase


class EchoModule(Module):
    """Answers the DM "ping" and the action "echo:tap"; counts what it saw."""

    key = "echo"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.dms: list[str] = []
        self.actions: list[str] = []

    async def on_dm(self, services, event, text):
        self.dms.append(text)
        if self.fail:
            raise RuntimeError("echo broke")
        if text.lower() != "ping":
            return False
        await services.slack.post_message(event["channel"], "pong")
        return True

    async def on_action(self, services, payload, action_id):
        self.actions.append(action_id)
        return True

    def slack_home_blocks(self, services, viewer):
        return [{"type": "section", "text": {"type": "mrkdwn", "text": f"Echo for {viewer.name}"}}]


class RouterTest(DMTestBase):
    def setUp(self):
        super().setUp()
        self.echo = EchoModule()
        self.original = modules.installed
        modules.installed = lambda: [*self.original(), self.echo]

    def tearDown(self):
        modules.installed = self.original
        super().tearDown()

    def test_a_module_answers_its_own_words_and_the_ai_never_sees_them(self):
        ts = self.send("Ping", ask=False)
        self.assertEqual(self.echo.dms, ["Ping"])
        self.assertIsNone(self.message_for(ts))
        self.assertTrue(any(p["text"] == "pong" for p in self.slack.posts))

    def test_ask_goes_to_the_ai_for_review(self):
        ts = self.send("How do I tune a PID loop?")
        mid = self.message_for(ts)
        self.assertEqual(self.h.message(mid)["status"], "pending")
        self.assertEqual(self.h.message(mid)["content"], "How do I tune a PID loop?")
        self.assertIsNotNone(self.store.card(mid))

    def test_anything_else_gets_the_command_list_and_starts_nothing(self):
        for text in ("How do I tune a PID loop?", "I'm here", "asking for a friend"):
            with self.subTest(text=text):
                ts = self.send(text, ask=False)
                self.assertIsNone(self.message_for(ts))
                self.assertTrue(self.slack.posts[-1]["text"].startswith("I didn't understand that"))
        self.assertEqual(self.echo.dms[-1], "asking for a friend", "modules still see it first")
        help_reply = self.send("help", ask=False)
        self.assertTrue(self.slack.posts[-1]["text"].startswith("*Here's what you can send me:*"))
        self.assertIsNone(self.message_for(help_reply))
        self.assertNotIn("help", self.echo.dms, "help is answered before the modules")

    def test_a_reply_in_an_ai_thread_needs_no_ask(self):
        first = self.send("How do I tune a PID loop?")
        self.send("And the I term?", thread_ts=first)
        self.assertFalse(self.slack.posts[-1]["text"].startswith("I didn't understand"))
        self.assertNotIn("And the I term?", self.echo.dms)

    def test_thread_replies_edits_and_files_skip_the_modules(self):
        first = self.send("How do I tune a PID loop?")
        self.echo.dms.clear()
        self.send("ping", thread_ts=first)
        self.send("ping", subtype="message_changed")
        self.send("ping", files=[{"id": "F1"}])
        self.assertEqual(self.echo.dms, [])

    def test_a_failing_module_doesnt_pass_the_message_on(self):
        self.echo.fail = True
        ts = self.send("ping")
        self.assertIsNone(self.message_for(ts))

    def test_module_button_taps_go_to_the_module(self):
        payload = {"type": "block_actions", "user": {"id": KID}, "channel": {"id": DM},
                   "actions": [{"action_id": "echo:tap", "value": "1"}]}
        self.h.call(self.h.services.interactions.dispatch, {"type": "interactive", "payload": payload})
        self.h.idle()
        self.assertEqual(self.echo.actions, ["echo:tap"])

    def test_an_action_for_no_module_is_ignored(self):
        payload = {"type": "block_actions", "user": {"id": KID}, "actions": [{"action_id": "nobody:tap"}]}
        self.h.call(self.h.services.interactions.dispatch, {"type": "interactive", "payload": payload})
        self.h.idle()
        self.assertEqual(self.echo.actions, [])

    def home(self, user=KID):
        event = {"type": "events_api", "payload": {"event": {"type": "app_home_opened", "tab": "home", "user": user}}}
        self.h.call(self.h.services.interactions.dispatch, event)
        self.h.idle()
        return self.slack.homes[-1]

    def test_the_home_tab_has_each_modules_section(self):
        home = self.home()
        self.assertEqual(home["user"], KID)
        text = str(home["view"]["blocks"])
        self.assertIn("SubZero AI", text)
        self.assertIn("A mentor checks it first.", text)
        self.assertIn("Echo for Kid K", text)

    def test_the_home_tab_for_an_account_that_cant_use_the_hub(self):
        self.slack.users["UGUEST"] = {"id": "UGUEST", "is_restricted": True}
        text = str(self.home("UGUEST")["view"]["blocks"])
        self.assertIn("can't use the team's tools", text)
        self.assertNotIn("Echo for", text)

    def test_the_messages_tab_opening_publishes_nothing(self):
        event = {"type": "events_api", "payload": {"event": {"type": "app_home_opened", "tab": "messages",
                                                             "user": KID}}}
        self.h.call(self.h.services.interactions.dispatch, event)
        self.h.idle()
        self.assertEqual(self.slack.homes, [])


class ToolsModule(Module):
    key = "tools"

    def nav(self, viewer):
        return [NavItem("Time Clock", "/timeclock", "log")]


class HomePageTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.ava = self.h.login(STUDENT)
        self.original = modules.installed

    def tearDown(self):
        modules.installed = self.original
        self.h.close()

    def test_with_one_tool_home_goes_straight_to_the_chat(self):
        modules.installed = lambda: [m for m in self.original() if m.key in ("hub", "ai")]
        resp = self.ava.get("/", follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/chats")
        self.assertNotIn('<nav aria-label="Tools">', self.ava.get("/chats").text)

    def test_with_more_tools_home_lists_them_and_the_sidebar_switches_between_them(self):
        modules.installed = lambda: [*self.original(), ToolsModule()]
        page = self.ava.get("/").text
        self.assertIn('class="tool-tile" href="/chats"', page)
        self.assertIn('class="tool-tile" href="/timeclock"', page)
        sidebar = self.ava.get("/chats").text
        self.assertIn('<nav aria-label="Tools">', sidebar)
        self.assertIn('href="/timeclock"', sidebar)

    def test_signed_out_goes_to_sign_in(self):
        self.h.client.cookies.clear()
        self.assertEqual(self.h.client.get("/", follow_redirects=False).headers["location"], "/login")
