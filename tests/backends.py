"""Run the same tests against each AI backend. The approval flow must not care which engine answers.

Backends: "fake" (FakeClaude directly, today's behavior), "anthropic" (the real ClaudeClient over a fake
requests.Session) and "pi" (the real PiClient over tests/fake_pi_gateway.py in its "claude" mode). In the last two,
every reply is still FakeClaude's own scripted logic, carried over that backend's wire format, so one set of magic
words (ALERT-TEST, SCREEN-FLAG, ...) drives all three.

Use `add_variants(globals(), SomeTest, ...)` at the end of a test module. It adds `SomeTestAnthropic` and
`SomeTestPi`; the original class keeps running the fake variant, unchanged. Harness asks `client_for_active_backend()`
for its AI client when a test doesn't pass its own."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import requests

from subzero_ai.claude import ClaudeClient, ClaudeError, FakeClaude
from subzero_ai.pi_client import PiClient, child_env

FAKE_GATEWAY = str(Path(__file__).with_name("fake_pi_gateway.py"))
VARIANTS = {"Anthropic": "anthropic", "Pi": "pi"}

_active = {"name": "fake", "tmp": None, "clients": []}


# ------------------------------------------------------------------------------------------- anthropic
class FakeResponse:
    """The bits of requests.Response that ClaudeClient uses."""

    def __init__(self, status_code: int = 200, *, body: dict | None = None, sse: str = ""):
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.encoding = "utf-8"
        self._body = body
        self._sse = sse
        self.text = json.dumps(body) if body is not None else sse

    def json(self):
        return self._body

    def iter_lines(self, decode_unicode: bool = False):
        for line in self._sse.split("\n"):
            yield line

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def sse(events: list[tuple[str, dict]]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


class FakeMessagesSession:
    """Stands in for requests.Session. Answers POST /v1/messages from FakeClaude's scripted logic, as the Messages
    API would. `calls` is FakeClaude's own record of what it was asked (model, system, messages, ...)."""

    def __init__(self):
        self.fake = FakeClaude(delay=0)
        self.requests: list[dict] = []

    @property
    def calls(self) -> list[dict]:
        return self.fake.calls

    def post(self, url, *, json=None, headers=None, stream=False, timeout=None):
        body = json
        self.requests.append(body)
        model = body["model"] if "/" in body["model"] else f"anthropic/{body['model']}"  # as the app passed it
        system_blocks = body["system"]
        common = dict(model=model, system=system_blocks[0]["text"], messages=body["messages"],
                      max_tokens=body["max_tokens"])
        effort = (body.get("output_config") or {}).get("effort")
        try:
            if body.get("stream"):
                extra = system_blocks[1]["text"] if len(system_blocks) > 1 else None
                searches = (body.get("tools") or [{}])[0].get("max_uses", 0)
                return FakeResponse(sse=asyncio.run(self._stream(common, effort, extra, searches)))
            if body.get("tools"):
                message = asyncio.run(self.fake.tool_turn(tools=body["tools"], effort=effort, **common))
                return FakeResponse(body=message)
            text = asyncio.run(self.fake.complete(effort=effort, **common))
            return FakeResponse(body={"content": [{"type": "text", "text": text}]})
        except ClaudeError as exc:
            return FakeResponse(400, body={"type": "error", "error": {"type": "invalid_request_error",
                                                                      "message": str(exc)}})

    async def _stream(self, common: dict, effort, extra, searches) -> str:
        events = [e async for e in self.fake.stream(effort=effort, system_extra=extra, web_searches=searches,
                                                    **common)]
        out: list[tuple[str, dict]] = []
        usages = [e.usage for e in events if e.kind == "usage"]
        for e in events:
            if e.kind == "error":
                out.append(("error", {"type": "error", "error": {"type": "api_error", "message": e.error}}))
                return sse(out)
        first = usages[0] if usages else {}
        out.append(("message_start", {"type": "message_start", "message": {"id": "msg_fake", "usage": {
            "input_tokens": first.get("input_tokens", 0), "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "output_tokens": 1}}}))
        out.append(("content_block_start", {"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "text", "text": ""}}))
        for e in events:
            if e.kind == "text":
                out.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                    "delta": {"type": "text_delta", "text": e.text}}))
        out.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
        last = usages[-1] if len(usages) > 1 else {}
        out.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": last.get("stop_reason")},
                                      "usage": {"output_tokens": last.get("output_tokens", 0)}}))
        out.append(("message_stop", {"type": "message_stop"}))
        return sse(out)


class RecordingClaudeClient(ClaudeClient):
    """The real client, talking to FakeMessagesSession. `calls` mirrors FakeClaude.calls."""

    def __init__(self):
        self.fake_session = FakeMessagesSession()
        super().__init__("sk-test", session=self.fake_session)  # type: ignore[arg-type]

    @property
    def calls(self) -> list[dict]:
        return self.fake_session.calls


# ------------------------------------------------------------------------------------------------- pi
class RecordingPiClient(PiClient):
    """The real PiClient, talking to tests/fake_pi_gateway.py in "claude" mode. The gateway writes each request it
    receives to a file; `calls` reads it back in FakeClaude.calls' shape."""

    def __init__(self, calls_file: Path):
        env = child_env({"anthropic": "sk-test-key"})
        env.update(FAKE_PI_MODE="claude", FAKE_PI_CALLS=str(calls_file))
        super().__init__([sys.executable, FAKE_GATEWAY], env)
        self.calls_file = calls_file

    @property
    def calls(self) -> list[dict]:
        try:
            lines = self.calls_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines if line.strip()]


# ------------------------------------------------------------------------------------- wiring into tests
def client_for_active_backend():
    """A fresh AI client for the backend the running test class is set to, or None for the fake one."""
    name = _active["name"]
    if name == "fake":
        return None
    if name == "anthropic":
        client = RecordingClaudeClient()
    else:
        _active["tmp"] = _active["tmp"] or Path(tempfile.mkdtemp(prefix="subzero-pi-calls-"))
        client = RecordingPiClient(_active["tmp"] / f"calls-{len(_active['clients'])}.jsonl")
    _active["clients"].append(client)
    return client


def _finish() -> None:
    clients, tmp = _active["clients"], _active["tmp"]
    _active.update(name="fake", tmp=None, clients=[])
    for client in clients:
        if isinstance(client, PiClient):
            asyncio.run(client.aclose())  # stops the gateway process
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


class BackendVariant:
    """Mixed in ahead of a TestCase: points Harness at one backend for each test."""

    backend = "fake"

    def setUp(self):
        _active["name"] = self.backend
        self.addCleanup(_finish)
        super().setUp()


def add_variants(namespace: dict, *classes: type, fake_only: dict[str, set[str]] | None = None) -> None:
    """Add an Anthropic and a Pi copy of each class to the module. `fake_only` lists, by class name, tests that
    build their own FakeClaude subclass to inject behavior; those make no sense on another backend, so the copies
    leave them out."""
    for cls in classes:
        for suffix, backend in VARIANTS.items():
            skipped = {name: None for name in (fake_only or {}).get(cls.__name__, ())}
            variant = type(cls.__name__ + suffix, (BackendVariant, cls), {"backend": backend, **skipped})
            variant.__module__ = cls.__module__
            namespace[variant.__name__] = variant
