"""The Files page: who can open it, the inventory for each kind, sorting and filtering, viewing, the three ways to delete,
the confirmation rules, the protection of sessions in use, attempts to reach other files, the audit rows and CSRF."""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from subzero_ai import files_admin
from subzero_ai.db import utcnow

from .helpers import ADMIN, MENTOR, STUDENT, Harness

HTML = {"Accept": "text/html"}
OLD = "2020-03-04T10:00:00+00:00"


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat(timespec="seconds")


class FilesBase(unittest.TestCase):
    def setUp(self):
        self.h = Harness(ai_backend="fake", code_sandbox="fake", sandbox_min_free_gb=0)
        self.addCleanup(self.h.close)
        self.s = self.h.services
        self.db = self.s.db
        self.admin = self.h.login(ADMIN)
        self.kim = self.h.login(MENTOR)
        self.ava = self.h.login(STUDENT)
        self.ava_id = self.ava.user["id"]
        self.kim_id = self.kim.user["id"]
        self.data = self.h.settings.data_dir
        self.files = self.data / "files"
        self.files.mkdir(parents=True, exist_ok=True)

    # ---- seeding
    def chat(self, owner: int | None = None) -> int:
        owner = owner or self.ava_id
        cid = self.db.execute(
            "INSERT INTO conversations (owner_id, title, visibility, model, created_at, updated_at) "
            "VALUES (?, 'Old chat', 'team', 'anthropic/claude-sonnet-5-5', ?, ?)", (owner, OLD, OLD)).lastrowid
        self.mid = self.db.execute(
            "INSERT INTO messages (conversation_id, author_id, role, content, status, created_at, updated_at) "
            "VALUES (?, ?, 'user', 'hi', 'approved', ?, ?)", (cid, owner, OLD, OLD)).lastrowid
        return cid

    def attach(self, name="Arm.java", data=b"double kP = 0.1;\n", kind="text", media="text/plain", created=OLD,
               owner: int | None = None) -> int:
        self.chat(owner)
        relative = f"{self.mid}-seed-{name}"
        (self.files / relative).write_bytes(data)
        return self.db.execute(
            "INSERT INTO attachments (message_id, name, kind, media_type, size, path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (self.mid, name, kind, media, len(data), relative, created)).lastrowid

    def project_file(self, name="notes.txt", data=b"notes") -> int:
        (self.files / "projects").mkdir(exist_ok=True)
        pid = self.db.execute("INSERT INTO projects (name, created_at, updated_at) VALUES ('Old project', ?, ?)",
                              (OLD, OLD)).lastrowid
        (self.files / "projects" / f"{pid}-{name}").write_bytes(data)
        return self.db.execute(
            "INSERT INTO project_files (project_id, name, kind, media_type, size, path, added_by, created_at) "
            "VALUES (?, ?, 'text', 'text/plain', ?, ?, ?, ?)",
            (pid, name, len(data), f"projects/{pid}-{name}", self.kim_id, OLD)).lastrowid

    def session(self, status="done", owner: int | None = None, updated: str | None = None, files=True) -> int:
        now = utcnow()
        sid = self.db.execute(
            "INSERT INTO code_sessions (title, task, status, model, repo, ref, created_by, created_at, updated_at, "
            "engine) VALUES ('Fix the arm', 'Fix it', ?, 'anthropic/claude-sonnet-5-5', 'team/robot-code', 'main', ?, "
            "?, ?, 'agent')", (status, owner or self.ava_id, now, updated or now)).lastrowid
        if files:
            root = self.data / "code" / str(sid)
            (root / "work").mkdir(parents=True)
            (root / "work" / "Arm.java").write_text("class Arm {}\n")
            (root / "agent.patch").write_text("diff --git a/x b/x\n")
        return sid

    def sandbox(self, sid: int | None, state="running", data=b"x" * 2000) -> int:
        home = self.data / "sandbox-fake" / f"szsandbox-{sid or 0}-s001-abc{state[:1]}"
        home.mkdir(parents=True, exist_ok=True)
        (home / "disk.bin").write_bytes(data)
        expires = (datetime.now(timezone.utc) + timedelta(hours=40)).isoformat(timespec="seconds")
        return self.db.execute(
            "INSERT INTO sandbox_sessions (code_session_id, backend, name, state, created_at, expires_at) "
            "VALUES (?, 'fake', ?, ?, ?, ?)", (sid, home.name, state, utcnow(), expires)).lastrowid

    def archive_zip(self, name="old-chats-20250101-120000.zip", data=b"PK") -> Path:
        folder = self.h.settings.backups_path
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        return folder / name

    # ---- requests
    def page(self, actor=None, path="/files"):
        return (actor or self.kim).get(path, headers=HTML)

    def confirm(self, actor=None, **form):
        return (actor or self.kim).post("/files/delete", {"csrf": (actor or self.kim).csrf, **form}, headers=HTML)

    def do(self, actor=None, **form):
        actor = actor or self.kim
        return actor.post("/files/delete/confirm", {"csrf": actor.csrf, **form}, headers=HTML,
                          follow_redirects=False)

    def audits(self):
        return [json.loads(r["details"]) for r in self.db.all(
            "SELECT details FROM audit_log WHERE action = 'files_deleted' ORDER BY id")]

    def store(self):
        return files_admin.FileStore(self.s.settings, self.db, self.s.code, self.s.sandboxes)


class AccessTest(FilesBase):
    def test_mentors_and_admins_can_open_it_students_cannot(self):
        self.assertEqual(self.page(self.kim).status_code, 200)
        self.assertEqual(self.page(self.admin).status_code, 200)
        self.assertEqual(self.page(self.ava).status_code, 403)
        self.assertEqual(self.ava.post("/files/delete", {"one": "a-1"}).status_code, 403)
        self.assertEqual(self.ava.post("/files/delete/confirm", {"ids": "a-1"}).status_code, 403)
        self.assertEqual(self.ava.get("/files/view/a-1", headers=HTML).status_code, 403)

    def test_signed_out_goes_to_sign_in(self):
        r = self.h.client.get("/files", headers=HTML, follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def test_sidebar_link_is_for_mentors_only(self):
        self.assertIn('href="/files"', self.page(self.kim, "/about").text)
        self.assertNotIn('href="/files"', self.page(self.ava, "/about").text)

    def test_old_attachment_viewer_still_works(self):
        aid = self.attach()
        self.assertEqual(self.kim.get(f"/files/{aid}").status_code, 200)


class InventoryTest(FilesBase):
    def test_every_kind_is_listed_with_owner_and_size(self):
        aid = self.attach()
        pf = self.project_file()
        sid = self.session()
        sb = self.sandbox(sid)
        self.archive_zip()
        items = {i.id: i for i in self.store().items()}
        self.assertEqual(set(items), {f"a-{aid}", f"p-{pf}", f"w-{sid}", f"s-{sb}", "z-old-chats-20250101-120000.zip"})
        self.assertEqual(items[f"a-{aid}"].owner, self.ava.user["name"])
        self.assertEqual(items[f"a-{aid}"].size, len(b"double kP = 0.1;\n"))
        self.assertEqual(items[f"p-{pf}"].owner, self.kim.user["name"])
        self.assertEqual(items[f"w-{sid}"].owner, self.ava.user["name"])
        self.assertGreater(items[f"w-{sid}"].size, 10)
        self.assertEqual(items[f"s-{sb}"].size, 2000)
        text = self.page().text
        for label in ("Old chat files", "Old project files", "Code copies", "Sandbox disks", "Old-chat archives",
                      "free of", "Arm.java", "Fix the arm"):
            self.assertIn(label, text)

    def test_missing_files_are_not_listed(self):
        self.attach()
        for p in self.files.glob("*seed*"):
            p.unlink()
        self.assertEqual(self.store().items((files_admin.ATTACHMENT,)), [])

    def test_totals_count_each_kind(self):
        self.attach("a.txt")
        self.attach("b.txt")
        self.archive_zip()
        text = self.page().text
        self.assertRegex(text, r"Old chat files</a></td>\s*<td[^>]*>2</td>")
        self.assertRegex(text, r"Old-chat archives</a></td>\s*<td[^>]*>1</td>")

    def test_automatic_rules_are_shown_from_settings(self):
        self.s.settings.code_keep_days = 9
        text = self.page().text
        self.assertIn("9 days after the session ends", text)
        self.assertIn(f"{self.s.settings.sandbox_ttl_hours} hours", text)
        self.assertNotIn("Claude Code", text)

    def test_in_use_sessions_have_no_tick_box(self):
        busy = self.session("running")
        done = self.session("done")
        text = self.page().text
        self.assertIn("In use", text)
        self.assertNotIn(f'value="w-{busy}"', text)
        self.assertIn(f'value="w-{done}"', text)

    def test_hostile_session_folder_symlink_is_not_listed(self):
        sid = self.session(files=False)
        outside = self.data / "elsewhere"
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        (self.data / "code").mkdir(exist_ok=True)
        (self.data / "code" / str(sid)).symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.store().items((files_admin.WORKSPACE,)), [])


class SortFilterTest(FilesBase):
    def setUp(self):
        super().setUp()
        self.old = self.attach("old.txt", b"o" * 10, created=days_ago(200))
        self.mid = self.project_file("mid.txt", b"m" * 500)
        self.new = self.session(updated=days_ago(1))

    def names(self, query):
        text = self.page(path="/files?" + query).text
        return [n for n in ("old.txt", "mid.txt", "Fix the arm") if n in text.split('id="list-title"')[1]]

    def order(self, query):
        text = self.page(path="/files?" + query).text.split('id="list-title"')[1]
        found = sorted((text.index(n), n) for n in ("old.txt", "mid.txt", "Fix the arm") if n in text)
        return [n for _, n in found]

    def test_filter_by_kind(self):
        self.assertEqual(self.names("kind=workspace"), ["Fix the arm"])

    def test_filter_by_owner(self):
        self.assertEqual(self.names(f"owner={self.kim_id}"), ["mid.txt"])

    def test_filter_by_age(self):
        self.assertEqual(self.names("older=100"), ["old.txt", "mid.txt"])

    def test_sort_by_size_and_age(self):
        self.assertEqual(self.order("sort=size"), ["mid.txt", "Fix the arm", "old.txt"])
        self.assertEqual(self.order("sort=newest")[0], "Fix the arm")
        self.assertEqual(self.order("sort=age")[-1], "Fix the arm")

    def test_bad_filters_are_ignored(self):
        self.assertEqual(self.page(path="/files?kind=zzz&owner=x&older=-3&sort=evil").status_code, 200)


class ViewTest(FilesBase):
    def test_text_is_shown_as_plain_text_with_safe_headers(self):
        aid = self.attach("page.html", b"<script>alert(1)</script>", media="text/html")
        r = self.kim.get(f"/files/view/a-{aid}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertIn("nosniff", r.headers["x-content-type-options"])
        self.assertTrue(r.headers["content-disposition"].startswith("inline"))
        self.assertIn("sandbox", r.headers["content-security-policy"])

    def test_pictures_in_safe_formats_show_inline(self):
        aid = self.attach("a.png", b"\x89PNG", kind="image", media="image/png")
        r = self.kim.get(f"/files/view/a-{aid}")
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertTrue(r.headers["content-disposition"].startswith("inline"))

    def test_svg_and_other_types_are_downloads_never_inline(self):
        aid = self.attach("x.svg", b"<svg onload=alert(1)>", kind="image", media="image/svg+xml")
        r = self.kim.get(f"/files/view/a-{aid}")
        self.assertEqual(r.headers["content-type"], "application/octet-stream")
        self.assertTrue(r.headers["content-disposition"].startswith("attachment"))
        vid = self.attach("v.mp4", b"0000", kind="video", media="video/mp4")
        self.assertTrue(self.kim.get(f"/files/view/a-{vid}").headers["content-disposition"].startswith("attachment"))

    def test_project_files_can_be_viewed(self):
        pf = self.project_file()
        self.assertEqual(self.kim.get(f"/files/view/p-{pf}").text, "notes")

    def test_other_kinds_and_bad_ids_cannot_be_viewed(self):
        sid = self.session()
        for item in (f"w-{sid}", "z-old-chats-20250101-120000.zip", "../../etc/passwd", "a-0", "a-99", "a-1%2F..%2F"):
            self.assertEqual(self.kim.get(f"/files/view/{item}", headers=HTML).status_code, 404, item)


class DeleteTest(FilesBase):
    def test_delete_one_asks_first_and_names_count_and_size(self):
        aid = self.attach(data=b"a" * 2048)
        r = self.confirm(one=f"a-{aid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Delete 1 item?", r.text)
        self.assertIn("2.0 KB", r.text)
        self.assertEqual(len(list(self.files.glob("*seed*"))), 1)  # nothing deleted yet
        self.assertEqual(self.audits(), [])

    def test_delete_one(self):
        aid = self.attach()
        r = self.do(ids=f"a-{aid}", count="1")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(list(self.files.glob("*seed*")), [])
        audit = self.audits()
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]["kind"], audit[0]["count"], audit[0]["bytes"]), ("attachment", 1, 17))
        self.assertIn(f"a-{aid}", audit[0]["items"][0])
        row = self.db.one("SELECT actor_id FROM audit_log WHERE action = 'files_deleted'")
        self.assertEqual(row["actor_id"], self.kim_id)

    def test_delete_ticked_set_across_kinds_audits_each_kind(self):
        a = self.attach()
        z = self.archive_zip()
        sid = self.session()
        self.do(ids=[f"a-{a}", f"w-{sid}", f"z-{z.name}"], count="3")
        self.assertFalse(z.exists())
        self.assertFalse((self.data / "code" / str(sid)).exists())
        self.assertEqual(sorted(a["kind"] for a in self.audits()), ["archive", "attachment", "workspace"])

    def test_delete_older_than_n_days(self):
        old = self.attach("old.txt", created=days_ago(100))
        new = self.attach("new.txt", created=days_ago(2))
        r = self.confirm(mode="older", kind="attachment", days="30")
        self.assertIn("Delete 1 item?", r.text)
        self.do(mode="older", kind="attachment", days="30", count="1")
        names = sorted(p.name for p in self.files.glob("*seed*"))
        self.assertEqual(len(names), 1)
        self.assertIn("new.txt", names[0])
        self.assertEqual(self.audits()[0]["mode"], "older")
        self.assertIsNotNone(old and new)

    def test_older_than_needs_valid_inputs(self):
        self.attach()
        for form in ({"mode": "older", "kind": "nope", "days": "5"}, {"mode": "older", "kind": "attachment", "days": "0"},
                     {"mode": "older", "kind": "attachment", "days": "x"}):
            r = self.confirm(**form)
            self.assertEqual(r.status_code, 200)  # redirected back to the page
            self.assertIn("Files", r.text)
        self.assertEqual(len(list(self.files.glob("*seed*"))), 1)

    def test_nothing_ticked_deletes_nothing(self):
        self.attach()
        self.confirm()
        self.assertEqual(len(list(self.files.glob("*seed*"))), 1)

    def test_wrong_count_is_refused_and_nothing_deleted(self):
        aid = self.attach()
        r = self.do(ids=f"a-{aid}", count="5")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(list(self.files.glob("*seed*"))), 1)

    def test_more_than_twenty_needs_the_number_typed(self):
        ids = [f"a-{self.attach(f'f{i}.txt')}" for i in range(21)]
        page = self.confirm(ids=ids)
        self.assertIn("Delete 21 items?", page.text)
        self.assertIn("Type the number 21", page.text)
        self.assertEqual(self.do(ids=ids, count="21").status_code, 400)
        self.assertEqual(self.do(ids=ids, count="21", typed="20").status_code, 400)
        self.assertEqual(self.do(ids=ids, count="21", typed="twenty one").status_code, 400)
        self.assertEqual(len(list(self.files.glob("*seed*"))), 21)
        self.assertEqual(self.audits(), [])
        self.assertEqual(self.do(ids=ids, count="21", typed="21").status_code, 303)
        self.assertEqual(list(self.files.glob("*seed*")), [])
        audit = self.audits()[0]
        self.assertEqual(audit["count"], 21)
        self.assertLessEqual(len(audit["items"]), files_admin.AUDIT_NAMES)
        self.assertEqual(audit["more"], 1)

    def test_twenty_or_fewer_needs_no_typed_number(self):
        ids = [f"a-{self.attach(f'g{i}.txt')}" for i in range(20)]
        self.assertNotIn("Type the number", self.confirm(ids=ids).text)
        self.assertEqual(self.do(ids=ids, count="20").status_code, 303)

    def test_any_mentor_may_delete_and_admins_too(self):
        a = self.attach()
        self.assertEqual(self.do(self.admin, ids=f"a-{a}", count="1").status_code, 303)

    def test_deleting_an_attachment_keeps_the_old_chat_page(self):
        aid = self.attach("Arm.java")
        cid = self.db.one("SELECT c.id FROM conversations c JOIN messages m ON m.conversation_id = c.id "
                          "JOIN attachments a ON a.message_id = m.id WHERE a.id = ?", (aid,))["id"]
        self.do(ids=f"a-{aid}", count="1")
        r = self.kim.get(f"/c/{cid}", headers=HTML)
        self.assertEqual(r.status_code, 200)
        self.assertIn("file removed", r.text)
        self.assertEqual(self.kim.get(f"/files/{aid}").status_code, 404)

    def test_project_file_deleted_with_its_empty_project(self):
        pf = self.project_file()
        self.do(ids=f"p-{pf}", count="1")
        self.assertIsNone(self.db.one("SELECT id FROM project_files"))
        self.assertIsNone(self.db.one("SELECT id FROM projects"))

    def test_project_kept_when_a_chat_still_uses_it(self):
        pf = self.project_file()
        pid = self.db.one("SELECT project_id FROM project_files WHERE id = ?", (pf,))["project_id"]
        cid = self.chat()
        self.db.execute("UPDATE conversations SET project_id = ? WHERE id = ?", (pid, cid))
        self.do(ids=f"p-{pf}", count="1")
        self.assertIsNotNone(self.db.one("SELECT id FROM projects"))

    def test_finished_workspace_delete_keeps_the_session_page(self):
        sid = self.session("done")
        self.do(ids=f"w-{sid}", count="1")
        self.assertFalse((self.data / "code" / str(sid)).exists())
        r = self.kim.get(f"/code/{sid}", headers=HTML)
        self.assertEqual(r.status_code, 200)
        self.assertIn("deleted this session", r.text)
        # the patch lived in the workspace, so it is gone and the page says so plainly
        self.assertIn("downloaded any more", r.text)
        self.assertEqual(self.kim.get(f"/code/{sid}/changes.patch", headers=HTML).status_code, 404)

    def test_sandbox_disk_is_deleted_through_the_manager(self):
        sid = self.session("done", files=False)
        sb = self.sandbox(sid)
        home = self.data / "sandbox-fake" / self.db.one("SELECT name FROM sandbox_sessions WHERE id = ?", (sb,))["name"]
        self.do(ids=f"s-{sb}", count="1")
        self.assertFalse(home.exists())
        self.assertEqual(self.db.one("SELECT state FROM sandbox_sessions WHERE id = ?", (sb,))["state"], "destroyed")
        self.assertEqual(self.audits()[0]["kind"], "sandbox")

    def test_csrf_is_required(self):
        a = self.attach()
        r = self.kim.post("/files/delete", {"one": f"a-{a}"}, csrf=False)
        self.assertEqual(r.status_code, 403)
        r = self.kim.post("/files/delete/confirm", {"ids": f"a-{a}", "count": "1"}, csrf=False)
        self.assertEqual(r.status_code, 403)
        r = self.kim.post("/files/delete/confirm", {"ids": f"a-{a}", "count": "1", "csrf": "wrong"}, csrf=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(len(list(self.files.glob("*seed*"))), 1)

    def test_get_cannot_delete(self):
        self.assertEqual(self.kim.get("/files/delete/confirm?ids=a-1").status_code, 405)


class ProtectionTest(FilesBase):
    def test_in_use_sessions_are_refused_even_if_posted(self):
        sessions = {s: self.session(s) for s in ("awaiting_approval", "preparing", "running", "waiting")}
        boxes = {s: self.sandbox(sid, "running", data=b"y" * 10) for s, sid in sessions.items()}
        for sid in sessions.values():
            self.assertEqual(self.confirm(ids=f"w-{sid}").status_code, 200)  # redirected back with a note
            self.do(ids=f"w-{sid}", count="1")
        for sb in boxes.values():
            self.do(ids=f"s-{sb}", count="1")
        for status, sid in sessions.items():
            self.assertTrue((self.data / "code" / str(sid) / "work").is_dir(), status)
        self.assertEqual(self.db.all("SELECT 1 FROM sandbox_sessions WHERE state != 'running'"), [])
        self.assertEqual(self.audits(), [])

    def test_in_use_is_checked_again_at_delete_time(self):
        sid = self.session("done")
        items = self.store().find_all([f"w-{sid}"])
        self.assertFalse(items[0].in_use)
        self.db.execute("UPDATE code_sessions SET status = 'running' WHERE id = ?", (sid,))  # started meanwhile
        outcome = self.store().delete(items, actor_id=self.kim_id, mode="one")
        self.assertEqual((len(outcome.deleted), outcome.in_use), (0, 1))
        self.assertTrue((self.data / "code" / str(sid)).is_dir())

    def test_busy_session_is_protected_even_when_its_status_looks_finished(self):
        sid = self.session("done")
        self.s.code._busy.add(sid)
        self.do(ids=f"w-{sid}", count="1")
        self.assertTrue((self.data / "code" / str(sid)).is_dir())

    def test_older_than_skips_sessions_in_use(self):
        busy = self.session("running", updated=OLD)
        done = self.session("done", updated=OLD)
        self.do(mode="older", kind="workspace", days="30", count="1")
        self.assertTrue((self.data / "code" / str(busy)).is_dir())
        self.assertFalse((self.data / "code" / str(done)).exists())

    def test_a_sandbox_still_being_made_is_protected(self):
        sb = self.sandbox(None, "creating")
        self.do(ids=f"s-{sb}", count="1")
        self.assertEqual(self.db.one("SELECT state FROM sandbox_sessions WHERE id = ?", (sb,))["state"], "creating")


class PathEscapeTest(FilesBase):
    def test_ids_that_are_not_ours_are_ignored(self):
        outside = self.data / "secret.txt"
        outside.write_text("keep")
        bad = ["../secret.txt", "a-../../secret.txt", "z-../secret.txt", "z-old-chats-20250101-120000.zip/../../x",
               "w-1/../../..", "/etc/passwd", "a-0", "a--1", "s-1e3", str(outside), "z-old-chats-1.zip"]
        for item in bad:
            self.assertFalse(files_admin.valid_id(item), item)
            self.do(ids=item, count="1")
            self.confirm(one=item)
        self.assertTrue(outside.exists())
        self.assertEqual(self.audits(), [])

    def test_attachment_row_pointing_outside_files_is_not_deleted(self):
        outside = self.data / "secret.txt"
        outside.write_text("keep")
        self.chat()
        aid = self.db.execute("INSERT INTO attachments (message_id, name, kind, media_type, size, path, created_at) "
                              "VALUES (?, 'x', 'text', 'text/plain', 4, '../secret.txt', ?)", (self.mid, OLD)).lastrowid
        self.assertEqual(self.store().items((files_admin.ATTACHMENT,)), [])
        self.do(ids=f"a-{aid}", count="1")
        self.assertTrue(outside.exists())
        self.assertEqual(self.kim.get(f"/files/view/a-{aid}").status_code, 404)

    def test_attachment_symlink_to_outside_is_not_followed(self):
        outside = self.data / "secret.txt"
        outside.write_text("keep")
        self.chat()
        (self.files / "link.txt").symlink_to(outside)
        aid = self.db.execute("INSERT INTO attachments (message_id, name, kind, media_type, size, path, created_at) "
                              "VALUES (?, 'x', 'text', 'text/plain', 4, 'link.txt', ?)", (self.mid, OLD)).lastrowid
        self.do(ids=f"a-{aid}", count="1")
        self.assertTrue(outside.exists())

    def test_only_archive_names_we_make_can_be_deleted(self):
        other = self.h.settings.backups_path / "notes.zip"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"x")
        backup = self.h.settings.backups_path / "subzero-backup-20250101.zip"
        backup.write_bytes(b"x")
        self.do(ids=["z-notes.zip", "z-subzero-backup-20250101.zip"], count="1")
        self.assertTrue(other.exists() and backup.exists())

    def test_the_base_image_is_never_listed_or_deleted(self):
        base = self.data / "base.vhdx"
        base.write_bytes(b"base")
        self.s.settings.sandbox_base_image = str(base)
        sb = self.db.execute("INSERT INTO sandbox_sessions (code_session_id, backend, name, state, created_at, "
                             "expires_at) VALUES (NULL, 'fake', 'base.vhdx', 'running', ?, ?)",
                             (utcnow(), utcnow())).lastrowid  # a row that doesn't carry our prefix
        self.assertEqual(self.store().items((files_admin.SANDBOX,)), [])
        self.do(ids=f"s-{sb}", count="1")
        self.assertTrue(base.exists())

    def test_workspace_folder_that_is_a_link_is_not_removed(self):
        sid = self.session(files=False)
        outside = self.data / "elsewhere"
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        (self.data / "code").mkdir(exist_ok=True)
        (self.data / "code" / str(sid)).symlink_to(outside, target_is_directory=True)
        self.do(ids=f"w-{sid}", count="1")
        self.assertTrue((outside / "keep.txt").exists())


if __name__ == "__main__":
    unittest.main()
