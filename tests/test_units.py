"""Smaller pieces: Markdown rendering, what Claude sees, the alert marker, prompts, Slack layouts, settings."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from subzero_ai import slack_cards
from subzero_ai.config import load_settings, parse_env_file
from subzero_ai.generation import build_context, detect_alert, load_system_prompt
from subzero_ai.render import markdown_html, plain_html


class RenderTest(unittest.TestCase):
    def test_code_blocks_are_highlighted_with_a_copy_button(self):
        out = markdown_html("Try this:\n\n```java\nint x = 5;\n```\n")
        self.assertIn('<div class="code">', out)
        self.assertIn("<span>java</span>", out)
        self.assertIn('data-copy', out)
        self.assertIn('<span class="kt">int</span>', out)  # Pygments token classes

    def test_unknown_language_falls_back_to_plain_text(self):
        out = markdown_html("```notalanguage\n<b>x</b>\n```")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out)

    def test_raw_html_is_escaped(self):
        out = markdown_html('<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">')
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)

    def test_dangerous_links_are_not_linked(self):
        out = markdown_html("[click](javascript:alert(1)) and [ok](https://docs.wpilib.org)")
        self.assertNotIn('href="javascript', out)
        self.assertIn('href="https://docs.wpilib.org" target="_blank" rel="noopener noreferrer nofollow"', out)

    def test_images_become_links(self):
        out = markdown_html("![diagram](https://example.org/a.png)")
        self.assertNotIn("<img", out)
        self.assertIn('<a href="https://example.org/a.png"', out)

    def test_tables_render(self):
        out = markdown_html("| Material | Yield |\n|---|---|\n| 6061-T6 | 40 ksi |\n")
        self.assertIn("<table>", out)

    def test_plain_text_is_escaped(self):
        self.assertEqual(plain_html("a < b & c"), "a &lt; b &amp; c")

    def test_short_titles_cut_at_a_word(self):
        from subzero_ai.web import short_text
        self.assertEqual(short_text("Our arm drifts down when the robot is disabled. Here's the subsystem"),
                         "Our arm drifts down when the robot is disabled. Here's the…")
        self.assertEqual(short_text("  short\n\ntitle "), "short title")
        self.assertEqual(short_text("x" * 80), "x" * 60 + "…")


def row(id, role, status, content):
    return {"id": id, "role": role, "status": status, "content": content}


class ContextTest(unittest.TestCase):
    def test_only_approved_messages_and_finished_replies(self):
        rows = [
            row(1, "user", "approved", "Q1"),
            row(2, "assistant", "complete", "A1"),
            row(3, "user", "rejected", "bad"),
            row(4, "user", "held", "private"),
            row(5, "user", "withdrawn", "never mind"),
            row(6, "user", "edited", "Q2 (edited)"),
            row(7, "assistant", "error", ""),
            row(8, "user", "pending", "later"),
        ]
        self.assertEqual(build_context(rows, 6), [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2 (edited)"},
        ])

    def test_consecutive_turns_are_merged(self):
        rows = [row(1, "user", "approved", "First"), row(2, "assistant", "error", ""),
                row(3, "user", "approved", "Second")]
        self.assertEqual(build_context(rows, 3), [{"role": "user", "content": "First\n\nSecond"}])

    def test_context_starts_with_the_user(self):
        rows = [row(1, "assistant", "complete", "Hello"), row(2, "user", "auto", "Q")]
        self.assertEqual(build_context(rows, 2), [{"role": "user", "content": "Q"}])


class AlertMarkerTest(unittest.TestCase):
    def test_marker_line_is_removed(self):
        self.assertEqual(detect_alert("[MENTOR ALERT]\nPlease talk to someone.", False),
                         (True, True, "Please talk to someone."))

    def test_waits_while_the_marker_might_still_arrive(self):
        self.assertEqual(detect_alert("[MENT", False), (False, False, ""))
        self.assertEqual(detect_alert("  [MENTOR ALERT]", False), (False, False, ""))

    def test_normal_replies_pass_straight_through(self):
        self.assertEqual(detect_alert("Here's how", False), (True, False, "Here's how"))
        self.assertEqual(detect_alert("[Note] x", False), (True, False, "[Note] x"))

    def test_short_final_reply(self):
        self.assertEqual(detect_alert("[MENT", True), (True, False, "[MENT"))
        self.assertEqual(detect_alert("[MENTOR ALERT] Talk to a mentor.", True), (True, True, "Talk to a mentor."))


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = load_settings(search_cwd=False, data_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_three_layers_with_team_filled_in(self):
        text = load_system_prompt(self.settings)
        self.assertIn("high school FIRST Robotics Competition team", text)
        self.assertIn("[MENTOR ALERT]", text)
        self.assertIn("## Team context", text)
        self.assertIn("## Formatting", text)
        self.assertNotIn("{app_name}", text)
        self.assertNotIn("5690", text)

    def test_a_file_in_the_data_folder_replaces_a_layer(self):
        (self.tmp / "prompts").mkdir()
        (self.tmp / "prompts" / "team.md").write_text("## Team context\n- We use Java with AdvantageKit.", encoding="utf-8")
        text = load_system_prompt(self.settings)
        self.assertIn("AdvantageKit", text)
        self.assertIn("[MENTOR ALERT]", text, "the safety layer stays")


class SlackCardsTest(unittest.TestCase):
    def test_long_messages_are_cut_with_a_link(self):
        blocks = slack_cards.approval_card(message_id=7, student_name="Ava", subteam="Programming", title=None,
                                           text="x" * 5000, link="https://ai.example.org/c/3")
        body = blocks[1]["text"]["text"]
        self.assertLess(len(body), 3000, "Slack section text is limited to 3,000 characters")
        self.assertIn("Open in SubZero AI to read the whole message", body)

    def test_text_is_escaped_for_slack(self):
        blocks = slack_cards.approval_card(message_id=7, student_name="<!channel>", subteam=None, title="a & b",
                                           text="<@U123> hi\n\nsecond", link="https://x")
        flat = str(blocks)
        self.assertNotIn("<!channel>", flat)
        self.assertNotIn("<@U123>", flat)
        self.assertIn("&lt;@U123&gt;", flat)
        self.assertIn("> second", blocks[1]["text"]["text"])

    def test_follow_up_shows_200_characters_of_the_last_reply(self):
        blocks = slack_cards.approval_card(message_id=7, student_name="Ava", subteam=None, title="t", text="q",
                                           link="https://x", previous_reply="y" * 500)
        context = blocks[2]["elements"][0]["text"]
        self.assertTrue(context.startswith("Replying to Claude: "))
        self.assertEqual(context.removeprefix("Replying to Claude: "), "y" * 200 + "…")

    def test_handled_card_has_no_buttons(self):
        blocks = slack_cards.handled_card(student_name="Ava", subteam=None, title="t", text="q", link="https://x",
                                          outcome="rejected", mentor_slack_id="UKIM",
                                          acted_at="2026-10-25T19:42:00+00:00", reason="Too broad")
        self.assertFalse(any(b["type"] == "actions" for b in blocks))
        line = blocks[-1]["elements"][0]["text"]
        self.assertIn("Rejected by <@UKIM>", line)
        self.assertIn("<!date^", line)
        self.assertIn("Reason: Too broad", line)

    def test_modals(self):
        edit = slack_cards.edit_modal(5, "original text")
        self.assertEqual(edit["callback_id"], "edit_submit")
        self.assertEqual(edit["private_metadata"], "5")
        self.assertEqual(edit["blocks"][0]["element"]["initial_value"], "original text")
        reject = slack_cards.reject_modal(5)
        self.assertEqual(reject["blocks"][0]["block_id"], "reason")
        self.assertLessEqual(len(edit["title"]["text"]), 24, "Slack modal titles are limited to 24 characters")
        self.assertLessEqual(len(reject["title"]["text"]), 24)
        self.assertLessEqual(len(edit["submit"]["text"]), 24)

    def test_dm_text(self):
        text = slack_cards.dm_text("rejected", title="Arm <PID>", link="https://x/c/1", mentor_name="Kim",
                                   reason="Ask one thing at a time")
        self.assertIn("Kim didn't approve your message in “Arm &lt;PID&gt;”", text)
        self.assertIn("Ask one thing at a time", text)
        with self.assertRaises(ValueError):
            slack_cards.dm_text("held", title=None, link="x", mentor_name=None)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("DATA_DIR", "SUBZERO_CONFIG", "PORT", "DEV_LOGIN", "ADMIN_SLACK_IDS"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_env_file(self):
        path = self.tmp / "config.env"
        path.write_text('﻿# comment\nA=1\nB = "quoted value"\n\nC=\'x=y\'\nnot a setting\n', encoding="utf-8")
        self.assertEqual(parse_env_file(path), {"A": "1", "B": "quoted value", "C": "x=y"})

    def test_config_file_then_environment(self):
        (self.tmp / "config.env").write_text(
            "PORT=9000\nDEV_LOGIN=yes\nADMIN_SLACK_IDS=U1, U2,\nLIMIT_PENDING_PER_STUDENT=5\n", encoding="utf-8")
        os.environ["PORT"] = "9100"
        settings = load_settings(search_cwd=False, data_dir=self.tmp)
        self.assertEqual(settings.port, 9100, "environment variables win over config.env")
        self.assertTrue(settings.dev_login)
        self.assertEqual(settings.admin_slack_ids, frozenset({"U1", "U2"}))
        self.assertEqual(settings.limit_pending_per_student, 5)
        self.assertEqual(settings.config_file, str(self.tmp / "config.env"))
        for sub in ("data", "files", "backups", "logs"):
            self.assertTrue((self.tmp / sub).is_dir())

    def test_session_secret_is_created_once(self):
        first = load_settings(search_cwd=False, data_dir=self.tmp).session_secret
        second = load_settings(search_cwd=False, data_dir=self.tmp).session_secret
        self.assertEqual(first, second)
        self.assertGreater(len(first), 40)

    def test_slack_and_https_flags(self):
        settings = load_settings(search_cwd=False, data_dir=self.tmp, public_url="https://ai.example.org/",
                                 slack_bot_token="xoxb-1", slack_app_token="xapp-1")
        self.assertTrue(settings.https)
        self.assertTrue(settings.socket_mode_enabled)
        self.assertFalse(settings.slack_login_enabled)
        self.assertEqual(settings.url("/c/1"), "https://ai.example.org/c/1")


if __name__ == "__main__":
    unittest.main()
