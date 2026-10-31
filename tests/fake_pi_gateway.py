"""A stand-in for pi_gateway/gateway.mjs that speaks the same JSON-lines protocol. What each request does is
chosen by its model name (fake/echo, fake/slow, ...). No Node, no network.

With FAKE_PI_MODE=claude, every request is instead answered by FakeClaude's scripted logic (the same replies, magic
words and screener verdicts as the offline app), whatever the model. FAKE_PI_CALLS names a file where each request is
appended as one JSON line, in the shape of FakeClaude.calls. FAKE_PI_CHUNK=n re-cuts the streamed text into pieces of
n characters (to test things that span chunks). In claude mode FakeClaude's search and sources events are dropped (pi
has no web search), and the recorded calls say web_searches=0, as the real gateway reports."""

from __future__ import annotations

import json
import os
import sys
import threading
import time

out_lock = threading.Lock()
aborts: dict[str, threading.Event] = {}
aborted_ids: list[str] = []
state_lock = threading.Lock()


def emit(**record) -> None:
    with out_lock:
        sys.stdout.write(json.dumps(record) + "\n")
        sys.stdout.flush()


def last_user_text(req: dict) -> str:
    messages = req.get("messages") or []
    content = messages[-1]["content"] if messages else ""
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content)


def usage() -> dict:
    return {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "cost_usd": 0.001, "stop_reason": "end_turn"}


def reply(rid: str, req: dict, text: str, *, chunks: int = 3) -> None:
    if req["op"] == "complete":
        emit(id=rid, kind="text", text=text)
    else:
        size = max(1, -(-len(text) // chunks))
        for i in range(0, len(text), size):
            emit(id=rid, kind="text", text=text[i:i + size])
    emit(id=rid, kind="usage", usage=usage())


def fail(rid: str, code: str, message: str) -> None:
    emit(id=rid, kind="error", code=code, error=message)


def record_call(req: dict) -> None:
    """Note a request in FAKE_PI_CALLS, in the shape FakeClaude.calls uses. Called in arrival order."""
    path = os.environ.get("FAKE_PI_CALLS")
    if not path or req["op"] not in ("stream", "complete", "tool_turn"):
        return
    call = {"model": req.get("model"), "system": req.get("system"), "messages": req.get("messages")}
    if req["op"] == "stream":
        call.update(max_tokens=req.get("max_tokens"), effort=req.get("effort"), system_extra=req.get("system_extra"),
                    web_searches=0)
    else:
        call["kind"] = req["op"]
    with state_lock, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(call) + "\n")


def handle_as_claude(rid: str, req: dict) -> None:
    """Answer with FakeClaude's logic over the gateway protocol."""
    import asyncio

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from subzero_ai.claude import ClaudeError, FakeClaude

    fake = FakeClaude(delay=0)
    args = dict(model=req["model"], system=req.get("system", ""), messages=req.get("messages", []),
                max_tokens=req.get("max_tokens", 0), effort=req.get("effort"))
    try:
        if req["op"] == "stream":
            async def run() -> None:
                async for event in fake.stream(system_extra=req.get("system_extra"), **args):
                    if event.kind == "text":
                        size = int(os.environ.get("FAKE_PI_CHUNK") or 0) or len(event.text) or 1
                        for i in range(0, len(event.text), size):
                            emit(id=rid, kind="text", text=event.text[i:i + size])
                    elif event.kind == "usage":
                        emit(id=rid, kind="usage", usage=event.usage)
                    elif event.kind == "error":
                        return fail(rid, "provider", event.error)
            asyncio.run(run())
        elif req["op"] == "complete":
            emit(id=rid, kind="text", text=asyncio.run(fake.complete(**args)))
            emit(id=rid, kind="usage", usage=usage())
        else:
            message = asyncio.run(fake.tool_turn(tools=req.get("tools", []), **args))
            emit(id=rid, kind="message", message=message)
            emit(id=rid, kind="usage", usage=usage())
    except ClaudeError as exc:
        fail(rid, "provider", str(exc))


def handle(req: dict) -> None:
    rid, model = req["id"], str(req.get("model", ""))
    name = model.split("/", 1)[-1]
    event = aborts[rid]
    try:
        text = last_user_text(req)
        if os.environ.get("FAKE_PI_MODE") == "claude":
            handle_as_claude(rid, req)
        elif name == "echo":
            reply(rid, req, text)
        elif name == "safe":
            reply(rid, req, "SAFE")
        elif name == "flag":
            reply(rid, req, "REVIEW: looks odd")
        elif name == "slow":
            words = text.split()
            for i, word in enumerate(words):
                if event.wait(0.03):
                    return fail(rid, "aborted", "request aborted")
                emit(id=rid, kind="text", text=word + (" " if i < len(words) - 1 else ""))
            emit(id=rid, kind="usage", usage=usage())
        elif name == "loop":
            while not event.wait(0.03):
                emit(id=rid, kind="text", text="tick ")
            fail(rid, "aborted", "request aborted")
        elif name == "crash":
            emit(id=rid, kind="text", text="partial ")
            time.sleep(0.05)
            os._exit(3)
        elif name == "garbage":
            with out_lock:
                sys.stdout.write("this is not json\n")
                sys.stdout.write(json.dumps({"id": "zzz", "kind": "text", "text": "stray"}) + "\n")
                sys.stdout.write("[1, 2]\n")
                sys.stdout.flush()
            reply(rid, req, "fine")
        elif name == "hang":
            event.wait()
        elif name == "deaf":  # ignores aborts, never finishes
            threading.Event().wait()
        elif name == "drip":  # keeps talking, never finishes
            while not event.wait(0.03):
                emit(id=rid, kind="text", text=".")
        elif name == "error":
            fail(rid, "provider", "boom")
        elif name == "nokey":
            fail(rid, "no_key", "no API key set for provider fake (SUBZERO_KEY_FAKE)")
        elif name == "unknown":
            fail(rid, "unknown_model", "unknown model")
        elif name == "badreq":
            fail(rid, "bad_request", "max_tokens must be positive; secret detail")
        elif name == "tool":
            message = {"content": [{"type": "text", "text": "Let me look."},
                                   {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                                    "input": {"path": "src/Robot.java"}}],
                       "stop_reason": "tool_use", "usage": usage()}
            emit(id=rid, kind="message", message=message)
            emit(id=rid, kind="usage", usage=usage())
        elif name == "seen":  # what the gateway has been told so far
            with state_lock:
                emit(id=rid, kind="text", text=json.dumps({"aborted": aborted_ids, "pid": os.getpid()}))
            emit(id=rid, kind="usage", usage=usage())
        elif name == "request":  # the request as received (minus id)
            emit(id=rid, kind="text", text=json.dumps({k: v for k, v in req.items() if k != "id"}))
            emit(id=rid, kind="usage", usage=usage())
        else:
            fail(rid, "unknown_model", f"unknown model {model}")
    finally:
        aborts.pop(rid, None)
        emit(id=rid, kind="done")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        rid, op = req.get("id"), req.get("op")
        if op == "abort":
            with state_lock:
                aborted_ids.append(rid)
            event = aborts.get(rid)
            if event:
                event.set()
            continue
        if op == "ping":
            emit(id=rid, kind="pong", version="0.0.0-fake", pi_ai="9.9.9", node="v0.fake", env=dict(os.environ))
            emit(id=rid, kind="done")
        elif op == "models":
            emit(id=rid, kind="models", models=[{"id": "fake/echo", "name": "Echo", "provider": "fake"}])
            emit(id=rid, kind="done")
        else:
            aborts[rid] = threading.Event()
            if os.environ.get("FAKE_PI_MODE") == "claude":
                record_call(req)
            threading.Thread(target=handle, args=(req,), daemon=True).start()
    os._exit(0)  # stdin closed: leave at once, even with handler threads still running


if __name__ == "__main__":
    main()
