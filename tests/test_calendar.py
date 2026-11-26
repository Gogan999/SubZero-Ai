"""The Calendar: reading .ics files, syncing iCal addresses, the agenda and month pages, and managing calendars."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from subzero_ai.db import Database
from subzero_ai.hub.modules import installed
from subzero_ai.modules.calendar import calendars_for, ics
from subzero_ai.modules.calendar.store import Calendars
from subzero_ai.store import Viewer

from .helpers import ADMIN, MENTOR, STUDENT, Harness

CHICAGO = ZoneInfo("America/Chicago")
NOW = datetime(2026, 12, 1, 18, 0, tzinfo=timezone.utc)  # noon in Chicago


def wrap(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def local(o) -> str:
    return o.start.astimezone(CHICAGO).strftime("%Y-%m-%d %H:%M")


class IcsTest(unittest.TestCase):
    def expand(self, text, days=60):
        return ics.expand(text, NOW - timedelta(days=30), NOW + timedelta(days=days), CHICAGO)

    def test_times_zones_and_text(self):
        text = wrap(
            event("UID:a", "DTSTART;TZID=America/Chicago:20261201T153000", "DTEND;TZID=America/Chicago:20261201T"
                  "180000", "SUMMARY:Build\\, then test", "LOCATION:Shop", "DESCRIPTION:Bring safety glasses.\\nAnd",
                  "  gloves."),
            event("UID:b", "DTSTART:20261202T000000Z", "DURATION:PT1H30M", "SUMMARY:UTC call"),
            event("UID:c", "DTSTART;VALUE=DATE:20261205", "DTEND;VALUE=DATE:20261207", "SUMMARY:Competition"),
            event("UID:d", 'DTSTART;TZID="Eastern Standard Time":20261203T190000', "SUMMARY:Outlook event"),
        )
        found = {o.uid: o for o in self.expand(text)}
        self.assertEqual(local(found["a"]), "2026-12-01 15:30")
        self.assertEqual(found["a"].end - found["a"].start, timedelta(hours=2, minutes=30))
        self.assertEqual(found["a"].title, "Build, then test")
        self.assertEqual(found["a"].description, "Bring safety glasses.\nAnd gloves.")
        self.assertEqual(local(found["b"]), "2026-12-01 18:00")
        self.assertEqual(found["b"].end - found["b"].start, timedelta(minutes=90))
        self.assertTrue(found["c"].all_day)
        self.assertEqual(found["c"].end - found["c"].start, timedelta(days=2))
        self.assertEqual(local(found["d"]), "2026-12-03 18:00")

    def test_repeats_skips_and_moves(self):
        text = wrap(
            event("UID:practice", "DTSTART;TZID=America/Chicago:20261201T153000",
                  "DTEND;TZID=America/Chicago:20261201T180000", "RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261217T215959Z",
                  "EXDATE;TZID=America/Chicago:20261203T153000", "SUMMARY:Practice"),
            event("UID:practice", "RECURRENCE-ID;TZID=America/Chicago:20261208T153000",
                  "DTSTART;TZID=America/Chicago:20261208T170000", "DTEND;TZID=America/Chicago:20261208T190000",
                  "SUMMARY:Practice (late)"),
            event("UID:practice", "RECURRENCE-ID;TZID=America/Chicago:20261215T153000",
                  "DTSTART;TZID=America/Chicago:20261215T153000", "STATUS:CANCELLED", "SUMMARY:Practice"),
        )
        found = self.expand(text)
        self.assertEqual([(local(o), o.title) for o in found], [
            ("2026-12-01 15:30", "Practice"),
            ("2026-12-08 17:00", "Practice (late)"),
            ("2026-12-10 15:30", "Practice"),
            ("2026-12-17 15:30", "Practice"),
        ])

    def test_an_old_weekly_event_still_shows_up_now(self):
        text = wrap(event("UID:old", "DTSTART;TZID=America/Chicago:20150106T190000", "DURATION:PT1H",
                          "RRULE:FREQ=WEEKLY", "SUMMARY:Board meeting"))
        found = self.expand(text, days=15)
        self.assertEqual([local(o) for o in found if o.start >= NOW],
                         ["2026-12-01 19:00", "2026-12-08 19:00", "2026-12-15 19:00"])

    def test_all_day_repeats(self):
        text = wrap(event("UID:x", "DTSTART;VALUE=DATE:20261130", "RRULE:FREQ=DAILY;COUNT=3", "SUMMARY:Camp"))
        self.assertEqual([local(o) for o in self.expand(text)],
                         ["2026-11-30 00:00", "2026-12-01 00:00", "2026-12-02 00:00"])

    def test_not_a_calendar(self):
        with self.assertRaises(ics.IcsError):
            ics.expand("hello", NOW, NOW, CHICAGO)


ONE = wrap(event("UID:one", "DTSTART;TZID=America/Chicago:20261201T153000",
                 "DTEND;TZID=America/Chicago:20261201T180000", "SUMMARY:Practice"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cal-test-"))
        self.answer: str | Exception = ONE
        self.now = NOW
        self.calendars = Calendars(Database(self.tmp / "app.db"), "America/Chicago", clock=lambda: self.now,
                                   fetch=self.fetch)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fetch(self, url):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def test_addresses(self):
        source = self.calendars.add_url("Team", "webcal://calendar.example/x.ics", True)
        self.assertEqual(self.calendars.source(source)["url"], "https://calendar.example/x.ics")
        for bad in ("http://calendar.example/x.ics", "file:///etc/passwd", "calendar.example"):
            with self.subTest(bad=bad), self.assertRaises(ics.IcsError):
                self.calendars.add_url("Team", bad, False)

    def test_sync_keeps_old_events_when_a_download_fails(self):
        source = self.calendars.add_url("Team", "https://calendar.example/x.ics", True)
        self.assertEqual(self.calendars.due(), [source])
        self.assertIsNone(self.calendars.sync(source))
        self.assertEqual(self.calendars.due(), [])
        self.answer = requests.ConnectionError("down")
        self.now += timedelta(minutes=16)
        self.assertEqual(self.calendars.sync(source), "Couldn't download the calendar.")
        self.assertEqual(len(self.calendars.between(NOW, NOW + timedelta(days=1))), 1)
        self.assertEqual(self.calendars.source(source)["last_error"], "Couldn't download the calendar.")

    def test_opens_the_clock_before_and_during_events(self):
        source = self.calendars.add_file("Builds", ONE, True)
        start = datetime(2026, 12, 1, 15, 30, tzinfo=CHICAGO)
        self.assertFalse(self.calendars.opens_clock(start - timedelta(minutes=16), 15))
        self.assertTrue(self.calendars.opens_clock(start - timedelta(minutes=15), 15))
        self.assertTrue(self.calendars.opens_clock(start + timedelta(hours=2), 15))
        self.assertFalse(self.calendars.opens_clock(start + timedelta(hours=2, minutes=30), 15))
        self.calendars.set_opens_clock(source, False)
        self.assertFalse(self.calendars.opens_clock(start, 15))

    def test_all_day_events_never_open_the_clock(self):
        holiday = wrap(event("UID:h", "DTSTART;VALUE=DATE:20261201", "SUMMARY:No school"))
        self.calendars.add_file("Notes", holiday, True)
        self.assertFalse(self.calendars.opens_clock(NOW, 15))

    def test_remove(self):
        source = self.calendars.add_file("Builds", ONE, False)
        self.calendars.remove(source)
        self.assertEqual(self.calendars.sources(), [])
        self.assertEqual(self.calendars.between(NOW - timedelta(days=1), NOW + timedelta(days=1)), [])


class PagesTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.calendars = calendars_for(self.h.services)
        self.calendars.clock = lambda: NOW
        self.calendars.fetch = lambda url: ONE
        self.coach = self.h.login(ADMIN)
        self.kim = self.h.login(MENTOR)
        self.ava = self.h.login(STUDENT)

    def tearDown(self):
        self.h.close()

    def test_agenda_and_month(self):
        self.assertIn("No calendars yet", self.ava.get("/calendar").text)
        self.calendars.add_file("Builds", ONE, True)
        page = self.ava.get("/calendar").text
        self.assertIn("Practice", page)
        self.assertIn("3:30 pm to 6:00 pm", page)
        self.assertIn("opens the time clock", page)
        month = self.ava.get("/calendar/month?month=2026-12").text
        self.assertIn("December 2026", month)
        self.assertIn('class="month-event opens-clock"', month)
        self.assertIn("November 2026", self.ava.get("/calendar/month?month=2026-11").text)

    def test_only_managers_manage(self):
        self.assertEqual(self.ava.get("/calendar/manage").status_code, 403)
        self.assertEqual(self.kim.get("/calendar/manage").status_code, 200)
        self.assertNotIn('href="/calendar/manage"', self.ava.get("/calendar").text)

    def test_adding_changing_and_removing(self):
        self.kim.post("/calendar/manage", data={"name": "Team", "url": "https://calendar.example/x.ics",
                                                "opens_clock": "1"})
        source = self.calendars.sources()[0]
        self.assertEqual((source["name"], source["events"], source["opens_clock"]), ("Team", 1, 1))
        self.kim.post(f"/calendar/manage/{source['id']}", data={"action": "closes"})
        self.assertEqual(self.calendars.source(source["id"])["opens_clock"], 0)
        upload = {"file": ("robotics.ics", ONE.encode(), "text/calendar")}
        self.kim.post("/calendar/manage", data={"name": ""}, files=upload)
        names = sorted(s["name"] for s in self.calendars.sources())
        self.assertEqual(names, ["Team", "robotics"])
        self.kim.post(f"/calendar/manage/{source['id']}", data={"action": "remove"})
        self.assertEqual([s["name"] for s in self.calendars.sources()], ["robotics"])
        audit = [r["action"] for r in self.h.services.db.all("SELECT action FROM audit_log WHERE action LIKE 'cal%'")]
        self.assertEqual(audit, ["calendar_added", "calendar_added", "calendar_removed"])

    def test_a_bad_upload(self):
        self.kim.post("/calendar/manage", files={"file": ("x.ics", b"not a calendar", "text/calendar")})
        self.assertEqual(self.calendars.sources(), [])

    def test_home_page_and_slack_home(self):
        self.calendars.add_file("Builds", ONE, False)
        self.assertIn("Practice", self.ava.get("/").text)
        module = next(m for m in installed() if m.key == "calendar")
        row = self.ava.user
        viewer = Viewer(row["id"], row["slack_user_id"], row["name"], "student")
        blocks = module.slack_home_blocks(self.h.services, viewer)
        self.assertIn("3:30 pm to 6:00 pm: Practice", str(blocks))


if __name__ == "__main__":
    unittest.main()
