"""Reads time entries from a CSV export (such as Attendance Bot's) into a preview to confirm before importing.

Columns are found by their headings: a person (Slack ID, email or name), a clock-in and a clock-out, and optionally a
date when the in and out columns hold only times. Times are read as local time.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from dateutil import parser as dateparser

from ...db import Database
from .store import TimeClock, iso

MAX_ROWS = 20000
PERSON = (("slack id", "slack user id", "user id", "slack_id", "user_id"), ("email", "e-mail", "email address"),
          ("name", "user", "username", "user name", "member", "employee", "person", "full name", "display name"))
IN = ("clock in", "clocked in", "in", "time in", "in time", "start", "start time", "check in", "punch in", "clock-in")
OUT = ("clock out", "clocked out", "out", "time out", "out time", "end", "end time", "check out", "punch out",
       "clock-out")
DATE = ("date", "day")


class CsvImportError(ValueError):
    """A file that can't be read, with a sentence to show."""


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvImportError("That file isn't text. Export the time sheet as CSV and try again.")


def _find(headings: list[str], names: tuple[str, ...]) -> int | None:
    lowered = [h.strip().lower() for h in headings]
    for name in names:
        if name in lowered:
            return lowered.index(name)
    return None


def _when(text: str, day: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    try:
        return dateparser.parse(f"{day} {text}" if day else text, fuzzy=False)
    except (ValueError, OverflowError):
        return None


def parse_csv(data: bytes, clock: TimeClock, db: Database) -> dict:
    """The preview: the columns used, each row with its person and times, and the people it couldn't match."""
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = list(csv.reader(io.StringIO(text), dialect))
    if len(reader) < 2:
        raise CsvImportError("That file has no rows.")
    headings, body = reader[0], reader[1:MAX_ROWS + 1]
    person_cols = [_find(headings, names) for names in PERSON]
    in_col, out_col, date_col = _find(headings, IN), _find(headings, OUT), _find(headings, DATE)
    if all(c is None for c in person_cols) or in_col is None or out_col is None:
        raise CsvImportError("Couldn't find the columns for the person, the clock-in and the clock-out. The first row "
                             "needs headings like Name or Email, Clock in and Clock out.")
    people = db.all("SELECT id, name, email, slack_user_id FROM users")
    by_key = {}
    for p in people:
        for key in (p["slack_user_id"], (p["email"] or "").lower(), p["name"].lower()):
            if key:
                by_key.setdefault(key, p)
    rows, unmatched = [], set()
    for line in body:
        if not any(cell.strip() for cell in line):
            continue
        cells = line + [""] * (len(headings) - len(line))
        who = next((cells[c].strip() for c in person_cols if c is not None and cells[c].strip()), "")
        match = next((by_key[k] for k in (who, who.lower()) if k in by_key), None)
        day = cells[date_col] if date_col is not None else ""
        start, end = _when(cells[in_col], day), _when(cells[out_col], day)
        problem = ""
        if match is None:
            problem = "No one with this name, email or Slack ID has signed in."
            unmatched.add(who or "(blank)")
        elif start is None or end is None:
            problem = "Couldn't read the times."
        elif end <= start:
            problem = "The clock-out is before the clock-in."
        row = {"who": who, "user_id": match["id"] if match and not problem else None,
               "name": match["name"] if match else "", "problem": problem}
        if start is not None and end is not None and not problem:
            start_utc, end_utc = clock.from_local(start.date(), start.time()), clock.from_local(end.date(), end.time())
            row.update(start=iso(start_utc), end=iso(end_utc), shown=f"{start:%Y-%m-%d %H:%M} to {end:%H:%M}",
                       hours=f"{(end - start).total_seconds() / 3600:.1f}")
        rows.append(row)
    return {"columns": {"person": next((headings[c] for c in person_cols if c is not None), ""),
                        "in": headings[in_col], "out": headings[out_col],
                        "date": headings[date_col] if date_col is not None else ""},
            "rows": rows, "ready": sum(1 for r in rows if r["user_id"]), "unmatched": sorted(unmatched)}
