"""Code display, old attachments and projects (read only now), search, migration, and the long-chat notice.
Interactive explainers, uploads and projects were removed in 0.11; these tests keep what is left of them."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from subzero_ai.db import MIGRATIONS, Database
from subzero_ai.generation import attached_note, build_context
from subzero_ai.render import markdown_html

from .helpers import MENTOR, STUDENT, STUDENT_2, Harness


def png_bytes(width=40, height=30, color=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


class CodeRenderTest(unittest.TestCase):
    def test_diffs_are_highlighted(self):
        out = markdown_html("```diff\n- double kP = 0.5;\n+ double kP = 0.1;\n```\n")
        self.assertIn('<span class="gd">', out)
        self.assertIn('<span class="gi">', out)

    def test_an_explainer_fence_is_an_ordinary_code_block(self):
        out = markdown_html("Here:\n\n```explainer\n<!doctype html><script>go()</script>\n```\n")
        self.assertNotIn("<iframe", out)
        self.assertNotIn("data-explainer", out)
        self.assertIn('<div class="code">', out)
        self.assertNotIn("<script>", out, "the page is shown as text, never run")
        self.assertIn("&lt;", out)


class ContextTest(unittest.TestCase):
    def test_old_attachments_become_a_short_note(self):
        rows = [
            {"id": 1, "role": "user", "status": "approved", "content": "Review this"},
            {"id": 2, "role": "assistant", "status": "complete", "content": "Looks good"},
            {"id": 3, "role": "user", "status": "approved", "content": "And these"},
        ]
        out = build_context(rows, 3, {1: 1, 3: 2})
        self.assertEqual(out[0]["content"], "Review this\n\n[1 attached file not shown]")
        self.assertEqual(out[1]["content"], "Looks good")
        self.assertEqual(out[2]["content"], "And these\n\n[2 attached files not shown]")
        self.assertEqual(attached_note(1), "[1 attached file not shown]")

    def test_merging_keeps_plain_text(self):
        rows = [
            {"id": 1, "role": "user", "status": "approved", "content": "first"},
            {"id": 2, "role": "user", "status": "approved", "content": "second"},
        ]
        self.assertEqual(build_context(rows, 2, {2: 1}),
                         [{"role": "user", "content": "first\n\nsecond\n\n[1 attached file not shown]"}])


class MigrationTest(unittest.TestCase):
    def test_a_version_1_database_upgrades_in_place(self):
        tmp = Path(tempfile.mkdtemp(prefix="subzero-mig-"))
        path = tmp / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(MIGRATIONS[0] + "PRAGMA user_version = 1;")
        conn.execute("INSERT INTO users (slack_user_id, name, created_at) VALUES ('U1', 'Ava', 'now')")
        conn.execute("INSERT INTO conversations (owner_id, model, created_at, updated_at) VALUES (1, 'm', 'now', 'now')")
        conn.execute("INSERT INTO messages (conversation_id, author_id, role, content, status, created_at, updated_at) "
                     "VALUES (1, 1, 'user', 'hi', 'approved', 'now', 'now')")
        conn.commit()
        conn.close()
        db = Database(path)
        self.assertEqual(db.one("PRAGMA user_version")[0], len(MIGRATIONS))
        self.assertEqual(db.one("SELECT wants_explainer FROM messages")[0], 0)
        self.assertEqual(db.all("SELECT * FROM attachments"), [])


class Milestone3FlowTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.ava = self.h.login(STUDENT)
        self.ben = self.h.login(STUDENT_2)
        self.kim = self.h.login(MENTOR)

    def tearDown(self):
        self.h.close()

    def last_stream(self):
        """The latest reply request (title requests are skipped)."""
        return [c for c in self.h.claude.calls if c.get("kind") != "complete"][-1]

    def approve(self, mid):
        resp = self.kim.post(f"/m/{mid}/approve")
        self.h.idle()
        self.assertEqual(resp.status_code, 200, resp.text)

    # ----------------------------------------------------------- replies
    def test_everyday_questions_stay_at_default_effort(self):
        sent = self.h.slack_dm("What is a PID loop?", user=STUDENT)
        self.approve(sent.message_id)
        call = self.last_stream()
        self.assertEqual((call["max_tokens"], call["effort"], call["system_extra"]), (8000, "medium", None))

    # ------------------------------------------------- old attachments and projects
    def test_a_chat_with_old_attachment_and_project_rows_still_gets_a_reply(self):
        sent = self.h.slack_dm("Review my arm code", user=STUDENT)
        mid, cid = sent.message_id, sent.conversation_id
        self.approve(mid)
        # Rows from 0.10: a file on the first message, and the chat inside a project (with a missing file)
        self.h.add_old_attachment(mid)
        self.h.add_old_attachment(mid, "shot.png", png_bytes(), "image")
        pid = self.h.add_old_project(cid)
        follow = self.h.slack_dm("Now the intake", user=STUDENT, thread_ts=sent.thread_ts).message_id
        self.approve(follow)
        call = self.last_stream()
        self.assertEqual(self.h.reply_to(follow)["status"], "complete")
        first = call["messages"][0]["content"]
        self.assertEqual(first, "Review my arm code\n\n[2 attached files not shown]")
        self.assertNotIn("metric units", str(call), "a project's instructions are not sent any more")
        self.assertIsNone(call["system_extra"])
        self.assertEqual(self.h.services.store.conversation(cid)["project_id"], pid, "the old row is left alone")
        self.assertEqual(self.kim.get(f"/c/{cid}").status_code, 200)
        self.assertEqual(self.kim.get("/queue").status_code, 200)

    def test_old_files_open_only_for_mentors_and_admins(self):
        mid = self.h.slack_dm("Review this", user=STUDENT).message_id
        aid = self.h.add_old_attachment(mid)
        for who in (self.ava, self.ben):  # students have no chat pages, so no files either, even their own
            self.assertEqual(who.get(f"/files/{aid}").status_code, 404, who.slack_user_id)
        mine = self.kim.get(f"/files/{aid}")
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(mine.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("sandbox", mine.headers["content-security-policy"])
        self.assertEqual(mine.headers["x-content-type-options"], "nosniff")
        self.approve(mid)
        self.assertEqual(self.ben.get(f"/files/{aid}").status_code, 404)
        self.assertIn("attachment", self.kim.get(f"/files/{aid}?download=1").headers["content-disposition"])
        self.assertEqual(self.kim.get("/files/99999").status_code, 404)
        cid = self.h.message(mid)["conversation_id"]
        self.assertIn(f'href="/files/{aid}"', self.kim.get(f"/c/{cid}").text)

    def test_oversized_requests_are_refused_early(self):
        resp = self.ava.post("/profile", {"theme": "dark"}, headers={"Content-Length": str(200 * 1024 * 1024)})
        self.assertEqual(resp.status_code, 413)


if __name__ == "__main__":
    unittest.main()
