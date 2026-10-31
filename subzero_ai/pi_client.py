"""AI client for the Pi backend: talks JSON lines to the Node gateway (pi_gateway/) running as a child process."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .claude import ClaudeError, StreamEvent
from .config import Settings

log = logging.getLogger("subzero.pi")

DOWN_MESSAGE = "The AI helper isn't running right now. Try again in a minute."
DIED_MESSAGE = "The AI helper stopped unexpectedly. Try again."
SHUTDOWN_MESSAGE = "The AI helper is shutting down. Try again in a minute."
GENERIC_MESSAGE = "Something went wrong talking to the AI helper."
NODE_MESSAGE = ("The AI helper couldn't start because Node.js couldn't be started. "
                "Ask an admin to check that Node.js is installed.")
IDLE_MESSAGE = "The AI helper stopped answering. Try again."
SLOW_MESSAGE = "The AI took too long to answer. Try again."

# Variables Node needs to run (Linux and Windows), and proxy / certificate settings. Nothing else is passed on.
_NODE_VARS = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "HOME",
              "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA")
_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
               "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")
_ALLOWED = frozenset(_NODE_VARS + _PROXY_VARS)
_ALLOWED_UPPER = frozenset(name.upper() for name in _ALLOWED)

_DEAD = object()  # put in a request's queue when the process exits under it
_SHUTDOWN = object()  # same, when we closed the process on purpose


def key_env_name(provider: str) -> str:
    """The environment variable the gateway reads a provider's key from."""
    return "SUBZERO_KEY_" + re.sub(r"[^A-Za-z0-9]", "_", provider).upper()


KEY_SETTINGS = {"anthropic": "anthropic_api_key", "openai": "openai_api_key", "google": "gemini_api_key",
                "openrouter": "openrouter_api_key"}


def provider_keys(settings: Settings) -> dict[str, str]:
    """The API keys to hand the gateway, by provider id: those that are set, for providers that are turned on
    (Anthropic counts as on whenever its key is set)."""
    enabled = set(settings.ai_providers) | {"anthropic"}
    return {provider: key for provider, name in KEY_SETTINGS.items()
            if provider in enabled and (key := getattr(settings, name, ""))}


def child_env(keys: dict[str, str], environ: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal environment for the gateway: what Node needs, proxy settings, and one SUBZERO_KEY_* per key."""
    source = os.environ if environ is None else environ
    env: dict[str, str] = {}
    for name, value in source.items():
        if name in _ALLOWED or (os.name == "nt" and name.upper() in _ALLOWED_UPPER):
            env[name] = value
    for provider, key in keys.items():
        if key:
            env[key_env_name(provider)] = key
    return env


def default_gateway_script() -> str:
    """pi_gateway/gateway.mjs next to the subzero_ai package."""
    return str(Path(__file__).resolve().parent.parent / "pi_gateway" / "gateway.mjs")


def error_message(code: str, detail: str, model: str) -> str:
    """Plain-language text for a gateway error record."""
    if code == "no_key":
        provider = model.split("/", 1)[0] if "/" in model else "anthropic"
        return f"No API key is set for {provider}. An admin can add it in Settings."
    if code == "unknown_model":
        return f"The model {model} isn't available."
    if code == "provider":
        return f"The AI service returned an error: {detail}"
    if code == "aborted":
        return "The AI request was cancelled. Try again."
    return GENERIC_MESSAGE


class _Failure(Exception):
    """A request failed; the message is already plain language."""


@dataclass
class _Pending:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue


@dataclass
class _Proc:
    popen: subprocess.Popen
    started: float
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    pending: dict[str, _Pending] = field(default_factory=dict)
    aborted: set[str] = field(default_factory=set)
    closing: bool = False
    dead: bool = False
    retired: bool = False  # replaced by a newer process (new keys); stops when its last request finishes


class PiClient:
    """Same interface as ClaudeClient (stream, complete, tool_turn), answered by the Node gateway."""

    def __init__(self, command: list[str], env: dict[str, str], *, request_timeout: float = 300,
                 idle_timeout: float = 120, backoff_base: float = 1.0, backoff_max: float = 30.0,
                 healthy_after: float = 60.0, kill_wait: float = 3.0):
        self.command = list(command)
        self.env = dict(env)
        self.request_timeout = request_timeout
        self.idle_timeout = idle_timeout
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.healthy_after = healthy_after
        self.kill_wait = kill_wait
        self._lock = threading.Lock()
        self._proc: _Proc | None = None
        self._retired: list[_Proc] = []  # old processes still finishing requests
        self._closed = False
        self._retry_at = 0.0
        self._streak = 0
        self._ids = itertools.count(1)

    @classmethod
    def from_settings(cls, settings: Settings) -> PiClient:
        script = settings.pi_gateway_script or default_gateway_script()
        return cls([settings.pi_node or "node", script], child_env(provider_keys(settings)),
                   request_timeout=settings.pi_request_timeout, idle_timeout=settings.pi_idle_timeout)

    def set_keys(self, keys: dict[str, str]) -> None:
        """Use new API keys. The next request starts a fresh gateway with them. A gateway with no request in
        flight stops now; one that is busy finishes what it's doing first, then stops. Keys are never logged."""
        with self._lock:
            self.env = {name: value for name, value in self.env.items() if not name.startswith("SUBZERO_KEY_")}
            self.env.update(child_env(keys, {}))
            proc, self._proc = self._proc, None
            if proc is None or proc.dead:
                return
            proc.retired = True
            proc.closing = True
            if proc.pending:
                self._retired.append(proc)
                return
        self._stop_in_background(proc)

    def _stop_in_background(self, proc: _Proc) -> None:
        threading.Thread(target=self._stop, args=(proc,), name="pi-stop", daemon=True).start()

    # ------------------------------------------------------------------ process
    def _spawn(self) -> _Proc:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        popen = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=self.env, creationflags=flags)
        proc = _Proc(popen, time.monotonic())
        threading.Thread(target=self._read_stdout, args=(proc,), name="pi-stdout", daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(proc,), name="pi-stderr", daemon=True).start()
        log.info("Started the AI helper (pid %s)", popen.pid)
        return proc

    def _note_crash(self, ran: float) -> None:
        """Set the earliest restart time. Caller holds the lock."""
        if ran >= self.healthy_after:
            self._streak = 0
        delay = min(self.backoff_base * (2 ** self._streak), self.backoff_max)
        self._streak += 1
        self._retry_at = time.monotonic() + delay

    def _ensure_proc(self) -> _Proc:
        with self._lock:
            if self._closed:
                raise _Failure(SHUTDOWN_MESSAGE)
            if self._proc is not None and not self._proc.dead:
                return self._proc
            if time.monotonic() < self._retry_at:
                raise _Failure(DOWN_MESSAGE)
            try:
                self._proc = self._spawn()
            except OSError as exc:
                log.error("Couldn't start the AI helper %s: %s", self.command[:1], exc)
                self._note_crash(0.0)
                raise _Failure(NODE_MESSAGE) from exc
            return self._proc

    def _read_stdout(self, proc: _Proc) -> None:
        try:
            for raw in proc.popen.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._handle_line(proc, line)
        except Exception:
            log.exception("Reading from the AI helper failed")
        finally:
            self._on_exit(proc)

    def _handle_line(self, proc: _Proc, line: str) -> None:
        try:
            record = json.loads(line)
        except ValueError:
            log.warning("The AI helper printed something that isn't JSON: %.200s", line)
            return
        rid = record.get("id") if isinstance(record, dict) else None
        if not isinstance(rid, str):
            log.warning("The AI helper sent a record without a request id: %.200s", line)
            return
        self._deliver(proc, rid, record)

    def _deliver(self, proc: _Proc, rid: str, item: Any) -> None:
        with self._lock:
            pending = proc.pending.get(rid)
            known_abort = rid in proc.aborted
        if pending is None:
            (log.debug if known_abort else log.warning)("The AI helper sent a record for unknown request %s", rid)
            return
        try:
            pending.loop.call_soon_threadsafe(pending.queue.put_nowait, item)
        except RuntimeError:  # the request's event loop is gone
            pass

    def _on_exit(self, proc: _Proc) -> None:
        code = proc.popen.wait()
        with self._lock:
            proc.dead = True
            waiting = list(proc.pending)
            if self._proc is proc:
                self._proc = None
            if proc in self._retired:
                self._retired.remove(proc)
            if not proc.closing:
                self._note_crash(time.monotonic() - proc.started)
        if proc.closing:
            log.info("The AI helper exited (code %s)", code)
        else:
            log.warning("The AI helper exited unexpectedly (code %s)", code)
        for rid in waiting:
            self._deliver(proc, rid, _SHUTDOWN if proc.closing else _DEAD)

    def _drain_stderr(self, proc: _Proc) -> None:
        try:
            for raw in proc.popen.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    level = logging.WARNING if re.search(r"error|warn|fail", line, re.I) else logging.INFO
                    log.log(level, "gateway: %s", line)
        except Exception:
            pass

    def _send(self, proc: _Proc, obj: dict[str, Any]) -> bool:
        """Write one request line. False if the process can't take it."""
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with proc.write_lock:
                proc.popen.stdin.write(data)
                proc.popen.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    # ----------------------------------------------------------------- requests
    async def _records(self, payload: dict[str, Any], model: str = "",
                       total: float | None = None) -> AsyncIterator[dict[str, Any]]:
        """Send one request and yield its records until done. Raises _Failure. Aborts if closed early."""
        rid = f"r{next(self._ids)}"
        loop = asyncio.get_running_loop()
        pending = _Pending(loop, asyncio.Queue())
        while True:
            proc = self._ensure_proc()
            with self._lock:
                if proc.dead:
                    raise _Failure(DIED_MESSAGE)
                if not proc.retired:  # set_keys retires under this lock, so it can't stop a process we register on
                    proc.pending[rid] = pending
                    break
        finished = False
        try:
            if not self._send(proc, {"id": rid, **payload}):
                finished = True  # nothing to abort
                raise _Failure(DIED_MESSAGE)
            deadline = loop.time() + (total if total is not None else self.request_timeout)
            failure: str | None = None
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    raise _Failure(SLOW_MESSAGE)
                try:
                    item = await asyncio.wait_for(pending.queue.get(), min(self.idle_timeout, left))
                except asyncio.TimeoutError:
                    raise _Failure(SLOW_MESSAGE if loop.time() >= deadline else IDLE_MESSAGE) from None
                if item is _DEAD or item is _SHUTDOWN:
                    finished = True
                    raise _Failure(DIED_MESSAGE if item is _DEAD else SHUTDOWN_MESSAGE)
                kind = item.get("kind")
                if kind == "done":
                    finished = True
                    if failure is not None:
                        raise _Failure(failure)
                    return
                if kind == "error":
                    code = str(item.get("code") or "")
                    detail = str(item.get("error") or "")
                    if code not in ("provider", "no_key", "unknown_model", "aborted"):
                        log.warning("The AI helper rejected a request (%s): %s", code, detail)
                    failure = error_message(code, detail, model)
                    continue
                yield item
        finally:
            with self._lock:
                proc.pending.pop(rid, None)
                if not finished:
                    proc.aborted.add(rid)
                last_one = proc.retired and not proc.pending and not proc.dead
            if not finished and not proc.dead:
                self._send(proc, {"id": rid, "op": "abort"})
            if last_one:
                self._stop_in_background(proc)

    def _payload(self, op: str, *, model: str, system: str, messages: list[dict], max_tokens: int,
                 effort: str | None, system_extra: str | None = None,
                 tools: list[dict] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": op, "model": model if "/" in model else f"anthropic/{model}",
                                   "system": system, "messages": messages, "max_tokens": max_tokens}
        if effort:
            payload["effort"] = effort
        if system_extra:
            payload["system_extra"] = system_extra
        if tools is not None:
            payload["tools"] = tools
        return payload

    async def stream(self, *, model: str, system: str, messages: list[dict], max_tokens: int,
                     effort: str | None = None, system_extra: str | None = None,
                     web_searches: int = 0) -> AsyncIterator[StreamEvent]:
        if web_searches:
            log.warning("Web search isn't available with the Pi backend; answering without it")
        payload = self._payload("stream", model=model, system=system, messages=messages, max_tokens=max_tokens,
                                effort=effort, system_extra=system_extra)
        try:
            async with contextlib.aclosing(self._records(payload, model)) as records:
                async for record in records:
                    kind = record.get("kind")
                    if kind == "text":
                        yield StreamEvent("text", text=str(record.get("text") or ""))
                    elif kind == "usage" and isinstance(record.get("usage"), dict):
                        yield StreamEvent("usage", usage=record["usage"])
        except _Failure as exc:
            yield StreamEvent("error", error=str(exc))
            return
        yield StreamEvent("done")

    async def complete(self, *, model: str, system: str, messages: list[dict], max_tokens: int,
                       effort: str | None = None) -> str:
        payload = self._payload("complete", model=model, system=system, messages=messages, max_tokens=max_tokens,
                                effort=effort)
        parts: list[str] = []
        try:
            async with contextlib.aclosing(self._records(payload, model)) as records:
                async for record in records:
                    if record.get("kind") == "text":
                        parts.append(str(record.get("text") or ""))
        except _Failure as exc:
            raise ClaudeError(str(exc)) from None
        return "".join(parts)

    async def tool_turn(self, *, model: str, system: str, messages: list[dict], tools: list[dict], max_tokens: int,
                        effort: str | None = None) -> dict[str, Any]:
        payload = self._payload("tool_turn", model=model, system=system, messages=messages, max_tokens=max_tokens,
                                effort=effort, tools=tools)
        message: dict[str, Any] | None = None
        try:
            async with contextlib.aclosing(self._records(payload, model)) as records:
                async for record in records:
                    if record.get("kind") == "message" and isinstance(record.get("message"), dict):
                        message = record["message"]
        except _Failure as exc:
            raise ClaudeError(str(exc)) from None
        if message is None:
            log.warning("The AI helper finished a tool turn without a message")
            raise ClaudeError(GENERIC_MESSAGE)
        return message

    async def ping(self) -> dict[str, Any]:
        """The gateway's pong record (version, pi_ai, node). Raises ClaudeError if it doesn't answer."""
        try:
            async with contextlib.aclosing(self._records({"op": "ping"}, total=min(self.request_timeout, 30))) as recs:
                async for record in recs:
                    if record.get("kind") == "pong":
                        return record
        except _Failure as exc:
            raise ClaudeError(str(exc)) from None
        raise ClaudeError(GENERIC_MESSAGE)

    async def list_models(self, providers: list[str] | None = None) -> list[dict[str, Any]]:
        """The models the gateway knows, optionally only for some providers."""
        payload: dict[str, Any] = {"op": "models"}
        if providers is not None:
            payload["providers"] = list(providers)
        try:
            async with contextlib.aclosing(self._records(payload, total=min(self.request_timeout, 30))) as recs:
                async for record in recs:
                    if record.get("kind") == "models":
                        return list(record.get("models") or [])
        except _Failure as exc:
            raise ClaudeError(str(exc)) from None
        raise ClaudeError(GENERIC_MESSAGE)

    # ----------------------------------------------------------------- shutdown
    async def aclose(self) -> None:
        """Abort what's running, close the gateway's input, and stop it if it doesn't leave by itself."""
        with self._lock:
            self._closed = True
            procs = ([self._proc] if self._proc is not None else []) + self._retired
            self._proc, self._retired = None, []
            work = []
            for proc in procs:
                proc.closing = True
                work.append((proc, list(proc.pending)))
        for proc, waiting in work:
            if proc.dead:
                continue
            for rid in waiting:
                self._send(proc, {"id": rid, "op": "abort"})
            await asyncio.get_running_loop().run_in_executor(None, self._stop, proc)

    def _stop(self, proc: _Proc) -> None:
        try:
            proc.popen.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.popen.wait(timeout=self.kill_wait)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.popen.terminate()
        try:
            proc.popen.wait(timeout=self.kill_wait)
        except subprocess.TimeoutExpired:
            proc.popen.kill()
            proc.popen.wait()
