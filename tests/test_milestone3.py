"""Milestone 3: interactive explainers, attachments, code display, and the long-chat notice.
Covers the plan's acceptance test that an explainer can't reach the network or the app's cookies."""

from __future__ import annotations

import base64
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from subzero_ai.attachments import AttachmentError, prepare
from subzero_ai.db import MIGRATIONS, Database
from subzero_ai.generation import build_context
from subzero_ai.render import EXPLAINER_MAX_BYTES, explainer_download, explainers, markdown_html

from .helpers import MENTOR, STUDENT, STUDENT_2, Harness

JAVA = b"public class Arm {\r\n  double kP = 0.1;\r\n}\r\n"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000  # just the header a real MP4 starts with


def png_bytes(width=40, height=30, color=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


def jpeg_with_gps(width=3000, height=2000) -> bytes:
    img = Image.new("RGB", (width, height), (10, 120, 200))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # camera make
    exif[0x8825] = {1: "N", 2: (41.0, 52.0, 0.0)}  # GPS
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


class ExplainerRenderTest(unittest.TestCase):
    PAGE = "<!doctype html><html><head><title>t</title></head><body><script>go()</script></body></html>"

    def test_explainer_blocks_become_sandboxed_frames(self):
        out = markdown_html(f"Look:\n\n```explainer\n{self.PAGE}\n```\n", explainer_base="/c/1/x/9")
        self.assertIn('<iframe src="/c/1/x/9/0" sandbox="allow-scripts"', out)
        self.assertNotIn("allow-same-origin", out)
        self.assertIn('href="/c/1/x/9/0?download=1"', out)
        self.assertIn("data-fullscreen", out)
        self.assertIn("data-view-code", out)
        self.assertNotIn("<script>go()", out, "the page itself must only be served inside the frame")

    def test_explainers_are_numbered_in_order(self):
        text = "```explainer\none\n```\n\n```python\nx = 1\n```\n\n```explainer\ntwo\n```\n"
        self.assertEqual(explainers(text), ["one\n", "two\n"])
        out = markdown_html(text, explainer_base="/c/1/x/2")
        self.assertIn("/c/1/x/2/0", out)
        self.assertIn("/c/1/x/2/1", out)

    def test_without_a_base_explainers_show_as_code(self):
        out = markdown_html(f"```explainer\n{self.PAGE}\n```\n")
        self.assertNotIn("<iframe", out)
        self.assertNotIn("<script", out)
        self.assertIn('<div class="code">', out)

    def test_oversized_explainers_show_as_code(self):
        big = "<p>" + "x" * EXPLAINER_MAX_BYTES + "</p>"
        out = markdown_html(f"```explainer\n{big}\n```\n", explainer_base="/c/1/x/2")
        self.assertNotIn("<iframe", out)
        self.assertIn("over 500 KB", out)

    def test_downloads_carry_the_same_limits(self):
        out = explainer_download(self.PAGE)
        self.assertTrue(out.startswith("<!doctype html><html><head><meta http-equiv=\"Content-Security-Policy\""))
        self.assertIn("connect-src &#x27;none&#x27;", out)
        # <header> isn't <head>
        self.assertTrue(explainer_download("<header>x</header>").startswith("<meta"))

    def test_diffs_are_highlighted(self):
        out = markdown_html("```diff\n- double kP = 0.5;\n+ double kP = 0.1;\n```\n")
        self.assertIn('<span class="gd">', out)
        self.assertIn('<span class="gi">', out)


class PrepareTest(unittest.TestCase):
    def test_text_files_are_normalized(self):
        item = prepare("src/main/java/Arm.java", JAVA, max_text_bytes=200 * 1024)
        self.assertEqual(item.name, "Arm.java")
        self.assertEqual(item.kind, "text")
        self.assertEqual(item.data, b"public class Arm {\n  double kP = 0.1;\n}\n")

    def test_windows_paths_and_odd_names_are_cleaned(self):
        item = prepare("C:\\Users\\ava\\Drive\u202e.java", b"x", max_text_bytes=1000)
        self.assertEqual(item.name, "Drive.java")

    def test_photos_lose_location_and_shrink(self):
        item = prepare("arm.jpg", jpeg_with_gps(), max_text_bytes=1000)
        self.assertEqual(item.media_type, "image/jpeg")
        with Image.open(io.BytesIO(item.data)) as img:
            self.assertLessEqual(max(img.size), 1568)
            self.assertEqual(len(img.getexif()), 0, "EXIF (including GPS) must be removed")

    def test_refusals(self):
        cases = [
            ("robot.exe", b"MZ", "can't be attached"),
            ("notes.txt", b"", "empty"),
            ("blob.txt", b"abc\x00def", "doesn't look like a text file"),
            ("big.csv", b"a," * 200_000, "Text files can be up to 200 KB"),
            ("fake.png", b"not really an image", "couldn't be opened"),
            ("huge.png", b"x" * (50 * 1024 * 1024 + 1), "over 50 MB"),
            ("clip.mp4", b"not a video at all", "doesn't look like a video"),
            ("clip.webm", MP4, "doesn't look like a video"),
        ]
        for name, data, words in cases:
            with self.subTest(name=name), self.assertRaises(AttachmentError) as ctx:
                prepare(name, data, max_text_bytes=200 * 1024)
            self.assertIn(words, str(ctx.exception))

    def test_huge_images_are_refused_before_decoding(self):
        # A blank 7500 x 7500 PNG is tiny on disk but would take hundreds of MB to decode. Pillow alone
        # only refuses images above twice its limit, so prepare() checks the size from the header first.
        out = io.BytesIO()
        Image.new("L", (7500, 7500)).save(out, "PNG")
        self.assertLess(len(out.getvalue()), 1024 * 1024)
        with mock.patch.object(Image.Image, "load", side_effect=AssertionError("decoded the pixels")):
            with self.assertRaises(AttachmentError) as ctx:
                prepare("bomb.png", out.getvalue(), max_text_bytes=1000)
        self.assertIn("too large an image", str(ctx.exception))

    def test_a_gif_named_png_is_refused(self):
        out = io.BytesIO()
        Image.new("RGB", (4, 4)).save(out, "GIF")
        with self.assertRaises(AttachmentError):
            prepare("sneaky.png", out.getvalue(), max_text_bytes=1000)


class ContextTest(unittest.TestCase):
    def test_attachments_go_before_the_message_text(self):
        rows = [
            {"id": 1, "role": "user", "status": "approved", "content": "Review this"},
            {"id": 2, "role": "assistant", "status": "complete", "content": "Looks good"},
        ]
        files = {1: [{"type": "text", "text": '<file name="Arm.java">\nx\n</file>'}]}
        out = build_context(rows, 2, files)
        self.assertEqual(out[0]["content"], [files[1][0], {"type": "text", "text": "Review this"}])
        self.assertEqual(out[1]["content"], "Looks good")

    def test_merging_mixes_text_and_blocks(self):
        rows = [
            {"id": 1, "role": "user", "status": "approved", "content": "first"},
            {"id": 2, "role": "user", "status": "approved", "content": "second"},
        ]
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}
        out = build_context(rows, 2, {2: [image]})
        self.assertEqual(out, [{"role": "user", "content": [
            {"type": "text", "text": "first"}, image, {"type": "text", "text": "second"}]}])


class SearchTest(unittest.TestCase):
    def test_wildcards_are_searched_literally(self):
        h = Harness()
        try:
            ava = h.login(STUDENT)
            ava.new_chat("Tuning kP_2 on the arm")
            ava.new_chat("Tuning kPx2 on the arm")
            h.idle()
            # Only the page's list, not the sidebar's Recents, which shows all of Ava's chats
            listing = ava.get("/recents?q=kP_2&scope=mine").text.split('id="main"', 1)[1]
            self.assertIn("Tuning kP_2", listing)
            self.assertNotIn("Tuning kPx2", listing)
            self.assertIn("Tuning kPx2", ava.get("/recents?q=kP&scope=mine").text.split('id="main"', 1)[1])
        finally:
            h.close()


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

    def send_with(self, actor, data, files=None, cid=None):
        url = f"/c/{cid}/messages" if cid else "/chats/new"
        payload = {"text": "Can you review this?", "subteam": "Programming", **data}
        resp = actor.post(url, payload, files=files or [])
        self.h.idle()
        return resp

    def last_stream(self):
        """The latest reply request (title requests are skipped)."""
        return [c for c in self.h.claude.calls if c.get("kind") != "complete"][-1]

    def approve(self, mid):
        resp = self.kim.post(f"/m/{mid}/approve")
        self.h.idle()
        self.assertEqual(resp.status_code, 200, resp.text)

    # ----------------------------------------------------------- explainers
    def ask_for_explainer(self):
        resp = self.send_with(self.ava, {"text": "Can you make a simulation of how kP changes the arm?"})
        self.assertEqual(resp.status_code, 200, resp.text)
        mid = resp.json()["message_id"]
        cid = int(resp.json()["url"].rsplit("/", 1)[1])
        return mid, cid

    def test_explainer_request_reaches_the_card_and_claude(self):
        mid, _ = self.ask_for_explainer()
        self.assertTrue(self.h.message(mid)["wants_explainer"])
        card = json.dumps(self.h.slack.posts[-1]["blocks"])
        self.assertIn("interactive explainer", card)
        self.assertIn("Asked for an interactive explainer", self.kim.get("/queue").text)
        self.approve(mid)
        call = self.last_stream()
        self.assertEqual(call["max_tokens"], 16000)
        self.assertEqual(call["effort"], "high")
        self.assertIn("may want an interactive explainer", call["system_extra"])

    def test_everyday_questions_stay_at_default_effort(self):
        resp = self.send_with(self.ava, {"text": "What is a PID loop?"})
        self.approve(resp.json()["message_id"])
        call = self.last_stream()
        self.assertEqual((call["max_tokens"], call["effort"], call["system_extra"]), (8000, "medium", None))

    def test_explainer_frame_is_locked_down(self):
        mid, cid = self.ask_for_explainer()
        self.approve(mid)
        reply = self.h.reply_to(mid)
        page = self.ava.get(f"/c/{cid}").text
        url = f"/c/{cid}/x/{reply['id']}/0"
        self.assertIn(f'<iframe src="{url}" sandbox="allow-scripts"', page)

        resp = self.ava.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<canvas", resp.text)
        csp = resp.headers["content-security-policy"]
        # No network: nothing may connect anywhere, and only the two CDNs may serve scripts.
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'none'", csp)
        self.assertIn("form-action 'none'", csp)
        self.assertIn("script-src 'unsafe-inline' 'unsafe-eval' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net;", csp)
        # No cookies: the page gets an origin of its own even if it's opened in its own tab.
        self.assertTrue(csp.endswith("sandbox allow-scripts"))
        self.assertNotIn("allow-same-origin", csp)
        self.assertEqual(len(resp.headers.get_list("content-security-policy")), 1)
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertEqual(resp.headers["x-frame-options"], "SAMEORIGIN")
        self.assertNotIn("set-cookie", resp.headers)
        # The chat page itself still can't be framed by anyone
        self.assertEqual(self.ava.get(f"/c/{cid}").headers["x-frame-options"], "DENY")

        download = self.ava.get(url + "?download=1")
        self.assertIn("attachment;", download.headers["content-disposition"])
        self.assertIn('http-equiv="Content-Security-Policy"', download.text)

    def test_explainer_access_follows_the_chat(self):
        mid, cid = self.ask_for_explainer()
        self.approve(mid)
        reply = self.h.reply_to(mid)
        url = f"/c/{cid}/x/{reply['id']}/0"
        self.assertEqual(self.ben.get(url).status_code, 200)  # an approved team chat
        self.assertEqual(self.ava.get(f"/c/{cid}/x/{reply['id']}/1").status_code, 404)
        self.assertEqual(self.ava.get(f"/c/{cid + 1}/x/{reply['id']}/0").status_code, 404)
        self.h.services.store.make_private(cid)
        self.assertEqual(self.ben.get(url).status_code, 404)
        self.assertEqual(self.ava.get(url).status_code, 200)
        signed_out = self.h.client.get(url, headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(signed_out.status_code, 303)

    # ---------------------------------------------------------- attachments
    def test_files_wait_for_approval_then_reach_claude(self):
        files = [("files", ("Arm.java", JAVA, "text/x-java")),
                 ("files", ("screen.png", png_bytes(), "image/png"))]
        resp = self.send_with(self.ava, {}, files)
        self.assertEqual(resp.status_code, 200, resp.text)
        mid = resp.json()["message_id"]
        cid = int(resp.json()["url"].rsplit("/", 1)[1])
        self.assertEqual(self.h.claude.calls, [])

        card = json.dumps(self.h.slack.posts[-1]["blocks"])
        self.assertIn("2 files: Arm.java (", card)
        self.assertIn("screen.png", card)
        queue = self.kim.get("/queue").text
        self.assertIn("Arm.java", queue)

        page = self.ava.get(f"/c/{cid}").text
        self.assertIn("Arm.java", page)
        self.assertRegex(page, r'<img src="/files/\d+" alt="screen.png"')

        self.approve(mid)
        call = self.last_stream()
        self.assertEqual(call["effort"], "high")
        content = call["messages"][0]["content"]
        self.assertEqual(content[0]["text"], '<file name="Arm.java">\npublic class Arm {\n  double kP = 0.1;\n}\n\n</file>')
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(content[1]["source"]["media_type"], "image/png")
        Image.open(io.BytesIO(base64.b64decode(content[1]["source"]["data"]))).verify()
        self.assertEqual(content[2], {"type": "text", "text": "Can you review this?"})
        reply = self.h.reply_to(mid)
        self.assertIn("You attached 1 text file and 1 image", reply["content"])

        # Follow-ups still send the files from earlier in the chat
        follow = self.send_with(self.ava, {"text": "And the gravity term?"}, cid=cid)
        self.approve(follow.json()["message_id"])
        self.assertEqual(self.last_stream()["messages"][0]["content"][0]["text"][:22], '<file name="Arm.java">')

    def test_files_are_visible_only_with_their_message(self):
        resp = self.send_with(self.ava, {}, [("files", ("Arm.java", JAVA, "text/plain"))])
        mid = resp.json()["message_id"]
        aid = self.h.services.attachments.for_message(mid)[0]["id"]
        self.assertEqual(self.ben.get(f"/files/{aid}").status_code, 404)  # still waiting for review
        mine = self.ava.get(f"/files/{aid}")
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(mine.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("sandbox", mine.headers["content-security-policy"])
        self.assertEqual(mine.headers["x-content-type-options"], "nosniff")
        self.approve(mid)
        self.assertEqual(self.ben.get(f"/files/{aid}").status_code, 200)
        self.assertIn("attachment", self.ben.get(f"/files/{aid}?download=1").headers["content-disposition"])
        self.assertEqual(self.ben.get("/files/99999").status_code, 404)

    def test_rejected_uploads(self):
        cases = [
            ([("files", ("tool.exe", b"MZ", "application/octet-stream"))], {}, "can't be attached"),
            ([("files", (f"f{i}.txt", b"x", "text/plain")) for i in range(6)], {}, "up to 5 files"),
            ([("files", ("a.txt", b"x", "text/plain"))], {"text": "  "}, "what you'd like Claude to do"),
        ]
        for files, data, words in cases:
            with self.subTest(words=words):
                resp = self.send_with(self.ava, data, files)
                self.assertEqual(resp.status_code, 400)
                self.assertIn(words, resp.json()["error"])
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) FROM attachments")[0], 0)
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) FROM messages")[0], 0)

    # ---------------------------------------------------------------- videos
    def test_videos_are_stored_and_claude_gets_the_stills(self):
        files = [("files", ("intake.mp4", MP4, "video/mp4")),
                 ("frames", ("intake.mp4@0.jpg", png_bytes(), "image/jpeg")),
                 ("frames", ("intake.mp4@4.jpg", png_bytes(), "image/jpeg")),
                 ("frames", ("other.mp4@1.jpg", png_bytes(), "image/jpeg"))]  # no such video: dropped
        resp = self.send_with(self.ava, {"text": "Why does the intake jam?"}, files)
        self.assertEqual(resp.status_code, 200, resp.text)
        mid, cid = resp.json()["message_id"], int(resp.json()["url"].rsplit("/", 1)[1])
        rows = self.h.services.attachments.for_message(mid)
        self.assertEqual([r["kind"] for r in rows], ["video", "frame", "frame"])
        self.assertEqual(rows[2]["name"], "intake.mp4 at 0:04")
        page = self.ava.get(f"/c/{cid}").text
        self.assertIn('<video class="file-video"', page)
        self.assertNotIn("intake.mp4 at 0:04", page, "stills are for Claude, not shown on the page")
        self.assertIn("1 file:", json.dumps(self.h.slack.posts[-1]["blocks"]), "the card lists the video only")
        self.approve(mid)
        content = self.last_stream()["messages"][0]["content"]
        self.assertIn("2 still frames", content[0]["text"])
        self.assertEqual([b["type"] for b in content[:5]], ["text", "text", "image", "text", "image"])
        video = self.ava.get(f"/files/{rows[0]['id']}", headers={"Range": "bytes=0-99"})
        self.assertEqual(video.status_code, 206, "videos answer range requests so they can be played and seeked")
        self.assertEqual(video.headers["content-type"], "video/mp4")

    def test_old_pictures_are_not_resent_forever(self):
        from subzero_ai import generation
        frames = [("frames", (f"clip.mp4@{i}.jpg", png_bytes(), "image/jpeg")) for i in range(8)]
        resp = self.send_with(self.kim, {"text": "first"}, [("files", ("clip.mp4", MP4, "video/mp4")), *frames])
        cid = int(resp.json()["url"].rsplit("/", 1)[1])
        with mock.patch.object(generation, "HISTORY_IMAGES", 5):
            self.send_with(self.kim, {"text": "second"}, [("files", ("shot.png", png_bytes(), "image/png"))], cid=cid)
        images = [b for m in self.last_stream()["messages"] if isinstance(m["content"], list)
                  for b in m["content"] if b["type"] == "image"]
        self.assertEqual(len(images), 1, "only the newest message's picture; the 8 old stills are over budget")
        self.assertIn("isn't sent again", json.dumps(self.last_stream()["messages"]))

    def test_a_video_without_stills_says_so(self):
        resp = self.send_with(self.ava, {}, [("files", ("clip.mov", MP4, "video/quicktime"))])
        self.approve(resp.json()["message_id"])
        self.assertIn("no still frames", self.last_stream()["messages"][0]["content"][0]["text"])

    def test_oversized_requests_are_refused_early(self):
        resp = self.ava.post("/chats/new", {"text": "hi"}, headers={"Content-Length": str(200 * 1024 * 1024)})
        self.assertEqual(resp.status_code, 413)

    def test_edited_messages_keep_their_files(self):
        resp = self.send_with(self.ava, {}, [("files", ("Arm.java", JAVA, "text/plain"))])
        mid = resp.json()["message_id"]
        self.kim.post(f"/m/{mid}/approve", {"text": "Review only the kP line"})
        self.h.idle()
        content = self.last_stream()["messages"][0]["content"]
        self.assertTrue(content[0]["text"].startswith('<file name="Arm.java">'))
        self.assertEqual(content[1]["text"], "Review only the kP line")

    # ------------------------------------------------------------ long chats
    def test_long_chats_suggest_a_fresh_start(self):
        resp = self.send_with(self.ava, {"text": "Question"})
        mid = resp.json()["message_id"]
        cid = int(resp.json()["url"].rsplit("/", 1)[1])
        self.approve(mid)
        self.assertNotIn("This chat has gotten long", self.ava.get(f"/c/{cid}").text)
        self.h.services.db.execute("UPDATE messages SET input_tokens = 2000, cache_read_tokens = 149000 "
                                   "WHERE role = 'assistant'")
        self.assertIn("This chat has gotten long", self.ava.get(f"/c/{cid}").text)
        self.assertNotIn("This chat has gotten long", self.ben.get(f"/c/{cid}").text)

    def test_composer_offers_files_and_explainers(self):
        page = self.ava.get("/chats").text
        self.assertIn('type="file" name="files" multiple accept=".cpp,.csv,.gradle,.h,.java,.jpeg,.jpg,.json,.m4v,.md,.mov,.mp4,.png,.py,.txt,.webm,.yaml,.yml"', page)
        self.assertNotIn('name="explainer"', page, "Claude decides on explainers; there's no switch")

    def test_asking_in_words_counts_as_an_explainer_request(self):
        from subzero_ai.generation import asks_for_explainer
        for text in ("Make an interactive graph of torque", "Could you simulate the elevator?", "explainer please"):
            self.assertTrue(asks_for_explainer(text), text)
        for text in ("What is a PID loop?", "Explain how a gearbox works", "Gradle says the build artifact is missing",
                     "Why is the AdvantageScope animation choppy?", "How do I visualize odometry in Glass?",
                     "the interactive shell won't start"):
            self.assertFalse(asks_for_explainer(text), text)


if __name__ == "__main__":
    unittest.main()
