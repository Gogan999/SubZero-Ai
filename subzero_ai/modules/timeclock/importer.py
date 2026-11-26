"""Reads time from a CSV export into a preview to check before importing.

Two layouts work, found by their column headings anywhere in the first rows:
  - one row per shift: a person, a clock-in and a clock-out (and optionally a date, when those hold only times);
  - one row per punch, as Attendance Bot exports: a person, an action (In or Out) and a timestamp. Each In is
    paired with the same person's next Out.
People are matched by Slack ID, email or name against everyone who has signed in and the Slack workspace's members.
Times are read as local time.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from dateutil import parser as dateparser

from .store import TimeClock, iso

MAX_ROWS = 20000
LONG_HOURS = 12
HEADER_ROWS = 40
PERSON = (("slack id", "slack user id", "user id", "slack_id", "user_id"), ("email", "e-mail", "email address"),
          ("employee name", "name", "user", "username", "user name", "member", "employee", "person", "full name",
           "display name"))
IN = ("clock in", "clocked in", "in", "time in", "in time", "start", "start time", "check in", "punch in", "clock-in")
OUT = ("clock out", "clocked out", "out", "time out", "out time", "end", "end time", "check out", "punch out",
       "clock-out")
DATE = ("date", "day")
ACTION = ("action type", "action", "punch type", "type", "event")
WHEN = ("timestamp", "time stamp", "date time", "datetime", "time")
REMARK = ("remark", "remarks", "note", "notes", "comment")
SOURCE = ("source",)
IGNORED_REMARKS = {"day change"}


class CsvImportError(ValueError):
    """A file that can't be read, with a sentence to show."""


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


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


def _when(text: str, day: str = "") -> datetime | None:
    text = text.strip()
    if not text or text.upper() == "NA":
        return None
    try:
        moment = dateparser.parse(f"{day} {text}" if day else text, fuzzy=False)
    except (ValueError, OverflowError):
        return None
    return moment if 2000 <= moment.year <= 2100 else None


def _layout(rows: list[list[str]]) -> tuple[int, dict]:
    """The heading row's index and the columns it names."""
    for index, headings in enumerate(rows[:HEADER_ROWS]):
        person = [_find(headings, names) for names in PERSON]
        if all(c is None for c in person):
            continue
        cols = {"person": person, "remark": _find(headings, REMARK), "source": _find(headings, SOURCE),
                "headings": headings}
        action, when = _find(headings, ACTION), _find(headings, WHEN)
        if action is not None and when is not None:
            return index, {**cols, "kind": "punches", "action": action, "when": when}
        start, end = _find(headings, IN), _find(headings, OUT)
        if start is not None and end is not None:
            return index, {**cols, "kind": "shifts", "in": start, "out": end, "date": _find(headings, DATE)}
    raise CsvImportError("Couldn't find the columns for the person and the times. The headings need a person (Slack "
                         "ID, Email or Name) and either Clock in and Clock out, or Action Type and Timestamp.")


def _cell(cells: list[str], col: int | None) -> str:
    return cells[col].strip() if col is not None and col < len(cells) else ""


def _remarks(cells: list[str], cols: dict) -> str:
    return _note(_cell(cells, cols["source"]), _cell(cells, cols["remark"]))


def _note(*remarks: str) -> str:
    remarks = tuple(r.strip().strip('"').strip() for r in remarks)
    kept = [r for r in remarks if r and r.casefold() not in IGNORED_REMARKS]
    return "; ".join(dict.fromkeys(kept))[:300]


def _shifts(body: list[list[str]], cols: dict) -> list[dict]:
    """(who, start, end, note, problem) for each shift row."""
    found = []
    for cells in body:
        who = next((_cell(cells, c) for c in cols["person"] if _cell(cells, c)), "")
        day = _cell(cells, cols["date"])
        start, end = _when(_cell(cells, cols["in"]), day), _when(_cell(cells, cols["out"]), day)
        found.append({"who": who, "start": start, "end": end, "note": _remarks(cells, cols),
                      "problem": "" if start and end else "Couldn't read the times."})
    return found


def _punches(body: list[list[str]], cols: dict) -> list[dict]:
    """Pairs each person's In with their next Out, in file order."""
    found, waiting = [], {}
    for cells in body:
        action = _cell(cells, cols["action"]).casefold()
        if action not in ("in", "out"):
            continue
        who = next((_cell(cells, c) for c in cols["person"] if _cell(cells, c)), "")
        moment, remark = _when(_cell(cells, cols["when"])), _remarks(cells, cols)
        if moment is None:
            found.append({"who": who, "start": None, "end": None, "note": "", "problem": "Couldn't read the time."})
            continue
        key = normalize(who)
        if action == "in":
            if key in waiting:
                earlier = waiting.pop(key)
                found.append({**earlier, "end": None, "problem": "A clock-in with no clock-out."})
            waiting[key] = {"who": who, "start": moment, "note": remark}
        elif key in waiting:
            earlier = waiting.pop(key)
            found.append({"who": who, "start": earlier["start"], "end": moment,
                          "note": _note(earlier["note"], remark), "problem": ""})
        else:
            found.append({"who": who, "start": None, "end": moment, "note": "",
                          "problem": "A clock-out with no clock-in."})
    for earlier in waiting.values():
        found.append({**earlier, "end": None, "problem": "A clock-in with no clock-out."})
    return found


def parse_csv(data: bytes, clock: TimeClock, people: list[dict]) -> dict:
    """The preview. `people` are {"key", "name", "aliases"} for everyone a row can be matched to."""
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect, skipinitialspace=True))
    start_row, cols = _layout(rows)
    body = [r for r in rows[start_row + 1:start_row + 1 + MAX_ROWS] if any(c.strip() for c in r)]
    found = _punches(body, cols) if cols["kind"] == "punches" else _shifts(body, cols)
    if not found:
        raise CsvImportError("That file has no time entries.")
    by_alias = {}
    for person in people:
        for alias in person["aliases"]:
            by_alias.setdefault(normalize(alias), person["key"])
    result = []
    for row in found:
        key = by_alias.get(normalize(row["who"]), "")
        start, end, problem = row["start"], row["end"], row["problem"]
        entry = {"who": row["who"], "key": key, "note": row["note"], "problem": problem}
        if not problem and end <= start:
            entry["problem"] = "No time between the clock-in and the clock-out." if end == start \
                else "The clock-out is before the clock-in."
        if not entry["problem"]:
            hours = (end - start).total_seconds() / 3600
            entry.update(start=iso(clock.from_local(start.date(), start.time())),
                         end=iso(clock.from_local(end.date(), end.time())), hours=round(hours, 2),
                         shown=f"{start:%b} {start.day}, {start:%I:%M %p} to {end:%I:%M %p}".replace(" 0", " "),
                         long=hours > LONG_HOURS)
        elif start or end:
            moment = start or end
            entry["shown"] = f"{moment:%b} {moment.day}, {moment:%I:%M %p}".replace(" 0", " ")
        result.append(entry)
    headings = cols["headings"]
    used = [headings[c] for c in cols["person"] if c is not None][:1]
    used += [headings[cols[k]] for k in ("action", "when", "in", "out", "date") if cols.get(k) is not None]
    preview = {"columns": used, "rows": result,
               "options": sorted(([p["key"], p["name"]] for p in people), key=lambda o: o[1].casefold())}
    return summarize(preview)


def summarize(preview: dict) -> dict:
    """Recounts the per-person totals and what is ready, after rows were matched."""
    names = dict(map(tuple, preview["options"]))
    totals: dict[str, dict] = {}
    for row in preview["rows"]:
        person = totals.setdefault(row["who"], {"who": row["who"], "key": row["key"], "entries": 0, "hours": 0.0,
                                                "problems": 0, "long": 0})
        if row["problem"]:
            person["problems"] += 1
        else:
            person["entries"] += 1
            person["hours"] += row["hours"]
            person["long"] += int(row["long"])
    for person in totals.values():
        person["name"] = names.get(person["key"], "")
        person["hours"] = round(person["hours"], 2)
    preview["people"] = sorted(totals.values(), key=lambda p: p["who"].casefold())
    preview["ready"] = sum(1 for r in preview["rows"] if r["key"] and not r["problem"])
    preview["unmatched"] = [p["who"] for p in preview["people"] if not p["key"]]
    return preview


def match(preview: dict, who: str, key: str) -> dict:
    """Matches every row for a name in the file to one person (or to nobody, with an empty key)."""
    if key and key not in dict(map(tuple, preview["options"])):
        return preview
    for row in preview["rows"]:
        if row["who"] == who:
            row["key"] = key
    return summarize(preview)
