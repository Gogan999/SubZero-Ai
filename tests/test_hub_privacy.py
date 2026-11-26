"""Keeping things private and pages friendly: secrets never stored or cached, calendar downloads that stay on the
internet, guests left out of imports, old previews cleared, odd dates and missing pages handled gently."""

from __future__ import annotations

import asyncio
import base64
import socket
import unittest
from datetime import timedelta
from unittest import mock

from starlette.requests import Request

from subzero_ai.hub.modules import installed, tick_all
from subzero_ai.modules.calendar import calendars_for, ics
from subzero_ai.modules.calendar.store import MAX_BYTES, check_public, download
from subzero_ai.store import Viewer
from subzero_ai.web import server_error

from .test_calendar import NOW, ONE, event, wrap
from .test_timeclock import ClockedHarness


def resolver(*addresses: str):
    def resolve(host, port, proto=0):
        family = socket.AF_INET6 if ":" in addresses[0] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, proto, "", (a, port)) for a in addresses]
    return resolve


PUBLIC = resolver("93.184.216.34")


class FakeResponse:
    def __init__(self, status=200, body=b"", location=""):
        self.status_code, self.body = status, body
        self.headers = {"location": location} if location else {}
        self.is_redirect = bool(location)
        self.encoding = "utf-8"
        self.raw = self

    def read(self, amount, decode_content=True):
        return self.body[:amount]

    def raise_for_status(self):
        pass

    def close(self):
        pass


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.urls = list(responses), []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


def session_text(cookie: str) -> str:
    """The readable part of a signed session cookie."""
    payload = cookie.split(".")[0]
    return base64.b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8", "replace")


class CalendarDownloadTest(unittest.TestCase):
    def test_only_public_https_addresses(self):
        check_public("https://calendar.example/x.ics", PUBLIC)
        for url, resolve in (("http://calendar.example/x.ics", PUBLIC),
                             ("https://calendar.example/x.ics", resolver("127.0.0.1")),
                             ("https://calendar.example/x.ics", resolver("10.0.0.5")),
                             ("https://calendar.example/x.ics", resolver("192.168.1.20")),
                             ("https://calendar.example/x.ics", resolver("169.254.169.254")),
                             ("https://calendar.example/x.ics", resolver("::1")),
                             ("https://calendar.example/x.ics", resolver("93.184.216.34", "10.0.0.5"))):
            with self.subTest(url=url, resolve=resolve("h", 443)[-1][-1][0]), self.assertRaises(ics.IcsError):
                check_public(url, resolve)

    def test_an_unknown_server(self):
        def fail(*args, **kwargs):
            raise socket.gaierror("no such host")
        with self.assertRaises(ics.IcsError):
            check_public("https://nowhere.example/x.ics", fail)

    def test_redirects_are_checked_too(self):
        hops = {"good.example": "93.184.216.34", "inside.example": "10.1.2.3"}

        def resolve(host, port, proto=0):
            return resolver(hops[host])(host, port, proto)

        session = FakeSession(FakeResponse(302, location="https://good.example/b.ics"), FakeResponse(body=b"ok"))
        self.assertEqual(download("https://good.example/a.ics", session, resolve), "ok")
        self.assertEqual(session.urls, ["https://good.example/a.ics", "https://good.example/b.ics"])
        session = FakeSession(FakeResponse(302, location="https://inside.example/secret"))
        with self.assertRaises(ics.IcsError):
            download("https://good.example/a.ics", session, resolve)
        self.assertEqual(session.urls, ["https://good.example/a.ics"])

    def test_too_many_redirects_and_too_big(self):
        loop = FakeSession(*[FakeResponse(302, location="/again") for _ in range(20)])
        with self.assertRaises(ics.IcsError):
            download("https://calendar.example/x.ics", loop, PUBLIC)
        big = FakeSession(FakeResponse(body=b"x" * (MAX_BYTES + 10)))
        with self.assertRaises(ics.IcsError):
            download("https://calendar.example/x.ics", big, PUBLIC)

    def test_events_that_repeat_every_minute_are_skipped(self):
        text = wrap(event("UID:spam", "DTSTART:20261201T150000Z", "RRULE:FREQ=MINUTELY", "SUMMARY:Spam"),
                    event("UID:ok", "DTSTART:20261201T160000Z", "SUMMARY:Practice"))
        found = ics.expand(text, NOW - timedelta(days=1), NOW + timedelta(days=30), NOW.tzinfo)
        self.assertEqual([o.uid for o in found], ["ok"])


class PrivacyTest(ClockedHarness):
    def test_a_new_key_is_shown_once_and_never_kept(self):
        resp = self.browse(self.ava, "POST", "/timeclock/phone/keys", data={"label": "iPhone"})
        self.assertEqual(resp.headers.get("cache-control"), "no-store")
        key = resp.text.split('class="mono copyable">')[1].split("<")[0]
        self.assertTrue(key.startswith("tc_"))
        self.assertNotIn(key, session_text(self.ava.cookie))
        again = self.browse(self.ava, "GET", "/timeclock/phone")
        self.assertNotIn(key, again.text)
        self.assertEqual(again.headers.get("cache-control"), "no-store", "no signed-in page is kept")
        self.assertIsNone(self.h.client.get("/static/app.css").headers.get("cache-control"))

    def test_a_pairing_link_is_shown_once_and_never_kept(self):
        resp = self.browse(self.kim, "POST", "/timeclock/admin/kiosks", data={"name": "Shop door"})
        self.assertEqual(resp.headers.get("cache-control"), "no-store")
        link = resp.text.split('class="mono copyable">')[1].split("<")[0]
        code = link.rsplit("/", 1)[1]
        self.assertNotIn(code, session_text(self.kim.cookie))
        self.assertNotIn(code, self.browse(self.kim, "GET", "/timeclock/admin").text)

    def test_slack_guests_are_not_matched_on_import(self):
        self.h.slack.users["UGUEST"] = {"profile": {"real_name": "Gus Guest"}, "is_restricted": True}
        self.h.slack.users["UMEM"] = {"profile": {"real_name": "Mia Member"}}
        csv_text = ("Name,Date,Clock In,Clock Out\nGus Guest,2026-11-30,3:30 PM,6:00 PM\n"
                    "Mia Member,2026-11-30,3:30 PM,6:00 PM\n")
        page = self.browse(self.kim, "POST", "/timeclock/import",
                           files={"file": ("export.csv", csv_text.encode(), "text/csv")}).text
        self.assertIn("1 of 2 entries are ready", page)
        self.assertNotIn("UGUEST", page)

    def test_old_import_previews_are_cleared(self):
        csv_text = "Name,Date,Clock In,Clock Out\nNobody,2026-11-30,3:30 PM,6:00 PM\n"
        self.browse(self.kim, "POST", "/timeclock/import", files={"file": ("x.csv", csv_text.encode(), "text/csv")})
        self.browse(self.kim, "POST", "/timeclock/import", files={"file": ("x.csv", csv_text.encode(), "text/csv")})
        previews = "SELECT COUNT(*) AS n FROM kv WHERE key LIKE 'timeclock.import.%'"
        self.assertEqual(self.h.services.db.one(previews)["n"], 1, "a new upload replaces the last preview")
        self.h.call(tick_all, self.h.services)
        self.assertEqual(self.h.services.db.one(previews)["n"], 1)
        self.now += timedelta(days=2)
        self.h.call(tick_all, self.h.services)
        self.assertEqual(self.h.services.db.one(previews)["n"], 0)

    def test_odd_dates_are_handled(self):
        for path in ("/timeclock/team?start=0001-01-01&end=9999-12-31", "/timeclock/team?start=nope",
                     "/timeclock/export.csv?start=0001-01-01", "/calendar/month?month=9999-12",
                     "/calendar/month?month=0001-01", "/calendar/month?month=junk"):
            with self.subTest(path=path):
                self.assertEqual(self.kim.get(path).status_code, 200)
        resp = self.browse(self.ava, "POST", "/timeclock/entries/new",
                           data={"start": "0001-01-01T10:00", "end": "0001-01-01T11:00", "note": ""})
        self.assertIn("Pick a date and time for both ends.", resp.text)
        self.assertEqual(self.h.services.db.one("SELECT COUNT(*) AS n FROM tc_entries")["n"], 0)

    def test_missing_pages(self):
        resp = self.ava.get("/no-such-page")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("Page not found", resp.text)
        self.assertIn("Back home", resp.text)
        signed_out = self.h.client.get("/no-such-page")
        self.assertEqual(signed_out.status_code, 404)
        api = self.h.client.get("/no-such-page", headers={"Accept": "application/json"})
        self.assertEqual(api.json(), {"ok": False, "error": "Not found."})

    def test_unexpected_errors_say_nothing_about_the_error(self):
        def request(method, accept):
            return Request({"type": "http", "method": method, "path": "/x", "root_path": "", "scheme": "http",
                            "query_string": b"", "server": ("localhost", 8787),
                            "headers": [(b"accept", accept.encode())]})

        problem = RuntimeError("database at C:\\ProgramData\\SubZeroAI\\app.db is locked")
        with mock.patch("subzero_ai.web.log") as log:
            page = asyncio.run(server_error(request("POST", "text/html,*/*"), problem))
        self.assertIs(log.error.call_args.kwargs["exc_info"], problem)
        self.assertEqual(page.status_code, 500)
        self.assertIn(b"Something went wrong", page.body)
        self.assertNotIn(b"ProgramData", page.body)
        with mock.patch("subzero_ai.web.log"):
            api = asyncio.run(server_error(request("POST", "application/json"), problem))
        self.assertEqual(api.media_type, "application/json")
        self.assertNotIn(b"ProgramData", api.body)

    def test_calendar_titles_cant_ping_slack(self):
        calendars = calendars_for(self.h.services)
        calendars.clock = lambda: NOW
        calendars.add_file("Builds", ONE.replace("SUMMARY:Practice", "SUMMARY:<!channel> Practice"), False)
        module = next(m for m in installed() if m.key == "calendar")
        row = self.ava.user
        blocks = str(module.slack_home_blocks(self.h.services, Viewer(row["id"], row["slack_user_id"], row["name"],
                                                                       "student")))
        self.assertIn("&lt;!channel&gt; Practice", blocks)
        self.assertNotIn("<!channel>", blocks)
