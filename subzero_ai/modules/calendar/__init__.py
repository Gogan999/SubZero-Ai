"""The Calendar: the team's schedule from Google Calendar (an iCal address that syncs) or uploaded .ics files.
Events of a calendar marked "opens the time clock" open clock-in shortly before they start."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from typing import Any

from ...hub.modules import Module, NavItem
from ...hub.permissions import Permission
from ...slack_dm import escape
from .store import MIGRATIONS, Calendars

VIEW = "calendar.view"
MANAGE = "calendar.manage"


def calendars_for(services: Any) -> Calendars:
    """The services' Calendars, made on first use."""
    found = getattr(services, "_calendars", None)
    if found is None:
        found = Calendars(services.db, services.settings.timezone)
        services._calendars = found
    return found


def event_time(calendars: Calendars, row) -> str:
    """'All day', '3:30 pm' or '3:30 pm to 6:00 pm'."""
    if row["all_day"]:
        return "All day"
    start, end = (datetime.fromisoformat(row[k]).astimezone(calendars.tz) for k in ("starts_at", "ends_at"))
    return clock_text(start) if end <= start else f"{clock_text(start)} to {clock_text(end)}"


def clock_text(moment: datetime) -> str:
    """'3:30 pm'."""
    return moment.strftime("%I:%M %p").lstrip("0").lower()


class CalendarModule(Module):
    key = "calendar"
    name = "Calendar"
    permissions = (
        Permission(VIEW, "See the team calendar", frozenset({"admin", "mentor", "lead", "student"})),
        Permission(MANAGE, "Add and remove calendars", frozenset({"admin", "mentor"}),
                   never_roles=frozenset({"student"})),
    )
    migrations = MIGRATIONS

    def routes(self):
        from .pages import routes
        return routes

    def nav(self, viewer) -> list[NavItem]:
        return [NavItem("Calendar", "/calendar", "calendar")] if viewer.can(VIEW) else []

    def opens_clock(self, services: Any, moment: datetime, lead_minutes: int) -> bool:
        return calendars_for(services).opens_clock(moment, lead_minutes)

    def today(self, services: Any) -> list:
        calendars = calendars_for(services)
        now = calendars.clock().astimezone(calendars.tz)
        start = datetime.combine(now.date(), time(), tzinfo=calendars.tz)
        return calendars.between(max(start, now - timedelta(hours=1)), start + timedelta(days=1))

    def home_card(self, services: Any, viewer) -> dict[str, Any] | None:
        if not viewer.can(VIEW):
            return None
        calendars = calendars_for(services)
        return {"template": "calendar/_home_card.html",
                "events": [{"title": r["title"], "time": event_time(calendars, r), "location": r["location"]}
                           for r in self.today(services)[:5]]}

    def slack_home_blocks(self, services: Any, viewer) -> list[dict[str, Any]]:
        if not viewer.can(VIEW):
            return []
        calendars = calendars_for(services)
        events = self.today(services)[:5]
        lines = [f"• {event_time(calendars, r)}: {escape(r['title'])}" for r in events]
        lines = lines or ["Nothing on the calendar today."]
        link = services.settings.url("/calendar")
        text = f"*Today*  <{link}|Calendar>\n" + "\n".join(lines)
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    async def tick(self, services: Any) -> None:
        calendars = calendars_for(services)
        for source_id in calendars.due():
            await asyncio.to_thread(calendars.sync, source_id)
