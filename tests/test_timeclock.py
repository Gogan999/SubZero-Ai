"""The Time Clock: its rules, clocking in and out, reminders and the nightly clock-out, editing, Slack, the web pages,
CSV import and export, personal keys for Shortcuts, and the kiosk."""

from __future__ import annotations

import io
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import segno

from subzero_ai.hub.modules import tick_all
from subzero_ai.modules.calendar import calendars_for
from subzero_ai.modules.timeclock import calendar_opens_clock, rules
from subzero_ai.modules.timeclock.store import EntryError, TimeClock, iso, parse_iso

from .helpers import ADMIN, MENTOR, STUDENT, STUDENT_2, Harness

CHICAGO = ZoneInfo("America/Chicago")
LEAD = "DEV-lead-lee"


def at(year, month, day, hour, minute=0) -> datetime:
    """A Chicago wall-clock time, in UTC."""
    return datetime(year, month, day, hour, minute, tzinfo=CHICAGO).astimezone(timezone.utc)


TUESDAY_AFTERNOON = at(2026, 12, 1, 16, 0)  # Tuesday, 4 pm: the window is open
TUESDAY_NOON = at(2026, 12, 1, 12, 0)  # closed until 3:15 pm


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.settings = rules.Settings.from_dict(None)

    def test_commands(self):
        for text, expected in (("in", "in"), ("IN!", "in"), ("clock in", "in"), ("Clock  Out.", "out"),
                               ("out", "out"), ("status", "status"), ("Hours?", "hours"), ("in the shop?", None),
                               ("how do I clock in", None), ("", None), ("checkout", None)):
            with self.subTest(text=text):
                self.assertEqual(rules.parse_command(text), expected)

    def test_window(self):
        monday = datetime(2026, 11, 30, 15, 14)
        self.assertFalse(rules.window_open(self.settings, monday))
        self.assertTrue(rules.window_open(self.settings, monday.replace(minute=15)))
        self.assertTrue(rules.window_open(self.settings, datetime(2026, 12, 5, 9, 0)))  # Saturday
        self.settings.open_from[0] = ""
        self.assertFalse(rules.window_open(self.settings, monday.replace(hour=20)))

    def test_next_opening_text(self):
        noon = datetime(2026, 12, 1, 12, 0)
        self.assertEqual(rules.opening_text(rules.next_opening(self.settings, noon), noon.date()),
                         "Clock-in opens at 3:15 pm.")
        night = datetime(2026, 12, 1, 23, 0)
        self.assertEqual(rules.opening_text(rules.next_opening(self.settings, night), night.date()),
                         "Clock-in opens tomorrow at 3:15 pm.")
        friday_night = datetime(2026, 12, 4, 23, 0)
        self.assertEqual(rules.opening_text(rules.next_opening(self.settings, friday_night), friday_night.date()),
                         "Clock-in opens tomorrow at 12:00 am.")
        closed = rules.Settings.from_dict({"open_from": [""] * 7})
        self.assertIn("isn't open this week", rules.opening_text(rules.next_opening(closed, noon), noon.date()))

    def test_settings_form(self):
        form = {f"open_{i}": "15:15" for i in range(5)} | {"open_5": "", "open_6": "9:00", "reminder_hours": "4",
                                                           "reminder_repeat_hours": "1.5", "auto_close": "21:30",
                                                           "leave_grace_minutes": "5", "event_lead_minutes": "20",
                                                           "season_start": "2027-01-04"}
        settings, errors = rules.validate_settings(form)
        self.assertEqual(errors, [])
        self.assertEqual(settings.open_from[5:], ["", "9:00"])
        self.assertEqual(settings.reminder_repeat_hours, 1.5)
        _, errors = rules.validate_settings(form | {"open_2": "3pm", "reminder_hours": "50", "auto_close": "late",
                                                    "season_start": "soon"})
        self.assertEqual(len(errors), 4)

    def test_text(self):
        self.assertEqual(rules.duration_text(3 * 3600 + 5 * 60 + 59), "3 h 05 m")
        self.assertEqual(rules.duration_text(45 * 60), "45 m")
        self.assertEqual(rules.hours_text(5400), "1.5")
        self.assertEqual(rules.season_start(self.settings, date(2027, 2, 1)), date(2026, 9, 1))
        self.assertEqual(rules.season_start(self.settings, date(2026, 9, 1)), date(2026, 9, 1))


class ClockedHarness(unittest.TestCase):
    """A Harness whose time clock runs on a clock the test moves."""

    def setUp(self):
        self.h = Harness()
        self.now = TUESDAY_AFTERNOON
        s = self.h.services
        self.clock = TimeClock(s.db, s.settings.timezone, clock=lambda: self.now,
                               events_open=lambda moment: calendar_opens_clock(s, moment))
        s._timeclock = self.clock
        self.coach = self.h.login(ADMIN)
        self.kim = self.h.login(MENTOR)
        self.ava = self.h.login(STUDENT)
        self.ben = self.h.login(STUDENT_2)
        self.lee = self.h.login(LEAD)

    def tearDown(self):
        self.h.close()

    def uid(self, actor) -> int:
        return actor.user["id"]

    def browse(self, actor, method: str, path: str, **kwargs):
        """A request that keeps the session cookie the app sends back, as a browser does, and follows redirects."""
        client = self.h.client
        client.cookies.clear()
        client.cookies.set("subzero_session", actor.cookie)
        try:
            resp = client.request(method, path, headers={"X-CSRF-Token": actor.csrf}, follow_redirects=True, **kwargs)
            fresh = [c.value for c in client.cookies.jar if c.name == "subzero_session" and c.value != actor.cookie]
            actor.cookie = fresh[-1] if fresh else actor.cookie
            return resp
        finally:
            client.cookies.clear()

    def open_entry(self, actor):
        return self.clock.open_entry(self.uid(actor))


class ClockingTest(ClockedHarness):
    def test_in_and_out(self):
        out = self.clock.clock_in(self.uid(self.ava), "web")
        self.assertEqual((out.ok, out.action, out.message), (True, "in", "Clocked in at 4:00 pm."))
        self.assertEqual(self.clock.clock_in(self.uid(self.ava), "web").action, "")
        self.now += timedelta(hours=2, minutes=5)
        out = self.clock.clock_out(self.uid(self.ava), "web")
        self.assertEqual(out.message, "Clocked out at 6:05 pm. That's 2 h 05 m.")
        self.assertEqual(self.clock.clock_out(self.uid(self.ava), "web").message, "You're not clocked in.")
        self.assertEqual(self.clock.summary(self.uid(self.ava))["week"], 2 * 3600 + 300)

    def test_outside_the_window(self):
        self.now = TUESDAY_NOON
        out = self.clock.clock_in(self.uid(self.ava), "web")
        self.assertEqual((out.ok, out.message), (False, "Clock-in opens at 3:15 pm."))
        self.assertTrue(self.clock.clock_in(self.uid(self.coach), "web", any_time=True).ok)

    def test_a_calendar_event_opens_the_clock(self):
        self.now = TUESDAY_NOON
        calendars = calendars_for(self.h.services)
        calendars.clock = lambda: self.now
        ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:e1\nDTSTART;TZID=America/Chicago:20261201T121000\n"
               "DTEND;TZID=America/Chicago:20261201T130000\nSUMMARY:Lunch build\nEND:VEVENT\nEND:VCALENDAR\n")
        source = calendars.add_file("Builds", ics, opens_clock=True)
        self.assertTrue(self.clock.clock_in(self.uid(self.ava), "web").ok)
        calendars.set_opens_clock(source, False)
        self.assertFalse(self.clock.clock_in(self.uid(self.ben), "web").ok)

    def test_reminders_and_still_here(self):
        entry = self.clock.clock_in(self.uid(self.ava), "web").entry_id
        self.now += timedelta(hours=4, minutes=59)
        self.assertEqual(self.clock.reminders_due(), [])
        self.now += timedelta(minutes=1)
        self.assertEqual([r["id"] for r in self.clock.reminders_due()], [entry])
        self.assertEqual(self.clock.reminders_due(), [])
        self.clock.still_here(entry)
        self.now += timedelta(hours=2)
        self.assertEqual([r["id"] for r in self.clock.reminders_due()], [entry])

    def test_leaving_closes_after_the_grace_period_at_the_time_they_left(self):
        self.clock.clock_in(self.uid(self.ava), "web")
        self.now += timedelta(hours=2)
        left_at = self.now
        self.clock.left(self.uid(self.ava))
        self.now += timedelta(minutes=9)
        self.assertEqual(self.clock.close_departed(), [])
        self.now += timedelta(minutes=1)
        self.assertEqual(len(self.clock.close_departed()), 1)
        row = self.clock.entries(self.uid(self.ava), left_at - timedelta(days=1), left_at)[0]
        self.assertEqual((row["clock_out"], row["out_source"]), (iso(left_at), "shortcut_leave"))

    def test_scanning_in_again_cancels_a_leave(self):
        self.clock.clock_in(self.uid(self.ava), "web")
        self.clock.left(self.uid(self.ava))
        self.now += timedelta(minutes=3)
        self.clock.clock_in(self.uid(self.ava), "kiosk")
        self.now += timedelta(minutes=30)
        self.assertEqual(self.clock.close_departed(), [])
        self.assertIsNotNone(self.open_entry(self.ava))

    def test_nightly_clock_out_closes_at_the_last_sign_and_flags_it(self):
        entry = self.clock.clock_in(self.uid(self.ava), "web").entry_id
        self.now += timedelta(hours=1)
        self.clock.still_here(entry)
        seen = self.now
        self.now = at(2026, 12, 1, 21, 59)
        self.assertEqual(self.clock.auto_close(), [])
        self.now = at(2026, 12, 1, 22, 0)
        self.assertEqual([r["id"] for r in self.clock.auto_close()], [entry])
        row = self.clock.entry(entry)
        self.assertEqual((row["clock_out"], row["flag"]), (iso(seen), "auto_closed"))
        self.assertEqual(self.clock.auto_close(), [])
        self.assertEqual([r["id"] for r in self.clock.flagged()], [entry])

    def test_the_nightly_clock_out_leaves_entries_started_after_it(self):
        self.now = at(2026, 12, 1, 22, 30)
        self.clock.clock_in(self.uid(self.coach), "web", any_time=True)
        self.assertEqual(self.clock.auto_close(), [])
        self.assertIsNotNone(self.open_entry(self.coach))


class EditingTest(ClockedHarness):
    def add(self, actor, start, end):
        return self.clock.add_entry(self.uid(actor), start, end, "", editor_id=self.uid(actor))

    def test_checks(self):
        base = TUESDAY_AFTERNOON - timedelta(days=3)
        self.add(self.ava, base, base + timedelta(hours=2))
        cases = [
            (base + timedelta(hours=1), base + timedelta(hours=3), "overlaps"),
            (base + timedelta(hours=5), base + timedelta(hours=4), "after the start"),
            (base, base + timedelta(hours=25), "24 hours"),
            (self.now + timedelta(hours=1), self.now + timedelta(hours=2), "future"),
        ]
        for start, end, words in cases:
            with self.subTest(words=words), self.assertRaisesRegex(EntryError, words):
                self.add(self.ava, start, end)

    def test_edit_clears_the_flag_and_is_logged(self):
        entry = self.clock.clock_in(self.uid(self.ava), "web").entry_id
        self.now = at(2026, 12, 1, 22, 0)
        self.clock.auto_close()
        start = parse_iso(self.clock.entry(entry)["clock_in"])
        self.clock.edit_entry(entry, start, start + timedelta(hours=3), "Left at 7", editor_id=self.uid(self.ava))
        row = self.clock.entry(entry)
        self.assertIsNone(row["flag"])
        self.assertEqual(row["note"], "Left at 7")
        self.assertEqual([h["action"] for h in self.clock.history(entry)], ["edit"])
        self.assertEqual(self.clock.edited_ids([entry]), {entry})

    def test_closing_an_open_entry_by_editing_it(self):
        entry = self.clock.clock_in(self.uid(self.ava), "web").entry_id
        self.now += timedelta(hours=3)
        start = parse_iso(self.clock.entry(entry)["clock_in"])
        self.clock.edit_entry(entry, start, start + timedelta(hours=1), "", editor_id=self.uid(self.ava))
        self.assertIsNone(self.open_entry(self.ava))
        self.assertEqual(self.clock.reminders_due(), [])

    def test_delete_and_undo_import(self):
        base = TUESDAY_AFTERNOON - timedelta(days=2)
        entry = self.add(self.ava, base, base + timedelta(hours=1))
        self.clock.delete_entry(entry, editor_id=self.uid(self.coach))
        self.assertIsNone(self.clock.entry(entry))
        self.assertEqual([h["action"] for h in self.clock.history(entry)], ["add", "delete"])
        for hours in (0, 3):
            self.clock.add_entry(self.uid(self.ben), base + timedelta(hours=hours), base + timedelta(hours=hours + 1),
                                 "", editor_id=None, source="import", batch="b1")
        self.assertEqual(self.clock.undo_import("b1", editor_id=None), 2)


class PopOutEditorTest(ClockedHarness):
    FETCH = {"X-Requested-With": "fetch"}

    def setUp(self):
        super().setUp()
        start = TUESDAY_AFTERNOON - timedelta(days=1)
        self.entry = self.clock.add_entry(self.uid(self.ava), start, start + timedelta(hours=2), "",
                                          editor_id=self.uid(self.ava))

    def test_the_links_open_the_pop_out(self):
        page = self.ava.get("/timeclock").text
        self.assertIn(f'href="/timeclock/entries/{self.entry}" data-entry-link', page)
        self.assertIn('href="/timeclock/entries/new" data-entry-link', page)

    def test_the_form_alone(self):
        part = self.ava.get(f"/timeclock/entries/{self.entry}?partial=1").text
        self.assertNotIn("<html", part)
        self.assertIn('id="entry-dialog-title">Edit time</h2>', part)
        self.assertIn(f'action="/timeclock/entries/{self.entry}"', part)
        self.assertIn("data-close-dialog", part)
        new = self.kim.get(f"/timeclock/entries/new?user={self.uid(self.ava)}&partial=1").text
        self.assertIn("Add time for Ava (student)", new)
        self.assertIn(f'action="/timeclock/entries/new?user={self.uid(self.ava)}"', new)
        self.assertEqual(self.ben.get(f"/timeclock/entries/{self.entry}?partial=1").status_code, 404)

    def test_saving_and_deleting_answer_with_json(self):
        start = TUESDAY_AFTERNOON - timedelta(days=1)
        form = {"start": f"{start.astimezone(CHICAGO):%Y-%m-%dT%H:%M}",
                "end": f"{(start + timedelta(hours=3)).astimezone(CHICAGO):%Y-%m-%dT%H:%M}", "note": "Stayed late"}
        resp = self.ava.post(f"/timeclock/entries/{self.entry}", data=form, headers=self.FETCH)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(self.clock.entry(self.entry)["note"], "Stayed late")
        bad = self.ava.post(f"/timeclock/entries/{self.entry}", data={**form, "end": form["start"]}, headers=self.FETCH)
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(bad.json()["ok"])
        self.assertIn("after the start", bad.json()["error"])
        added = self.ava.post("/timeclock/entries/new", headers=self.FETCH, data={
            "start": "2026-11-27T15:30", "end": "2026-11-27T17:00", "note": ""})
        self.assertEqual(added.json(), {"ok": True})
        gone = self.ava.post(f"/timeclock/entries/{self.entry}/delete", headers=self.FETCH)
        self.assertEqual(gone.json(), {"ok": True})
        self.assertIsNone(self.clock.entry(self.entry))

    def test_the_full_page_still_works_without_the_pop_out(self):
        page = self.ava.get(f"/timeclock/entries/{self.entry}").text
        self.assertIn("<h1>Edit time</h1>", page)
        resp = self.ava.post(f"/timeclock/entries/{self.entry}", data={"start": "", "end": "", "note": ""},
                             follow_redirects=False)
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, f"/timeclock/entries/{self.entry}"))


class SlackTest(ClockedHarness):
    def setUp(self):
        super().setUp()
        self.slack = self.h.slack
        self.slack.users["UKID"] = {"id": "UKID", "profile": {"display_name": "Kid"}, "team_id": "TDEV"}

    def dm(self, text, ts="1.0"):
        event = {"type": "message", "channel_type": "im", "channel": "DKID", "user": "UKID", "text": text, "ts": ts}
        self.h.call(self.h.services.interactions.dispatch, {"type": "events_api", "payload": {"event": event}})
        self.h.idle()
        return [p["text"] for p in self.slack.posts if p["channel"] == "DKID"]

    def kid(self):
        return self.h.services.store.user_by_slack_id("UKID")

    def test_in_out_status_and_hours(self):
        self.assertEqual(self.dm("in", "1.1")[-1], "Clocked in at 4:00 pm.")
        self.assertTrue(self.dm("status", "1.2")[-1].startswith("You've been clocked in since 4:00 pm"))
        self.now += timedelta(hours=1)
        self.assertEqual(self.dm("Out!", "1.3")[-1], "Clocked out at 5:00 pm. That's 1 h 00 m.")
        self.assertIn("This week: 1 h 00 m", self.dm("hours", "1.4")[-1])
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) AS n FROM messages")["n"], 0)
        self.assertTrue(self.slack.homes, "the Home tab is refreshed")

    def test_a_repeated_delivery_is_answered_once(self):
        self.dm("in", "2.1")
        self.assertEqual(len(self.dm("in", "2.1")), 1)

    def test_other_messages_still_go_to_the_ai(self):
        self.dm("How do I clock the motor controller?", "3.1")
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) AS n FROM messages")["n"], 1)

    def test_outside_the_window(self):
        self.now = TUESDAY_NOON
        self.assertEqual(self.dm("in", "4.1")[-1], "Clock-in opens at 3:15 pm.")

    def test_slash_command(self):
        payload = {"command": "/clock", "text": "in", "user_id": "UKID"}
        reply = self.h.call(self.h.services.interactions.dispatch, {"type": "slash_commands", "payload": payload})
        self.assertEqual(reply, {"response_type": "ephemeral", "text": "Clocked in at 4:00 pm."})
        reply = self.h.call(self.h.services.interactions.dispatch,
                            {"type": "slash_commands", "payload": payload | {"text": "dance"}})
        self.assertIn("/clock in", reply["text"])

    def tap(self, action_id, **extra):
        payload = {"type": "block_actions", "user": {"id": "UKID"}, "channel": {"id": "DKID"},
                   "message": {"ts": "9.9"}, "actions": [{"action_id": action_id, **extra}]}
        self.h.call(self.h.services.interactions.dispatch, {"type": "interactive", "payload": payload})
        self.h.idle()

    def test_home_tab_button(self):
        self.tap("timeclock:toggle")
        self.assertIsNotNone(self.clock.open_entry(self.kid()["id"]))
        text = str(self.slack.homes[-1]["view"]["blocks"])
        self.assertIn("Clocked in since 4:00 pm", text)
        self.assertIn("Clock out", text)

    def test_reminder_buttons(self):
        self.dm("in", "5.1")
        self.now += timedelta(hours=5)
        self.h.call(tick_all, self.h.services)
        reminder = [d for d in self.slack.dms if d["user"] == "UKID"][-1]
        self.assertIn("Still here?", reminder["text"])
        actions = [e["action_id"] for e in reminder["blocks"][1]["elements"]]
        self.tap(actions[1])
        self.assertIn("I'll check again", self.slack.updates[-1]["text"])
        self.assertIsNotNone(self.clock.open_entry(self.kid()["id"]))
        self.tap(actions[0])
        self.assertIsNone(self.clock.open_entry(self.kid()["id"]))

    def test_nightly_clock_out_message_and_time_picker(self):
        self.dm("in", "6.1")
        self.now = at(2026, 12, 1, 22, 5)
        self.h.call(tick_all, self.h.services)
        note = [d for d in self.slack.dms if d["user"] == "UKID"][-1]
        self.assertIn("When did you leave?", note["text"])
        picker = note["blocks"][1]["elements"][0]
        self.assertEqual(picker["type"], "timepicker")
        self.tap(picker["action_id"], selected_time="19:30")
        row = self.h.services.db.one("SELECT * FROM tc_entries")
        self.assertEqual((row["clock_out"], row["flag"]), (iso(at(2026, 12, 1, 19, 30)), None))

    def test_a_guest_cant_use_it(self):
        self.slack.users["UKID"]["is_restricted"] = True
        self.assertEqual(self.dm("in", "7.1")[-1], "This Slack account can't use the time clock.")


class PagesTest(ClockedHarness):
    def test_my_time_and_the_button(self):
        page = self.ava.get("/timeclock").text
        self.assertIn("Clocked out", page)
        page = self.browse(self.ava, "POST", "/timeclock/toggle").text
        self.assertIn("Clocked in at 4:00 pm.", page)
        self.assertIn("Clock out", page)

    def test_the_home_page_shows_the_clock_and_tools(self):
        page = self.ava.get("/").text
        self.assertIn("Time Clock", page)
        self.assertIn('href="/calendar"', page)
        self.assertIn('href="/chats"', page)

    def test_editing_own_time(self):
        resp = self.ava.post("/timeclock/entries/new", {"start": "2026-11-30T15:30", "end": "2026-11-30T18:00",
                                                        "note": "Build night"}, follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/timeclock")
        entry = self.h.services.db.one("SELECT * FROM tc_entries")
        self.assertEqual(entry["clock_in"], iso(at(2026, 11, 30, 15, 30)))
        page = self.browse(self.ava, "POST", f"/timeclock/entries/{entry['id']}",
                           data={"start": "2026-11-30T15:30", "end": "2026-11-30T15:00"}).text
        self.assertIn("after the start", page)
        self.assertIn('value="2026-11-30T15:00"', page)
        self.ava.post(f"/timeclock/entries/{entry['id']}/delete")
        self.assertIsNone(self.clock.entry(entry["id"]))

    def test_who_may_see_and_edit_whom(self):
        entry = self.clock.add_entry(self.uid(self.ava), TUESDAY_NOON - timedelta(days=1),
                                     TUESDAY_NOON - timedelta(days=1, hours=-1), "", editor_id=None)
        self.assertEqual(self.ben.get(f"/timeclock/entries/{entry}").status_code, 404)
        self.assertEqual(self.ben.get(f"/timeclock?user={self.uid(self.ava)}").status_code, 403)
        self.assertEqual(self.lee.get(f"/timeclock?user={self.uid(self.ava)}").status_code, 200)
        self.assertEqual(self.lee.get(f"/timeclock/entries/{entry}").status_code, 404)
        self.assertEqual(self.lee.get("/timeclock/team").status_code, 200)
        self.assertEqual(self.lee.get("/timeclock/admin").status_code, 403)
        self.assertEqual(self.ben.get("/timeclock/team").status_code, 403)
        self.assertEqual(self.kim.get(f"/timeclock/entries/{entry}").status_code, 200)
        self.assertEqual(self.kim.get("/timeclock/admin").status_code, 200)

    def test_team_page(self):
        self.clock.clock_in(self.uid(self.ava), "web")
        page = self.kim.get("/timeclock/team").text
        self.assertIn("Ava (student)", page)
        self.assertIn("since 4:00 pm", page)

    def test_export(self):
        name = self.ava.user["name"]
        self.h.services.db.execute("UPDATE users SET name = '=HYPERLINK(1)' WHERE id = ?", (self.uid(self.ava),))
        self.clock.add_entry(self.uid(self.ava), at(2026, 11, 30, 16), at(2026, 11, 30, 18, 30), "", editor_id=None)
        csv_text = self.kim.get("/timeclock/export.csv?start=2026-11-30&end=2026-12-01").text
        self.assertIn("'=HYPERLINK(1),DEV-student-ava,2026-11-30 16:00,2026-11-30 18:30,2.5", csv_text)
        totals = self.kim.get("/timeclock/export.csv?kind=totals&start=2026-11-30&end=2026-12-01").text
        self.assertIn(",2.5", totals)
        self.assertEqual(self.ava.get("/timeclock/export.csv").status_code, 403)
        self.assertTrue(name)

    def test_settings(self):
        form = {f"open_{i}": "16:00" for i in range(7)} | {"reminder_hours": "4", "reminder_repeat_hours": "2",
                                                           "auto_close": "21:00", "leave_grace_minutes": "10",
                                                           "event_lead_minutes": "15", "season_start": ""}
        self.kim.post("/timeclock/admin/settings", form)
        self.assertEqual(self.clock.settings().open_from, ["16:00"] * 7)
        self.kim.post("/timeclock/admin/settings", form | {"auto_close": "never"})
        self.assertEqual(self.clock.settings().auto_close, "21:00")


class ImportTest(ClockedHarness):
    def test_preview_confirm_and_undo(self):
        csv_text = ("Name,Date,Clock In,Clock Out\nAva (student),2026-11-30,3:30 PM,6:00 PM\n"
                    "dev-student-ben,11/29/2026,15:00,17:00\nNobody,2026-11-30,3:00 PM,4:00 PM\n"
                    "Ava (student),2026-11-28,5:00 PM,4:00 PM\n")
        self.h.services.db.execute("UPDATE users SET email = 'dev-student-ben' WHERE id = ?", (self.uid(self.ben),))
        page = self.browse(self.kim, "POST", "/timeclock/import",
                           files={"file": ("export.csv", csv_text.encode(), "text/csv")}).text
        self.assertIn("2 of 4 entries are ready", page)
        self.assertIn("Nobody", page)
        self.browse(self.kim, "POST", "/timeclock/import/confirm")
        rows = self.h.services.db.all("SELECT * FROM tc_entries ORDER BY clock_in")
        self.assertEqual([(r["clock_in"], r["in_source"]) for r in rows],
                         [(iso(at(2026, 11, 29, 15)), "import"), (iso(at(2026, 11, 30, 15, 30)), "import")])
        batch = rows[0]["import_batch"]
        self.assertIn("Undo", self.kim.get("/timeclock/admin").text)
        self.kim.post(f"/timeclock/import/{batch}/undo")
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) AS n FROM tc_entries WHERE deleted = 0")["n"], 0)

    ATTENDANCE_BOT = (
        "AttendanceBot Time Sheet Report\nFrom Aug 01 to Nov 28\nGenerated by Someone\n\n\n"
        "Employee Name, Department, Action Type, Timestamp, Duration Elapsed (HH:MM), Duration (HH:MM), Source, "
        "Remark, Project, Total Day Duration (HH:MM), Edited by, Edited at\n"
        "Robin Park,,In,11-23-2026 03:20 PM,NA,02:10,Late punch at 05:42 PM,,,,,\n"
        "Robin Park,,Out,11-23-2026 05:30 PM,02:10,NA,,,,02:10,,\n"
        "Robin Park,,In,11-24-2026 03:41 PM,NA,NA,,,,,,\n"
        "Robin Park,,Out,11-24-2026 11:59 PM,08:18,NA,Day Change,,,08:18,,\n"
        "Robin Park,,In,11-25-2026 12:00 AM,NA,13:00,Day Change,,,,,\n"
        "Robin Park,,Out,11-25-2026 01:00 PM,13:00,NA,,\"\"\"I forgot to clock out\"\"\",,13:00,,\n"
        "Sam Lee,Students,In,11-24-2026 03:22 PM,NA,,,,,,,\n"
        "Sam Lee,Students,Out,11-24-2026 03:22 PM,,NA,,,,,,\n"
        "Sam Lee,Students,In,11-24-2026 03:22 PM,NA,01:47,Created Via Dashboard,,,,,\n"
        "Sam Lee,Students,Out,11-24-2026 05:09 PM,01:47,NA,Created Via Dashboard,,,01:47,,\n"
        "Sam Lee,Students,Out,11-25-2026 05:09 PM,,NA,,,,,,\n"
        "Pat Quinn,,In,11-24-2026 03:00 PM,NA,01:00,,,,,,\n"
        "Pat Quinn,,Out,11-24-2026 04:00 PM,01:00,NA,,,,01:00,,\n"
        "\n\nRobin Park's total hours (HH:MM): 23:28\n\nLearn more at www.attendancebot.com\n")

    def test_attendance_bot_export(self):
        self.h.slack.users["URP"] = {"profile": {"real_name": "Robin Park"}, "team_id": "TDEV"}
        self.h.slack.users["USL"] = {"profile": {"display_name": "sam lee", "real_name": "Samuel Lee"}}
        self.h.slack.users["UOLD"] = {"profile": {"real_name": "Pat Quinn"}, "deleted": True}
        page = self.browse(self.kim, "POST", "/timeclock/import",
                           files={"file": ("report.csv", self.ATTENDANCE_BOT.encode(), "text/csv")}).text
        self.assertIn("4 of 7 entries are ready", page)
        self.assertIn("Employee Name, Action Type, Timestamp", page)
        self.assertIn("23.47", page)
        self.assertIn("1 over 12 h", page)
        self.assertIn("2 rows can", page)
        self.assertIn("No time between the clock-in and the clock-out.", page)
        self.assertIn("A clock-out with no clock-in.", page)
        self.assertIn("Robin Park (not signed in yet)", page)
        index = page.split('value="Pat Quinn"')[0].count('type="hidden" name="w')
        form = {f"w{index}": "Pat Quinn", f"p{index}": f"u:{self.uid(self.ben)}"}
        page = self.browse(self.kim, "POST", "/timeclock/import/match", data=form).text
        self.assertIn("5 of 7 entries are ready", page)
        self.browse(self.kim, "POST", "/timeclock/import/confirm")
        robin = self.h.services.store.user_by_slack_id("URP")
        self.assertEqual(robin["name"], "Robin Park")
        rows = self.h.services.db.all("SELECT * FROM tc_entries WHERE user_id = ? ORDER BY clock_in", (robin["id"],))
        self.assertEqual([(r["clock_in"], r["clock_out"], r["flag"], r["note"]) for r in rows], [
            (iso(at(2026, 11, 23, 15, 20)), iso(at(2026, 11, 23, 17, 30)), None, "Late punch at 05:42 PM"),
            (iso(at(2026, 11, 24, 15, 41)), iso(at(2026, 11, 24, 23, 59)), None, None),
            (iso(at(2026, 11, 25, 0, 0)), iso(at(2026, 11, 25, 13, 0)), "long", "I forgot to clock out"),
        ])
        sam = self.h.services.store.user_by_slack_id("USL")
        self.assertEqual(len(self.clock.entries(sam["id"], at(2026, 11, 1, 0), at(2026, 12, 1, 0))), 1)
        self.assertEqual(len(self.clock.entries(self.uid(self.ben), at(2026, 11, 1, 0), at(2026, 12, 1, 0))), 1)
        self.assertIn("Robin Park", self.kim.get("/timeclock/admin").text)

    def test_a_file_without_the_columns(self):
        page = self.browse(self.kim, "POST", "/timeclock/import", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
        self.assertIn("t find the columns", page.text)


class ShortcutTest(ClockedHarness):
    def key(self, actor) -> str:
        page = self.browse(actor, "POST", "/timeclock/phone/keys", data={"label": "iPhone"}).text
        return page.split('id="new-key"')[1].split('class="mono copyable">')[1].split("<")[0]

    def event(self, key, event, **kwargs):
        return self.h.client.post("/timeclock/api/event", json={"event": event},
                                  headers={"Authorization": f"Bearer {key}"}, **kwargs)

    def test_events(self):
        key = self.key(self.ava)
        self.assertEqual(self.event(key, "in").json()["action"], "in")
        self.assertEqual(self.event(key, "leave").json()["message"], "Leaving noted.")
        self.now += timedelta(minutes=11)
        self.h.call(tick_all, self.h.services)
        self.assertIsNone(self.open_entry(self.ava))
        self.assertEqual(self.event(key, "nfc").json()["action"], "in")
        self.assertEqual(self.event(key, "toggle").json()["action"], "out")
        self.assertEqual(self.event(key, "dance").status_code, 400)
        form = self.h.client.post("/timeclock/api/event", data={"event": "in"},
                                  headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(form.json()["action"], "in")

    def test_bad_and_revoked_keys(self):
        self.assertEqual(self.event("tc_nope", "in").status_code, 401)
        key = self.key(self.ava)
        token = self.clock.tokens(self.uid(self.ava))[0]
        self.ava.post(f"/timeclock/phone/keys/{token['id']}/revoke")
        self.assertEqual(self.event(key, "in").status_code, 401)
        self.assertNotIn(key, self.ava.get("/timeclock/phone").text)


class KioskTest(ClockedHarness):
    def pair(self):
        page = self.browse(self.kim, "POST", "/timeclock/admin/kiosks", data={"name": "Shop door"}).text
        url = page.split('class="mono copyable">')[1].split("<")[0]
        self.pair_path = url.replace("http://localhost:8787", "")
        return self.pair_path

    def test_pairing_and_the_screen(self):
        path = self.pair()
        self.h.client.cookies.clear()
        self.assertEqual(self.h.client.get("/timeclock/kiosk").status_code, 403)
        resp = self.h.client.get(path, follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/timeclock/kiosk")
        page = self.h.client.get("/timeclock/kiosk").text
        self.assertIn("Scan to clock in or out", page)
        self.assertIn("<svg", page)
        self.assertTrue(self.h.client.get("/timeclock/kiosk/code").json()["ok"])
        self.h.client.cookies.clear()
        self.assertEqual(self.h.client.get(path).status_code, 404, "a pairing link works once")

    def test_closed_kiosk_still_shows_the_code_and_clocks_people_out(self):
        self.pair()
        self.now = TUESDAY_NOON
        self.clock.clock_in(self.uid(self.ava), "web", any_time=True)
        kiosk = self.clock.kiosks()[0]
        self.h.client.cookies.clear()
        self.h.client.get(self.pair_path)
        page = self.h.client.get("/timeclock/kiosk").text
        self.assertIn("<svg", page)
        self.assertIn("Scanning still clocks you out.", page)
        self.h.client.cookies.clear()
        scan = f"/timeclock/k/{kiosk['id']}/{self.clock.kiosk_code(kiosk)}"
        self.assertIn("re clocked out</h1>", self.ava.get(scan).text)
        page = self.ben.get(f"/timeclock/k/{kiosk['id']}/{self.clock.kiosk_code(kiosk)}").text
        self.assertIn("Clock-in opens at 3:15 pm.", page)

    def test_scanning(self):
        self.pair()
        kiosk = self.clock.kiosks()[0]
        code = self.clock.kiosk_code(kiosk)
        page = self.ava.get(f"/timeclock/k/{kiosk['id']}/{code}").text
        self.assertIn("re clocked in</h1>", page)
        self.now += timedelta(seconds=35)
        self.assertIn("re clocked out</h1>", self.ava.get(f"/timeclock/k/{kiosk['id']}/{code}").text)
        self.now += timedelta(seconds=60)
        resp = self.ava.get(f"/timeclock/k/{kiosk['id']}/{code}")
        self.assertEqual(resp.status_code, 410)

    def test_scanning_while_signed_out_signs_in_first(self):
        self.pair()
        kiosk = self.clock.kiosks()[0]
        self.h.client.cookies.clear()
        resp = self.h.client.get(f"/timeclock/k/{kiosk['id']}/{self.clock.kiosk_code(kiosk)}", follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/login")
        page = self.h.client.post("/auth/dev", data={"user": STUDENT}, follow_redirects=True).text
        self.assertIn("re clocked in</h1>", page)
        self.h.client.cookies.clear()

    def test_removed_kiosk_stops(self):
        path = self.pair()
        kiosk = self.clock.kiosks()[0]
        self.kim.post(f"/timeclock/admin/kiosks/{kiosk['id']}/remove")
        self.h.client.cookies.clear()
        self.assertEqual(self.h.client.get(path).status_code, 404)
        self.assertEqual(self.ava.get(f"/timeclock/k/{kiosk['id']}/{self.clock.kiosk_code(kiosk)}").status_code, 410)


if __name__ == "__main__":
    unittest.main()


class SlidesTest(ClockedHarness):
    def add(self):
        page = self.browse(self.kim, "POST", "/timeclock/admin/kiosks", data={"name": "Morning deck",
                                                                              "kind": "slides"})
        self.assertEqual(page.headers.get("cache-control"), "no-store")
        self.url = page.text.split("const IMAGE_URL = '")[1].split("'")[0]
        self.image_path = self.url.replace("http://localhost:8787", "")
        self.kiosk = self.clock.kiosks()[0]
        return page.text

    def code(self):
        return self.clock.kiosk_code(self.kiosk)

    def test_switches_at_3_am_and_3_pm(self):
        cases = [(at(2026, 12, 1, 2, 59), at(2026, 11, 30, 15)), (at(2026, 12, 1, 3), at(2026, 12, 1, 3)),
                 (at(2026, 12, 1, 14, 59), at(2026, 12, 1, 3)), (at(2026, 12, 1, 15), at(2026, 12, 1, 15)),
                 (at(2026, 11, 1, 2, 30), at(2026, 10, 31, 15)), (at(2026, 11, 1, 9), at(2026, 11, 1, 3))]
        for moment, start in cases:
            with self.subTest(moment=moment):
                self.assertEqual(self.clock.slides_period(moment), start)

    def test_the_admin_page_shows_the_script_once(self):
        page = self.add()
        self.assertIn("Extensions", page)
        self.assertIn("function setUp()", page)
        self.assertRegex(self.image_path, r"^/timeclock/slides/\d+/[\w-]{32}\.png$")
        again = self.browse(self.kim, "GET", "/timeclock/admin").text
        self.assertNotIn(self.image_path, again)
        self.assertIn("New image link", again)
        self.assertNotIn(self.image_path.split("/")[-1].removesuffix(".png"), str(self.kiosk["image_hash"]))

    def test_the_image(self):
        self.add()
        resp = self.h.client.get(self.image_path)
        self.assertEqual((resp.status_code, resp.headers["content-type"]), (200, "image/png"))
        self.assertEqual(resp.headers["cache-control"], "no-store")
        expected = io.BytesIO()
        segno.make(f"http://localhost:8787/timeclock/k/{self.kiosk['id']}/{self.code()}", error="m").save(
            expected, kind="png", scale=12, border=2, dark="#142231")
        self.assertEqual(resp.content, expected.getvalue())
        self.now += timedelta(hours=1)
        self.assertEqual(self.h.client.get(self.image_path).content, resp.content, "same code until 3 am")
        self.assertIsNotNone(self.clock.kiosk(self.kiosk["id"])["last_seen_at"])
        for bad in (self.image_path.replace(".png", "x.png"), f"/timeclock/slides/999/{self.image_path[-36:]}"):
            self.assertEqual(self.h.client.get(bad).status_code, 404)

    def test_a_new_link_stops_the_old_one(self):
        self.add()
        page = self.browse(self.kim, "POST", f"/timeclock/admin/kiosks/{self.kiosk['id']}/pair").text
        fresh = page.split("const IMAGE_URL = '")[1].split("'")[0].replace("http://localhost:8787", "")
        self.assertNotEqual(fresh, self.image_path)
        self.assertEqual(self.h.client.get(self.image_path).status_code, 404)
        self.assertEqual(self.h.client.get(fresh).status_code, 200)
        self.assertIsNone(self.clock.pair(fresh.split("/")[-1].removesuffix(".png")))

    def test_scanning_and_the_grace_after_a_switch(self):
        self.add()
        self.now = at(2026, 12, 5, 14, 50)  # a Saturday, open all day
        morning = self.code()
        self.assertIn("re clocked in</h1>", self.ava.get(f"/timeclock/k/{self.kiosk['id']}/{morning}").text)
        self.now = at(2026, 12, 5, 15, 15)
        self.assertNotEqual(self.code(), morning)
        self.assertIn("re clocked out</h1>", self.ava.get(f"/timeclock/k/{self.kiosk['id']}/{morning}").text)
        self.now = at(2026, 12, 5, 15, 25)
        resp = self.ben.get(f"/timeclock/k/{self.kiosk['id']}/{morning}")
        self.assertEqual(resp.status_code, 410)
        self.assertIn("It changes at 3 am and 3 pm.", resp.text)
        self.assertIn("re clocked in</h1>", self.ben.get(f"/timeclock/k/{self.kiosk['id']}/{self.code()}").text)

    def test_removing_it(self):
        self.add()
        code = self.code()
        self.kim.post(f"/timeclock/admin/kiosks/{self.kiosk['id']}/remove")
        self.assertEqual(self.h.client.get(self.image_path).status_code, 404)
        self.assertEqual(self.ava.get(f"/timeclock/k/{self.kiosk['id']}/{code}").status_code, 410)

    def test_screen_kiosks_still_change_every_30_seconds(self):
        self.browse(self.kim, "POST", "/timeclock/admin/kiosks", data={"name": "Door", "kind": "screen"})
        kiosk = self.clock.kiosks()[0]
        self.assertEqual(kiosk["kind"], "screen")
        code = self.clock.kiosk_code(kiosk)
        self.now += timedelta(seconds=35)
        self.assertTrue(self.clock.kiosk_code_ok(kiosk, code))
        self.now += timedelta(seconds=30)
        self.assertFalse(self.clock.kiosk_code_ok(kiosk, code))
