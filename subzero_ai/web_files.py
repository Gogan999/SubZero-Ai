"""The Files page for mentors and admins: what is stored on the mini PC, and deleting it after a confirmation."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.routing import Route

from .attachments import size_label
from .files_admin import (
    ARCHIVE, ATTACHMENT, BULK_TYPED_OVER, HINTS, KINDS, LABELS, PROJECT, SAFE_IMAGES, WORKSPACE, FileStore,
    Item, disposition, valid_id,
)
from .store import Viewer
from .web_common import error_page, page, render, services

SHOWN = 300  # rows listed at once; filter to narrow down
SORTS = {
    "age": ("Oldest first", lambda i: -i.age_days()),
    "newest": ("Newest first", lambda i: i.age_days()),
    "size": ("Biggest first", lambda i: -i.size),
    "kind": ("Kind", lambda i: (KINDS.index(i.kind), i.name.lower())),
    "owner": ("Owner", lambda i: (i.owner.lower() or "￿", i.name.lower())),
    "name": ("Name", lambda i: i.name.lower()),
}
MAX_DAYS = 3650
TOKEN_SECONDS = 600  # how long a confirmation page stays valid


def store_for(request: Request) -> FileStore:
    s = services(request)
    return FileStore(s.settings, s.db, s.code, s.sandboxes)


def age_text(item: Item) -> str:
    days = item.age_days()
    if days < 1:
        return "today"
    if days < 60:
        return f"{int(days)} day{'s' if int(days) != 1 else ''}"
    return f"{int(days // 30)} months"


def size_text(size: int) -> str:
    return size_label(size) if size < 1024 * 1024 * 1024 else f"{size / 1024 ** 3:.1f} GB"


def item_size(item: Item) -> str:
    return size_text(item.size) if item.size_known else "unknown"


def row_view(item: Item) -> dict[str, Any]:
    return {"id": item.id, "kind": item.kind, "kind_label": LABELS[item.kind], "name": item.name,
            "owner": item.owner or "Unknown", "size": item_size(item), "age": age_text(item),
            "detail": item.detail, "link": item.link, "view": item.view, "in_use": item.in_use,
            "view_url": f"/files/view/{item.id}" if item.view else ""}


def _days(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() and 0 < int(text) <= MAX_DAYS else None


def flash_text(request: Request) -> str:
    return request.session.pop("flash", None) or ""


@page(require="mentor")
async def files_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    store = store_for(request)
    q = request.query_params
    await asyncio.to_thread(store.clear_leftovers)
    everything = await asyncio.to_thread(store.items)
    totals = []
    for kind in KINDS:
        mine = [i for i in everything if i.kind == kind]
        unmeasured = sum(1 for i in mine if not i.size_known)
        totals.append({"kind": kind, "label": LABELS[kind], "hint": HINTS[kind], "count": len(mine),
                       "size": size_text(sum(i.size for i in mine)) + (
                           f" plus {unmeasured} sandbox disk{'s' if unmeasured != 1 else ''} not measured"
                           if unmeasured else "")})
    unmeasured_all = sum(1 for i in everything if not i.size_known)
    kind = q.get("kind", "") if q.get("kind", "") in KINDS else ""
    owner = int(q["owner"]) if q.get("owner", "").isdigit() else None
    older = _days(q.get("older"))
    sort = q.get("sort", "") if q.get("sort", "") in SORTS else "age"
    shown = [i for i in everything if (not kind or i.kind == kind) and (owner is None or i.owner_id == owner)
             and (older is None or i.age_days() > older)]
    shown.sort(key=SORTS[sort][1])
    owners = sorted({(i.owner_id, i.owner) for i in everything if i.owner_id is not None and i.owner},
                    key=lambda o: o[1].lower())
    return render(request, "files.html", viewer, totals=totals, total_count=len(everything),
                  total_size=size_text(sum(i.size for i in everything)) + (
                      f" plus {unmeasured_all} sandbox disk{'s' if unmeasured_all != 1 else ''} not measured"
                      if unmeasured_all else ""), rows=[row_view(i) for i in shown[:SHOWN]],
                  matching=len(shown), shown_limit=SHOWN, owners=owners, kind=kind, owner=owner, older=older or "",
                  sort=sort, sorts=[(k, v[0]) for k, v in SORTS.items()], kinds=[(k, LABELS[k]) for k in KINDS],
                  filtered=bool(kind or owner is not None or older), disk=store.disk(), disk_text=size_text,
                  rules=store.rules(), backup_expires=store.backup_note(), flash=flash_text(request),
                  max_days=MAX_DAYS)


# --------------------------------------------------------------------------- viewing
@page(require="mentor")
async def file_view(request: Request, viewer: Viewer) -> Response:
    """Show a picture or a text file in the browser. Only safe types are shown inline; nothing is rendered as a page."""
    item_id = request.path_params["item"]
    item = (await asyncio.to_thread(store_for(request).find, item_id)) if valid_id(item_id) else None
    if item is None or item.path is None or item.kind not in (ATTACHMENT, PROJECT):
        return error_page(request, viewer, 404, "Not found", "That file isn't there any more.")
    row = services(request).db.one(
        "SELECT media_type FROM " + ("attachments" if item.kind == ATTACHMENT else "project_files") + " WHERE id = ?",
        (int(item.number),))
    media_type = row["media_type"] if row is not None else ""
    if item.view == "image" and media_type in SAFE_IMAGES:
        content_type, kind = media_type, "inline"
    elif item.view == "text":
        content_type, kind = "text/plain; charset=utf-8", "inline"
    else:
        content_type, kind = "application/octet-stream", "attachment"
    if request.query_params.get("download"):
        kind = "attachment"
    return FileResponse(item.path, media_type=content_type, headers={
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
        "Content-Disposition": disposition(kind, item.name),
        "Cache-Control": "private, no-store",
    })


# --------------------------------------------------------------------------- deleting
@dataclass
class Selection:
    mode: str  # "one", "ticked" or "older"
    items: list[Item] = field(default_factory=list)  # everything the form names (some may be in use)
    error: str = ""
    skipped_changes: int = 0  # "older than" only: Code copies skipped because no mentor released their changes
    key: str = ""  # what the form asked for, so the confirmation can't be reused for something else

    @property
    def ok(self) -> list[Item]:
        return [i for i in self.items if not i.in_use]


def _select(store: FileStore, form) -> Selection:
    """What the form asks to delete. Items are looked up again here, never trusted."""
    one = str(form.get("one", "") or "")
    if one:
        return Selection("one", store.find_all([one]), key=f"ids:{one}")
    if str(form.get("mode", "")) == "older":
        kind = str(form.get("kind", ""))
        days = _days(form.get("days"))
        if kind not in KINDS or days is None:
            return Selection("older", error=f"Pick a kind and a number of days between 1 and {MAX_DAYS}.")
        found = store.items((kind,), older_than=days)
        keep = [i for i in found if i.changes != "waiting"]  # a student's unreleased changes are only deleted one by one
        return Selection("older", keep, skipped_changes=len(found) - len(keep), key=f"older:{kind}:{days}")
    ids = [str(v) for v in form.getlist("ids")]
    found = store.find_all(ids)
    return Selection("one" if len(found) == 1 else "ticked", found, "" if ids else "Tick at least one file first.",
                     key="ids:" + ",".join(sorted(dict.fromkeys(ids))))


def _fingerprint(items: list[Item]) -> str:
    return hashlib.sha256(",".join(sorted(i.id for i in items)).encode()).hexdigest()


def _issue_token(request: Request, viewer: Viewer, sel: Selection) -> str:
    token = secrets.token_urlsafe(24)
    request.session["files_confirm"] = {"token": token, "uid": viewer.id, "exp": time.time() + TOKEN_SECONDS,
                                        "key": sel.key, "items": _fingerprint(sel.ok)}
    return token


def _token_ok(request: Request, viewer: Viewer, form, sel: Selection) -> bool:
    saved = request.session.get("files_confirm") or {}
    sent = str(form.get("token", "") or "")
    return bool(sent and saved.get("token") and secrets.compare_digest(sent, str(saved["token"]))
                and saved.get("uid") == viewer.id and float(saved.get("exp", 0)) > time.time()
                and saved.get("key") == sel.key)


def _confirm(request: Request, viewer: Viewer, store: FileStore, sel: Selection, form, *, error: str = "",
             status: int = 200) -> Response:
    ok = sel.ok
    store.measure(ok)  # only now are sandbox disks measured, and only the ones about to be deleted
    copies = [i for i in ok if i.kind == WORKSPACE]
    unknown = sum(1 for i in ok if not i.size_known)
    token = _issue_token(request, viewer, sel)
    return render(request, "files_confirm.html", viewer, status_code=status, mode=sel.mode, error=error,
                  count=len(ok), size=size_text(sum(i.size for i in ok)) + (
                      f" and {unknown} not measured" if unknown else ""), in_use=len(sel.items) - len(ok),
                  shown=[row_view(i) for i in ok[:BULK_TYPED_OVER]], more=max(0, len(ok) - BULK_TYPED_OVER),
                  ids=[i.id for i in ok] if sel.mode != "older" else [], token=token,
                  post_mode="older" if sel.mode == "older" else "ids", kind=str(form.get("kind", "")),
                  days=str(form.get("days", "")), needs_number=len(ok) > BULK_TYPED_OVER,
                  kinds_in=sorted({LABELS[i.kind] for i in ok}), has_archive=any(i.kind == ARCHIVE for i in ok),
                  copies=len(copies), released=sum(1 for i in copies if i.changes == "released"),
                  waiting=sum(1 for i in copies if i.changes == "waiting"),
                  mine=sum(1 for i in copies if i.changes == "mine"), skipped_changes=sel.skipped_changes)


@page(require="mentor")
async def files_delete(request: Request, viewer: Viewer) -> Response:
    """Step one: work out what would be deleted and ask. Nothing is deleted here."""
    form = await request.form()
    store = store_for(request)
    await asyncio.to_thread(store.clear_leftovers)
    sel = await asyncio.to_thread(_select, store, form)
    if sel.error or not sel.ok:
        request.session["flash"] = sel.error or (
            "Nothing to delete. Those files are in use by a Code session, or they are already gone." if sel.items else
            "Nothing to delete. Those files are already gone." if not sel.skipped_changes else
            f"Nothing to delete. {sel.skipped_changes} Code "
            f"{'copy was' if sel.skipped_changes == 1 else 'copies were'} skipped because no mentor has released "
            "their changes yet. You can still delete them one at a time.")
        return RedirectResponse("/files", status_code=303)
    return await asyncio.to_thread(_confirm, request, viewer, store, sel, form)


@page(require="mentor")
async def files_delete_confirm(request: Request, viewer: Viewer) -> Response:
    """Step two: only after step one's confirmation page. Checks its one-time token, that the same things would be
    deleted as were shown, and the typed number for a big delete."""
    form = await request.form()
    store = store_for(request)
    sel = await asyncio.to_thread(_select, store, form)
    if sel.error or not _token_ok(request, viewer, form, sel):
        request.session.pop("files_confirm", None)
        request.session["flash"] = sel.error or ("Nothing was deleted. Review what will be deleted first, then "
                                                 "confirm. A confirmation lasts 10 minutes.")
        return RedirectResponse("/files", status_code=303)
    ok = sel.ok
    if not ok:
        request.session.pop("files_confirm", None)
        request.session["flash"] = "Nothing was deleted. Those files are in use or already gone."
        return RedirectResponse("/files", status_code=303)
    saved = request.session["files_confirm"]
    if str(form.get("count", "")).strip() != str(len(ok)) or saved.get("items") != _fingerprint(ok):
        return await asyncio.to_thread(_confirm, request, viewer, store, sel, form, status=409, error=(
            "The list changed while you were looking at it, so nothing was deleted. Check the new numbers below."))
    if len(ok) > BULK_TYPED_OVER and str(form.get("typed", "")).strip() != str(len(ok)):
        return await asyncio.to_thread(_confirm, request, viewer, store, sel, form, status=400, error=(
            f"That isn't the right number, so nothing was deleted. Type {len(ok)} to go ahead."))
    request.session.pop("files_confirm", None)  # one use only
    outcome = await asyncio.to_thread(store.delete, ok, actor_id=viewer.id, mode=sel.mode)
    parts = [f"Deleted {len(outcome.deleted)} item{'s' if len(outcome.deleted) != 1 else ''} "
             f"({size_text(outcome.bytes)})."]
    skipped = outcome.in_use + (len(sel.items) - len(ok))
    if skipped:
        parts.append(f"{skipped} in use by a Code session {'was' if skipped == 1 else 'were'} left alone.")
    if sel.skipped_changes:
        parts.append(f"{sel.skipped_changes} Code {'copy was' if sel.skipped_changes == 1 else 'copies were'} skipped "
                     "because no mentor has released their changes yet.")
    if outcome.failed:
        parts.append("Some couldn't be deleted: " + "; ".join(outcome.failed[:3]))
    request.session["flash"] = " ".join(parts)
    return RedirectResponse("/files", status_code=303)


files_routes = [
    Route("/files", files_page),
    Route("/files/view/{item:str}", file_view),
    Route("/files/delete", files_delete, methods=["POST"]),
    Route("/files/delete/confirm", files_delete_confirm, methods=["POST"]),
]
