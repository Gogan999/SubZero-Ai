"""The Files page for mentors and admins: what is stored on the mini PC, and deleting it after a confirmation."""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.routing import Route

from .attachments import size_label
from .files_admin import (
    ARCHIVE, ATTACHMENT, BULK_TYPED_OVER, KINDS, LABELS, HINTS, PROJECT, SAFE_IMAGES, FileStore, Item, valid_id,
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


def row_view(item: Item) -> dict[str, Any]:
    return {"id": item.id, "kind": item.kind, "kind_label": LABELS[item.kind], "name": item.name,
            "owner": item.owner or "Unknown", "size": size_text(item.size), "age": age_text(item),
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
    everything = await asyncio.to_thread(store.items)
    totals = []
    for kind in KINDS:
        mine = [i for i in everything if i.kind == kind]
        totals.append({"kind": kind, "label": LABELS[kind], "hint": HINTS[kind], "count": len(mine),
                       "size": size_text(sum(i.size for i in mine))})
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
                  total_size=size_text(sum(i.size for i in everything)), rows=[row_view(i) for i in shown[:SHOWN]],
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
        content_type, disposition = media_type, "inline"
    elif item.view == "text":
        content_type, disposition = "text/plain; charset=utf-8", "inline"
    else:
        content_type, disposition = "application/octet-stream", "attachment"
    if request.query_params.get("download"):
        disposition = "attachment"
    ascii_name = item.name.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    return FileResponse(item.path, media_type=content_type, headers={
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
        "Content-Disposition": f'{disposition}; filename="{ascii_name}"',
        "Cache-Control": "private, no-store",
    })


# --------------------------------------------------------------------------- deleting
def _select(store: FileStore, form) -> tuple[str, list[Item], str]:
    """What the form asks to delete: (mode, items, error). Items are looked up again here, never trusted."""
    one = str(form.get("one", "") or "")
    if one:
        return "one", store.find_all([one]), ""
    if str(form.get("mode", "")) == "older":
        kind = str(form.get("kind", ""))
        days = _days(form.get("days"))
        if kind not in KINDS or days is None:
            return "older", [], f"Pick a kind and a number of days between 1 and {MAX_DAYS}."
        return "older", [i for i in store.items((kind,)) if i.age_days() > days], ""
    ids = [str(v) for v in form.getlist("ids")]
    found = store.find_all(ids)
    return ("one" if len(found) == 1 else "ticked"), found, "" if ids else "Tick at least one file first."


def _confirm(request: Request, viewer: Viewer, mode: str, form, items: list[Item], *, error: str = "",
             status: int = 200) -> Response:
    ok = [i for i in items if not i.in_use]
    return render(request, "files_confirm.html", viewer, status_code=status, mode=mode, error=error,
                  count=len(ok), size=size_text(sum(i.size for i in ok)), in_use=len(items) - len(ok),
                  shown=[row_view(i) for i in ok[:BULK_TYPED_OVER]], more=max(0, len(ok) - BULK_TYPED_OVER),
                  ids=[i.id for i in ok] if mode != "older" else [], post_mode="older" if mode == "older" else "ids", kind=str(form.get("kind", "")),
                  days=str(form.get("days", "")), needs_number=len(ok) > BULK_TYPED_OVER,
                  kinds_in=sorted({LABELS[i.kind] for i in ok}),
                  has_archive=any(i.kind == ARCHIVE for i in ok))


@page(require="mentor")
async def files_delete(request: Request, viewer: Viewer) -> Response:
    """Step one: work out what would be deleted and ask. Nothing is deleted here."""
    form = await request.form()
    store = store_for(request)
    mode, items, problem = await asyncio.to_thread(_select, store, form)
    if problem or not [i for i in items if not i.in_use]:
        request.session["flash"] = problem or ("Nothing to delete. Those files are in use by a running Code "
                                               "session, or they are already gone." if items else
                                               "Nothing to delete. Those files are already gone.")
        return RedirectResponse("/files", status_code=303)
    return _confirm(request, viewer, mode, form, items)


@page(require="mentor")
async def files_delete_confirm(request: Request, viewer: Viewer) -> Response:
    """Step two: delete, after checking the count the person was shown (and the typed number for a big delete)."""
    form = await request.form()
    store = store_for(request)
    mode, items, problem = await asyncio.to_thread(_select, store, form)
    ok = [i for i in items if not i.in_use]
    if problem or not ok:
        request.session["flash"] = problem or "Nothing was deleted. Those files are in use or already gone."
        return RedirectResponse("/files", status_code=303)
    shown = str(form.get("count", "")).strip()
    if shown != str(len(ok)):
        return _confirm(request, viewer, mode, form, items, status=409, error=(
            "The list changed while you were looking at it, so nothing was deleted. Check the new numbers below."))
    if len(ok) > BULK_TYPED_OVER and str(form.get("typed", "")).strip() != str(len(ok)):
        return _confirm(request, viewer, mode, form, items, status=400, error=(
            f"That isn't the right number, so nothing was deleted. Type {len(ok)} to go ahead."))
    outcome = await asyncio.to_thread(store.delete, ok, actor_id=viewer.id, mode=mode)
    parts = [f"Deleted {len(outcome.deleted)} item{'s' if len(outcome.deleted) != 1 else ''} "
             f"({size_text(outcome.bytes)})."]
    skipped = outcome.in_use + (len(items) - len(ok))
    if skipped:
        parts.append(f"{skipped} in use by a Code session {'was' if skipped == 1 else 'were'} left alone.")
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
