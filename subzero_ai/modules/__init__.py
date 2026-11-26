"""The modules installed in this Hub, in menu order."""

from __future__ import annotations

from ..hub.core import HubCore
from .ai import AIModule
from .calendar import CalendarModule
from .timeclock import TimeClockModule

MODULES = [HubCore(), TimeClockModule(), CalendarModule(), AIModule()]
