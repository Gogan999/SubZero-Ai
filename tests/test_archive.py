"""Old website chats: which chats count, the archive zip, deleting them, and who may do either (admins only)."""

from __future__ import annotations

import html
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from subzero_ai import archive
from subzero_ai.backup import list_backups
from subzero_ai.slack_api import SlackError

from .helpers import ADMIN, MENTOR, STUDENT, STUDENT_2, Harness

HTML = {"Accept": "text/html"}
OLD = "2020-03-04T10:00:00+00:00"
LATER = "2020-03-05T11:30:00+00:00"
SAME_TEXT = "How do I tune a PID loop?"


class ArchiveBase(unittest.TestCase):
    def setUp(self):
        self.h = Harness(ai_backend="fake")
        self.addCleanup(self.h.close)
        self.s = self.h.services
        self.db = self.s.db
        self.admin = self.h.login(ADMIN)
        self.ava = self.h.login(STUDENT).user["id"]
        self.ben = self.h.login(STUDENT_2).user["id"]
        self.mentor_id = self.h.login(MENTOR).user["id"]
        self.files = self.h.settings.data_dir / "files"
        self.files.mkdir(parents=True, exist_ok=True)

    # ---- seeding: the rows older versions wrote (nothing in the app writes them now)
    def chat(self, owner: int, title: str, messages: list[tuple[str, str, str]], *, created: str = OLD) -> int:
        cid = self.db.execute(
            "INSERT INTO conversations (owner_id, title, visibility, model, created_at, updated_at) "
            "VALUES (?, ?, 'team', 'anthropic/claude-sonnet-5-5', ?, ?)", (owner, title, created, LATER)).lastrowid
        for role, text, status in messages:
            self.message(cid, owner if role == "user" else None, role, text, status)
        return cid

    def message(self, cid: int, author: int | None, role: str, text: str, status: str, **extra) -> int:
        values = {"conversation_id": cid, "author_id": author, "role": role, "content": text, "status": status,
                  "created_at": OLD, "updated_at": LATER, **extra}
        return self.db.execute(
            f"INSERT INTO messages ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
            tuple(values.values())).lastrowid

    def attach(self, mid: int, name: str, data: bytes, kind: str = "text") -> str:
        relative = f"{mid}-seed-{name}"
        (self.files / relative).write_bytes(data)
        self.db.execute("INSERT INTO attachments (message_id, name, kind, media_type, size, path, created_at) "
                        "VALUES (?, ?, ?, 'application/octet-stream', ?, ?, ?)",
                        (mid, name, kind, len(data), relative, OLD))
        return relative

    def count(self, table: str) -> int:
        return self.db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]

    def audit(self, action: str) -> list[dict]:
        return [json.loads(r["details"] or "{}") for r in
                self.db.all("SELECT details FROM audit_log WHERE action = ? ORDER BY id", (action,))]

    def archives(self) -> list[Path]:
        return sorted(self.h.settings.backups_path.glob("old-chats-*.zip"))

    def run_job(self, path: str, form: dict | None = None):
        """Post the form, let the background job finish, and return the Settings page as it is afterwards."""
        self.admin.post(path, form or {}, headers=HTML, follow_redirects=False)
        self.h.idle()
        return self.admin.get("/settings")

    def name(self, user_id: int) -> str:
        return self.s.store.user(user_id)["name"]

    def make_archive(self, **form) -> zipfile.ZipFile:
        self.run_job("/settings/archive/start", form)
        (path,) = self.archives()
        return zipfile.ZipFile(path)


class Seeded(ArchiveBase):
    """Two old website chats (one with a file, a project, a share link and a review), and a Slack chat that says the
    same thing and has a file, a card and a project of its own."""

    def setUp(self):
        super().setUp()
        self.h.slack_people()
        self.web1 = self.chat(self.ava, "Arm <b>tuning</b>", [("user", SAME_TEXT, "approved"),
                                                              ("assistant", "Start with kP.", "complete")])
        self.msg1 = self.db.one("SELECT id FROM messages WHERE conversation_id = ? AND role = 'user'",
                                (self.web1,))["id"]
        self.db.execute("UPDATE messages SET reviewed_by = ?, reviewed_at = ?, review_note = 'Looks fine', "
                        "input_tokens = 120, output_tokens = 45, model = 'anthropic/claude-sonnet-5-5' WHERE id = ?",
                        (self.mentor_id, LATER, self.msg1))
        self.db.audit("message_approved", actor_id=self.mentor_id, target_type="message", target_id=self.msg1,
                      details={"conversation": self.web1})
        self.rel_text = self.attach(self.msg1, "Arm.java", b"double kP = 0.1;\n")
        self.rel_video = self.attach(self.msg1, "demo.mp4", b"\x00" * 2048, kind="video")
        self.web2 = self.chat(self.ben, "Drive base", [("user", "Which motors?", "pending")])
        self.pending2 = self.db.one("SELECT id FROM messages WHERE conversation_id = ?", (self.web2,))["id"]
        self.db.execute("INSERT INTO slack_cards (message_id, channel, ts) VALUES (?, 'CAPPROVALS', '111.000100')",
                        (self.pending2,))
        self.project = self.h.add_old_project(self.web1)
        (self.files / "missing-file.txt").write_bytes(b"notes")
        self.db.execute("INSERT INTO shares (conversation_id, token, upto_message_id, audience, status, requested_by, "
                        "views, created_at) VALUES (?, 'SECRETLINKTOKEN', ?, 'Sponsor', 'active', ?, 3, ?)",
                        (self.web1, self.msg1, self.ava, OLD))
        # the Slack chat: same words, with everything an old chat has
        sent = self.h.slack_dm(SAME_TEXT, user="UKID0001")
        self.slack_chat, self.slack_msg = sent.conversation_id, sent.message_id
        self.slack_file = self.attach(self.slack_msg, "Slack.java", b"class Slack {}")
        self.slack_project = self.h.add_old_project(self.slack_chat, "Slack chat project")
        (self.files / "missing-file.txt").write_bytes(b"notes")  # the helper's fixed path, shared with the first
        self.slack_card = self.s.store.card(self.slack_msg)


class SelectionTest(Seeded):
    def test_only_chats_without_a_slack_thread_count(self):
        summary = archive.summarize(self.db)
        self.assertEqual((summary.chats, summary.messages), (2, 3))
        self.assertEqual((summary.files, summary.videos), (2, 1))
        self.assertEqual(summary.file_bytes, len(b"double kP = 0.1;\n") + 2048)
        self.assertEqual(summary.video_bytes, 2048)
        self.assertEqual(summary.oldest, OLD)
        self.assertEqual(summary.newest, LATER)
        self.assertEqual(summary.shares, 1)
        with self.db.connect() as conn:
            archive._select_old(conn)
            ids = {r["id"] for r in conn.execute("SELECT id FROM _old_chats")}
        self.assertEqual(ids, {self.web1, self.web2})
        self.assertNotIn(self.slack_chat, ids)
        text = {r["conversation_id"] for r in self.db.all("SELECT conversation_id FROM messages WHERE content = ?",
                                                          (SAME_TEXT,))}
        self.assertEqual(text, {self.web1, self.slack_chat}, "identical content, only one of them is old")

    def test_a_brand_new_chat_without_a_thread_yet_is_not_old(self):
        young = self.chat(self.ava, "Just now", [("user", "Hi", "pending")], created=archive.utcnow())
        with self.db.connect() as conn:
            archive._select_old(conn)
            ids = {r["id"] for r in conn.execute("SELECT id FROM _old_chats")}
        self.assertNotIn(young, ids)

    def test_the_panel_shows_the_numbers(self):
        page = self.admin.get("/settings").text
        box = page.split('id="old-chats"')[1].split("</section>")[0]
        self.assertIn("<strong>2</strong> old website chats, with 3 messages", box)
        self.assertIn("<strong>2</strong> attached files", box)
        self.assertIn("Mar 4, 2020", box)
        self.assertIn("Mar 5, 2020", box)
        self.assertIn('name="include_videos"', box)
        self.assertIn('value="1"', box)
        self.assertNotIn(" checked", box.split('name="include_videos"')[1].split(">")[0], "videos are off by default")


class ArchiveBuildTest(Seeded):
    def test_the_zip_holds_everything_readable(self):
        zf = self.make_archive()
        names = set(zf.namelist())
        for expected in ("index.html", "archive.json", "shares.json", f"chats/{self.web1}.html",
                         f"chats/{self.web1}.json", f"chats/{self.web2}.html", f"chats/{self.web2}.json",
                         f"projects/{self.project}/project.json", f"projects/{self.project}/instructions.txt"):
            self.assertIn(expected, names)
        self.assertEqual([n for n in names if f"chats/{self.slack_chat}" in n or f"files/{self.slack_chat}" in n], [])
        index = zf.read("index.html").decode()
        self.assertIn(f'href="chats/{self.web1}.html"', index)
        self.assertIn(self.name(self.ava), index)
        data = json.loads(zf.read(f"chats/{self.web1}.json"))
        self.assertEqual((data["id"], data["title"], data["owner"]),
                         (self.web1, "Arm <b>tuning</b>", self.name(self.ava)))
        user, reply = data["messages"]
        self.assertEqual((user["role"], user["status"], user["text"], user["author"]),
                         ("user", "approved", SAME_TEXT, self.name(self.ava)))
        self.assertEqual(user["review"]["by"], self.name(self.mentor_id))
        self.assertEqual(user["review"]["reason"], "Looks fine")
        self.assertEqual(user["tokens"], {"input": 120, "output": 45, "cache_read": None})
        self.assertEqual(user["created_at"], OLD)
        self.assertEqual([d["action"] for d in user["decisions"]], ["message_approved"])
        self.assertEqual(reply["role"], "assistant")
        # attached file
        (text_file,) = [f for f in user["files"] if f["name"] == "Arm.java"]
        self.assertEqual(zf.read(text_file["archive_path"]), b"double kP = 0.1;\n")
        self.assertTrue(text_file["archive_path"].startswith(f"files/{self.web1}/"))
        self.assertNotIn("stored_path", text_file)
        # project and share link
        project = json.loads(zf.read(f"projects/{self.project}/project.json"))
        self.assertEqual(project["instructions"], "Always answer in metric units.")
        self.assertEqual(zf.read(project["files"][0]["archive_path"]), b"notes")
        self.assertEqual(zf.read(f"projects/{self.project}/instructions.txt").decode(),
                         "Always answer in metric units.")
        (share,) = json.loads(zf.read("shares.json"))
        self.assertEqual((share["chat"], share["audience"], share["status"], share["views"]),
                         (self.web1, "Sponsor", "active", 3))
        self.assertNotIn("SECRETLINKTOKEN", zf.read("shares.json").decode())
        self.assertNotIn(f"projects/{self.slack_project}/project.json", names)  # a Slack chat uses that project
        manifest = json.loads(zf.read("archive.json"))
        self.assertEqual((manifest["chats"], manifest["messages"], manifest["files"]), (2, 3, 1))

    def test_videos_are_left_out_unless_ticked(self):
        zf = self.make_archive()
        self.assertEqual([n for n in zf.namelist() if n.endswith("demo.mp4")], [])
        data = json.loads(zf.read(f"chats/{self.web1}.json"))
        (video,) = [f for f in data["messages"][0]["files"] if f["kind"] == "video"]
        self.assertIsNone(video["archive_path"])
        self.assertIn("not included in this archive", zf.read(f"chats/{self.web1}.html").decode())
        self.assertEqual(json.loads(zf.read("archive.json"))["videos_left_out"], 1)
        zf.close()
        self.archives()[0].unlink()
        with_videos = self.make_archive(include_videos="1")
        (name,) = [n for n in with_videos.namelist() if n.endswith("demo.mp4")]
        self.assertEqual(len(with_videos.read(name)), 2048)

    def test_pages_are_escaped_and_have_no_scripts(self):
        self.message(self.web2, self.ben, "user", "<script>alert('x')</script> & <img src=x onerror=1>", "pending")
        self.db.execute("UPDATE conversations SET title = ? WHERE id = ?", ("<script>t</script>", self.web2))
        self.db.execute("UPDATE messages SET review_note = '<b>no</b>' WHERE id = ?", (self.msg1,))
        zf = self.make_archive()
        for name in zf.namelist():
            if name.endswith(".html"):
                page = zf.read(name).decode()
                self.assertNotIn("<script", page.lower(), name)
                self.assertNotIn("onerror=", page.replace("onerror=1", "").replace("&lt;img src=x onerror=1&gt;", ""))
        page = zf.read(f"chats/{self.web2}.html").decode()
        self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; &lt;img src=x onerror=1&gt;", page)
        self.assertIn("&lt;script&gt;t&lt;/script&gt;", zf.read("index.html").decode())
        self.assertIn("&lt;b&gt;no&lt;/b&gt;", zf.read(f"chats/{self.web1}.html").decode())
        self.assertIn("Content-Security-Policy", page)
        self.assertEqual(json.loads(zf.read(f"chats/{self.web2}.json"))["title"], "<script>t</script>")

    def test_building_is_audited_and_listed_with_a_download_link(self):
        resp = self.run_job("/settings/archive/start", {})
        (path,) = self.archives()
        self.assertRegex(path.name, r"^old-chats-\d{8}-\d{6}\.zip$")
        (entry,) = self.audit("old_chats_archived")
        self.assertEqual((entry["file"], entry["chats"], entry["messages"]), (path.name, 2, 3))
        self.assertEqual(entry["bytes"], path.stat().st_size)
        self.assertIn(path.name, resp.text)
        self.assertIn(f'href="/settings/archives/{path.name}"', resp.text)
        job = archive.job_state(self.db)
        self.assertEqual((job["state"], job["kind"]), ("done", "archive"))
        self.assertEqual(self.admin.get("/settings/archive/status").json()["state"], "done")
        self.assertEqual(list(self.h.settings.backups_path.glob("*.partial")), [])
        self.assertEqual(list_backups(self.h.settings), [], "an archive isn't listed as a backup")

    def test_a_job_runs_in_the_background_and_one_at_a_time(self):
        self.assertIsNotNone(archive.start_job(self.db, "archive", "Coach"))
        self.assertTrue(archive.job_running(self.db))
        resp = self.admin.post("/settings/archive/start", {}, headers=HTML, follow_redirects=True)
        self.assertIn("already running", resp.text)
        self.assertIn("data-archive-progress", resp.text)
        self.assertEqual(self.archives(), [])
        # a job a restart left "running" is reported as stopped
        job = json.loads(self.db.get_kv(archive.JOB_KEY))
        job["boot"] = "an-earlier-run"
        self.db.set_kv(archive.JOB_KEY, json.dumps(job))
        self.assertEqual(archive.job_state(self.db)["state"], "failed")
        self.assertFalse(archive.job_running(self.db))

    def test_a_failed_build_leaves_nothing_behind(self):
        with mock.patch.object(archive, "build_archive", side_effect=OSError("disk full")):
            resp = self.run_job("/settings/archive/start", {})
        self.assertIn("couldn", html.unescape(resp.text))
        self.assertEqual(self.archives(), [])
        self.assertEqual(self.audit("old_chats_archived"), [])
        self.assertEqual(len(self.audit("old_chats_archive_failed")), 1)

    def test_build_leaves_the_chats_alone(self):
        self.make_archive()
        self.assertEqual(self.count("conversations"), 3)
        self.assertTrue((self.files / self.rel_text).exists())


class AccessTest(Seeded):
    def test_admin_only_and_csrf_on_every_route(self):
        self.run_job("/settings/archive/start", {})
        (path,) = self.archives()
        routes = [("post", "/settings/archive/start", {}),
                  ("post", "/settings/archive/delete-old", {"confirm": "DELETE"}),
                  ("post", f"/settings/archives/{path.name}/delete", {}),
                  ("post", "/settings/repost-cards", {})]
        for method, route, form in routes:
            for who in (STUDENT, MENTOR):
                self.assertEqual(self.h.login(who).post(route, form).status_code, 403, (route, who))
            self.assertEqual(self.admin.post(route, form, csrf=False).status_code, 403, route)
        for route in ("/settings/archive/status", f"/settings/archives/{path.name}"):
            for who in (STUDENT, MENTOR):
                self.assertEqual(self.h.login(who).get(route).status_code, 403, (route, who))
            self.assertEqual(self.h.client.get(route, follow_redirects=False).status_code, 303)  # signed out: login
        self.assertTrue(path.exists())
        self.assertEqual(self.count("conversations"), 3)
        self.assertEqual(self.audit("old_chats_deleted_by_admin"), [])
        self.assertEqual(len(self.audit("old_chats_archived")), 1)

    def test_a_non_admin_can_not_start_an_archive(self):
        before = self.db.get_kv(archive.JOB_KEY)
        self.h.login(MENTOR).post("/settings/archive/start", {})
        self.assertEqual(self.db.get_kv(archive.JOB_KEY), before)
        self.assertEqual(self.archives(), [])

    def test_only_this_module_and_the_settings_page_can_delete(self):
        root = Path(__file__).resolve().parent.parent / "subzero_ai"
        users = sorted(p.name for p in root.glob("*.py")
                       if ("delete_old_chats_now" in p.read_text(encoding="utf-8")
                           or "run_delete_job" in p.read_text(encoding="utf-8")))
        self.assertEqual(users, ["archive.py", "web_admin.py"])
        self.h.settings.retention_days = 0  # (the old chats are from 2020, which the usual two-year rule would delete)
        self.h.call(self.s.nightly.run)  # the nightly clean-up never touches them
        self.assertEqual(self.count("conversations"), 3)


class DownloadTest(Seeded):
    def test_download_streams_the_file_as_an_attachment(self):
        self.run_job("/settings/archive/start", {})
        (path,) = self.archives()
        resp = self.admin.get(f"/settings/archives/{path.name}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/zip")
        self.assertIn(f'filename="{path.name}"', resp.headers["content-disposition"])
        self.assertEqual(resp.content, path.read_bytes())
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(resp.content)))
        self.assertEqual(len(self.audit("old_chats_archive_downloaded")), 1)

    def test_paths_that_are_not_archives_are_refused(self):
        self.run_job("/settings/archive/start", {})
        (path,) = self.archives()
        self.h.settings.config_path.write_text("SECRET=1\n", encoding="utf-8")
        backup = self.h.call(self.s.nightly.run)
        outside = Path(tempfile.mkdtemp(prefix="subzero-out-")) / "old-chats-20200101-000000.zip"
        outside.write_bytes(b"PK")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        for name in ("..%2F..%2Fconfig.env", "%2E%2E%2Fconfig.env", "..%5Cconfig.env", "config.env", backup,
                     "old-chats-20200101-000000.zip", "old-chats-20200101-000000.zip.partial", "OLD-CHATS-1.zip",
                     f"..%2F{path.name}", f"{path.name}%00", str(outside).replace("/", "%2F")):
            resp = self.admin.get(f"/settings/archives/{name}")
            self.assertEqual(resp.status_code, 404, name)
            self.assertNotIn(b"SECRET=1", resp.content, name)
        self.assertIsNone(archive.archive_path(self.h.settings, "../config.env"))
        self.assertIsNone(archive.archive_path(self.h.settings, backup))
        # a symlink in the backups folder that points outside it isn't followed
        link = self.h.settings.backups_path / "old-chats-20210101-000000.zip"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks aren't available")
        self.assertEqual(self.admin.get(f"/settings/archives/{link.name}").status_code, 404)
        self.assertIsNotNone(archive.archive_path(self.h.settings, path.name))

    def test_delete_one_archive_is_audited(self):
        self.run_job("/settings/archive/start", {})
        (path,) = self.archives()
        resp = self.admin.post(f"/settings/archives/{path.name}/delete", {}, headers=HTML, follow_redirects=True)
        self.assertIn(f"Deleted the archive {path.name}", resp.text)
        self.assertFalse(path.exists())
        self.assertEqual(self.audit("old_chats_archive_deleted")[0]["file"], path.name)
        resp = self.admin.post(f"/settings/archives/{path.name}/delete", {}, headers=HTML, follow_redirects=True)
        self.assertIn("no archive with that name", resp.text)
        resp = self.admin.post("/settings/archives/not-an-archive.zip/delete", {}, headers=HTML,
                               follow_redirects=True)
        self.assertEqual(len(self.audit("old_chats_archive_deleted")), 1)
        page = self.admin.get("/settings").text
        self.assertIn("No archives yet.", page)

    def test_the_delete_button_asks_first(self):
        self.run_job("/settings/archive/start", {})
        page = self.admin.get("/settings").text
        self.assertRegex(page, r'class="button small reject" type="submit" data-confirm="Delete old-chats-')


class DeleteTest(Seeded):
    def delete(self, word: str = "DELETE"):
        return self.run_job("/settings/archive/delete-old", {"confirm": word})

    def test_the_exact_word_is_required(self):
        for word in ("", "delete", "Delete", "DELET", "DELETE ALL", "yes"):
            resp = self.admin.post("/settings/archive/delete-old", {"confirm": word}, headers=HTML,
                                   follow_redirects=True)
            self.h.idle()
            self.assertIn("Nothing was deleted", resp.text, word)
        self.assertEqual(self.count("conversations"), 3)
        self.assertIsNone(self.db.get_kv(archive.JOB_KEY))
        self.assertEqual(list_backups(self.h.settings), [])
        self.assertEqual(self.audit("old_chats_deleted_by_admin"), [])

    def test_a_backup_is_made_first_and_holds_the_chats(self):
        resp = self.delete()
        self.assertIn("Deleted 2 old website chats", resp.text)
        (backup,) = list_backups(self.h.settings)
        self.assertIn("before-old-chats-delete", backup.name)
        with zipfile.ZipFile(backup.path) as zf, tempfile.TemporaryDirectory() as tmp:
            zf.extract("subzero.db", tmp)
            conn = sqlite3.connect(Path(tmp) / "subzero.db")
            try:
                ids = {r[0] for r in conn.execute("SELECT id FROM conversations")}
            finally:
                conn.close()
        self.assertIn(self.web1, ids)
        self.assertIn(self.web2, ids)
        (entry,) = self.audit("old_chats_deleted_by_admin")
        self.assertEqual(entry["backup"], backup.name)

    def test_a_failed_backup_stops_everything(self):
        with mock.patch.object(archive, "make_backup", side_effect=OSError("backups folder is gone")):
            resp = self.delete()
        self.assertIn("The backup failed, so nothing was deleted", resp.text)
        self.assertEqual(self.count("conversations"), 3)
        self.assertEqual(self.count("messages"), 4)
        self.assertTrue((self.files / self.rel_text).exists())
        self.assertEqual(self.audit("old_chats_deleted_by_admin"), [])
        self.assertEqual(len(self.audit("old_chats_delete_stopped")), 1)
        self.assertEqual(archive.job_state(self.db)["state"], "failed")

    def test_old_chats_rows_and_files_are_removed(self):
        self.delete()
        self.assertIsNone(self.db.one("SELECT 1 FROM conversations WHERE id IN (?, ?)", (self.web1, self.web2)))
        self.assertIsNone(self.db.one("SELECT 1 FROM messages WHERE conversation_id IN (?, ?)",
                                      (self.web1, self.web2)))
        self.assertIsNone(self.db.one("SELECT 1 FROM attachments WHERE message_id = ?", (self.msg1,)))
        self.assertFalse((self.files / self.rel_text).exists())
        self.assertFalse((self.files / self.rel_video).exists(), "videos are files too")
        self.assertEqual(self.count("shares"), 0, "share rows of deleted chats go with them")
        # the project's files go; the project row and the tables stay
        self.assertIsNotNone(self.db.one("SELECT 1 FROM projects WHERE id = ?", (self.project,)))
        self.assertIsNone(self.db.one("SELECT 1 FROM project_files WHERE project_id = ?", (self.project,)))
        self.assertEqual(self.count("projects"), 2)
        (entry,) = self.audit("old_chats_deleted_by_admin")
        self.assertEqual((entry["chats"], entry["messages"], entry["files"], entry["shares"]), (2, 3, 2, 1))
        self.assertEqual(entry["project_files"], 1)
        self.assertEqual(entry["bytes"], len(b"double kP = 0.1;\n") + 2048 + 5)
        self.assertEqual(entry["open_cards"], 1)
        self.assertEqual(archive.job_state(self.db)["state"], "done")

    def test_slack_chats_their_cards_and_files_are_never_touched(self):
        self.delete()
        self.assertEqual(self.count("conversations"), 1)
        self.assertIsNotNone(self.db.one("SELECT 1 FROM conversations WHERE id = ?", (self.slack_chat,)))
        self.assertIsNotNone(self.db.one("SELECT 1 FROM slack_threads WHERE conversation_id = ?", (self.slack_chat,)))
        self.assertEqual(self.db.one("SELECT content FROM messages WHERE id = ?", (self.slack_msg,))["content"],
                         SAME_TEXT)
        self.assertIsNotNone(self.db.one("SELECT 1 FROM attachments WHERE message_id = ?", (self.slack_msg,)))
        self.assertEqual((self.files / self.slack_file).read_bytes(), b"class Slack {}")
        self.assertEqual(self.s.store.card(self.slack_msg)["ts"], self.slack_card["ts"])
        self.assertFalse([u for u in self.h.slack.updates if u["ts"] == self.slack_card["ts"]],
                         "the Slack chat's card isn't touched")
        self.assertEqual(self.db.one("SELECT project_id FROM conversations WHERE id = ?",
                                     (self.slack_chat,))["project_id"], self.slack_project)
        self.assertIsNotNone(self.db.one("SELECT 1 FROM project_files WHERE project_id = ?", (self.slack_project,)))
        self.assertEqual(self.s.store.message(self.slack_msg)["status"], "pending")

    def test_open_cards_are_marked_deleted_by_an_admin(self):
        self.delete()
        (update,) = [u for u in self.h.slack.updates if u["ts"] == "111.000100"]
        self.assertEqual(update["channel"], "CAPPROVALS")
        text = json.dumps(update["blocks"])
        self.assertIn("Chat deleted by", text)
        self.assertIn("Drive base", text)
        self.assertNotIn("approve", text.lower().replace("deleted by an admin", ""))
        self.assertFalse([b for b in update["blocks"] if b["type"] == "actions"], "no buttons are left")

    def test_a_card_that_cant_be_updated_doesnt_stop_the_delete(self):
        async def refuse(channel, ts, text, blocks=None):
            raise SlackError("chat.update", "message_not_found")
        self.h.slack.update_message = refuse
        resp = self.delete()
        self.assertIn("Deleted 2 old website chats", resp.text)
        self.assertEqual(self.count("conversations"), 1)
        self.assertEqual(self.audit("old_chats_deleted_by_admin")[0]["chats"], 2)

    def test_running_it_again_does_nothing(self):
        self.delete()
        backups, updates = len(list_backups(self.h.settings)), len(self.h.slack.updates)
        resp = self.delete()
        self.assertIn("There were no old website chats to delete", resp.text)
        self.assertEqual(len(list_backups(self.h.settings)), backups, "no second backup")
        self.assertEqual(len(self.audit("old_chats_deleted_by_admin")), 1)
        self.assertEqual(len(self.h.slack.updates), updates)
        self.assertEqual(self.count("conversations"), 1)
        self.assertNotIn("Delete old website chats</button>", self.admin.get("/settings").text)

    def test_deleting_after_an_archive_keeps_the_archive(self):
        self.make_archive()
        (path,) = self.archives()
        self.delete()
        self.assertTrue(path.exists())
        with zipfile.ZipFile(path) as zf:
            self.assertIn(f"chats/{self.web1}.json", zf.namelist())

    def test_a_delete_job_runs_in_the_background_with_progress(self):
        self.admin.post("/settings/archive/delete-old", {"confirm": "DELETE"}, headers=HTML)
        status = self.admin.get("/settings/archive/status").json()
        self.assertIn(status["state"], ("running", "done"))
        self.h.idle()
        self.assertEqual(self.admin.get("/settings/archive/status").json()["state"], "done")

    def test_mentors_and_students_cant_delete(self):
        for who in (STUDENT, MENTOR):
            self.assertEqual(self.h.login(who).post("/settings/archive/delete-old", {"confirm": "DELETE"}).status_code,
                             403)
        self.assertEqual(self.admin.post("/settings/archive/delete-old", {"confirm": "DELETE"},
                                         csrf=False).status_code, 403)
        self.assertEqual(self.count("conversations"), 3)


if __name__ == "__main__":
    unittest.main()
