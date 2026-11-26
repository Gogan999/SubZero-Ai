"""The Time Clock's web pages, the kiosk, and the API that Shortcuts calls."""

from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import date, datetime, time, timedelta
from typing import Any

import segno
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from ...store import Viewer
from ...web import current_viewer, error_page, page, render, services
from . import EDIT_OTHERS, EDIT_OWN, USE, VIEW_ALL, clock_for, rules
from .importer import CsvImportError, parse_csv
from .store import EntryError, TimeClock, iso, parse_iso

SOURCES = {"slack": "Slack", "web": "Website", "kiosk": "Kiosk", "nfc": "NFC tag", "shortcut": "Shortcut",
           "shortcut_leave": "Left the building", "auto_close": "Nightly clock-out", "edit": "Edited",
           "import": "Imported"}


# ---------------------------------------------------------------------- helpers
def flash(request: Request, text: str) -> None:
    request.session["flash"] = text


def target(request: Request, viewer: Viewer) -> tuple[Any, bool]:
    """The person a page is about (?user=), and whether the viewer may edit their time. None if not allowed."""
    s = services(request)
    raw = request.query_params.get("user", "")
    if not raw.isdigit() or int(raw) == viewer.id:
        return s.store.user(viewer.id), viewer.can(EDIT_OWN)
    row = s.store.user(int(raw))
    if row is None or not (viewer.can(VIEW_ALL) or viewer.can(EDIT_OTHERS)):
        return None, False
    return row, viewer.can(EDIT_OTHERS)


def may_edit(viewer: Viewer, user_id: int) -> bool:
    return viewer.can(EDIT_OWN) if user_id == viewer.id else viewer.can(EDIT_OTHERS)


def entry_view(clock: TimeClock, row, edited: set[int]) -> dict[str, Any]:
    start = clock.local(row["clock_in"])
    end = clock.local(row["clock_out"]) if row["clock_out"] else None
    return {"id": row["id"], "day": f"{start:%a %b} {start.day}", "start": rules.clock_text(start),
            "end": (rules.clock_text(end) + ("" if end.date() == start.date() else f" ({end:%b} {end.day})"))
            if end else "", "open": end is None, "duration": rules.duration_text(clock.seconds(row)),
            "how": SOURCES.get(row["in_source"], row["in_source"]),
            "how_out": SOURCES.get(row["out_source"] or "", row["out_source"] or ""),
            "flag": row["flag"], "edited": row["id"] in edited, "note": row["note"] or ""}


def local_input(clock: TimeClock, text: str | None) -> str:
    """A stored time as a datetime-local value."""
    return clock.local(text).strftime("%Y-%m-%dT%H:%M") if text else ""


def from_input(clock: TimeClock, raw: str) -> datetime | None:
    try:
        naive = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return clock.from_local(naive.date(), naive.time())


def day_range(request: Request, clock: TimeClock, default_days: int = 7) -> tuple[date, date]:
    """The ?start= and ?end= dates (inclusive), defaulting to this week so far."""
    today = clock.local(clock.now()).date()
    start, end = today - timedelta(days=today.weekday() if default_days == 7 else default_days), today
    try:
        start = date.fromisoformat(request.query_params.get("start", "")) if request.query_params.get("start") else start
        end = date.fromisoformat(request.query_params.get("end", "")) if request.query_params.get("end") else end
    except ValueError:
        pass
    return (start, end) if start <= end else (end, start)


def cell(text: str) -> str:
    """A CSV cell that a spreadsheet won't run as a formula."""
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def span(clock: TimeClock, start: date, end: date) -> tuple[datetime, datetime]:
    return clock.from_local(start, time()), clock.from_local(end + timedelta(days=1), time())


# ---------------------------------------------------------------------- my time
@page(require=USE)
async def my_time(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    clock = clock_for(s)
    person, editable = target(request, viewer)
    if person is None:
        return error_page(request, viewer, 403, "Not allowed", "You can only see your own time.")
    now = clock.now()
    rows = clock.entries(person["id"], now - timedelta(days=60), now + timedelta(days=1))
    edited = clock.edited_ids([r["id"] for r in rows])
    current = clock.open_entry(person["id"])
    summary = clock.summary(person["id"])
    today = clock.local(now).date()
    return render(request, "timeclock/my.html", viewer, person=person, own=person["id"] == viewer.id,
                  editable=editable, entries=[entry_view(clock, r, edited) for r in rows],
                  current=current, since=rules.when_text(clock.local(current["clock_in"]), today) if current else "",
                  running=rules.duration_text(clock.seconds(current)) if current else "",
                  summary={k: rules.duration_text(v) for k, v in summary.items()},
                  open=clock.window_open() or viewer.can(EDIT_OTHERS), closed_message=clock.closed_message(),
                  flash=request.session.pop("flash", None), tools=tool_links(viewer))


def tool_links(viewer: Viewer) -> list[tuple[str, str]]:
    links = [("/timeclock", "My time"), ("/timeclock/phone", "Phone setup")]
    if viewer.can(VIEW_ALL) or viewer.can(EDIT_OTHERS):
        links.append(("/timeclock/team", "Team"))
    if viewer.can(EDIT_OTHERS):
        links.append(("/timeclock/admin", "Admin"))
    return links


@page(require=USE)
async def toggle(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    outcome = clock_for(s).toggle(viewer.id, "web", any_time=viewer.can(EDIT_OTHERS))
    flash(request, outcome.message)
    if outcome.action:
        s.tasks.spawn(s.slack_router.publish_home(viewer.slack_user_id), name="slack-home")
    form = await request.form()
    back = str(form.get("next", ""))
    return RedirectResponse(back if back in ("/", "/timeclock") else "/timeclock", status_code=303)


# ---------------------------------------------------------------------- editing entries
@page(require=USE)
async def entry_form(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    clock = clock_for(s)
    entry_id = request.path_params.get("eid")
    if entry_id is None:
        person, editable = target(request, viewer)
        if person is None or not editable:
            return error_page(request, viewer, 403, "Not allowed", "You can't add time for this person.")
        row, history = None, []
    else:
        row = clock.entry(entry_id)
        if row is None or not may_edit(viewer, row["user_id"]):
            return error_page(request, viewer, 404, "Not found", "That entry doesn't exist or isn't yours to edit.")
        person, history = s.store.user(row["user_id"]), clock.history(entry_id)
    error = request.session.pop("tc_error", None)
    values = request.session.pop("tc_values", None) or {
        "start": local_input(clock, row["clock_in"]) if row else "",
        "end": local_input(clock, row["clock_out"]) if row else "",
        "note": (row["note"] or "") if row else ""}
    return render(request, "timeclock/entry.html", viewer, person=person, row=row, values=values, error=error,
                  history=[{"who": h["editor"] or "System", "what": h["action"], "when": rules.when_text(
                      clock.local(h["created_at"]), clock.local(clock.now()).date())} for h in history],
                  back=back_link(viewer, person["id"]), tools=tool_links(viewer))


def back_link(viewer: Viewer, user_id: int) -> str:
    return "/timeclock" if user_id == viewer.id else f"/timeclock?user={user_id}"


@page(require=USE)
async def entry_save(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    clock = clock_for(s)
    form = await request.form()
    values = {k: str(form.get(k, "")) for k in ("start", "end", "note")}
    entry_id = request.path_params.get("eid")
    start, end = from_input(clock, values["start"]), from_input(clock, values["end"]) if values["end"] else None
    if entry_id is None:
        person, editable = target(request, viewer)
        if person is None or not editable:
            return error_page(request, viewer, 403, "Not allowed", "You can't add time for this person.")
        user_id, here = person["id"], f"/timeclock/entries/new?user={person['id']}"
    else:
        row = clock.entry(entry_id)
        if row is None or not may_edit(viewer, row["user_id"]):
            return error_page(request, viewer, 404, "Not found", "That entry doesn't exist or isn't yours to edit.")
        user_id, here = row["user_id"], f"/timeclock/entries/{entry_id}"
    try:
        if start is None or (values["end"] and end is None):
            raise EntryError("Pick a date and time for both ends.")
        if entry_id is None:
            if end is None:
                raise EntryError("Give the entry an end time.")
            clock.add_entry(user_id, start, end, values["note"].strip()[:300], editor_id=viewer.id)
        else:
            clock.edit_entry(entry_id, start, end, values["note"].strip()[:300], editor_id=viewer.id)
    except EntryError as exc:
        request.session["tc_error"], request.session["tc_values"] = str(exc), values
        return RedirectResponse(here, status_code=303)
    flash(request, "Saved.")
    return RedirectResponse(back_link(viewer, user_id), status_code=303)


@page(require=USE)
async def entry_delete(request: Request, viewer: Viewer) -> Response:
    clock = clock_for(services(request))
    row = clock.entry(request.path_params["eid"])
    if row is None or not may_edit(viewer, row["user_id"]):
        return error_page(request, viewer, 404, "Not found", "That entry doesn't exist or isn't yours to edit.")
    clock.delete_entry(row["id"], editor_id=viewer.id)
    flash(request, "Deleted the entry.")
    return RedirectResponse(back_link(viewer, row["user_id"]), status_code=303)


# ---------------------------------------------------------------------- team
@page(require=VIEW_ALL)
async def team(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    clock = clock_for(s)
    start, end = day_range(request, clock)
    totals = clock.totals(*span(clock, start, end))
    people = s.db.all("SELECT id, name FROM users WHERE active = 1 ORDER BY name COLLATE NOCASE")
    today = clock.local(clock.now()).date()
    inside = [{"id": r["user_id"], "name": r["name"],
               "since": rules.when_text(clock.local(r["clock_in"]), today),
               "for": rules.duration_text(clock.seconds(r))} for r in clock.who_is_in()]
    rows = [{"id": p["id"], "name": p["name"], "hours": rules.hours_text(totals.get(p["id"], 0)),
             "seconds": totals[p["id"]]} for p in people if p["id"] in totals]
    rows.sort(key=lambda r: -r["seconds"])
    return render(request, "timeclock/team.html", viewer, inside=inside, rows=rows, start=start, end=end,
                  total=rules.hours_text(sum(totals.values())), tools=tool_links(viewer),
                  can_edit=viewer.can(EDIT_OTHERS))


# ---------------------------------------------------------------------- admin
@page(require=EDIT_OTHERS)
async def admin(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    clock = clock_for(s)
    settings = clock.settings()
    today = clock.local(clock.now()).date()
    flagged = [{"id": r["id"], "user_id": r["user_id"], "name": r["name"], **entry_view(clock, r, set())}
               for r in clock.flagged()]
    kiosks = [{"id": k["id"], "name": k["name"], "seen": rules.when_text(clock.local(k["last_seen_at"]), today)
               if k["last_seen_at"] else "Never"} for k in clock.kiosks()]
    batches = s.db.all("SELECT import_batch, COUNT(*) AS n, MIN(created_at) AS at FROM tc_entries WHERE "
                       "import_batch IS NOT NULL AND deleted = 0 GROUP BY import_batch ORDER BY at DESC")
    return render(request, "timeclock/admin.html", viewer, settings=settings, days=rules.DAYS, flagged=flagged,
                  kiosks=kiosks, batches=batches, pairing=request.session.pop("tc_pairing", None),
                  errors=request.session.pop("tc_errors", None), flash=request.session.pop("flash", None),
                  people=s.db.all("SELECT id, name FROM users WHERE active = 1 ORDER BY name COLLATE NOCASE"),
                  week_start=today - timedelta(days=today.weekday()), today=today,
                  season=rules.season_start(settings, today), tools=tool_links(viewer))


@page(require=EDIT_OTHERS)
async def admin_settings(request: Request, viewer: Viewer) -> Response:
    form = await request.form()
    settings, errors = rules.validate_settings(dict(form))
    if errors:
        request.session["tc_errors"] = errors
    else:
        clock_for(services(request)).save_settings(settings, actor_id=viewer.id)
        flash(request, "Saved the time clock settings.")
    return RedirectResponse("/timeclock/admin#settings", status_code=303)


@page(require=EDIT_OTHERS)
async def kiosk_add(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    kiosk_id, code = clock_for(s).create_kiosk(str(form.get("name", "")))
    s.db.audit("kiosk_added", actor_id=viewer.id, target_type="kiosk", target_id=kiosk_id)
    request.session["tc_pairing"] = {"id": kiosk_id, "url": s.settings.url(f"/timeclock/kiosk/pair/{code}")}
    return RedirectResponse("/timeclock/admin#kiosks", status_code=303)


@page(require=EDIT_OTHERS)
async def kiosk_repair(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    clock = clock_for(s)
    kiosk = clock.kiosk(request.path_params["kid"])
    if kiosk is not None:
        code = clock.new_pairing_code(kiosk["id"])
        request.session["tc_pairing"] = {"id": kiosk["id"], "url": s.settings.url(f"/timeclock/kiosk/pair/{code}")}
    return RedirectResponse("/timeclock/admin#kiosks", status_code=303)


@page(require=EDIT_OTHERS)
async def kiosk_remove(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    clock_for(s).revoke_kiosk(request.path_params["kid"])
    s.db.audit("kiosk_removed", actor_id=viewer.id, target_type="kiosk", target_id=request.path_params["kid"])
    flash(request, "Removed the kiosk. Its screen stops working at once.")
    return RedirectResponse("/timeclock/admin#kiosks", status_code=303)


@page(require=EDIT_OTHERS)
async def export_csv(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    clock = clock_for(s)
    start, end = day_range(request, clock)
    lo, hi = span(clock, start, end)
    out = io.StringIO()
    writer = csv.writer(out)
    if request.query_params.get("kind") == "totals":
        writer.writerow(["Name", "Slack ID", "Hours"])
        totals = clock.totals(lo, hi)
        for person in s.db.all("SELECT id, name, slack_user_id FROM users ORDER BY name COLLATE NOCASE"):
            if person["id"] in totals:
                writer.writerow([cell(person["name"]), person["slack_user_id"], rules.hours_text(totals[person["id"]])])
    else:
        writer.writerow(["Name", "Slack ID", "Clock in", "Clock out", "Hours", "Clocked in by", "Clocked out by",
                         "Flag", "Note"])
        rows = s.db.all("SELECT e.*, u.name, u.slack_user_id FROM tc_entries e JOIN users u ON u.id = e.user_id "
                        "WHERE e.deleted = 0 AND e.clock_in >= ? AND e.clock_in < ? ORDER BY u.name, e.clock_in",
                        (iso(lo), iso(hi)))
        for r in rows:
            writer.writerow([cell(r["name"]), r["slack_user_id"], clock.local(r["clock_in"]).strftime("%Y-%m-%d %H:%M"),
                             clock.local(r["clock_out"]).strftime("%Y-%m-%d %H:%M") if r["clock_out"] else "",
                             rules.hours_text(clock.seconds(r)), SOURCES.get(r["in_source"], r["in_source"]),
                             SOURCES.get(r["out_source"] or "", r["out_source"] or ""), r["flag"] or "",
                             cell(r["note"] or "")])
    s.db.audit("timeclock_exported", actor_id=viewer.id, details={"start": start.isoformat(),
                                                                  "end": end.isoformat()})
    name = f"time-{request.query_params.get('kind') or 'entries'}-{start}-to-{end}.csv"
    return PlainTextResponse(out.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------------- import
@page(require=EDIT_OTHERS)
async def import_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    key = request.session.get("tc_import")
    preview = json.loads(s.db.get_kv(f"timeclock.import.{key}") or "null") if key else None
    return render(request, "timeclock/import.html", viewer, preview=preview, tools=tool_links(viewer),
                  error=request.session.pop("tc_error", None))


@page(require=EDIT_OTHERS)
async def import_upload(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    upload = form.get("file")
    data = await upload.read() if upload is not None and hasattr(upload, "read") else b""
    try:
        preview = parse_csv(data[:5_000_000], clock_for(s), s.db)
    except CsvImportError as exc:
        request.session["tc_error"] = str(exc)
        return RedirectResponse("/timeclock/import", status_code=303)
    key = secrets.token_hex(8)
    s.db.set_kv(f"timeclock.import.{key}", json.dumps(preview))
    request.session["tc_import"] = key
    return RedirectResponse("/timeclock/import", status_code=303)


@page(require=EDIT_OTHERS)
async def import_confirm(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    clock = clock_for(s)
    key = request.session.pop("tc_import", None)
    raw = s.db.get_kv(f"timeclock.import.{key}") if key else None
    if not raw:
        return RedirectResponse("/timeclock/import", status_code=303)
    s.db.delete_kv(f"timeclock.import.{key}")
    batch = f"import-{key}"
    added, skipped = 0, 0
    for row in json.loads(raw)["rows"]:
        if not row.get("user_id"):
            continue
        try:
            clock.add_entry(row["user_id"], parse_iso(row["start"]), parse_iso(row["end"]), "",
                            editor_id=viewer.id, source="import", batch=batch)
            added += 1
        except EntryError:
            skipped += 1
    s.db.audit("timeclock_imported", actor_id=viewer.id, details={"batch": batch, "added": added, "skipped": skipped})
    flash(request, f"Imported {added} entries." + (f" Skipped {skipped} that overlapped others." if skipped else ""))
    return RedirectResponse("/timeclock/admin#import", status_code=303)


@page(require=EDIT_OTHERS)
async def import_cancel(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    key = request.session.pop("tc_import", None)
    if key:
        s.db.delete_kv(f"timeclock.import.{key}")
    return RedirectResponse("/timeclock/import", status_code=303)


@page(require=EDIT_OTHERS)
async def import_undo(request: Request, viewer: Viewer) -> Response:
    count = clock_for(services(request)).undo_import(request.path_params["batch"], editor_id=viewer.id)
    flash(request, f"Removed {count} imported entries.")
    return RedirectResponse("/timeclock/admin#import", status_code=303)


# ---------------------------------------------------------------------- phone setup
@page(require=USE)
async def phone(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    clock = clock_for(s)
    today = clock.local(clock.now()).date()
    keys = [{"id": k["id"], "label": k["label"],
             "used": rules.when_text(clock.local(k["last_used_at"]), today) if k["last_used_at"] else "Not yet"}
            for k in clock.tokens(viewer.id)]
    return render(request, "timeclock/phone.html", viewer, keys=keys, new_key=request.session.pop("tc_key", None),
                  api_url=s.settings.url("/timeclock/api/event"), tools=tool_links(viewer),
                  flash=request.session.pop("flash", None))


@page(require=USE)
async def phone_key(request: Request, viewer: Viewer) -> Response:
    form = await request.form()
    request.session["tc_key"] = clock_for(services(request)).create_token(viewer.id, str(form.get("label", "")))
    return RedirectResponse("/timeclock/phone#new-key", status_code=303)


@page(require=USE)
async def phone_key_revoke(request: Request, viewer: Viewer) -> Response:
    if clock_for(services(request)).revoke_token(viewer.id, request.path_params["kid"]):
        flash(request, "Turned that key off. Shortcuts using it stop working.")
    return RedirectResponse("/timeclock/phone", status_code=303)


async def api_event(request: Request) -> Response:
    """POST from a Shortcut: Authorization: Bearer <key>, and {"event": "in" | "out" | "toggle" | "leave" | "nfc"}."""
    s = services(request)
    clock = clock_for(s)
    auth = request.headers.get("authorization", "")
    user_id = clock.user_for_token(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
    row = s.store.user(user_id) if user_id else None
    if row is None or not row["active"]:
        return JSONResponse({"ok": False, "message": "That key doesn't work. Make a new one on the Phone setup page."},
                            status_code=401)
    role = await s.directory.role_for(row["slack_user_id"])
    viewer = Viewer(id=row["id"], slack_user_id=row["slack_user_id"], name=row["name"], role=role,
                    permissions=s.directory.permissions_for(role))
    if not viewer.can(USE):
        return JSONResponse({"ok": False, "message": "You can't use the time clock."}, status_code=403)
    try:
        body = await request.json() if "json" in request.headers.get("content-type", "") else dict(await request.form())
    except ValueError:
        body = {}
    event = str(body.get("event", "toggle")).lower()
    any_time = viewer.can(EDIT_OTHERS)
    if event == "leave":
        outcome = clock.left(viewer.id)
    elif event == "in":
        outcome = clock.clock_in(viewer.id, "shortcut", any_time=any_time)
    elif event == "out":
        outcome = clock.clock_out(viewer.id, "shortcut")
    elif event in ("toggle", "nfc"):
        outcome = clock.toggle(viewer.id, "nfc" if event == "nfc" else "shortcut", any_time=any_time)
    else:
        return JSONResponse({"ok": False, "message": "Unknown event."}, status_code=400)
    if outcome.action:
        s.tasks.spawn(s.slack_router.publish_home(viewer.slack_user_id), name="slack-home")
    return JSONResponse({"ok": outcome.ok, "message": outcome.message, "action": outcome.action})


# ---------------------------------------------------------------------- kiosk
def kiosk_of(request: Request):
    kiosk_id = request.session.get("tc_kiosk")
    return clock_for(services(request)).kiosk(kiosk_id) if isinstance(kiosk_id, int) else None


async def kiosk_pair(request: Request) -> Response:
    kiosk_id = clock_for(services(request)).pair(request.path_params["code"])
    if kiosk_id is None:
        return error_page(request, None, 404, "Pairing link expired",
                          "Ask a coach for a new pairing link on the Time Clock's Admin page.")
    request.session.clear()
    request.session["tc_kiosk"] = kiosk_id
    return RedirectResponse("/timeclock/kiosk", status_code=303)


def kiosk_state(request: Request, kiosk) -> dict[str, Any]:
    s = services(request)
    clock = clock_for(s)
    clock.kiosk_seen(kiosk["id"])
    url = s.settings.url(f"/timeclock/k/{kiosk['id']}/{clock.kiosk_code(kiosk)}")
    svg = segno.make(url, error="m").svg_inline(scale=8, dark="#142231", light="#ffffff", border=2)
    names = [r["name"].split(" ")[0] for r in clock.who_is_in()]
    return {"svg": svg, "inside": names, "open": clock.window_open(), "closed_message": clock.closed_message()}


async def kiosk_page(request: Request) -> Response:
    kiosk = kiosk_of(request)
    if kiosk is None:
        return error_page(request, None, 403, "This screen isn't a kiosk",
                          "A coach makes a pairing link on the Time Clock's Admin page and opens it here.")
    return render(request, "timeclock/kiosk.html", None, kiosk=kiosk, **kiosk_state(request, kiosk))


async def kiosk_code(request: Request) -> Response:
    kiosk = kiosk_of(request)
    if kiosk is None:
        return JSONResponse({"ok": False}, status_code=403)
    return JSONResponse({"ok": True, **kiosk_state(request, kiosk)}, headers={"Cache-Control": "no-store"})


async def scan(request: Request) -> Response:
    """A phone opened a kiosk's QR link. A sign-in in between keeps the scan for five minutes."""
    s = services(request)
    clock = clock_for(s)
    kiosk = clock.kiosk(request.path_params["kid"])
    code = request.path_params["code"]
    if code != "done":
        if kiosk is None or not clock.kiosk_code_ok(kiosk, code):
            return error_page(request, await current_viewer(request), 410, "That code has expired",
                              "Scan the code on the screen again. It changes every 30 seconds.")
        request.session["tc_scan"] = {"kiosk": kiosk["id"], "at": clock.now().timestamp()}
    pending = request.session.get("tc_scan") or {}
    if kiosk is None or pending.get("kiosk") != kiosk["id"] or clock.now().timestamp() - pending.get("at", 0) > 300:
        return error_page(request, await current_viewer(request), 410, "That code has expired",
                          "Scan the code on the screen again.")
    viewer = await current_viewer(request)
    if viewer is None:
        request.session["next"] = f"/timeclock/k/{kiosk['id']}/done"
        return RedirectResponse("/login", status_code=303)
    request.session.pop("tc_scan", None)
    if not viewer.can(USE):
        return error_page(request, viewer, 403, "Not allowed", "Your account can't use the time clock.")
    outcome = clock.toggle(viewer.id, "kiosk", any_time=viewer.can(EDIT_OTHERS))
    if outcome.action:
        s.tasks.spawn(s.slack_router.publish_home(viewer.slack_user_id), name="slack-home")
    return render(request, "timeclock/scan.html", viewer, outcome=outcome, tools=tool_links(viewer))


routes = [
    Route("/timeclock", my_time),
    Route("/timeclock/toggle", toggle, methods=["POST"]),
    Route("/timeclock/entries/new", entry_form),
    Route("/timeclock/entries/new", entry_save, methods=["POST"]),
    Route("/timeclock/entries/{eid:int}", entry_form),
    Route("/timeclock/entries/{eid:int}", entry_save, methods=["POST"]),
    Route("/timeclock/entries/{eid:int}/delete", entry_delete, methods=["POST"]),
    Route("/timeclock/team", team),
    Route("/timeclock/admin", admin),
    Route("/timeclock/admin/settings", admin_settings, methods=["POST"]),
    Route("/timeclock/admin/kiosks", kiosk_add, methods=["POST"]),
    Route("/timeclock/admin/kiosks/{kid:int}/pair", kiosk_repair, methods=["POST"]),
    Route("/timeclock/admin/kiosks/{kid:int}/remove", kiosk_remove, methods=["POST"]),
    Route("/timeclock/export.csv", export_csv),
    Route("/timeclock/import", import_page),
    Route("/timeclock/import", import_upload, methods=["POST"]),
    Route("/timeclock/import/confirm", import_confirm, methods=["POST"]),
    Route("/timeclock/import/cancel", import_cancel, methods=["POST"]),
    Route("/timeclock/import/{batch:str}/undo", import_undo, methods=["POST"]),
    Route("/timeclock/phone", phone),
    Route("/timeclock/phone/keys", phone_key, methods=["POST"]),
    Route("/timeclock/phone/keys/{kid:int}/revoke", phone_key_revoke, methods=["POST"]),
    Route("/timeclock/api/event", api_event, methods=["POST"]),
    Route("/timeclock/kiosk", kiosk_page),
    Route("/timeclock/kiosk/code", kiosk_code),
    Route("/timeclock/kiosk/pair/{code:str}", kiosk_pair),
    Route("/timeclock/k/{kid:int}/{code:str}", scan),
]
