"""The Time Clock: clocking in and out from Slack, the website, a kiosk QR code or Shortcuts, with reminders and a
nightly clock-out for anyone who forgets."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ...hub.modules import Module, NavItem, installed
from ...hub.permissions import Permission
from . import rules
from .store import MIGRATIONS, TimeClock, parse_iso

log = logging.getLogger("subzero.timeclock")

USE = "timeclock.use"
EDIT_OWN = "timeclock.edit_own"
VIEW_ALL = "timeclock.view_all"
EDIT_OTHERS = "timeclock.edit_others"
EVERYONE = frozenset({"admin", "mentor", "lead", "student"})


def clock_for(services: Any) -> TimeClock:
    """The services' TimeClock, made on first use."""
    clock = getattr(services, "_timeclock", None)
    if clock is None:
        clock = TimeClock(services.db, services.settings.timezone,
                          events_open=lambda moment: calendar_opens_clock(services, moment))
        services._timeclock = clock
    return clock


def calendar_opens_clock(services: Any, moment: datetime) -> bool:
    calendar = next((m for m in installed() if m.key == "calendar"), None)
    if calendar is None:
        return False
    lead = clock_for(services).settings().event_lead_minutes
    return calendar.opens_clock(services, moment, lead)


class TimeClockModule(Module):
    key = "timeclock"
    name = "Time Clock"
    permissions = (
        Permission(USE, "Clock in and out", EVERYONE),
        Permission(EDIT_OWN, "Edit their own time", EVERYONE),
        Permission(VIEW_ALL, "See who's in and everyone's hours", frozenset({"admin", "mentor", "lead"})),
        Permission(EDIT_OTHERS, "Edit and export anyone's time, and set up kiosks",
                   frozenset({"admin", "mentor"}), never_roles=frozenset({"student"})),
    )
    migrations = MIGRATIONS
    commands = ("/clock",)

    def routes(self):
        from .pages import routes
        return routes

    def nav(self, viewer) -> list[NavItem]:
        return [NavItem("Time Clock", "/timeclock", "clock")] if viewer.can(USE) else []

    def home_card(self, services: Any, viewer) -> dict[str, Any] | None:
        if not viewer.can(USE):
            return None
        clock = clock_for(services)
        current = clock.open_entry(viewer.id)
        today = clock.local(clock.now()).date()
        return {"template": "timeclock/_home_card.html", "current": current,
                "since": rules.when_text(clock.local(current["clock_in"]), today) if current else "",
                "week": rules.duration_text(clock.summary(viewer.id)["week"]),
                "open": clock.window_open() or viewer.can(EDIT_OTHERS), "closed_message": clock.closed_message()}

    # ------------------------------------------------------------------ Slack
    async def on_dm(self, services: Any, event: dict[str, Any], text: str) -> bool:
        command = rules.parse_command(text)
        if command is None:
            return False
        if not services.store.claim_dm_event(event.get("channel", ""), event.get("ts", "")):
            return True
        viewer = await services.slack_router.viewer(event["user"])
        if viewer is None or not viewer.can(USE):
            reply = "This Slack account can't use the time clock."
        else:
            reply = self.answer(services, viewer, command, "slack")
        await services.slack.post_message(event["channel"], reply)
        if viewer is not None and command in ("in", "out"):
            await services.slack_router.publish_home(event["user"])
        return True

    def answer(self, services: Any, viewer, command: str, source: str) -> str:
        clock = clock_for(services)
        if command == "in":
            return clock.clock_in(viewer.id, source, any_time=viewer.can(EDIT_OTHERS)).message
        if command == "out":
            return clock.clock_out(viewer.id, source).message
        if command == "status":
            current = clock.open_entry(viewer.id)
            if current is None:
                return "You're not clocked in. " + ("Send *in* to clock in." if clock.window_open()
                                                    else clock.closed_message())
            today = clock.local(clock.now()).date()
            return (f"You've been clocked in since {rules.when_text(clock.local(current['clock_in']), today)} "
                    f"({rules.duration_text(clock.seconds(current))}).")
        summary = clock.summary(viewer.id)
        return (f"This week: {rules.duration_text(summary['week'])}. Last 30 days: "
                f"{rules.duration_text(summary['month'])}. This season: {rules.duration_text(summary['season'])}.")

    async def on_command(self, services: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
        text = (payload.get("text") or "").strip().lower() or "status"
        command = rules.parse_command(text)
        viewer = await services.slack_router.viewer(payload.get("user_id", ""))
        if viewer is None or not viewer.can(USE):
            reply = "This Slack account can't use the time clock."
        elif command is None:
            reply = "Use /clock in, /clock out, /clock status or /clock hours."
        else:
            reply = self.answer(services, viewer, command, "slack")
            if command in ("in", "out"):
                services.tasks.spawn(services.slack_router.publish_home(payload.get("user_id", "")),
                                     name="slack-home")
        return {"response_type": "ephemeral", "text": reply}

    async def on_action(self, services: Any, payload: dict[str, Any], action_id: str) -> bool:
        user = (payload.get("user") or {}).get("id", "")
        viewer = await services.slack_router.viewer(user)
        if viewer is None or not viewer.can(USE):
            return True
        clock = clock_for(services)
        parts = action_id.split(":")
        verb = parts[1] if len(parts) > 1 else ""
        entry = clock.entry(int(parts[2])) if len(parts) > 2 and parts[2].isdigit() else None
        if entry is not None and entry["user_id"] != viewer.id:
            return True
        reply = ""
        if verb == "toggle":
            clock.toggle(viewer.id, "slack", any_time=viewer.can(EDIT_OTHERS))
        elif verb == "out_now" and entry is not None:
            reply = clock.clock_out(viewer.id, "slack").message if entry["clock_out"] is None else "Already clocked out."
        elif verb == "still_here" and entry is not None and entry["clock_out"] is None:
            clock.still_here(entry["id"])
            reply = "Got it. I'll check again later."
        elif verb == "left_at" and entry is not None:
            reply = self.fix_departure(clock, viewer, entry, (payload.get("actions") or [{}])[0].get("selected_time"))
        await self.update_slack_message(services, payload, reply)
        await services.slack_router.publish_home(user)
        return True

    @staticmethod
    def fix_departure(clock: TimeClock, viewer, entry, picked: str | None) -> str:
        at = rules.parse_time(picked or "")
        if at is None or entry["clock_out"] is None:
            return ""
        start = clock.local(entry["clock_in"])
        end = clock.from_local(start.date(), at)
        try:
            clock.edit_entry(entry["id"], parse_iso(entry["clock_in"]), end, entry["note"] or "", editor_id=viewer.id)
        except ValueError as exc:
            return str(exc)
        return f"Saved: you left at {rules.clock_text(clock.local(end))}."

    @staticmethod
    async def update_slack_message(services: Any, payload: dict[str, Any], text: str) -> None:
        channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id")
        ts = (payload.get("message") or {}).get("ts") or (payload.get("container") or {}).get("message_ts")
        if not text or not channel or not ts:
            return
        try:
            await services.slack.update_message(channel, ts, text,
                                                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])
        except Exception:
            log.warning("Couldn't update a time clock message in Slack", exc_info=True)

    def slack_home_blocks(self, services: Any, viewer) -> list[dict[str, Any]]:
        if not viewer.can(USE):
            return []
        clock = clock_for(services)
        current = clock.open_entry(viewer.id)
        today = clock.local(clock.now()).date()
        if current is not None:
            status = (f"Clocked in since {rules.when_text(clock.local(current['clock_in']), today)} "
                      f"({rules.duration_text(clock.seconds(current))}).")
        else:
            status = "Not clocked in." + ("" if clock.window_open() else " " + clock.closed_message())
        week = rules.duration_text(clock.summary(viewer.id)["week"])
        button = {"type": "button", "action_id": "timeclock:toggle", "style": "danger" if current else "primary",
                  "text": {"type": "plain_text", "text": "Clock out" if current else "Clock in"}}
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Time Clock*  <{services.settings.url('/timeclock')}"
                                                                   f"|Your time>\n{status}\nThis week: {week}."}},
            {"type": "actions", "elements": [button]},
        ]

    # ------------------------------------------------------------------ minute job
    async def tick(self, services: Any) -> None:
        clock = clock_for(services)
        clock.close_departed()
        for row in clock.reminders_due():
            await self.remind(services, clock, row)
        for row in clock.auto_close():
            await self.tell_auto_closed(services, clock, row)

    async def remind(self, services: Any, clock: TimeClock, row) -> None:
        if services.slack is None or not str(row["slack_user_id"]).startswith(("U", "W")):
            return
        since = rules.clock_text(clock.local(row["clock_in"]))
        text = f"Still here? You've been clocked in since {since} ({rules.duration_text(clock.seconds(row))})."
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": f"timeclock:out_now:{row['id']}", "style": "primary",
                 "text": {"type": "plain_text", "text": "Clock out now"}},
                {"type": "button", "action_id": f"timeclock:still_here:{row['id']}",
                 "text": {"type": "plain_text", "text": "Still here"}},
            ]},
        ]
        try:
            await services.slack.dm(row["slack_user_id"], text, blocks)
        except Exception:
            log.warning("Couldn't send a time clock reminder", exc_info=True)

    async def tell_auto_closed(self, services: Any, clock: TimeClock, row) -> None:
        if services.slack is None or not str(row["slack_user_id"]).startswith(("U", "W")):
            return
        entry = clock.entry(row["id"])
        out = clock.local(entry["clock_out"])
        text = (f"You were still clocked in at {clock.settings().auto_close}, so I clocked you out at "
                f"{rules.clock_text(out)}, the last time you were seen. When did you leave?")
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": [
                {"type": "timepicker", "action_id": f"timeclock:left_at:{row['id']}",
                 "initial_time": out.strftime("%H:%M"),
                 "placeholder": {"type": "plain_text", "text": "When you left"}},
            ]},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{services.settings.url('/timeclock')}"
                                                                        "|Or fix it on the website>"}]},
        ]
        try:
            await services.slack.dm(row["slack_user_id"], text, blocks)
        except Exception:
            log.warning("Couldn't tell someone they were clocked out", exc_info=True)

