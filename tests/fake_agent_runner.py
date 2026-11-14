#!/usr/bin/env python3
"""Offline stand-in for sandbox_runner/runner.mjs (same JSON-lines protocol, D67/D70).

Behaviour is scripted by magic words in the task or message text and works on real files in
`cwd`, so patches can be tested. Standalone: standard library only.

Magic words: AGENT-FAIL, AGENT-CRASH, AGENT-SLOW, AGENT-TODO, AGENT-SUB, AGENT-COMPACT.
"""
from __future__ import annotations

import collections
import json
import os
import sys
import threading
import time

RUNNER_VERSION = "0.1.0-fake"
SDK_VERSION = "0.3.316"
USAGE = {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
NOTES = "AGENT_NOTES.md"

_cond = threading.Condition()
_inbox: "collections.deque[dict]" = collections.deque()  # queued input, always in arrival order
_state = {"stop": False, "open": 0}  # stop: host sent stop or closed stdin; open: turns not yet finished
_emit_lock = threading.Lock()


class Stopped(Exception):
    """Raised when stop (or EOF, which means stop) arrives while a turn is running."""


def emit(ev: dict) -> None:
    with _emit_lock:
        sys.stdout.write(json.dumps(ev) + "\n")
        sys.stdout.flush()


def _reader() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            emit({"type": "error", "message": "Could not read that input line."})
            continue
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            emit({"type": "error", "message": "Input line has no type."})
            continue
        with _cond:
            if msg["type"] == "stop":
                _state["stop"] = True
            else:
                if msg["type"] in ("start", "message"):
                    _state["open"] += 1
                _inbox.append(msg)
            _cond.notify_all()
    with _cond:  # EOF means stop
        _state["stop"] = True
        _cond.notify_all()


def _check_stop() -> None:
    if _state["stop"]:
        raise Stopped()


def _sleep(seconds: float) -> None:
    """Sleep, waking early only for stop (queued messages never cut it short)."""
    with _cond:
        if _cond.wait_for(lambda: _state["stop"], timeout=seconds):
            raise Stopped()


def _wait_plan_decision() -> dict:
    with _cond:
        while True:
            for m in _inbox:
                if m["type"] == "plan_decision":
                    _inbox.remove(m)  # other queued messages keep their order
                    return m
            if _state["stop"]:
                raise Stopped()
            _cond.wait()


def _next_message() -> dict | None:
    """Next queued input in order; None when stop/EOF."""
    with _cond:
        while True:
            if _state["stop"]:
                return None
            if _inbox:
                return _inbox.popleft()
            _cond.wait()


def _turn_done(**kw) -> None:
    base = {"type": "turn_done", "usage": USAGE, "cost_usd": 0.01, "num_turns": 1, "is_error": False}
    base.update(kw)
    with _cond:
        _state["open"] = max(0, _state["open"] - 1)
    emit(base)


def _tool(tid: str, name: str, tool_input: dict, output: str, parent: str | None = None, is_error: bool = False) -> None:
    ev = {"type": "tool_start", "id": tid, "name": name, "input": tool_input}
    if parent:
        ev["parent_id"] = parent
    emit(ev)
    emit({"type": "tool_end", "id": tid, "output": output, "is_error": is_error})


def _turn(text: str, cwd: str, state: dict, plan_first: bool) -> None:
    state["n"] += 1
    n = state["n"]
    if "AGENT-SLOW" in text:
        for _ in range(300):  # up to 30 s in 0.1 s steps, watching for stop
            _sleep(0.1)
    _check_stop()
    emit({"type": "text", "text": "Looking at the project."})

    if plan_first:
        pid = "plan-%d" % n
        emit({"type": "tool_start", "id": pid, "name": "ExitPlanMode", "input": {}})
        emit({"type": "plan", "id": pid, "plan": "1. Look around.\n2. Write notes to %s." % NOTES})
        decision = _wait_plan_decision()
        if not decision.get("approve"):
            note = str(decision.get("note") or "").strip()
            emit({"type": "tool_end", "id": pid, "output": "The plan was declined: %s" % (note or "no note"), "is_error": True})
            emit({"type": "text", "text": "Understood, I will not make changes. Note: %s" % (note or "none")})
            _turn_done()
            return
        emit({"type": "tool_end", "id": pid, "output": "Plan approved.", "is_error": False})

    _check_stop()
    if "AGENT-COMPACT" in text:
        emit({"type": "compact"})
    if "AGENT-TODO" in text:
        todos = [
            {"content": "Look around", "status": "completed", "activeForm": "Looking around"},
            {"content": "Write notes", "status": "in_progress", "activeForm": "Writing notes"},
        ]
        emit({"type": "tool_start", "id": "todo-%d" % n, "name": "TodoWrite", "input": {"todos": todos}})
        emit({"type": "todo", "items": todos})
        emit({"type": "tool_end", "id": "todo-%d" % n, "output": "Todos updated.", "is_error": False})
    if "AGENT-SUB" in text:
        emit({"type": "tool_start", "id": "sub-%d" % n, "name": "Agent", "input": {"description": "Explore", "prompt": "Look"}})
        _tool("subchild-%d" % n, "Read", {"file_path": os.path.join(cwd, NOTES)}, "ok", parent="sub-%d" % n)
        emit({"type": "tool_end", "id": "sub-%d" % n, "output": "Explored.", "is_error": False})

    _check_stop()
    _tool("bash-%d" % n, "Bash", {"command": "ls"}, "\n".join(sorted(os.listdir(cwd))))
    if "AGENT-CRASH" in text:
        sys.stdout.flush()
        os._exit(3)
    if "AGENT-FAIL" in text:
        _turn_done(is_error=True, error="The agent could not finish the task.")
        return

    _check_stop()
    path = os.path.join(cwd, NOTES)
    line = "- %s\n" % " ".join(text.split())[:80]
    emit({"type": "tool_start", "id": "write-%d" % n, "name": "Write", "input": {"file_path": path, "content": line}})
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    emit({"type": "tool_end", "id": "write-%d" % n, "output": "Wrote %s." % NOTES, "is_error": False})
    emit({"type": "text", "text": "Done."})
    _turn_done()


def main() -> int:
    if "--version" in sys.argv[1:]:
        print(json.dumps({"runner_version": RUNNER_VERSION, "sdk_version": SDK_VERSION}))
        return 0
    emit({"type": "ready", "runner_version": RUNNER_VERSION, "sdk_version": SDK_VERSION})
    threading.Thread(target=_reader, daemon=True).start()
    state = {"n": 0}
    cwd = os.getcwd()
    started = False
    try:
        while True:
            msg = _next_message()
            if msg is None:
                raise Stopped()  # stop or EOF; emits "stopped" only if a turn is still open
            kind = msg["type"]
            if kind == "start" and not started:
                started = True
                cwd = msg.get("cwd") or cwd
                emit({"type": "session", "session_id": msg.get("resume") or "fake-session-%d" % os.getpid()})
                _turn(str(msg.get("task") or ""), cwd, state, bool(msg.get("plan_first")))
            elif kind == "message" and started:
                _turn(str(msg.get("text") or ""), cwd, state, False)
            elif kind in ("start", "message"):
                with _cond:
                    _state["open"] = max(0, _state["open"] - 1)
                emit({"type": "error", "message": "Already started." if kind == "start" else "Send start first."})
            elif kind == "plan_decision":
                emit({"type": "error", "message": "No plan is waiting."})
            else:
                emit({"type": "error", "message": "Unknown input type: %s" % kind[:40]})
    except Stopped:
        if _state["open"] > 0:
            emit({"type": "turn_done", "usage": {}, "cost_usd": 0, "num_turns": 0, "is_error": True, "error": "stopped"})
        return 0


if __name__ == "__main__":
    sys.exit(main())
