"""The AI backend seam: which engine answers, what each model can do, and the client the app uses."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .claude import ClaudeClient, ClaudeError, FakeClaude, StreamEvent
from .config import Settings
from .pi_client import PiClient

BACKENDS = ("anthropic", "pi", "fake")


class AIBackend(Protocol):
    """What the rest of the app needs from an AI client."""

    def stream(self, *, model: str, system: str, messages: list[dict], max_tokens: int,
               effort: str | None = None, system_extra: str | None = None,
               web_searches: int = 0) -> AsyncIterator[StreamEvent]: ...

    async def complete(self, *, model: str, system: str, messages: list[dict], max_tokens: int,
                       effort: str | None = None) -> str: ...

    async def tool_turn(self, *, model: str, system: str, messages: list[dict], tools: list[dict], max_tokens: int,
                        effort: str | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Capabilities:
    """What a model can do. The app turns features off when a model can't."""

    web_search: bool
    tools: bool
    thinking: bool
    images: bool


_ALL = Capabilities(web_search=True, tools=True, thinking=True, images=True)
_NONE = Capabilities(web_search=False, tools=False, thinking=False, images=False)


def capabilities_for(model: str, backend: str = "anthropic") -> Capabilities:
    """Claude models (bare or anthropic/-prefixed ids) can do everything; unknown models nothing.
    The pi backend has no web search."""
    name = model.strip().lower()
    if name.startswith("anthropic/"):
        name = name[len("anthropic/"):]
    if not name.startswith("claude-"):
        return _NONE
    return replace(_ALL, web_search=False) if backend == "pi" else _ALL


def resolve_backend(settings: Settings) -> str:
    """The backend name in use. FAKE_CLAUDE=1 is an alias for fake. May be an unknown string."""
    if settings.fake_claude:
        return "fake"
    return settings.ai_backend.strip().lower()


class UnavailableClient:
    """Stands in when the chosen backend can't run, so the service stays up and says why."""

    def __init__(self, message: str):
        self.message = message

    async def stream(self, *, model: str, system: str, messages: list[dict], max_tokens: int,
                     effort: str | None = None, system_extra: str | None = None,
                     web_searches: int = 0) -> AsyncIterator[StreamEvent]:
        yield StreamEvent("error", error=self.message)

    async def complete(self, *, model: str, system: str, messages: list[dict], max_tokens: int,
                       effort: str | None = None) -> str:
        raise ClaudeError(self.message)

    async def tool_turn(self, *, model: str, system: str, messages: list[dict], tools: list[dict], max_tokens: int,
                        effort: str | None = None) -> dict[str, Any]:
        raise ClaudeError(self.message)


def unavailable_message(backend: str) -> str:
    """Plain-language reason a backend can't be used, or '' when it can."""
    if backend not in BACKENDS:
        return (f"The AI backend setting AI_BACKEND={backend} isn't one of anthropic, pi or fake. "
                "Fix it in config.env and restart.")
    return ""


def make_client(settings: Settings, *, max_retries: int | None = None) -> AIBackend:
    """Build the AI client for the configured backend. Bad settings give an UnavailableClient, never a crash."""
    backend = resolve_backend(settings)
    if backend == "fake":
        return FakeClaude()
    if backend == "anthropic":
        extra = {} if max_retries is None else {"max_retries": max_retries}
        return ClaudeClient(settings.anthropic_api_key, base_url=settings.anthropic_base_url, **extra)
    if backend == "pi":
        return PiClient.from_settings(settings)
    return UnavailableClient(unavailable_message(backend))
