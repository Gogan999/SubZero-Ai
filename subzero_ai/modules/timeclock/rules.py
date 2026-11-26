"""Time clock rules that need no database: settings, the practice window, durations and DM commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

DEFAULTS: dict = {
    "open_from": ["15:15"] * 5 + ["00:00"] * 2,  # per weekday, Monday first; "" means closed all day
    "reminder_hours": 5,
    "reminder_repeat_hours": 2,
    "auto_close": "22:00",
    "leave_grace_minutes": 10,
    "event_lead_minutes": 15,
    "season_start": "",  # "" means September 1 of the current school year
}

TIME_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
COMMAND_RE = re.compile(r"(?:clock\s+)?(in|out)|status|hours", re.IGNORECASE)


@dataclass
class Settings:
    open_from: list[str]
    reminder_hours: float
    reminder_repeat_hours: float
    auto_close: str
    leave_grace_minutes: int
    event_lead_minutes: int
    season_start: str

    @classmethod
    def from_dict(cls, data: dict | None) -> Settings:
        merged = {**DEFAULTS, **{k: v for k, v in (data or {}).items() if k in DEFAULTS}}
        return cls(**merged)

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in DEFAULTS}


def parse_time(text: str) -> time | None:
    match = TIME_RE.fullmatch(text.strip())
    return time(int(match.group(1)), int(match.group(2))) if match else None


def validate_settings(form: dict) -> tuple[Settings | None, list[str]]:
    """Settings from the admin form, or the problems with it."""
    errors = []
    open_from = []
    for index, day in enumerate(DAYS):
        raw = str(form.get(f"open_{index}", "")).strip()
        if raw and parse_time(raw) is None:
            errors.append(f"{day}: type a time like 15:15, or leave it empty for closed.")
        open_from.append(raw if raw and parse_time(raw) else "")

    def number(name: str, label: str, low: float, high: float, kind=float):
        try:
            value = kind(str(form.get(name, "")).strip())
        except ValueError:
            value = None
        if value is None or not low <= value <= high:
            errors.append(f"{label} must be between {low:g} and {high:g}.")
            return DEFAULTS[name]
        return value

    reminder = number("reminder_hours", "Hours before the reminder", 1, 16)
    repeat = number("reminder_repeat_hours", "Hours between reminders", 0.5, 8)
    grace = number("leave_grace_minutes", "Minutes to wait after leaving", 0, 60, int)
    lead = number("event_lead_minutes", "Minutes before an event", 0, 120, int)
    auto_close = str(form.get("auto_close", "")).strip()
    if parse_time(auto_close) is None:
        errors.append("The nightly clock-out time must look like 22:00.")
        auto_close = DEFAULTS["auto_close"]
    season = str(form.get("season_start", "")).strip()
    if season:
        try:
            date.fromisoformat(season)
        except ValueError:
            errors.append("The season start must be a date.")
            season = ""
    if errors:
        return None, errors
    return Settings(open_from, reminder, repeat, auto_close, grace, lead, season), []


def window_opens(settings: Settings, day: date) -> datetime | None:
    """When the practice window opens on a local date (naive local time), or None when it's closed all day."""
    opens = parse_time(settings.open_from[day.weekday()])
    return datetime.combine(day, opens) if opens is not None and settings.open_from[day.weekday()] else None


def window_open(settings: Settings, now_local: datetime) -> bool:
    opens = window_opens(settings, now_local.date())
    return opens is not None and now_local.replace(tzinfo=None) >= opens


def next_opening(settings: Settings, now_local: datetime) -> datetime | None:
    """The next time the practice window opens after now, within a week (naive local time)."""
    naive = now_local.replace(tzinfo=None)
    for offset in range(8):
        opens = window_opens(settings, naive.date() + timedelta(days=offset))
        if opens is not None and opens > naive:
            return opens
    return None


def season_start(settings: Settings, today: date) -> date:
    if settings.season_start:
        return date.fromisoformat(settings.season_start)
    year = today.year if today.month >= 9 else today.year - 1
    return date(year, 9, 1)


def duration_text(seconds: float) -> str:
    """'2 h 05 m', '45 m' or '0 m'."""
    minutes = int(max(seconds, 0) // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} m" if hours else f"{minutes} m"


def hours_text(seconds: float) -> str:
    """Hours with one decimal, like '12.5'."""
    return f"{max(seconds, 0) / 3600:.1f}"


def clock_text(moment: datetime) -> str:
    """'3:22 pm'."""
    return moment.strftime("%I:%M %p").lstrip("0").lower()


def when_text(moment: datetime, today: date) -> str:
    """'3:22 pm', 'yesterday at 3:22 pm' or 'Mon Nov 23 at 3:22 pm'."""
    day = moment.date()
    if day == today:
        return clock_text(moment)
    if day == today - timedelta(days=1):
        return f"yesterday at {clock_text(moment)}"
    return f"{moment:%a %b} {moment.day} at {clock_text(moment)}"


def opening_text(opens: datetime | None, today: date) -> str:
    """'Clock-in opens at 3:15 pm.' or 'Clock-in opens Saturday at 12:00 am.'"""
    if opens is None:
        return "Clock-in isn't open this week. A coach can add your time."
    if opens.date() == today:
        return f"Clock-in opens at {clock_text(opens)}."
    if opens.date() == today + timedelta(days=1):
        return f"Clock-in opens tomorrow at {clock_text(opens)}."
    return f"Clock-in opens {DAYS[opens.weekday()]} at {clock_text(opens)}."


def parse_command(text: str) -> str | None:
    """'in', 'out', 'status' or 'hours' for a DM that is exactly one of the time clock's commands."""
    cleaned = text.strip().rstrip(".!?").strip()
    match = COMMAND_RE.fullmatch(cleaned)
    if match is None:
        return None
    return (match.group(1) or match.group(0)).lower()
