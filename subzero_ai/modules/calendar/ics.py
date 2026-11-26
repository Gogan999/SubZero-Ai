"""Reads iCalendar (.ics) files, as Google, Outlook and Apple calendars export them, into event occurrences.

Repeating events are expanded between two times with dateutil's RRULE support. EXDATE removes occurrences,
RECURRENCE-ID replaces one, and STATUS:CANCELLED drops it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

MAX_PER_EVENT = 1000
WINDOWS_ZONES = {
    "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles", "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu", "UTC": "UTC", "GMT Standard Time": "Europe/London",
}
DURATION_RE = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


class IcsError(ValueError):
    """A file that isn't a calendar, with a sentence to show."""


@dataclass
class Occurrence:
    uid: str
    start: datetime  # aware
    end: datetime  # aware
    all_day: bool
    title: str
    location: str
    description: str


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def split_property(line: str) -> tuple[str, dict[str, str], str]:
    """NAME;PARAM=VALUE:text into (NAME, params, text), with quoted parameter values."""
    name_end = len(line)
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            name_end = index
            break
    head, value = line[:name_end], line[name_end + 1:]
    parts = re.findall(r'(?:[^;"]|"[^"]*")+', head)
    params = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        params[key.upper()] = val.strip('"')
    return parts[0].upper() if parts else "", params, value


def unescape(text: str) -> str:
    return re.sub(r"\\([nN\\;,])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), text)


def zone(tzid: str | None, default: ZoneInfo) -> ZoneInfo | timezone:
    if not tzid:
        return default
    name = WINDOWS_ZONES.get(tzid, tzid)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return default


def parse_when(value: str, params: dict[str, str], default: ZoneInfo) -> tuple[datetime | date, bool]:
    """A DTSTART-style value as (aware datetime, False) or (date, True) for all-day values."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date(), True
    utc = value.endswith("Z")
    naive = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return naive.replace(tzinfo=timezone.utc), False
    return naive.replace(tzinfo=zone(params.get("TZID"), default)), False


def parse_duration(value: str) -> timedelta | None:
    match = DURATION_RE.fullmatch(value.strip())
    if match is None:
        return None
    sign, weeks, days, hours, minutes, seconds = match.groups()
    delta = timedelta(weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0),
                      minutes=int(minutes or 0), seconds=int(seconds or 0))
    return -delta if sign == "-" else delta


def read_events(text: str) -> list[dict]:
    """The VEVENT blocks as lists of (name, params, value)."""
    if "BEGIN:VCALENDAR" not in text.upper():
        raise IcsError("That isn't a calendar file. Export the calendar as .ics and try again.")
    events, current, depth = [], None, 0
    for line in unfold(text):
        name, params, value = split_property(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            current, depth = [], 0
        elif current is not None and name == "BEGIN":
            depth += 1
        elif current is not None and name == "END" and value.upper() == "VEVENT" and depth == 0:
            events.append(current)
            current = None
        elif current is not None and name == "END":
            depth -= 1
        elif current is not None and depth == 0:
            current.append((name, params, value))
    return events


def _first(props: list, name: str):
    return next(((p, v) for n, p, v in props if n == name), (None, None))


def _all(props: list, name: str) -> list:
    return [(p, v) for n, p, v in props if n == name]


def _key(moment: datetime | date) -> str:
    return moment.isoformat() if isinstance(moment, date) and not isinstance(moment, datetime) \
        else moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _as_aware(moment: datetime | date, all_day: bool, default: ZoneInfo) -> datetime:
    if all_day:
        return datetime.combine(moment, time(), tzinfo=default)
    return moment


def expand(text: str, start: datetime, end: datetime, default: ZoneInfo) -> list[Occurrence]:
    """Every occurrence that overlaps [start, end), sorted by start."""
    overrides: dict[tuple[str, str], list] = {}
    masters = []
    for props in read_events(text):
        uid = _first(props, "UID")[1] or ""
        rid_params, rid = _first(props, "RECURRENCE-ID")
        if rid:
            when, _ = parse_when(rid, rid_params, default)
            overrides[(uid, _key(when))] = props
        else:
            masters.append(props)
    found: list[Occurrence] = []
    for props in masters:
        found += _occurrences(props, overrides, start, end, default)
    for (uid, _), props in overrides.items():
        if not any(_first(m, "UID")[1] == uid for m in masters):
            found += _occurrences(props, {}, start, end, default)
    return sorted(found, key=lambda o: o.start)


def _occurrences(props: list, overrides: dict, start: datetime, end: datetime, default: ZoneInfo) -> list[Occurrence]:
    dt_params, dt_value = _first(props, "DTSTART")
    if not dt_value:
        return []
    uid = _first(props, "UID")[1] or ""
    first, all_day = parse_when(dt_value, dt_params, default)
    end_params, end_value = _first(props, "DTEND")
    if end_value:
        last, _ = parse_when(end_value, end_params, default)
        length = (_as_aware(last, all_day, default) - _as_aware(first, all_day, default))
    else:
        duration = parse_duration(_first(props, "DURATION")[1] or "")
        length = duration if duration is not None else (timedelta(days=1) if all_day else timedelta())
    rule = _first(props, "RRULE")[1]
    if rule and re.search(r"FREQ=(SECONDLY|MINUTELY)", rule, re.IGNORECASE):
        return []
    if rule:
        dtstart = datetime.combine(first, time()) if all_day else first
        if not all_day:
            rule = re.sub(r"UNTIL=(\d{8})(?=;|$)", r"UNTIL=\1T235959Z", rule)
            rule = re.sub(r"UNTIL=(\d{8}T\d{6})(?=;|$)", r"UNTIL=\1Z", rule)
        else:
            rule = re.sub(r"UNTIL=(\d{8})T\d{6}Z?", r"UNTIL=\1", rule)
        low, high = start - length - timedelta(days=1), end
        if all_day:
            low, high = low.astimezone(default).replace(tzinfo=None), high.astimezone(default).replace(tzinfo=None)
        try:
            moments = rrulestr(f"RRULE:{rule}", dtstart=dtstart).between(low, high, inc=True)
            starts = [m.date() if all_day else m for m in moments[:MAX_PER_EVENT]]
        except (ValueError, TypeError):
            starts = [first]
    else:
        starts = [first]
    skipped = set()
    for ex_params, ex_value in _all(props, "EXDATE"):
        for part in ex_value.split(","):
            if part.strip():
                skipped.add(_key(parse_when(part, ex_params, default)[0]))
    result = []
    for begin in starts:
        key = _key(begin)
        if key in skipped:
            continue
        source = overrides.get((uid, key), props)
        if (_first(source, "STATUS")[1] or "").upper() == "CANCELLED":
            continue
        if source is not props:
            o_params, o_value = _first(source, "DTSTART")
            begin_value, _ = parse_when(o_value, o_params, default) if o_value else (begin, all_day)
            oe_params, oe_value = _first(source, "DTEND")
            o_start = _as_aware(begin_value, all_day, default)
            o_end = _as_aware(parse_when(oe_value, oe_params, default)[0], all_day, default) if oe_value \
                else o_start + length
        else:
            o_start = _as_aware(begin, all_day, default)
            o_end = o_start + length
        if o_end <= start or o_start >= end:
            continue
        result.append(Occurrence(uid=uid, start=o_start, end=max(o_end, o_start), all_day=all_day,
                                 title=unescape(_first(source, "SUMMARY")[1] or "Untitled event").strip(),
                                 location=unescape(_first(source, "LOCATION")[1] or "").strip(),
                                 description=unescape(_first(source, "DESCRIPTION")[1] or "").strip()))
    return result
