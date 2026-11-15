"""Old website chats: the chats people had on the website before chat moved to Slack.

They are the conversations with no row in `slack_threads`. An admin can save them all in one zip file (readable web
pages, the same data as JSON, the attached files, the old projects and share links), and can then delete them. Both
jobs run in the background, because a long request would be cut by the Cloudflare Tunnel, and report their progress
through the `kv` table so the Settings page can show it. Nothing else in the app starts either job.

Slack DM chats are never selected, never archived and never deleted.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import __version__, slack_cards
from .backup import VIDEO_SUFFIXES, make_backup
from .config import Settings
from .db import Database, utcnow

log = logging.getLogger("subzero.archive")

NAME_RE = re.compile(r"old-chats-(\d{8})-(\d{6})\.zip")
JOB_KEY = "old_chats_job"
CONFIRM_WORD = "DELETE"
# A chat this new may be a Slack chat whose thread link isn't saved yet, so it is never counted as an old one
MIN_AGE_MINUTES = 10
# Set when this copy of the app starts, so a job left "running" by an earlier run is known to be dead
_BOOT = secrets.token_hex(4)

Progress = Callable[[int, int, str], None]


# --------------------------------------------------------------------------- what counts as old
def _select_old(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    """Make two temporary tables on this connection: _old_chats (conversations with no Slack thread, at least
    MIN_AGE_MINUTES old) and _old_projects (projects that no remaining chat uses)."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(minutes=MIN_AGE_MINUTES)).isoformat(timespec="seconds")
    conn.execute("DROP TABLE IF EXISTS temp._old_chats")
    conn.execute("DROP TABLE IF EXISTS temp._old_projects")
    conn.execute("CREATE TEMP TABLE _old_chats AS SELECT c.id AS id FROM conversations c "
                 "LEFT JOIN slack_threads t ON t.conversation_id = c.id "
                 "WHERE t.conversation_id IS NULL AND c.created_at <= ?", (cutoff,))
    conn.execute("CREATE TEMP TABLE _old_projects AS SELECT p.id AS id FROM projects p WHERE NOT EXISTS ("
                 "SELECT 1 FROM conversations c WHERE c.project_id = p.id AND c.id NOT IN (SELECT id FROM _old_chats))")


@dataclass
class Summary:
    chats: int = 0
    messages: int = 0
    files: int = 0
    file_bytes: int = 0
    videos: int = 0
    video_bytes: int = 0
    projects: int = 0
    project_files: int = 0
    project_bytes: int = 0
    shares: int = 0
    oldest: str = ""
    newest: str = ""


def summarize(db: Database, *, now: datetime | None = None) -> Summary:
    """How many old website chats there are, how big their files are, and when they were written."""
    with db.connect() as conn:
        _select_old(conn, now)

        def one(sql: str) -> sqlite3.Row:
            return conn.execute(sql).fetchone()

        chats = one("SELECT COUNT(*) AS n, MIN(created_at) AS oldest, MAX(updated_at) AS newest FROM conversations "
                    "WHERE id IN (SELECT id FROM _old_chats)")
        files = one("SELECT COUNT(*) AS n, COALESCE(SUM(a.size), 0) AS bytes, "
                    "COALESCE(SUM(CASE WHEN a.kind = 'video' THEN 1 ELSE 0 END), 0) AS videos, "
                    "COALESCE(SUM(CASE WHEN a.kind = 'video' THEN a.size ELSE 0 END), 0) AS video_bytes "
                    "FROM attachments a JOIN messages m ON m.id = a.message_id "
                    "WHERE m.conversation_id IN (SELECT id FROM _old_chats)")
        project = one("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM project_files "
                      "WHERE project_id IN (SELECT id FROM _old_projects)")
        return Summary(
            chats=chats["n"], oldest=chats["oldest"] or "", newest=chats["newest"] or "",
            messages=one("SELECT COUNT(*) AS n FROM messages "
                         "WHERE conversation_id IN (SELECT id FROM _old_chats)")["n"],
            files=files["n"], file_bytes=files["bytes"], videos=files["videos"], video_bytes=files["video_bytes"],
            projects=one("SELECT COUNT(*) AS n FROM _old_projects")["n"], project_files=project["n"],
            project_bytes=project["bytes"],
            shares=one("SELECT COUNT(*) AS n FROM shares WHERE conversation_id IN (SELECT id FROM _old_chats)")["n"])


# --------------------------------------------------------------------------- archives on disk
@dataclass
class ArchiveInfo:
    path: Path
    created: datetime  # local time on this PC
    size: int

    @property
    def name(self) -> str:
        return self.path.name


def list_archives(settings: Settings) -> list[ArchiveInfo]:
    """Old-chat archives in the backups folder, newest first."""
    folder = settings.backups_path
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob("old-chats-*.zip"):
        match = NAME_RE.fullmatch(path.name)
        if match and path.is_file():
            created = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
            found.append(ArchiveInfo(path, created, path.stat().st_size))
    return sorted(found, key=lambda a: a.created, reverse=True)


def archive_path(settings: Settings, name: str) -> Path | None:
    """The archive with this exact file name, or None. Only names made by build_archive are accepted, and the file
    must sit directly in the backups folder, so no other name or path can reach anything else."""
    if not NAME_RE.fullmatch(name):
        return None
    folder = settings.backups_path.resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        return None
    return path


# --------------------------------------------------------------------------- reading the chats
def _json_or_text(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"
    return cleaned[-80:]


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso


def _decisions(conn: sqlite3.Connection, message_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """The audit log's entries about these messages (approved, rejected, edited, ...), with who and why."""
    found: dict[int, list[dict[str, Any]]] = {}
    for start in range(0, len(message_ids), 500):
        chunk = message_ids[start:start + 500]
        rows = conn.execute(
            "SELECT a.target_id, a.action, a.details, a.created_at, u.name AS actor FROM audit_log a "
            "LEFT JOIN users u ON u.id = a.actor_id WHERE a.target_type = 'message' AND a.action LIKE 'message_%' "
            f"AND a.target_id IN ({','.join('?' * len(chunk))}) ORDER BY a.id", chunk).fetchall()
        for row in rows:
            found.setdefault(row["target_id"], []).append({
                "action": row["action"], "by": row["actor"], "at": row["created_at"],
                "details": _json_or_text(row["details"])})
    return found


def load_chat(conn: sqlite3.Connection, chat_id: int) -> dict[str, Any]:
    """One chat with all its messages, as plain data (this is what chats/<id>.json holds)."""
    conv = conn.execute(
        "SELECT c.*, o.name AS owner_name, o.role AS owner_role FROM conversations c "
        "LEFT JOIN users o ON o.id = c.owner_id WHERE c.id = ?", (chat_id,)).fetchone()
    rows = conn.execute(
        "SELECT m.*, a.name AS author_name, a.role AS author_role, r.name AS reviewer_name FROM messages m "
        "LEFT JOIN users a ON a.id = m.author_id LEFT JOIN users r ON r.id = m.reviewed_by "
        "WHERE m.conversation_id = ? ORDER BY m.id", (chat_id,)).fetchall()
    files: dict[int, list[dict[str, Any]]] = {}
    for f in conn.execute("SELECT a.* FROM attachments a JOIN messages m ON m.id = a.message_id "
                          "WHERE m.conversation_id = ? ORDER BY a.id", (chat_id,)):
        files.setdefault(f["message_id"], []).append({
            "id": f["id"], "name": f["name"], "kind": f["kind"], "media_type": f["media_type"], "size": f["size"],
            "created_at": f["created_at"], "stored_path": f["path"]})
    decisions = _decisions(conn, [r["id"] for r in rows])
    messages = []
    for m in rows:
        review = None
        if m["reviewed_by"] or m["reviewed_at"] or m["review_note"]:
            review = {"by": m["reviewer_name"], "at": m["reviewed_at"], "reason": m["review_note"]}
        messages.append({
            "id": m["id"], "role": m["role"], "author": m["author_name"], "author_role": m["author_role"],
            "status": m["status"], "text": m["content"],
            "original_text": m["original_content"],  # the student's own words, when a mentor edited the message
            "review": review, "decisions": decisions.get(m["id"], []), "screener_note": m["screen_note"],
            "model": m["model"],
            "tokens": {"input": m["input_tokens"], "output": m["output_tokens"], "cache_read": m["cache_read_tokens"]},
            "web_searches": m["web_searches"], "sources": _json_or_text(m["sources"]),
            "options": _json_or_text(m["options"]), "error": m["error"], "reply_to": m["reply_to"],
            "created_at": m["created_at"], "updated_at": m["updated_at"], "files": files.get(m["id"], [])})
    return {"id": conv["id"], "title": conv["title"], "owner": conv["owner_name"], "owner_role": conv["owner_role"],
            "visibility": conv["visibility"], "model": conv["model"], "subteam": conv["subteam"],
            "project_id": conv["project_id"], "created_at": conv["created_at"], "updated_at": conv["updated_at"],
            "messages": messages}


# --------------------------------------------------------------------------- the readable pages
STYLE = ("body{font:16px/1.5 system-ui,sans-serif;max-width:48rem;margin:2rem auto;padding:0 1rem;color:#142231}"
         "h1{font-size:1.5rem}.meta{color:#4a5b6a;font-size:.9rem}.msg{border:1px solid #d2dde5;border-radius:8px;"
         "padding:.6rem .9rem;margin:1rem 0}.msg.assistant{background:#eef3f6}.text{white-space:pre-wrap;"
         "overflow-wrap:anywhere}.note{color:#4a5b6a;font-size:.85rem;margin:.3rem 0 0}table{border-collapse:collapse;"
         "width:100%}th,td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #d2dde5;font-size:.92rem}")


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; "
            "img-src 'self'\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>{html.escape(title)}"
            f"</title><style>{STYLE}</style></head><body>{body}</body></html>")


def chat_html(chat: dict[str, Any], file_names: dict[int, str | None]) -> str:
    """A chat as one self-contained page: everything is escaped and there are no scripts."""
    e = html.escape
    out = [f"<p><a href=\"../index.html\">All old chats</a></p><h1>{e(chat['title'] or 'Untitled chat')}</h1>",
           f"<p class=\"meta\">Chat {chat['id']} · started by {e(chat['owner'] or 'unknown')} · "
           f"{e(_when(chat['created_at']))} to {e(_when(chat['updated_at']))} · model {e(chat['model'] or '')}</p>"]
    for m in chat["messages"]:
        who = m["author"] or ("Claude" if m["role"] == "assistant" else "Unknown")
        out.append(f"<div class=\"msg {e(m['role'])}\"><p class=\"meta\"><strong>{e(who)}</strong> · "
                   f"{'Claude' if m['role'] == 'assistant' else e(m['author_role'] or 'student')} · "
                   f"status: {e(m['status'])} · {e(_when(m['created_at']))}</p>"
                   f"<div class=\"text\">{e(m['text'] or '')}</div>")
        if m["original_text"]:
            out.append(f"<p class=\"note\">Edited by a mentor. The original was:</p>"
                       f"<div class=\"text note\">{e(m['original_text'])}</div>")
        if m["review"]:
            r = m["review"]
            line = f"Review: {e(r['by'] or 'a mentor')}, {e(_when(r['at']))}"
            if r["reason"]:
                line += f". Reason: {e(r['reason'])}"
            out.append(f"<p class=\"note\">{line}</p>")
        if m["screener_note"]:
            out.append(f"<p class=\"note\">Screener note: {e(m['screener_note'])}</p>")
        for f in m["files"]:
            stored = file_names.get(f["id"])
            label = f"{e(f['name'])} ({f['size']:,} bytes)"
            if stored:
                href = e(f"../files/{chat['id']}/{quote(stored)}")
                out.append(f"<p class=\"note\">File: <a href=\"{href}\">{label}</a></p>")
            else:
                out.append(f"<p class=\"note\">File: {label} (not included in this archive)</p>")
        tokens = m["tokens"]
        if any(v is not None for v in tokens.values()):
            out.append(f"<p class=\"note\">Tokens: {tokens['input'] or 0} in, {tokens['output'] or 0} out, "
                       f"{tokens['cache_read'] or 0} from cache</p>")
        if m["error"]:
            out.append(f"<p class=\"note\">Error: {e(m['error'])}</p>")
        out.append("</div>")
    return _page(chat["title"] or f"Chat {chat['id']}", "".join(out))


def index_html(chats: list[dict[str, Any]], created: str) -> str:
    e = html.escape
    rows = "".join(
        f"<tr><td><a href=\"chats/{c['id']}.html\">{c['id']}</a></td><td>{e(c['owner'] or '')}</td>"
        f"<td>{e(c['title'] or 'Untitled chat')}</td><td>{e(_when(c['created_at']))}</td>"
        f"<td>{e(_when(c['updated_at']))}</td><td>{len(c['messages'])}</td></tr>" for c in chats)
    body = (f"<h1>Old website chats</h1><p class=\"meta\">{len(chats)} chats. Saved {e(created)} by SubZero AI "
            f"{e(__version__)}. Projects are in the projects folder and share links are in shares.json.</p>"
            "<table><thead><tr><th>Chat</th><th>Owner</th><th>Title</th><th>Started</th><th>Last message</th>"
            f"<th>Messages</th></tr></thead><tbody>{rows}</tbody></table>")
    return _page("Old website chats", body)


# --------------------------------------------------------------------------- building the archive
def _safe_file(files_dir: Path, relative: str) -> Path | None:
    """The stored file, if it exists and sits inside the files folder."""
    root = files_dir.resolve()
    path = (files_dir / relative).resolve()
    return path if root in path.parents and path.is_file() else None


def _add_file(zf: zipfile.ZipFile, path: Path, arcname: str) -> None:
    kind = zipfile.ZIP_STORED if path.suffix.lower() in VIDEO_SUFFIXES else zipfile.ZIP_DEFLATED
    zf.write(path, arcname, compress_type=kind)


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def build_archive(settings: Settings, db: Database, *, include_videos: bool = False,
                  progress: Progress | None = None, now: datetime | None = None) -> tuple[Path, dict[str, Any]]:
    """Write the zip of all old website chats into the backups folder. Returns (path, what it holds).
    Raises OSError or sqlite3.Error if it can't be written; no half-made file is left behind."""
    progress = progress or (lambda done, total, text: None)
    files_dir = settings.data_dir / "files"
    dest = settings.backups_path
    dest.mkdir(parents=True, exist_ok=True)
    for stale in dest.glob("old-chats-*.zip.partial"):  # left by a build that was cut off
        stale.unlink(missing_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    final = dest / f"old-chats-{stamp}.zip"
    partial = dest / (final.name + ".partial")
    stats = {"chats": 0, "messages": 0, "files": 0, "files_missing": 0, "videos_included": 0, "videos_left_out": 0,
             "projects": 0, "shares": 0}
    with db.connect() as conn:
        _select_old(conn, now)
        chat_ids = [r["id"] for r in conn.execute("SELECT id FROM _old_chats ORDER BY id")]
        project_ids = [r["id"] for r in conn.execute("SELECT id FROM _old_projects ORDER BY id")]
        total = len(chat_ids) + len(project_ids) + 2
        done = 0
        try:
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                chats = []
                for chat_id in chat_ids:
                    chat = load_chat(conn, chat_id)
                    names: dict[int, str | None] = {}
                    for message in chat["messages"]:
                        for f in message["files"]:
                            names[f["id"]] = None
                            f["archive_path"] = None
                            if f["kind"] == "video" and not include_videos:
                                stats["videos_left_out"] += 1
                                continue
                            path = _safe_file(files_dir, f["stored_path"])
                            if path is None:
                                stats["files_missing"] += 1
                                continue
                            stored = f"{f['id']}-{_safe_name(f['name'])}"
                            _add_file(zf, path, f"files/{chat_id}/{stored}")
                            names[f["id"]] = stored
                            f["archive_path"] = f"files/{chat_id}/{stored}"
                            stats["files"] += 1
                            stats["videos_included"] += f["kind"] == "video"
                    for message in chat["messages"]:
                        for f in message["files"]:
                            f.pop("stored_path", None)  # where it was on the server isn't useful to a reader
                    zf.writestr(f"chats/{chat_id}.json", _dump(chat))
                    zf.writestr(f"chats/{chat_id}.html", chat_html(chat, names))
                    chats.append(chat)
                    stats["messages"] += len(chat["messages"])
                    done += 1
                    progress(done, total, f"Saving chat {done} of {len(chat_ids)}")
                stats["chats"] = len(chats)
                for project_id in project_ids:
                    _add_project(zf, conn, files_dir, project_id)
                    stats["projects"] += 1
                    done += 1
                    progress(done, total, f"Saving project {stats['projects']} of {len(project_ids)}")
                shares = _share_records(conn)
                stats["shares"] = len(shares)
                zf.writestr("shares.json", _dump(shares))
                created = utcnow()
                zf.writestr("index.html", index_html(chats, _when(created)))
                zf.writestr("archive.json", _dump({"app_version": __version__, "created_at": created, **stats,
                                                   "videos_requested": include_videos}))
                progress(total, total, "Finishing the zip file")
            os.replace(partial, final)
        finally:
            partial.unlink(missing_ok=True)
    return final, stats


def _add_project(zf: zipfile.ZipFile, conn: sqlite3.Connection, files_dir: Path, project_id: int) -> None:
    project = conn.execute("SELECT p.*, u.name AS creator FROM projects p LEFT JOIN users u ON u.id = p.created_by "
                           "WHERE p.id = ?", (project_id,)).fetchone()
    files = []
    for f in conn.execute("SELECT * FROM project_files WHERE project_id = ? ORDER BY id", (project_id,)):
        entry = {"id": f["id"], "name": f["name"], "kind": f["kind"], "media_type": f["media_type"],
                 "size": f["size"], "created_at": f["created_at"], "archive_path": None}
        path = _safe_file(files_dir, f["path"])
        if path is not None:
            entry["archive_path"] = f"projects/{project_id}/files/{f['id']}-{_safe_name(f['name'])}"
            _add_file(zf, path, entry["archive_path"])
        files.append(entry)
    zf.writestr(f"projects/{project_id}/project.json", _dump({
        "id": project["id"], "name": project["name"], "description": project["description"],
        "instructions": project["instructions"], "created_by": project["creator"],
        "archived": bool(project["archived"]), "created_at": project["created_at"],
        "updated_at": project["updated_at"], "files": files}))
    zf.writestr(f"projects/{project_id}/instructions.txt", project["instructions"] or "")


def _share_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The old share-link records. The secret part of each link is left out: those links stopped working in 0.11."""
    rows = conn.execute(
        "SELECT s.*, r.name AS requester, v.name AS reviewer FROM shares s LEFT JOIN users r ON r.id = s.requested_by "
        "LEFT JOIN users v ON v.id = s.reviewed_by WHERE s.conversation_id IN (SELECT id FROM _old_chats) "
        "ORDER BY s.id").fetchall()
    return [{"id": s["id"], "chat": s["conversation_id"], "audience": s["audience"], "status": s["status"],
             "up_to_message": s["upto_message_id"], "requested_by": s["requester"], "reviewed_by": s["reviewer"],
             "views": s["views"], "created_at": s["created_at"], "reviewed_at": s["reviewed_at"],
             "expires_at": s["expires_at"]} for s in rows]


# --------------------------------------------------------------------------- deleting
def _remove_files(files_dir: Path, rows: list[tuple[str, int]]) -> tuple[int, int]:
    """Delete stored files (paths relative to the files folder, never anything outside it). Returns (files removed,
    bytes freed)."""
    root = files_dir.resolve()
    removed = freed = 0
    for relative, _size in rows:
        path = (files_dir / relative).resolve()
        if root in path.parents and path.is_file():
            try:
                freed += path.stat().st_size
                path.unlink()
                removed += 1
            except OSError:
                log.warning("Couldn't delete the old file %s", path.name, exc_info=True)
    return removed, freed


def delete_old_chats_now(settings: Settings, db: Database, *, actor_id: int | None, backup: str = "",
                         progress: Progress | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Delete every old website chat with its messages, attachment rows and files, and the files of old projects.
    The `projects` and `shares` tables stay (their rows for deleted chats go with the chats). Returns the counts and
    the Slack review cards that were still open, so the caller can mark them. A second run finds nothing."""
    progress = progress or (lambda done, total, text: None)
    files_dir = settings.data_dir / "files"
    with db.transaction() as conn:
        _select_old(conn, now)
        chats = conn.execute("SELECT COUNT(*) AS n FROM _old_chats").fetchone()["n"]
        project_files = conn.execute("SELECT path, size FROM project_files WHERE project_id IN "
                                     "(SELECT id FROM _old_projects)").fetchall()
        if not chats and not project_files:
            return {"chats": 0, "messages": 0, "files": 0, "bytes": 0, "project_files": 0, "shares": 0, "cards": []}
        messages = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id IN "
                                "(SELECT id FROM _old_chats)").fetchone()["n"]
        shares = conn.execute("SELECT COUNT(*) AS n FROM shares WHERE conversation_id IN "
                              "(SELECT id FROM _old_chats)").fetchone()["n"]
        attachments = conn.execute(
            "SELECT a.path, a.size FROM attachments a JOIN messages m ON m.id = a.message_id "
            "WHERE m.conversation_id IN (SELECT id FROM _old_chats)").fetchall()
        cards = [dict(r) for r in conn.execute(
            "SELECT sc.channel, sc.ts, c.title, u.name AS student FROM slack_cards sc "
            "JOIN messages m ON m.id = sc.message_id JOIN conversations c ON c.id = m.conversation_id "
            "LEFT JOIN users u ON u.id = m.author_id "
            "WHERE m.status = 'pending' AND c.id IN (SELECT id FROM _old_chats)")]
        conn.execute("DELETE FROM conversations WHERE id IN (SELECT id FROM _old_chats)")  # messages, files rows,
        conn.execute("DELETE FROM project_files WHERE project_id IN (SELECT id FROM _old_projects)")  # cards, shares
        total_bytes = sum(r["size"] for r in attachments) + sum(r["size"] for r in project_files)
        details = {"chats": chats, "messages": messages, "files": len(attachments), "project_files": len(project_files),
                   "shares": shares, "bytes": total_bytes, "open_cards": len(cards), "backup": backup}
        db.audit("old_chats_deleted_by_admin", actor_id=actor_id, details=details, conn=conn)
    progress(0, 1, "Deleting the files")
    _remove_files(files_dir, [(r["path"], r["size"]) for r in attachments + project_files])
    return {**details, "cards": cards}


# --------------------------------------------------------------------------- background jobs
def job_state(db: Database) -> dict[str, Any] | None:
    """The latest archive or delete job, for the Settings page. A job left running by an earlier run of the app
    (it was restarted in the middle) is reported as stopped."""
    raw = db.get_kv(JOB_KEY)
    if not raw:
        return None
    try:
        job = json.loads(raw)
    except ValueError:
        return None
    if job.get("state") == "running" and job.get("boot") != _BOOT:
        job.update(state="failed", message="The app restarted while this was running, so it stopped. Nothing was "
                                           "deleted unless the page said so. You can start it again.")
    return job


def job_running(db: Database) -> bool:
    job = job_state(db)
    return bool(job and job.get("state") == "running")


def _save_job(db: Database, job: dict[str, Any]) -> None:
    db.set_kv(JOB_KEY, json.dumps(job))


def start_job(db: Database, kind: str, by: str) -> dict[str, Any] | None:
    """Mark a new job as running. Returns None when one is already running (only one at a time)."""
    if job_running(db):
        return None
    job = {"kind": kind, "state": "running", "step": "Starting", "done": 0, "total": 1, "boot": _BOOT,
           "started_at": utcnow(), "finished_at": "", "by": by, "message": "", "result": {}}
    _save_job(db, job)
    return job


def _progress_for(db: Database, job: dict[str, Any]) -> Progress:
    def report(done: int, total: int, text: str) -> None:
        job.update(done=done, total=max(total, 1), step=text)
        _save_job(db, job)
    return report


def _finish(db: Database, job: dict[str, Any], state: str, message: str, result: dict[str, Any] | None = None) -> None:
    job.update(state=state, message=message, result=result or {}, finished_at=utcnow(), done=job["total"])
    _save_job(db, job)


async def run_archive_job(settings: Settings, db: Database, job: dict[str, Any], *, actor_id: int,
                          include_videos: bool) -> None:
    """Build the archive in a thread and record the result. Never raises."""
    try:
        summary = await asyncio.to_thread(summarize, db)
        need = summary.file_bytes + summary.project_bytes + (0 if include_videos else -summary.video_bytes)
        settings.backups_path.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(settings.backups_path).free
        if free < max(need, 0) + 50 * 1024 * 1024:
            _finish(db, job, "failed", "There isn't enough free disk space in the backups folder for the archive.")
            return
        path, stats = await asyncio.to_thread(build_archive, settings, db, include_videos=include_videos,
                                              progress=_progress_for(db, job))
        size = path.stat().st_size
        db.audit("old_chats_archived", actor_id=actor_id, details={"file": path.name, "bytes": size, **stats})
        _finish(db, job, "done", f"Saved {path.name}.", {"file": path.name, "bytes": size, **stats})
    except Exception as exc:
        log.exception("Couldn't build the archive of old chats")
        db.audit("old_chats_archive_failed", actor_id=actor_id, details={"error": type(exc).__name__})
        _finish(db, job, "failed", "The archive couldn't be made. Check the backups folder, then try again.")


async def run_delete_job(settings: Settings, db: Database, slack: Any, job: dict[str, Any], *, actor_id: int,
                         admin_slack_id: str | None) -> None:
    """Back up, delete the old chats, then mark their open Slack cards. Never raises. If the backup fails nothing is
    deleted."""
    try:
        summary = await asyncio.to_thread(summarize, db)
        if not summary.chats and not summary.project_files:
            _finish(db, job, "done", "There were no old website chats to delete.", {"chats": 0})
            return
        report = _progress_for(db, job)
        report(0, 3, "Making a backup first")
        try:
            backup = (await asyncio.to_thread(make_backup, settings, label="before-old-chats-delete")).name
        except Exception as exc:
            log.exception("The backup before deleting old chats failed")
            db.audit("old_chats_delete_stopped", actor_id=actor_id, details={"reason": "backup failed",
                                                                            "error": str(exc)[:200]})
            _finish(db, job, "failed", "The backup failed, so nothing was deleted. Fix the backups folder, then try "
                                       "again.")
            return
        report(1, 3, "Deleting the chats and their files")
        result = await asyncio.to_thread(delete_old_chats_now, settings, db, actor_id=actor_id, backup=backup,
                                         progress=report)
        report(2, 3, "Marking the review cards in Slack")
        marked = await _mark_cards(slack, result.pop("cards"), admin_slack_id)
        result["cards_marked"] = marked
        _finish(db, job, "done", f"Deleted {result['chats']} old website chats. The backup {backup} was made first.",
                result)
    except Exception:
        log.exception("Couldn't delete the old website chats")
        _finish(db, job, "failed", "Deleting stopped because of an error. Run it again to finish; chats already "
                                   "deleted stay deleted.")


async def _mark_cards(slack: Any, cards: list[dict[str, Any]], admin_slack_id: str | None) -> int:
    """Replace each still-open review card with 'deleted by an admin'. A card that can't be updated is skipped."""
    if slack is None:
        return 0
    marked = 0
    deleted_at = utcnow()
    for card in cards:
        blocks = slack_cards.deleted_card(student_name=card["student"] or "A student", title=card["title"],
                                          admin_slack_id=admin_slack_id, deleted_at=deleted_at)
        try:
            await slack.update_message(card["channel"], card["ts"],
                                       slack_cards.card_fallback_text(card["student"] or "A student", "deleted"),
                                       blocks)
            marked += 1
        except Exception:
            log.warning("Couldn't mark a Slack review card as deleted", exc_info=True)
    return marked
