"""The Calendar's pages: the agenda, the month view, and managing calendars."""

from __future__ import annotations

import asyncio
import calendar as monthcal
from datetime import date, datetime, time, timedelta

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from ...store import Viewer
from ...web import page, render, services
from . import MANAGE, VIEW, calendars_for, clock_text, event_time
from .ics import IcsError

AGENDA_DAYS = 60


def event_view(calendars, row) -> dict:
    start = datetime.fromisoformat(row["starts_at"]).astimezone(calendars.tz)
    return {"title": row["title"], "time": event_time(calendars, row), "location": row["location"],
            "description": row["description"], "calendar": row["calendar"],
            "opens_clock": bool(row["opens_clock"]) and not row["all_day"],
            "day": start.date()}


@page(require=VIEW)
async def agenda(request: Request, viewer: Viewer) -> Response:
    calendars = calendars_for(services(request))
    today = calendars.clock().astimezone(calendars.tz).date()
    start = datetime.combine(today, time(), tzinfo=calendars.tz)
    days: dict[date, list] = {}
    for row in calendars.between(start, start + timedelta(days=AGENDA_DAYS)):
        view = event_view(calendars, row)
        days.setdefault(max(view["day"], today), []).append(view)
    groups = [{"day": d, "label": "Today" if d == today else ("Tomorrow" if d == today + timedelta(days=1)
                                                            else f"{d:%A, %B} {d.day}"), "events": events}
              for d, events in sorted(days.items())]
    return render(request, "calendar/agenda.html", viewer, groups=groups, today=today, view="agenda",
                  has_sources=bool(calendars.sources()), can_manage=viewer.can(MANAGE))


@page(require=VIEW)
async def month(request: Request, viewer: Viewer) -> Response:
    calendars = calendars_for(services(request))
    today = calendars.clock().astimezone(calendars.tz).date()
    try:
        first = date.fromisoformat(request.query_params.get("month", "") + "-01")
    except ValueError:
        first = today.replace(day=1)
    if not 2000 <= first.year <= 2100:
        first = today.replace(day=1)
    weeks = monthcal.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month)
    lo = datetime.combine(weeks[0][0], time(), tzinfo=calendars.tz)
    hi = datetime.combine(weeks[-1][-1] + timedelta(days=1), time(), tzinfo=calendars.tz)
    by_day: dict[date, list] = {}
    for row in calendars.between(lo, hi):
        view = event_view(calendars, row)
        end = datetime.fromisoformat(row["ends_at"]).astimezone(calendars.tz)
        last = (end - timedelta(seconds=1)).date() if end > datetime.fromisoformat(row["starts_at"]) else view["day"]
        day = max(view["day"], weeks[0][0])
        while day <= min(last, weeks[-1][-1]):
            by_day.setdefault(day, []).append(view)
            day += timedelta(days=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return render(request, "calendar/month.html", viewer, weeks=weeks, by_day=by_day, first=first, today=today,
                  prev=f"{prev:%Y-%m}", next=f"{nxt:%Y-%m}", view="month", can_manage=viewer.can(MANAGE))


@page(require=MANAGE)
async def manage(request: Request, viewer: Viewer) -> Response:
    calendars = calendars_for(services(request))
    rows = []
    for s in calendars.sources():
        synced = datetime.fromisoformat(s["last_synced_at"]).astimezone(calendars.tz) if s["last_synced_at"] else None
        rows.append({"id": s["id"], "name": s["name"], "kind": s["kind"], "events": s["events"],
                     "opens_clock": bool(s["opens_clock"]), "error": s["last_error"],
                     "synced": f"{synced:%b} {synced.day}, {clock_text(synced)}" if synced else "Not yet"})
    return render(request, "calendar/manage.html", viewer, sources=rows, view="manage", can_manage=True,
                  flash=request.session.pop("flash", None), error=request.session.pop("cal_error", None))


@page(require=MANAGE)
async def add(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    calendars = calendars_for(s)
    form = await request.form()
    name, opens = str(form.get("name", "")), form.get("opens_clock") == "1"
    upload = form.get("file")
    try:
        if upload is not None and hasattr(upload, "read") and getattr(upload, "filename", ""):
            data = await upload.read()
            if len(data) > 5 * 1024 * 1024:
                raise IcsError("That file is larger than 5 MB.")
            source_id = calendars.add_file(name or upload.filename.rsplit(".", 1)[0], data.decode("utf-8", "replace"),
                                           opens)
            request.session["flash"] = "Added the calendar."
        else:
            source_id = calendars.add_url(name, str(form.get("url", "")), opens)
            problem = await asyncio.to_thread(calendars.sync, source_id)
            request.session["flash"] = "Added the calendar." + (f" The first sync failed: {problem}" if problem else "")
        s.db.audit("calendar_added", actor_id=viewer.id, target_type="calendar", target_id=source_id)
    except IcsError as exc:
        request.session["cal_error"] = str(exc)
    return RedirectResponse("/calendar/manage", status_code=303)


@page(require=MANAGE)
async def change(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    calendars = calendars_for(s)
    source_id = request.path_params["sid"]
    form = await request.form()
    action = str(form.get("action", ""))
    if calendars.source(source_id) is None:
        return RedirectResponse("/calendar/manage", status_code=303)
    if action == "sync":
        problem = await asyncio.to_thread(calendars.sync, source_id)
        request.session["flash"] = f"Sync failed: {problem}" if problem else "Synced."
    elif action == "remove":
        calendars.remove(source_id)
        s.db.audit("calendar_removed", actor_id=viewer.id, target_type="calendar", target_id=source_id)
        request.session["flash"] = "Removed the calendar."
    elif action in ("opens", "closes"):
        calendars.set_opens_clock(source_id, action == "opens")
        request.session["flash"] = "Saved."
    return RedirectResponse("/calendar/manage", status_code=303)


routes = [
    Route("/calendar", agenda),
    Route("/calendar/month", month),
    Route("/calendar/manage", manage),
    Route("/calendar/manage", add, methods=["POST"]),
    Route("/calendar/manage/{sid:int}", change, methods=["POST"]),
]
