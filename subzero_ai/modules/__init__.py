"""The modules installed in this Hub, in menu order."""

from __future__ import annotations

from ..hub.core import HubCore
from .ai import AIModule

MODULES = [HubCore(), AIModule()]
