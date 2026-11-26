"""Calendar data: the calendars (an iCal address that syncs, or an uploaded file) and their events."""

from __future__ import annotations

import ipaddress
import re
import socket
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import certifi
import urllib3

from ...db import Database
from . import ics

SYNC_MINUTES = 15
PAST_DAYS = 30
FUTURE_DAYS = 180
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
REDIRECTS = frozenset({301, 302, 303, 307, 308})

MIGRATIONS = (
    # Version 1: calendars and their events
    """
    CREATE TABLE cal_sources (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('url', 'file')),
        url TEXT,
        opens_clock INTEGER NOT NULL DEFAULT 0,
        last_synced_at TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE cal_events (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES cal_sources(id) ON DELETE CASCADE,
        uid TEXT NOT NULL,
        starts_at TEXT NOT NULL,
        ends_at TEXT NOT NULL,
        all_day INTEGER NOT NULL,
        title TEXT NOT NULL,
        location TEXT NOT NULL,
        description TEXT NOT NULL
    );
    CREATE INDEX cal_events_time ON cal_events(starts_at, ends_at);
    """,
)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class Calendars:
    def __init__(self, db: Database, timezone_name: str, *, clock: Callable[[], datetime] | None = None,
                 fetch: Callable[[str], str] | None = None):
        self.db = db
        self.tz = ZoneInfo(timezone_name)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fetch = fetch or download

    def sources(self) -> list[sqlite3.Row]:
        return self.db.all("SELECT s.*, (SELECT COUNT(*) FROM cal_events e WHERE e.source_id = s.id) AS events "
                           "FROM cal_sources s ORDER BY s.name COLLATE NOCASE")

    def source(self, source_id: int) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM cal_sources WHERE id = ?", (source_id,))

    def add_url(self, name: str, url: str, opens_clock: bool) -> int:
        url = url.strip()
        if url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        if not url.lower().startswith("https://") or not urlsplit(url).hostname:
            raise ics.IcsError("Paste a calendar address that starts with https:// or webcal://.")
        cur = self.db.execute("INSERT INTO cal_sources (name, kind, url, opens_clock, created_at) VALUES "
                              "(?, 'url', ?, ?, ?)", (name.strip()[:80] or "Team calendar", url, int(opens_clock),
                                                     iso(self.clock())))
        return cur.lastrowid

    def add_file(self, name: str, text: str, opens_clock: bool) -> int:
        ics.read_events(text)
        cur = self.db.execute("INSERT INTO cal_sources (name, kind, opens_clock, created_at) VALUES "
                              "(?, 'file', ?, ?)", (name.strip()[:80] or "Uploaded calendar", int(opens_clock),
                                                    iso(self.clock())))
        self.load(cur.lastrowid, text)
        return cur.lastrowid

    def set_opens_clock(self, source_id: int, opens: bool) -> None:
        self.db.execute("UPDATE cal_sources SET opens_clock = ? WHERE id = ?", (int(opens), source_id))

    def remove(self, source_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM cal_events WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM cal_sources WHERE id = ?", (source_id,))

    def load(self, source_id: int, text: str) -> int:
        """Replaces a calendar's events with the file's, from PAST_DAYS ago to FUTURE_DAYS ahead."""
        now = self.clock()
        found = ics.expand(text, now - timedelta(days=PAST_DAYS), now + timedelta(days=FUTURE_DAYS), self.tz)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM cal_events WHERE source_id = ?", (source_id,))
            conn.executemany(
                "INSERT INTO cal_events (source_id, uid, starts_at, ends_at, all_day, title, location, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(source_id, o.uid, iso(o.start), iso(o.end), int(o.all_day), o.title[:200], o.location[:200],
                  o.description[:2000]) for o in found])
            conn.execute("UPDATE cal_sources SET last_synced_at = ?, last_error = NULL WHERE id = ?",
                         (iso(now), source_id))
        return len(found)

    def sync(self, source_id: int) -> str | None:
        """Downloads and loads an iCal address. Returns the problem, or None. A failure keeps the old events."""
        row = self.source(source_id)
        if row is None or row["kind"] != "url":
            return None
        try:
            self.load(source_id, self.fetch(row["url"]))
            return None
        except (OSError, urllib3.exceptions.HTTPError, ics.IcsError, ValueError) as exc:
            problem = str(exc) if isinstance(exc, ics.IcsError) else "Couldn't download the calendar."
            self.db.execute("UPDATE cal_sources SET last_synced_at = ?, last_error = ? WHERE id = ?",
                            (iso(self.clock()), problem, source_id))
            return problem

    def due(self) -> list[int]:
        """iCal addresses that haven't synced in SYNC_MINUTES."""
        cutoff = iso(self.clock() - timedelta(minutes=SYNC_MINUTES))
        return [r["id"] for r in self.db.all("SELECT id FROM cal_sources WHERE kind = 'url' AND "
                                             "(last_synced_at IS NULL OR last_synced_at <= ?)", (cutoff,))]

    def between(self, start: datetime, end: datetime, *, opens_clock: bool = False) -> list[sqlite3.Row]:
        """Events that overlap [start, end), with their calendar's name, by start time."""
        sql = ("SELECT e.*, s.name AS calendar, s.opens_clock FROM cal_events e JOIN cal_sources s "
               "ON s.id = e.source_id WHERE e.starts_at < ? AND e.ends_at > ?")
        if opens_clock:
            sql += " AND s.opens_clock = 1 AND e.all_day = 0"
        return self.db.all(sql + " ORDER BY e.starts_at, e.title", (iso(end), iso(start)))

    def opens_clock(self, moment: datetime, lead_minutes: int) -> bool:
        """True from lead_minutes before an event of a calendar that opens the time clock until it ends."""
        return bool(self.between(moment, moment + timedelta(minutes=lead_minutes) + timedelta(seconds=1),
                                 opens_clock=True))


def check_public(url: str, resolve: Callable = socket.getaddrinfo) -> str:
    """The address to connect to for an https url. Raises IcsError unless every address its host resolves to is on
    the internet."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ics.IcsError("Calendar addresses must start with https://.")
    try:
        found = resolve(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ics.IcsError("Couldn't find that calendar's server.") from exc
    addresses = [ipaddress.ip_address(sockaddr[0].split("%")[0]) for *_, sockaddr in found]
    if not addresses:
        raise ics.IcsError("Couldn't find that calendar's server.")
    if not all(a.is_global for a in addresses):
        raise ics.IcsError("That calendar address points to a private network, which the Hub doesn't fetch.")
    return str(addresses[0])


def open_pinned(url: str, address: str) -> urllib3.BaseHTTPResponse:
    """GETs an https url from the server at `address`, checking its certificate against the url's host name."""
    parts = urlsplit(url)
    host = parts.hostname.encode("idna").decode("ascii")
    pool = urllib3.HTTPSConnectionPool(address, parts.port or 443, server_hostname=host, assert_hostname=host,
                                       cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), maxsize=1,
                                       timeout=urllib3.Timeout(connect=10, read=20), retries=False)
    return pool.urlopen("GET", urlunsplit(("", "", parts.path or "/", parts.query, "")), redirect=False,
                        preload_content=False, decode_content=False,
                        headers={"Host": host if parts.port in (None, 443) else f"{host}:{parts.port}",
                                 "User-Agent": "SubZeroHub-Calendar/1.0", "Accept-Encoding": "identity"})


def download(url: str, opener: Callable = open_pinned, resolve: Callable = socket.getaddrinfo) -> str:
    """Fetches an iCal address, up to MAX_BYTES, uncompressed. Each hop connects only to the address check_public
    approved."""
    for _ in range(MAX_REDIRECTS + 1):
        resp = opener(url, check_public(url, resolve))
        try:
            location = resp.headers.get("location")
            if resp.status in REDIRECTS and location:
                url = urljoin(url, location)
                continue
            if resp.status >= 400:
                raise ics.IcsError(f"The calendar's server said no (error {resp.status}).")
            data = resp.read(MAX_BYTES + 1, decode_content=False)
        finally:
            resp.close()
        if len(data) > MAX_BYTES:
            raise ics.IcsError("That calendar is larger than 5 MB.")
        charset = re.search(r"charset=\"?([\w.:-]+)", resp.headers.get("content-type", ""))
        try:
            return data.decode(charset.group(1) if charset else "utf-8", errors="replace")
        except LookupError:
            return data.decode("utf-8", errors="replace")
    raise ics.IcsError("That calendar address redirects too many times.")
