"""Claude API client: streams replies over HTTPS, plus an offline stand-in for tests and demos."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger("subzero.claude")

API_VERSION = "2023-06-01"
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


@dataclass
class StreamEvent:
    kind: str  # "text", "usage", "error", or "done"
    text: str = ""
    usage: dict[str, Any] | None = None
    error: str = ""


class ClaudeError(Exception):
    pass


def parse_sse(lines: Iterable[str]) -> Iterator[StreamEvent]:
    """Turn the Messages API event stream into StreamEvents. Thinking blocks are skipped."""
    event_name: str | None = None
    data_lines: list[str] = []
    for line in lines:
        if line is None:
            continue
        if line == "":
            if data_lines:
                yield from _events_from(event_name, "\n".join(data_lines))
            event_name, data_lines = None, []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield from _events_from(event_name, "\n".join(data_lines))


def _events_from(event_name: str | None, data: str) -> Iterator[StreamEvent]:
    try:
        obj = json.loads(data)
    except ValueError:
        return
    kind = obj.get("type") or event_name
    if kind == "content_block_delta":
        delta = obj.get("delta") or {}
        if delta.get("type") == "text_delta":
            yield StreamEvent("text", text=delta.get("text", ""))
    elif kind == "message_start":
        usage = (obj.get("message") or {}).get("usage") or {}
        yield StreamEvent("usage", usage={
            "input_tokens": usage.get("input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        })
    elif kind == "message_delta":
        usage = obj.get("usage") or {}
        yield StreamEvent("usage", usage={
            "output_tokens": usage.get("output_tokens"),
            "stop_reason": (obj.get("delta") or {}).get("stop_reason"),
        })
    elif kind == "message_stop":
        yield StreamEvent("done")
    elif kind == "error":
        message = (obj.get("error") or {}).get("message") or "Claude returned an error"
        yield StreamEvent("error", error=message)


def _error_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
        return (body.get("error") or {}).get("message") or resp.text[:300]
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"


class ClaudeClient:
    def __init__(self, api_key: str, *, base_url: str = "https://api.anthropic.com",
                 session: requests.Session | None = None, max_retries: int = 2):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.max_retries = max_retries

    @staticmethod
    def request_body(*, model: str, system: str, messages: list[dict], max_tokens: int,
                     effort: str | None = None, stream: bool = False, system_extra: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            # Cache the system prompt: it's identical across requests, so repeats bill at the cache-read rate.
            # system_extra goes after the cached part, so it doesn't spoil the cache for other requests.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        if system_extra:
            body["system"].append({"type": "text", "text": system_extra})
        if effort:
            body["output_config"] = {"effort": effort}
        if stream:
            body["stream"] = True
        return body

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    def _post(self, body: dict[str, Any], *, stream: bool) -> requests.Response:
        url = self.base_url + "/v1/messages"
        for attempt in range(self.max_retries + 1):
            resp = self.session.post(url, json=body, headers=self._headers(), stream=stream, timeout=(10, 600))
            if resp.status_code == 200:
                return resp
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                wait = resp.headers.get("retry-after")
                delay = min(float(wait), 30.0) if wait and wait.replace(".", "", 1).isdigit() else 2.0 * (attempt + 1)
                resp.close()
                log.warning("Claude API busy (HTTP %s); retrying in %.0fs", resp.status_code, delay)
                time.sleep(delay)
                continue
            message = _error_message(resp)
            resp.close()
            raise ClaudeError(message)
        raise ClaudeError("Claude is busy right now. Try again in a minute.")

    async def stream(self, *, model: str, system: str, messages: list[dict], max_tokens: int,
                     effort: str | None = None, system_extra: str | None = None) -> AsyncIterator[StreamEvent]:
        body = self.request_body(model=model, system=system, messages=messages, max_tokens=max_tokens,
                                 effort=effort, stream=True, system_extra=system_extra)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        stop = threading.Event()

        def put(item: StreamEvent | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def worker() -> None:
            try:
                resp = self._post(body, stream=True)
                with resp:
                    resp.encoding = "utf-8"
                    for event in parse_sse(resp.iter_lines(decode_unicode=True)):
                        if stop.is_set():
                            break
                        put(event)
            except ClaudeError as exc:
                put(StreamEvent("error", error=str(exc)))
            except Exception as exc:  # network trouble, timeouts
                log.exception("Claude request failed")
                put(StreamEvent("error", error=f"Couldn't reach Claude ({type(exc).__name__}). Try again in a minute."))
            finally:
                put(None)

        loop.run_in_executor(None, worker)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            stop.set()

    async def complete(self, *, model: str, system: str, messages: list[dict], max_tokens: int,
                       effort: str | None = None) -> str:
        body = self.request_body(model=model, system=system, messages=messages, max_tokens=max_tokens, effort=effort)

        def call() -> str:
            resp = self._post(body, stream=False)
            with resp:
                data = resp.json()
            return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")

        return await asyncio.get_running_loop().run_in_executor(None, call)


def _last_user_text(messages: list[dict]) -> str:
    content = messages[-1]["content"] if messages else ""
    if isinstance(content, list):
        return " ".join(block.get("text", "") for block in content
                        if isinstance(block, dict) and block.get("type") == "text" and not block["text"].startswith("<file"))
    return str(content)


def _last_user_files(messages: list[dict]) -> tuple[int, int]:
    """How many text files and images the latest message carries."""
    content = messages[-1]["content"] if messages else ""
    if not isinstance(content, list):
        return 0, 0
    texts = sum(1 for b in content if b.get("type") == "text" and b.get("text", "").startswith("<file"))
    images = sum(1 for b in content if b.get("type") == "image")
    return texts, images


SAMPLE_EXPLAINER = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arm with a P controller</title>
<style>
  body { font: 16px system-ui, sans-serif; margin: 0; padding: 16px; background: #f4f7f9; color: #142231; }
  canvas { width: 100%; max-width: 520px; aspect-ratio: 4 / 3; background: #fff; border: 1px solid #d2dde5; border-radius: 8px; display: block; }
  label { display: block; margin-top: 12px; }
  input { width: 100%; max-width: 520px; }
  #net { font-size: 14px; color: #4a5b6b; }
</style>
</head>
<body>
<h1 style="font-size:1.2rem">Arm holding an angle with a P controller</h1>
<canvas id="c" width="520" height="390"></canvas>
<label>kP (volts per degree): <strong id="kpv"></strong><input id="kp" type="range" min="0" max="0.6" step="0.01" value="0.15"></label>
<label>Target angle (degrees): <strong id="tv"></strong><input id="t" type="range" min="0" max="90" step="1" value="45"></label>
<p id="net">Checking network access...</p>
<script>
  const c = document.getElementById("c"), g = c.getContext("2d");
  const kp = document.getElementById("kp"), t = document.getElementById("t");
  let angle = 0, speed = 0;
  function step() {
    const target = +t.value, volts = Math.max(-12, Math.min(12, +kp.value * (target - angle)));
    const accel = volts * 12 - 40 * Math.cos(angle * Math.PI / 180) - speed * 2.5;
    speed += accel / 60; angle = Math.max(-10, Math.min(120, angle + speed / 60));
    document.getElementById("kpv").textContent = (+kp.value).toFixed(2);
    document.getElementById("tv").textContent = target;
    g.clearRect(0, 0, 520, 390);
    const x0 = 120, y0 = 300, len = 260;
    g.strokeStyle = "#d2dde5"; g.setLineDash([6, 6]); g.beginPath(); g.moveTo(x0, y0);
    g.lineTo(x0 + len * Math.cos(target * Math.PI / 180), y0 - len * Math.sin(target * Math.PI / 180)); g.stroke();
    g.setLineDash([]); g.strokeStyle = "#1a5a86"; g.lineWidth = 14; g.lineCap = "round"; g.beginPath(); g.moveTo(x0, y0);
    g.lineTo(x0 + len * Math.cos(angle * Math.PI / 180), y0 - len * Math.sin(angle * Math.PI / 180)); g.stroke();
    g.lineWidth = 1; g.fillStyle = "#142231"; g.fillText("Angle " + angle.toFixed(1) + " deg, error " + (target - angle).toFixed(1) + " deg", 16, 24);
    requestAnimationFrame(step);
  }
  step();
  fetch("https://example.org/").then(() => { document.getElementById("net").textContent = "Network: reachable (it shouldn't be!)"; })
    .catch(() => { document.getElementById("net").textContent = "Network: blocked, as it should be. Cookies visible to this page: " + (function () { try { return document.cookie || "none"; } catch (e) { return "none"; } })(); });
</script>
</body>
</html>
"""


class FakeClaude:
    """Streams a canned reply. Lets you try the whole app without an API key.

    Include ALERT-TEST in a message to get a reply that starts with [MENTOR ALERT],
    or ERROR-TEST to simulate a failed request. Ask for an interactive explainer to get a sample one."""

    def __init__(self, delay: float = 0.02):
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def reply_for(question: str) -> str:
        quoted = question.strip().replace("\n", " ")
        if len(quoted) > 200:
            quoted = quoted[:200] + "…"
        return (
            "This is a reply from the offline test version of Claude, so you can try the app without an API key.\n\n"
            f"You asked:\n\n> {quoted}\n\n"
            "A real answer would explain the idea first, then show code like this:\n\n"
            "```java\n"
            "@Override\n"
            "public void periodic() {\n"
            "    SmartDashboard.putNumber(\"Arm angle (deg)\", getAngleDegrees());\n"
            "}\n"
            "```\n\n"
            "- Check the units before trusting a number.\n"
            "- Ask a mentor to confirm anything safety-critical."
        )

    async def stream(self, *, model: str, system: str, messages: list[dict], max_tokens: int,
                     effort: str | None = None, system_extra: str | None = None) -> AsyncIterator[StreamEvent]:
        self.calls.append({"model": model, "system": system, "messages": messages,
                           "max_tokens": max_tokens, "effort": effort, "system_extra": system_extra})
        question = _last_user_text(messages)
        if "ERROR-TEST" in question:
            yield StreamEvent("error", error="Simulated failure for testing.")
            return
        reply = self.reply_for(question)
        texts, images = _last_user_files(messages)
        if texts or images:
            reply += (f"\n\nYou attached {texts} text file{'s' if texts != 1 else ''} and "
                      f"{images} image{'s' if images != 1 else ''}. The real Claude would read them.")
        if system_extra and "interactive explainer" in system_extra:
            reply += ("\n\nHere's a sample interactive explainer. Drag the sliders to see how kP changes the way "
                      "the arm settles.\n\n```explainer\n" + SAMPLE_EXPLAINER + "```")
        if "ALERT-TEST" in question:
            reply = ("[MENTOR ALERT]\nThat sounds like a lot to carry. You don't have to handle it alone. "
                     "Please talk to a trusted adult today, like a parent, a mentor, or your school counselor. "
                     "If you're in the US you can call or text 988 any time, and call 911 in an emergency.")
        yield StreamEvent("usage", usage={"input_tokens": sum(len(str(m["content"])) for m in messages) // 4})
        for start in range(0, len(reply), 14):
            await asyncio.sleep(self.delay)
            yield StreamEvent("text", text=reply[start:start + 14])
        yield StreamEvent("usage", usage={"output_tokens": len(reply) // 4, "stop_reason": "end_turn"})
        yield StreamEvent("done")

    async def complete(self, *, model: str, system: str, messages: list[dict], max_tokens: int,
                       effort: str | None = None) -> str:
        self.calls.append({"model": model, "system": system, "messages": messages, "kind": "complete"})
        words = _last_user_text(messages).split()
        return " ".join(words[:6]).strip(".,:;!?") or "New chat"
