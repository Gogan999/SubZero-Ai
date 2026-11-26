"""Time clock data: entries, their edit log, personal keys for Shortcuts, and kiosks."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ...db import Database
from . import rules

SETTINGS_KEY = "timeclock.settings"
MAX_ENTRY = timedelta(hours=24)
KIOSK_SLOT_SECONDS = 30
PAIR_MINUTES = 30

MIGRATIONS = (
    # Version 1: entries, their edit log, personal keys and kiosks
    """
    CREATE TABLE tc_entries (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        clock_in TEXT NOT NULL,
        clock_out TEXT,
        in_source TEXT NOT NULL,
        out_source TEXT,
        flag TEXT,
        note TEXT,
        last_seen TEXT,
        next_reminder TEXT,
        leave_at TEXT,
        import_batch TEXT,
        deleted INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX tc_one_open ON tc_entries(user_id) WHERE clock_out IS NULL AND deleted = 0;
    CREATE INDEX tc_entries_user_in ON tc_entries(user_id, clock_in);

    CREATE TABLE tc_edits (
        id INTEGER PRIMARY KEY,
        entry_id INTEGER NOT NULL REFERENCES tc_entries(id),
        editor_id INTEGER REFERENCES users(id),
        action TEXT NOT NULL,
        before TEXT,
        after TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE tc_tokens (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        token_hash TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        revoked_at TEXT
    );

    CREATE TABLE tc_kiosks (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        secret TEXT NOT NULL,
        pair_hash TEXT,
        pair_expires TEXT,
        created_at TEXT NOT NULL,
        last_seen_at TEXT,
        revoked_at TEXT
    );
    """,
)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Outcome:
    ok: bool
    message: str
    entry_id: int | None = None
    action: str = ""  # "in", "out", or "" when nothing changed


class EntryError(ValueError):
    """An entry change that isn't allowed, with a sentence to show."""


class TimeClock:
    def __init__(self, db: Database, timezone_name: str, *, clock: Callable[[], datetime] | None = None,
                 events_open: Callable[[datetime], bool] | None = None):
        self.db = db
        self.tz = ZoneInfo(timezone_name)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.events_open = events_open or (lambda moment: False)

    # ------------------------------------------------------------------ settings and time
    def settings(self) -> rules.Settings:
        raw = self.db.get_kv(SETTINGS_KEY)
        return rules.Settings.from_dict(json.loads(raw) if raw else None)

    def save_settings(self, settings: rules.Settings, *, actor_id: int | None) -> None:
        self.db.set_kv(SETTINGS_KEY, json.dumps(settings.to_dict()))
        self.db.audit("timeclock_settings_changed", actor_id=actor_id, details=settings.to_dict())

    def now(self) -> datetime:
        return self.clock()

    def local(self, moment: datetime | str) -> datetime:
        if isinstance(moment, str):
            moment = parse_iso(moment)
        return moment.astimezone(self.tz)

    def from_local(self, day: date, at: time) -> datetime:
        return datetime.combine(day, at, tzinfo=self.tz).astimezone(timezone.utc)

    def window_open(self, moment: datetime | None = None) -> bool:
        moment = moment or self.now()
        return rules.window_open(self.settings(), self.local(moment)) or self.events_open(moment)

    def closed_message(self) -> str:
        now_local = self.local(self.now())
        return rules.opening_text(rules.next_opening(self.settings(), now_local), now_local.date())

    # ------------------------------------------------------------------ reading
    def entry(self, entry_id: int) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM tc_entries WHERE id = ? AND deleted = 0", (entry_id,))

    def open_entry(self, user_id: int) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM tc_entries WHERE user_id = ? AND clock_out IS NULL AND deleted = 0",
                           (user_id,))

    def entries(self, user_id: int, start: datetime, end: datetime) -> list[sqlite3.Row]:
        """A person's entries that started in [start, end), newest first."""
        return self.db.all("SELECT * FROM tc_entries WHERE user_id = ? AND deleted = 0 AND clock_in >= ? "
                           "AND clock_in < ? ORDER BY clock_in DESC", (user_id, iso(start), iso(end)))

    def seconds(self, row: sqlite3.Row) -> float:
        end = parse_iso(row["clock_out"]) if row["clock_out"] else self.now()
        return max((end - parse_iso(row["clock_in"])).total_seconds(), 0)

    def totals(self, start: datetime, end: datetime, user_id: int | None = None) -> dict[int, float]:
        """Seconds per person for entries that started in [start, end)."""
        sql = "SELECT * FROM tc_entries WHERE deleted = 0 AND clock_in >= ? AND clock_in < ?"
        params: tuple = (iso(start), iso(end))
        if user_id is not None:
            sql += " AND user_id = ?"
            params += (user_id,)
        result: dict[int, float] = {}
        for row in self.db.all(sql, params):
            result[row["user_id"]] = result.get(row["user_id"], 0) + self.seconds(row)
        return result

    def who_is_in(self) -> list[sqlite3.Row]:
        return self.db.all("SELECT e.*, u.name FROM tc_entries e JOIN users u ON u.id = e.user_id "
                           "WHERE e.clock_out IS NULL AND e.deleted = 0 ORDER BY u.name COLLATE NOCASE")

    def flagged(self) -> list[sqlite3.Row]:
        return self.db.all("SELECT e.*, u.name FROM tc_entries e JOIN users u ON u.id = e.user_id "
                           "WHERE e.flag IS NOT NULL AND e.deleted = 0 ORDER BY e.clock_in DESC")

    def edited_ids(self, entry_ids: list[int]) -> set[int]:
        """Which of these entries were changed after they were recorded."""
        if not entry_ids:
            return set()
        marks = ",".join("?" * len(entry_ids))
        return {r["entry_id"] for r in self.db.all(
            f"SELECT DISTINCT entry_id FROM tc_edits WHERE action = 'edit' AND entry_id IN ({marks})",
            tuple(entry_ids))}

    def week_start(self) -> datetime:
        today = self.local(self.now()).date()
        return self.from_local(today - timedelta(days=today.weekday()), time())

    def summary(self, user_id: int) -> dict[str, float]:
        """Seconds this week, in the last 30 days and this season."""
        now = self.now()
        today = self.local(now).date()
        season = self.from_local(rules.season_start(self.settings(), today), time())
        end = now + timedelta(days=1)
        return {
            "week": self.totals(self.week_start(), end, user_id).get(user_id, 0),
            "month": self.totals(now - timedelta(days=30), end, user_id).get(user_id, 0),
            "season": self.totals(season, end, user_id).get(user_id, 0),
        }

    # ------------------------------------------------------------------ clocking in and out
    def clock_in(self, user_id: int, source: str, *, any_time: bool = False) -> Outcome:
        now = self.now()
        current = self.open_entry(user_id)
        if current is not None:
            if current["leave_at"]:
                self.db.execute("UPDATE tc_entries SET leave_at = NULL, last_seen = ?, updated_at = ? WHERE id = ?",
                                (iso(now), iso(now), current["id"]))
            since = rules.when_text(self.local(current["clock_in"]), self.local(now).date())
            return Outcome(True, f"You've been clocked in since {since}.", current["id"])
        if not any_time and not self.window_open(now):
            return Outcome(False, self.closed_message())
        hours = self.settings().reminder_hours
        try:
            with self.db.transaction() as conn:
                cur = conn.execute(
                    "INSERT INTO tc_entries (user_id, clock_in, in_source, last_seen, next_reminder, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, iso(now), source, iso(now), iso(now + timedelta(hours=hours)), iso(now), iso(now)))
                entry_id = cur.lastrowid
        except sqlite3.IntegrityError:
            current = self.open_entry(user_id)
            return Outcome(True, "You're already clocked in.", current["id"] if current else None)
        return Outcome(True, f"Clocked in at {rules.clock_text(self.local(now))}.", entry_id, "in")

    def clock_out(self, user_id: int, source: str, *, at: datetime | None = None, flag: str | None = None) -> Outcome:
        now = self.now()
        current = self.open_entry(user_id)
        if current is None:
            return Outcome(True, "You're not clocked in.")
        out = max(at or now, parse_iso(current["clock_in"]))
        self.db.execute("UPDATE tc_entries SET clock_out = ?, out_source = ?, flag = ?, leave_at = NULL, "
                        "next_reminder = NULL, updated_at = ? WHERE id = ?",
                        (iso(out), source, flag, iso(now), current["id"]))
        total = rules.duration_text((out - parse_iso(current["clock_in"])).total_seconds())
        message = f"Clocked out at {rules.clock_text(self.local(out))}. That's {total}."
        return Outcome(True, message, current["id"], "out")

    def toggle(self, user_id: int, source: str, *, any_time: bool = False) -> Outcome:
        if self.open_entry(user_id) is None:
            return self.clock_in(user_id, source, any_time=any_time)
        return self.clock_out(user_id, source)

    def left(self, user_id: int) -> Outcome:
        """A Shortcuts "Leave" event: the entry closes at this time once the grace period passes."""
        current = self.open_entry(user_id)
        if current is None:
            return Outcome(True, "Not clocked in.")
        if not current["leave_at"]:
            now = self.now()
            self.db.execute("UPDATE tc_entries SET leave_at = ?, updated_at = ? WHERE id = ?",
                            (iso(now), iso(now), current["id"]))
        return Outcome(True, "Leaving noted.", current["id"])

    def still_here(self, entry_id: int) -> None:
        now = self.now()
        repeat = self.settings().reminder_repeat_hours
        self.db.execute("UPDATE tc_entries SET last_seen = ?, next_reminder = ?, updated_at = ? "
                        "WHERE id = ? AND clock_out IS NULL", (iso(now), iso(now + timedelta(hours=repeat)),
                                                                iso(now), entry_id))

    # ------------------------------------------------------------------ minute jobs
    def reminders_due(self) -> list[sqlite3.Row]:
        """Open entries whose reminder time has come; each is returned once."""
        now = iso(self.now())
        rows = self.db.all("SELECT e.*, u.slack_user_id, u.name FROM tc_entries e JOIN users u ON u.id = e.user_id "
                           "WHERE e.clock_out IS NULL AND e.deleted = 0 AND e.next_reminder IS NOT NULL "
                           "AND e.next_reminder <= ?", (now,))
        for row in rows:
            self.db.execute("UPDATE tc_entries SET next_reminder = NULL WHERE id = ?", (row["id"],))
        return rows

    def close_departed(self) -> list[int]:
        """Closes entries whose Leave event is older than the grace period, at the time they left."""
        cutoff = self.now() - timedelta(minutes=self.settings().leave_grace_minutes)
        closed = []
        for row in self.db.all("SELECT * FROM tc_entries WHERE clock_out IS NULL AND deleted = 0 "
                               "AND leave_at IS NOT NULL AND leave_at <= ?", (iso(cutoff),)):
            self.clock_out(row["user_id"], "shortcut_leave", at=parse_iso(row["leave_at"]))
            closed.append(row["id"])
        return closed

    def auto_close(self) -> list[sqlite3.Row]:
        """Once a night, after the nightly time: closes entries opened before it at the last time each person was
        seen, flagged for a fix. Returns the entries it closed."""
        now_local = self.local(self.now())
        at = rules.parse_time(self.settings().auto_close)
        today = now_local.date().isoformat()
        if at is None or now_local.time() < at or self.db.get_kv("timeclock.auto_closed_on") == today:
            return []
        self.db.set_kv("timeclock.auto_closed_on", today)
        cutoff = iso(self.from_local(now_local.date(), at))
        rows = self.db.all("SELECT e.*, u.slack_user_id, u.name FROM tc_entries e JOIN users u ON u.id = e.user_id "
                           "WHERE e.clock_out IS NULL AND e.deleted = 0 AND e.clock_in < ?", (cutoff,))
        for row in rows:
            seen = max(parse_iso(row["clock_in"]), parse_iso(row["last_seen"] or row["clock_in"]))
            self.clock_out(row["user_id"], "auto_close", at=seen, flag="auto_closed")
        return rows

    # ------------------------------------------------------------------ editing
    def _check(self, user_id: int, start: datetime, end: datetime | None, skip_id: int | None) -> None:
        now = self.now()
        if start > now + timedelta(minutes=1):
            raise EntryError("The start time is in the future.")
        if end is not None:
            if end <= start:
                raise EntryError("The end time must be after the start time.")
            if end > now + timedelta(minutes=1):
                raise EntryError("The end time is in the future.")
            if end - start > MAX_ENTRY:
                raise EntryError("An entry can be at most 24 hours long.")
        overlap = self.db.one(
            "SELECT id FROM tc_entries WHERE user_id = ? AND deleted = 0 AND id != ? AND clock_in < ? "
            "AND COALESCE(clock_out, ?) > ?",
            (user_id, skip_id or 0, iso(end or now), iso(now), iso(start)))
        if overlap is not None:
            raise EntryError("That overlaps another entry.")

    def _log(self, conn: sqlite3.Connection, entry_id: int, editor_id: int | None, action: str,
             before: dict | None, after: dict | None) -> None:
        conn.execute("INSERT INTO tc_edits (entry_id, editor_id, action, before, after, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (entry_id, editor_id, action,
                                                   json.dumps(before) if before else None,
                                                   json.dumps(after) if after else None, iso(self.now())))

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict:
        return {"clock_in": row["clock_in"], "clock_out": row["clock_out"], "note": row["note"]}

    def add_entry(self, user_id: int, start: datetime, end: datetime, note: str, *, editor_id: int | None,
                  source: str = "edit", batch: str | None = None) -> int:
        self._check(user_id, start, end, None)
        now = iso(self.now())
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO tc_entries (user_id, clock_in, clock_out, in_source, out_source, note, import_batch, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, iso(start), iso(end), source, source, note or None, batch, now, now))
            self._log(conn, cur.lastrowid, editor_id, "import" if batch else "add", None,
                      {"clock_in": iso(start), "clock_out": iso(end), "note": note or None})
            return cur.lastrowid

    def edit_entry(self, entry_id: int, start: datetime, end: datetime | None, note: str, *,
                   editor_id: int | None) -> None:
        row = self.entry(entry_id)
        if row is None:
            raise EntryError("That entry doesn't exist.")
        if end is None and row["clock_out"] is not None:
            raise EntryError("Give the entry an end time.")
        self._check(row["user_id"], start, end, entry_id)
        after = {"clock_in": iso(start), "clock_out": iso(end) if end else None, "note": note or None}
        with self.db.transaction() as conn:
            closing = row["clock_out"] is None and end is not None
            conn.execute("UPDATE tc_entries SET clock_in = ?, clock_out = ?, note = ?, flag = NULL, out_source = ?, "
                         "leave_at = CASE WHEN ? THEN NULL ELSE leave_at END, "
                         "next_reminder = CASE WHEN ? THEN NULL ELSE next_reminder END, updated_at = ? WHERE id = ?",
                         (after["clock_in"], after["clock_out"], after["note"],
                          "edit" if closing else row["out_source"], closing, closing, iso(self.now()), entry_id))
            self._log(conn, entry_id, editor_id, "edit", self._snapshot(row), after)

    def delete_entry(self, entry_id: int, *, editor_id: int | None) -> None:
        row = self.entry(entry_id)
        if row is None:
            raise EntryError("That entry doesn't exist.")
        with self.db.transaction() as conn:
            conn.execute("UPDATE tc_entries SET deleted = 1, updated_at = ? WHERE id = ?", (iso(self.now()), entry_id))
            self._log(conn, entry_id, editor_id, "delete", self._snapshot(row), None)

    def history(self, entry_id: int) -> list[sqlite3.Row]:
        return self.db.all("SELECT ed.*, u.name AS editor FROM tc_edits ed LEFT JOIN users u ON u.id = ed.editor_id "
                           "WHERE ed.entry_id = ? ORDER BY ed.id", (entry_id,))

    def undo_import(self, batch: str, *, editor_id: int | None) -> int:
        rows = self.db.all("SELECT id FROM tc_entries WHERE import_batch = ? AND deleted = 0", (batch,))
        for row in rows:
            self.delete_entry(row["id"], editor_id=editor_id)
        return len(rows)

    # ------------------------------------------------------------------ personal keys
    def create_token(self, user_id: int, label: str) -> str:
        token = "tc_" + secrets.token_urlsafe(24)
        self.db.execute("INSERT INTO tc_tokens (user_id, token_hash, label, created_at) VALUES (?, ?, ?, ?)",
                        (user_id, digest(token), label.strip()[:60] or "Phone", iso(self.now())))
        return token

    def tokens(self, user_id: int) -> list[sqlite3.Row]:
        return self.db.all("SELECT * FROM tc_tokens WHERE user_id = ? AND revoked_at IS NULL ORDER BY id", (user_id,))

    def revoke_token(self, user_id: int, token_id: int) -> bool:
        cur = self.db.execute("UPDATE tc_tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                              (iso(self.now()), token_id, user_id))
        return cur.rowcount > 0

    def user_for_token(self, token: str) -> int | None:
        row = self.db.one("SELECT * FROM tc_tokens WHERE token_hash = ? AND revoked_at IS NULL", (digest(token),))
        if row is None:
            return None
        self.db.execute("UPDATE tc_tokens SET last_used_at = ? WHERE id = ?", (iso(self.now()), row["id"]))
        return row["user_id"]

    # ------------------------------------------------------------------ kiosks
    def kiosks(self) -> list[sqlite3.Row]:
        return self.db.all("SELECT * FROM tc_kiosks WHERE revoked_at IS NULL ORDER BY id")

    def kiosk(self, kiosk_id: int) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM tc_kiosks WHERE id = ? AND revoked_at IS NULL", (kiosk_id,))

    def create_kiosk(self, name: str) -> tuple[int, str]:
        """A new kiosk and its one-time pairing code."""
        now = self.now()
        code = secrets.token_urlsafe(16)
        cur = self.db.execute(
            "INSERT INTO tc_kiosks (name, secret, pair_hash, pair_expires, created_at) VALUES (?, ?, ?, ?, ?)",
            (name.strip()[:60] or "Shop door", secrets.token_hex(32), digest(code),
             iso(now + timedelta(minutes=PAIR_MINUTES)), iso(now)))
        return cur.lastrowid, code

    def new_pairing_code(self, kiosk_id: int) -> str:
        code = secrets.token_urlsafe(16)
        self.db.execute("UPDATE tc_kiosks SET pair_hash = ?, pair_expires = ? WHERE id = ?",
                        (digest(code), iso(self.now() + timedelta(minutes=PAIR_MINUTES)), kiosk_id))
        return code

    def pair(self, code: str) -> int | None:
        """The kiosk a pairing code belongs to; each code works once, before it expires."""
        row = self.db.one("SELECT * FROM tc_kiosks WHERE pair_hash = ? AND revoked_at IS NULL", (digest(code),))
        if row is None or row["pair_expires"] < iso(self.now()):
            return None
        self.db.execute("UPDATE tc_kiosks SET pair_hash = NULL, pair_expires = NULL WHERE id = ?", (row["id"],))
        return row["id"]

    def revoke_kiosk(self, kiosk_id: int) -> None:
        self.db.execute("UPDATE tc_kiosks SET revoked_at = ? WHERE id = ?", (iso(self.now()), kiosk_id))

    def kiosk_seen(self, kiosk_id: int) -> None:
        self.db.execute("UPDATE tc_kiosks SET last_seen_at = ? WHERE id = ?", (iso(self.now()), kiosk_id))

    def kiosk_code(self, kiosk: sqlite3.Row, slot: int | None = None) -> str:
        """The code in the kiosk's QR link for a 30-second slot (the current one by default)."""
        if slot is None:
            slot = int(self.now().timestamp()) // KIOSK_SLOT_SECONDS
        return hmac.new(kiosk["secret"].encode(), str(slot).encode(), hashlib.sha256).hexdigest()[:16]

    def kiosk_code_ok(self, kiosk: sqlite3.Row, code: str) -> bool:
        """True for the current or the previous slot's code."""
        slot = int(self.now().timestamp()) // KIOSK_SLOT_SECONDS
        return any(hmac.compare_digest(self.kiosk_code(kiosk, s), code) for s in (slot, slot - 1))
