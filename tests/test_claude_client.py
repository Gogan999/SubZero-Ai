"""The Claude API client against a local stand-in server that speaks the Messages API."""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from subzero_ai.claude import ClaudeClient, ClaudeError, parse_sse

STREAM = [
    ("message_start", {"type": "message_start", "message": {"id": "msg_1", "usage": {
        "input_tokens": 1200, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("ping", {"type": "ping"}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": ", team ✓"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 42}}),
    ("message_stop", {"type": "message_stop"}),
]


def sse_bytes(events) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode("utf-8")


class FakeAnthropic(BaseHTTPRequestHandler):
    """Answers POST /v1/messages. Behavior is set on the server object."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        server = self.server
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length))
        server.requests.append({"headers": dict(self.headers), "body": body})
        if server.fail_first and len(server.requests) <= server.fail_first:
            self.send_response(529)
            self.send_header("content-type", "application/json")
            self.send_header("retry-after", "0")
            self.end_headers()
            self.wfile.write(json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}).encode())
            return
        if server.reject:
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "message": "model: not a valid model"}}).encode())
            return
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(sse_bytes(STREAM))
            if server.mid_stream_error:
                self.wfile.write(sse_bytes([("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})]))
            return
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"content": [{"type": "text", "text": "Arm feedforward tuning"}]}).encode())


class ClaudeClientTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
        self.server.requests, self.server.fail_first, self.server.reject, self.server.mid_stream_error = [], 0, False, False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        session = requests.Session()
        session.trust_env = False  # don't send local test traffic through a proxy
        self.client = ClaudeClient("sk-test", base_url=f"http://127.0.0.1:{self.server.server_port}", session=session)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def collect(self, **kwargs):
        async def run():
            return [e async for e in self.client.stream(**kwargs)]
        return asyncio.run(run())

    def test_streaming_request_and_events(self):
        events = self.collect(model="claude-sonnet-5-5", system="Be helpful.", max_tokens=8000, effort="medium",
                              messages=[{"role": "user", "content": "Hi"}])
        text = "".join(e.text for e in events if e.kind == "text")
        self.assertEqual(text, "Hello, team ✓")
        usage = {}
        for e in events:
            if e.kind == "usage":
                usage.update({k: v for k, v in e.usage.items() if v is not None})
        self.assertEqual(usage["input_tokens"], 1200)
        self.assertEqual(usage["cache_read_input_tokens"], 900)
        self.assertEqual(usage["output_tokens"], 42)
        self.assertEqual(events[-1].kind, "done")

        sent = self.server.requests[0]
        self.assertEqual(sent["headers"]["x-api-key"], "sk-test")
        self.assertEqual(sent["headers"]["anthropic-version"], "2023-06-01")
        body = sent["body"]
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        self.assertEqual(body["max_tokens"], 8000)
        self.assertTrue(body["stream"])
        self.assertEqual(body["output_config"], {"effort": "medium"})
        self.assertEqual(body["system"], [{"type": "text", "text": "Be helpful.", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(body["messages"], [{"role": "user", "content": "Hi"}])

    def test_busy_api_is_retried(self):
        self.server.fail_first = 2
        events = self.collect(model="m", system="s", max_tokens=10, messages=[{"role": "user", "content": "Hi"}])
        self.assertEqual("".join(e.text for e in events if e.kind == "text"), "Hello, team ✓")
        self.assertEqual(len(self.server.requests), 3)

    def test_gives_up_after_retries(self):
        self.server.fail_first = 10
        events = self.collect(model="m", system="s", max_tokens=10, messages=[{"role": "user", "content": "Hi"}])
        self.assertEqual(events[-1].kind, "error")
        self.assertEqual(len(self.server.requests), 3)

    def test_bad_request_reports_the_api_message(self):
        self.server.reject = True
        events = self.collect(model="nope", system="s", max_tokens=10, messages=[{"role": "user", "content": "Hi"}])
        self.assertEqual([(e.kind, e.error) for e in events], [("error", "model: not a valid model")])
        with self.assertRaises(ClaudeError):
            asyncio.run(self.client.complete(model="nope", system="s", max_tokens=5, messages=[]))

    def test_error_event_mid_stream(self):
        self.server.mid_stream_error = True
        events = self.collect(model="m", system="s", max_tokens=10, messages=[{"role": "user", "content": "Hi"}])
        self.assertEqual(events[-1].kind, "error")
        self.assertEqual(events[-1].error, "Overloaded")

    def test_complete(self):
        text = asyncio.run(self.client.complete(model="claude-haiku-5-5", system="Title", max_tokens=30,
                                                messages=[{"role": "user", "content": "x"}]))
        self.assertEqual(text, "Arm feedforward tuning")
        body = self.server.requests[0]["body"]
        self.assertNotIn("stream", body)
        self.assertNotIn("output_config", body, "no effort setting unless asked")

    def test_unreachable_server(self):
        session = requests.Session()
        session.trust_env = False
        client = ClaudeClient("k", base_url="http://127.0.0.1:9", session=session, max_retries=0)

        async def run():
            return [e async for e in client.stream(model="m", system="s", max_tokens=5, messages=[])]
        events = asyncio.run(run())
        self.assertEqual(events[-1].kind, "error")
        self.assertIn("Couldn't reach Claude", events[-1].error)


class ParseSseTest(unittest.TestCase):
    def test_skips_thinking_and_comments(self):
        lines = [": keep-alive", "",
                 "event: content_block_delta",
                 'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"hmm"}}', "",
                 "event: content_block_delta",
                 'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Answer"}}', ""]
        events = list(parse_sse(lines))
        self.assertEqual([(e.kind, e.text) for e in events], [("text", "Answer")])

    def test_data_without_trailing_blank_line(self):
        events = list(parse_sse(['data: {"type":"message_stop"}']))
        self.assertEqual([e.kind for e in events], ["done"])


if __name__ == "__main__":
    unittest.main()
