"""The Anthropic gateway (subzero_ai/anthropic_gateway.py), with real sockets on 127.0.0.1 and a local fake upstream
that stands in for Anthropic. Nothing leaves this computer. The keys and tokens here are obvious fakes."""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import ssl
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from subzero_ai.anthropic_gateway import (
    AnthropicGateway,
    Credential,
    SessionPolicy,
    SSEUsageCounter,
    Usage,
    estimate_input_tokens,
    filter_headers,
    rewrite_request,
)
from subzero_ai.pricing import cost_usd

TOKEN = "test-sandbox-token"
KEY = "test-key"
MODEL = "claude-sonnet-5-5"

SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"m1","model":"claude-sonnet-5-5",'
       b'"usage":{"input_tokens":100,"cache_creation_input_tokens":10,"cache_read_input_tokens":20,'
       b'"output_tokens":1}}}\n\n'
       b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
       b'"delta":{"type":"text_delta","text":"Hi"}}\n\n'
       b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
       b'"usage":{"output_tokens":42}}\n\n'
       b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
JSON_REPLY = {"id": "m1", "type": "message", "model": MODEL, "content": [{"type": "text", "text": "ok"}],
              "usage": {"input_tokens": 7, "output_tokens": 9, "cache_read_input_tokens": 3,
                        "server_tool_use": {"web_search_requests": 2}}}


class FakeUpstream:
    """A local stand-in for api.anthropic.com that records every request and returns a canned reply."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.content_type = "application/json"
        self.payload = json.dumps(JSON_REPLY).encode()
        self.slow_chunks: list[bytes] | None = None  # sent one by one, with a pause, for the disconnect tests
        self.chunk_delay = 0.05
        self.delay = 0.0  # seconds before the reply starts
        self.hang_before_head = False  # wait (until close) before replying
        self.hang_after_first = False  # send the first slow chunk, then wait (until close)
        self.extra_headers: list[tuple[str, str]] = []
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                owner.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": body})
                if owner.hang_before_head:
                    owner.release.wait(30)
                    return
                if owner.delay:
                    threading.Event().wait(owner.delay)
                self.send_response(owner.status)
                self.send_header("Content-Type", owner.content_type)
                self.send_header("request-id", "req_test")
                for name, value in owner.extra_headers:
                    self.send_header(name, value)
                self.end_headers()
                if owner.slow_chunks is not None:
                    try:
                        for i, chunk in enumerate(owner.slow_chunks):
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            if i == 0 and owner.hang_after_first:
                                owner.release.wait(30)
                                return
                            threading.Event().wait(owner.chunk_delay)
                    except OSError:
                        pass
                    return
                self.wfile.write(owner.payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


class GatewayCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream = FakeUpstream()
        self.addCleanup(self.upstream.close)
        self.auth_calls: list[tuple[str, str]] = []
        self.tokens = {TOKEN: "session-1"}
        self.policy = SessionPolicy(allowed_models={"anthropic/" + MODEL}, allow_web_tools=False, cap_usd=None,
                                    spent_usd=0.0, extra_system="SAFETY RULES")
        self.policies: dict[str, SessionPolicy] = {"session-1": self.policy}
        self.records: list[tuple[str, Usage, float]] = []
        self.credential: Credential | None = Credential("api_key", KEY)
        self.gateway = self.make_gateway()
        self.assertTrue(await self.gateway.start())
        self.addAsyncCleanup(self.gateway.stop)

    def make_gateway(self, **kwargs) -> AnthropicGateway:
        def authenticate(token: str, peer: str):
            self.auth_calls.append((token, peer))
            return self.tokens.get(token)

        return AnthropicGateway(host="127.0.0.1", port=0, authenticate=authenticate,
                                policy=self.policies.get,
                                record=lambda key, usage, cost: self.records.append((key, usage, cost)),
                                credential=lambda: self.credential,
                                **{"upstream_base": self.upstream.url, **kwargs})

    async def call(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
                   token: str | None = TOKEN, raw: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        hdrs = dict(headers or {})
        if token is not None:
            hdrs.setdefault("Authorization", f"Bearer {token}")
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")

        def run():
            conn = http.client.HTTPConnection("127.0.0.1", self.gateway.port, timeout=20)
            try:
                conn.request(method, path, body=data, headers=hdrs)
                resp = conn.getresponse()
                return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
            finally:
                conn.close()

        return await asyncio.to_thread(run)

    async def restart(self, **kwargs) -> None:
        """Replace the gateway with one made with these settings."""
        await self.gateway.stop()
        self.gateway = self.make_gateway(**kwargs)
        self.assertTrue(await self.gateway.start())
        self.addAsyncCleanup(self.gateway.stop)

    async def until(self, condition, timeout: float = 5.0) -> bool:
        """Wait (polling) until condition() is true, up to a deadline."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def raw(self, data: bytes, close_after: bool = True) -> tuple[int, dict[str, str], bytes]:
        """Send raw bytes and read the whole reply (the gateway closes the connection after each reply)."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
        try:
            writer.write(data)
            reply = await asyncio.wait_for(reader.read(), 10)
        finally:
            writer.close()
        head, _, body = reply.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        return int(lines[0].split()[1]), headers, body

    def raw_request(self, target: str = "/v1/messages", extra: str = "", body: bytes | None = None,
                    host: str = "x") -> bytes:
        body = json.dumps(self.message()).encode() if body is None else body
        if "ontent-" not in extra and "ransfer-" not in extra:
            extra += f"Content-Length: {len(body)}\r\n"
        return (f"POST {target} HTTP/1.1\r\nHost: {host}\r\nAuthorization: Bearer {TOKEN}\r\n"
                f"Content-Type: application/json\r\n{extra}\r\n").encode() + body

    def message(self, **extra) -> dict[str, Any]:
        return {"model": MODEL, "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}], **extra}

    def sent(self) -> dict[str, Any]:
        self.assertEqual(len(self.upstream.requests), 1)
        return self.upstream.requests[0]

    def sent_json(self) -> dict[str, Any]:
        return json.loads(self.sent()["body"])

    # ---- auth
    async def test_missing_and_wrong_token_are_401_and_not_forwarded(self):
        for token in (None, "wrong"):
            status, _, body = await self.call("POST", "/v1/messages", self.message(), token=token)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["error"]["type"], "authentication_error")
        self.assertEqual(self.upstream.requests, [])

    async def test_x_api_key_token_is_accepted_and_peer_is_passed(self):
        status, _, _ = await self.call("POST", "/v1/messages", self.message(), token=None,
                                       headers={"x-api-key": TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(self.auth_calls, [(TOKEN, "127.0.0.1")])

    async def test_token_tied_to_peer(self):
        # an authenticator that only accepts the token from another address refuses this client
        self.gateway._authenticate = lambda token, peer: "session-1" if peer == "10.77.0.9" else None
        status, _, _ = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 401)
        self.assertEqual(self.upstream.requests, [])

    async def test_unknown_session_is_401(self):
        self.tokens[TOKEN] = "gone"
        status, _, _ = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 401)
        self.assertEqual(self.upstream.requests, [])

    # ---- credentials and headers
    async def test_client_credentials_never_reach_upstream_and_real_key_does(self):
        await self.call("POST", "/v1/messages?beta=true", self.message(), headers={
            "x-api-key": "client-secret", "Cookie": "c=1", "Proxy-Authorization": "x",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "foo", "X-Stainless-Lang": "js", "X-Other": "no"})
        got = self.sent()["headers"]
        self.assertEqual(got["x-api-key"], KEY)
        self.assertNotIn("authorization", got)
        self.assertNotIn("cookie", got)
        self.assertNotIn("proxy-authorization", got)
        self.assertNotIn("x-other", got)
        self.assertEqual(got["x-stainless-lang"], "js")
        self.assertEqual(got["anthropic-beta"], "foo")
        self.assertNotIn(TOKEN, json.dumps(got))
        self.assertNotIn("client-secret", json.dumps(got))

    async def test_subscription_credential(self):
        self.credential = Credential("subscription", "test-oauth")
        await self.call("POST", "/v1/messages", self.message(), headers={"anthropic-beta": "foo, oauth-2025-04-20"})
        got = self.sent()["headers"]
        self.assertEqual(got["authorization"], "Bearer test-oauth")
        self.assertNotIn("x-api-key", got)
        self.assertEqual(got["anthropic-beta"], "foo,oauth-2025-04-20")

    async def test_subscription_adds_beta_when_missing(self):
        self.credential = Credential("subscription", "test-oauth")
        await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(self.sent()["headers"]["anthropic-beta"], "oauth-2025-04-20")

    async def test_credential_not_configured_is_503(self):
        self.credential = None
        status, _, body = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["type"], "error")
        self.assertEqual(self.upstream.requests, [])

    # ---- rules
    async def test_disallowed_model_is_400_and_not_forwarded(self):
        status, _, body = await self.call("POST", "/v1/messages", self.message(model="claude-opus-5"))
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertIn(MODEL, error["message"])
        self.assertEqual(self.upstream.requests, [])

    async def test_non_json_body_is_400(self):
        status, _, _ = await self.call("POST", "/v1/messages", raw=b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])

    async def test_safety_rules_appended_for_each_system_shape(self):
        extra = {"type": "text", "text": "SAFETY RULES"}
        first = {"type": "text", "text": "first", "cache_control": {"type": "ephemeral"}}
        for system, expected in (("be nice", [{"type": "text", "text": "be nice"}, extra]),
                                 ([first, {"type": "text", "text": "two"}],
                                  [first, {"type": "text", "text": "two"}, extra]),
                                 (None, [extra])):
            self.upstream.requests.clear()
            body = self.message() if system is None else self.message(system=system)
            await self.call("POST", "/v1/messages", body)
            self.assertEqual(self.sent_json()["system"], expected)

    async def test_web_tools_stripped_only_when_not_allowed(self):
        tools = [{"name": "Bash", "input_schema": {}}, {"type": "web_search_20250305", "name": "web_search"},
                 {"type": "web_fetch_20250910", "name": "web_fetch"}]
        await self.call("POST", "/v1/messages", self.message(tools=tools))
        self.assertEqual([t["name"] for t in self.sent_json()["tools"]], ["Bash"])
        self.upstream.requests.clear()
        self.policy.allow_web_tools = True
        await self.call("POST", "/v1/messages", self.message(tools=tools))
        self.assertEqual(len(self.sent_json()["tools"]), 3)

    async def test_cap_reached_is_402_and_not_forwarded(self):
        self.policy.cap_usd = 1.0
        self.policy.spent_usd = lambda: 1.0
        status, _, body = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 402)
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "billing_error")
        self.assertEqual(error["message"], "This Code session has reached its spending cap.")
        self.assertEqual(self.upstream.requests, [])
        self.policy.spent_usd = lambda: 0.5
        status, _, _ = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 200)

    # ---- replies and usage
    async def test_streaming_passes_through_and_records_once(self):
        self.upstream.content_type = "text/event-stream"
        self.upstream.payload = SSE
        status, headers, body = await self.call("POST", "/v1/messages", self.message(stream=True))
        self.assertEqual(status, 200)
        self.assertEqual(body, SSE)
        self.assertEqual(headers.get("transfer-encoding"), "chunked")
        self.assertTrue(await self.until(lambda: self.records))
        self.assertEqual(len(self.records), 1)
        key, usage, cost = self.records[0]
        self.assertEqual(key, "session-1")
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens),
                         (100, 42, 20, 10))
        expected = cost_usd(MODEL, input_tokens=100, output_tokens=42, cache_read_tokens=20, cache_write_tokens=10)
        self.assertAlmostEqual(cost, expected)
        self.assertGreater(cost, 0)

    async def test_non_stream_usage_recorded(self):
        status, _, body = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], "m1")
        self.assertTrue(await self.until(lambda: self.records))
        self.assertEqual(len(self.records), 1)
        usage = self.records[0][1]
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.web_searches),
                         (7, 9, 3, 2))

    async def test_upstream_error_passed_through_not_recorded(self):
        self.upstream.status = 429
        self.upstream.payload = error = b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
        status, headers, body = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 429)
        self.assertEqual(body, error)
        self.assertEqual(headers.get("request-id"), "req_test")
        self.assertEqual(self.records, [])

    async def test_count_tokens_forwarded_not_recorded(self):
        self.upstream.payload = b'{"input_tokens": 12}'
        status, _, body = await self.call("POST", "/v1/messages/count_tokens", self.message())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"input_tokens": 12})
        self.assertEqual(self.sent()["path"], "/v1/messages/count_tokens")
        self.assertEqual(self.records, [])

    async def test_query_string_preserved(self):
        await self.call("POST", "/v1/messages?beta=true", self.message())
        self.assertEqual(self.sent()["path"], "/v1/messages?beta=true")
        self.upstream.requests.clear()
        await self.call("POST", "/v1/messages?beta=true&evil=1&x=%0d%0a", self.message())
        self.assertEqual(self.sent()["path"], "/v1/messages?beta=true")
        self.upstream.requests.clear()
        await self.call("POST", "/v1/messages?evil=1", self.message())
        self.assertEqual(self.sent()["path"], "/v1/messages")

    async def test_chunked_request_body(self):
        data = json.dumps(self.message()).encode()
        raw = (b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
               + b"\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
               + b"%x\r\n" % 10 + data[:10] + b"\r\n" + b"%x\r\n" % (len(data) - 10) + data[10:] + b"\r\n0\r\n\r\n")
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
        writer.write(raw)
        reply = await asyncio.wait_for(reader.read(), 10)
        writer.close()
        self.assertTrue(reply.startswith(b"HTTP/1.1 200"))
        self.assertEqual(self.sent_json()["model"], MODEL)

    # ---- local routes
    async def test_unknown_path_404(self):
        status, _, body = await self.call("POST", "/v1/other", self.message())
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["type"], "error")
        status, _, _ = await self.call("GET", "/v1/messages")
        self.assertEqual(status, 404)
        self.assertEqual(self.upstream.requests, [])

    async def test_models_lists_allowed(self):
        self.policy.allowed_models = {"anthropic/claude-sonnet-5-5", "claude-haiku-5-5"}
        status, _, body = await self.call("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in json.loads(body)["data"]], ["claude-haiku-5-5", "claude-sonnet-5-5"])
        status, _, _ = await self.call("GET", "/v1/models", token=None)
        self.assertEqual(status, 401)

    async def test_hello(self):
        for method in ("GET", "HEAD"):
            status, _, _ = await self.call(method, "/api/hello", token=None)
            self.assertEqual(status, 200)
        self.assertEqual(self.upstream.requests, [])

    async def test_oversized_body_is_413(self):
        await self.restart(max_body_bytes=1000)
        status, _, _ = await self.call("POST", "/v1/messages", self.message(system="x" * 5000))
        self.assertEqual(status, 413)
        self.assertEqual(self.upstream.requests, [])

    async def test_too_many_at_once_is_429(self):
        await self.restart(per_session_limit=0)
        status, _, _ = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 429)

    # ---- logs and disconnects
    async def test_no_secret_in_logs(self):
        logging.disable(logging.NOTSET)  # tests/__init__ turns logging off; this test checks the log lines
        self.addCleanup(logging.disable, logging.CRITICAL)
        self.upstream.status = 500
        self.upstream.payload = b"{}"
        with self.assertLogs("subzero.anthropic_gateway", level=logging.DEBUG) as logs:
            await self.call("POST", "/v1/messages", self.message())
            await self.call("POST", "/v1/messages", self.message(), token="wrong-token")
            await self.call("POST", "/v1/messages", self.message(model="nope"))
            self.assertTrue(await self.until(lambda: len(logs.output) >= 2))  # the 500 is logged after its reply
        text = "\n".join(logs.output)
        for secret in (KEY, TOKEN, "wrong-token"):
            self.assertNotIn(secret, text)

    async def test_client_disconnect_mid_stream_does_not_break_the_server(self):
        self.upstream.content_type = "text/event-stream"
        self.upstream.slow_chunks = [SSE[:200]] + [b": ping\n\n"] * 20
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
        data = json.dumps(self.message(stream=True)).encode()
        writer.write(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
                     + b"\r\nContent-Length: %d\r\n\r\n" % len(data) + data)
        await writer.drain()
        await asyncio.wait_for(reader.read(100), 10)
        writer.transport.abort()
        self.assertTrue(await self.until(lambda: self.records))
        self.upstream.slow_chunks = None
        self.upstream.content_type = "application/json"
        status, _, _ = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 200)

    # ---- accounting when the sandbox goes away
    def sse_events(self) -> list[bytes]:
        return [part + b"\n\n" for part in SSE.split(b"\n\n") if part]

    async def start_stream_and_abort(self, wait_for_bytes: int = 50) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
        data = json.dumps(self.message(stream=True)).encode()
        writer.write(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
                     + b"\r\nContent-Length: %d\r\n\r\n" % len(data) + data)
        await writer.drain()
        await asyncio.wait_for(reader.read(wait_for_bytes), 10)
        writer.transport.abort()

    async def test_abort_mid_stream_still_records_the_final_usage(self):
        self.upstream.content_type = "text/event-stream"
        self.upstream.slow_chunks = self.sse_events()
        self.upstream.chunk_delay = 0.15
        await self.start_stream_and_abort()
        self.assertTrue(await self.until(lambda: self.records))
        self.assertEqual(len(self.records), 1)
        usage = self.records[0][1]
        self.assertEqual((usage.input_tokens, usage.output_tokens), (100, 42))

    async def test_abort_before_message_delta_estimates_output(self):
        start, delta = self.sse_events()[:2]
        text = json.dumps({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": "x" * 400}}).encode()
        stop = b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        self.upstream.content_type = "text/event-stream"
        self.upstream.slow_chunks = [start, b"event: content_block_delta\ndata: " + text + b"\n\n", stop]
        self.upstream.chunk_delay = 0.2
        await self.start_stream_and_abort()
        self.assertTrue(await self.until(lambda: self.records))
        self.assertEqual(len(self.records), 1)
        self.assertGreaterEqual(self.records[0][1].output_tokens, 100)
        self.assertEqual(self.records[0][1].input_tokens, 100)

    async def test_cancel_before_the_reply_head_is_still_recorded(self):
        self.upstream.delay = 0.3
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
        writer.write(self.raw_request())
        await writer.drain()
        self.assertTrue(await self.until(lambda: self.upstream.requests))
        writer.transport.abort()
        self.assertTrue(await self.until(lambda: self.records))
        self.assertEqual(self.records[0][1].output_tokens, 9)

    async def test_priced_by_requested_model_not_the_dated_upstream_id(self):
        reply = dict(JSON_REPLY, model="claude-sonnet-5-5-20990101")
        self.upstream.payload = json.dumps(reply).encode()
        await self.call("POST", "/v1/messages", self.message())
        self.assertTrue(await self.until(lambda: self.records))
        _, usage, cost = self.records[0]
        self.assertEqual(usage.model, MODEL)
        self.assertEqual(usage.upstream_model, "claude-sonnet-5-5-20990101")
        self.assertGreater(cost, 0)
        self.assertAlmostEqual(cost, cost_usd(MODEL, input_tokens=7, output_tokens=9, cache_read_tokens=3,
                                              web_searches=2))

    async def test_one_hour_cache_writes_cost_double_input(self):
        usage = Usage(model=MODEL, cache_write_tokens=1000)
        five = usage.cost()
        usage.update({"cache_creation": {"ephemeral_1h_input_tokens": 1000}})
        self.assertAlmostEqual(usage.cost() - five, 1000 * 2.00 * (2 - 1.25) / 1_000_000)
        usage.update({"server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 2}})
        self.assertEqual(usage.web_searches, 3)

    async def test_unpriced_model_is_refused(self):
        self.policy.allowed_models = {"claude-mystery-1"}
        status, _, body = await self.call("POST", "/v1/messages", self.message(model="claude-mystery-1"))
        self.assertEqual(status, 400)
        self.assertIn("price", json.loads(body)["error"]["message"])
        self.assertEqual(self.upstream.requests, [])

    # ---- stop, limits
    async def test_stop_returns_quickly_while_upstream_hangs_mid_stream(self):
        self.upstream.content_type = "text/event-stream"
        self.upstream.slow_chunks = self.sse_events()
        self.upstream.hang_after_first = True
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
        writer.write(self.raw_request())
        await writer.drain()
        await asyncio.wait_for(reader.read(50), 10)
        started = asyncio.get_running_loop().time()
        await self.gateway.stop()
        self.assertLess(asyncio.get_running_loop().time() - started, 2)
        writer.close()

    async def test_stop_returns_quickly_while_upstream_hangs_before_the_head(self):
        self.upstream.hang_before_head = True
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
        writer.write(self.raw_request())
        await writer.drain()
        self.assertTrue(await self.until(lambda: self.upstream.requests))
        started = asyncio.get_running_loop().time()
        await self.gateway.stop()
        self.assertLess(asyncio.get_running_loop().time() - started, 2)
        writer.close()

    async def test_total_connection_limit(self):
        await self.restart(total_limit=2)
        idle = [await asyncio.open_connection("127.0.0.1", self.gateway.port) for _ in range(2)]
        self.assertTrue(await self.until(lambda: self.gateway._connections == 2))
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
            reply = await asyncio.wait_for(reader.read(), 10)  # refused at once, before it sends anything
            writer.close()
            self.assertTrue(reply.startswith(b"HTTP/1.1 503"))
        finally:
            for _, writer in idle:
                writer.close()
        self.assertTrue(await self.until(lambda: self.gateway._connections == 0))
        status, _, _ = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 200)

    async def test_global_concurrency_limit(self):
        await self.restart(max_concurrent=1)
        self.upstream.delay = 0.5
        first = asyncio.create_task(self.call("POST", "/v1/messages", self.message()))
        self.assertTrue(await self.until(lambda: self.gateway._active))
        status, _, _ = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 429)
        self.assertEqual((await first)[0], 200)

    async def test_rate_limit_per_session(self):
        now = [1000.0]
        await self.restart(rate_per_minute=2, clock=lambda: now[0])
        for _ in range(2):
            self.assertEqual((await self.call("POST", "/v1/messages", self.message()))[0], 200)
        status, headers, _ = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 429)
        self.assertGreaterEqual(int(headers["retry-after"]), 1)
        now[0] += 61
        self.assertEqual((await self.call("POST", "/v1/messages", self.message()))[0], 200)

    async def test_reservations_stop_parallel_requests_overshooting_the_cap(self):
        self.policy.cap_usd = 0.035  # one request is reserved at about $0.0101 (1000 output tokens)
        self.upstream.delay = 0.6
        results = await asyncio.gather(*[self.call("POST", "/v1/messages", self.message(max_tokens=1000))
                                         for _ in range(8)])
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, [200] * 3 + [402] * 5)
        self.assertEqual(len(self.upstream.requests), 3)
        self.assertTrue(await self.until(lambda: len(self.records) == 3))
        self.assertEqual(self.gateway._reserved, {})  # all released
        self.assertEqual(self.gateway._active, {})

    async def test_request_without_max_tokens_reserves_the_default(self):
        self.policy.cap_usd = 0.25  # the default 32000 output tokens would cost about $0.32
        body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
        status, _, _ = await self.call("POST", "/v1/messages", body)
        self.assertEqual(status, 402)
        self.assertEqual(self.upstream.requests, [])

    # ---- request parsing
    async def test_odd_request_targets_never_reach_upstream_with_a_changed_path(self):
        for target in ("http://evil.example/v1/messages", "//evil/v1/messages", "/v1/messages/../x",
                       "/v1/messages%2Fcount_tokens", "/v1/messages/", "v1/messages", "*"):
            status, _, _ = await self.raw(self.raw_request(target))
            self.assertEqual(status, 404, target)
        self.assertEqual(self.upstream.requests, [])

    async def test_hostile_host_header_is_ignored(self):
        status, _, _ = await self.raw(self.raw_request(host="evil.example"))
        self.assertEqual(status, 200)
        got = self.sent()
        self.assertEqual(got["path"], "/v1/messages")
        self.assertTrue(got["headers"]["host"].startswith("127.0.0.1:"))

    async def test_transfer_encoding_with_content_length_is_400(self):
        extra = "Transfer-Encoding: chunked\r\nContent-Length: 5\r\n"
        status, _, _ = await self.raw(self.raw_request(extra=extra, body=b"0\r\n\r\n"))
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])

    async def test_duplicate_content_length_is_400(self):
        body = json.dumps(self.message()).encode()
        extra = f"Content-Length: {len(body)}\r\nContent-Length: {len(body)}\r\n"
        status, _, _ = await self.raw(self.raw_request(extra=extra, body=body))
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])

    async def test_bad_content_length_and_chunk_framing_are_400(self):
        for extra, body in (("Content-Length: -5\r\n", b"x"), ("Content-Length: 0x10\r\n", b"x"),
                            ("Transfer-Encoding: chunked\r\n", b"zz\r\nabc\r\n0\r\n\r\n"),
                            ("Transfer-Encoding: chunked\r\n", b"3\r\nabcXX0\r\n\r\n"),
                            ("Transfer-Encoding: chunked\r\n", b"+3\r\nabc\r\n0\r\n\r\n"),
                            ("Transfer-Encoding: gzip, chunked\r\n", b"0\r\n\r\n")):
            status, _, _ = await self.raw(self.raw_request(extra=extra, body=body))
            self.assertEqual(status, 400, (extra, body))
        self.assertEqual(self.upstream.requests, [])

    async def test_endless_chunk_trailers_are_400(self):
        trailers = b"X: y\r\n" * 100
        status, _, _ = await self.raw(self.raw_request(extra="Transfer-Encoding: chunked\r\n",
                                                       body=b"0\r\n" + trailers + b"\r\n"))
        self.assertEqual(status, 400)

    async def test_slow_body_hits_the_overall_deadline(self):
        await self.restart(body_total_seconds=0.3)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
        writer.write(f"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                     "Content-Length: 100\r\n\r\nabc".encode())
        reply = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        self.assertTrue(reply.startswith(b"HTTP/1.1 408"))

    async def test_model_ids_must_match_exactly(self):
        for model in ("Claude-Sonnet-5-5", MODEL + " ", "anthropic/" + MODEL, " " + MODEL, ""):
            status, _, _ = await self.call("POST", "/v1/messages", self.message(model=model))
            self.assertEqual(status, 400, repr(model))
        self.assertEqual(self.upstream.requests, [])

    async def test_system_must_be_text_or_a_list(self):
        for system in ({"type": "text", "text": "x"}, 5):
            status, _, _ = await self.call("POST", "/v1/messages", self.message(system=system))
            self.assertEqual(status, 400, repr(system))
        self.assertEqual(self.upstream.requests, [])

    # ---- what the rules strip or refuse (D77)
    async def test_mcp_servers_and_mcp_tools_always_stripped(self):
        self.policy.allow_web_tools = True
        tools = [{"name": "Bash", "input_schema": {}}, {"type": "mcp_toolset", "mcp_server_name": "x"}]
        await self.call("POST", "/v1/messages", self.message(
            tools=tools, mcp_servers=[{"type": "url", "url": "http://evil.example", "name": "x"}]))
        sent = self.sent_json()
        self.assertNotIn("mcp_servers", sent)
        self.assertEqual([t["name"] for t in sent["tools"]], ["Bash"])

    async def test_service_tier_and_speed_are_refused(self):
        for extra in ({"service_tier": "priority"}, {"speed": "fast"}):
            status, _, _ = await self.call("POST", "/v1/messages", self.message(**extra))
            self.assertEqual(status, 400, extra)
        self.assertEqual(self.upstream.requests, [])
        for extra in ({"service_tier": "auto"}, {"service_tier": "standard_only"}, {"speed": "standard"}):
            self.upstream.requests.clear()
            status, _, _ = await self.call("POST", "/v1/messages", self.message(**extra))
            self.assertEqual(status, 200, extra)

    async def test_url_sources_refused_when_web_is_off(self):
        image = {"type": "image", "source": {"type": "url", "url": "http://evil.example/a.png"}}
        nested = {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": "x"}, image]}
        for content in ([image], [nested]):
            body = self.message(messages=[{"role": "user", "content": content}])
            status, _, _ = await self.call("POST", "/v1/messages", body)
            self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])
        self.policy.allow_web_tools = True
        status, _, _ = await self.call("POST", "/v1/messages",
                                       self.message(messages=[{"role": "user", "content": [image]}]))
        self.assertEqual(status, 200)
        base64 = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
        self.policy.allow_web_tools = False
        self.upstream.requests.clear()
        status, _, _ = await self.call("POST", "/v1/messages",
                                       self.message(messages=[{"role": "user", "content": [base64]}]))
        self.assertEqual(status, 200)

    # ---- replies
    async def test_only_allowed_reply_headers_come_back(self):
        self.upstream.extra_headers = [("Set-Cookie", "a=1"), ("anthropic-ratelimit-x", "9"), ("X-Evil", "1"),
                                       ("anthropic-organization-id", "org"), ("retry-after-ms", "5")]
        _, headers, _ = await self.call("POST", "/v1/messages", self.message())
        self.assertNotIn("set-cookie", headers)
        self.assertNotIn("x-evil", headers)
        self.assertNotIn("anthropic-organization-id", headers)
        self.assertEqual(headers["anthropic-ratelimit-x"], "9")
        self.assertEqual(headers["request-id"], "req_test")
        self.assertEqual(headers["retry-after-ms"], "5")

    async def test_a_redirect_from_upstream_is_not_followed(self):
        self.upstream.status = 307
        self.upstream.extra_headers = [("Location", "/elsewhere")]
        self.upstream.payload = b""
        status, headers, _ = await self.call("POST", "/v1/messages", self.message())
        self.assertEqual(status, 307)
        self.assertNotIn("location", headers)
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(self.records, [])


    # ---- the upstream address, the cap estimate (round 2)
    async def test_upstream_address_must_be_https_or_local_http(self):
        for base in ("http://evil.example", "api.anthropic.com", "ftp://x", "htps://api.anthropic.com",
                     "https://host:abc", "http://[::1", "",
                     "https://"):
            gateway = self.make_gateway(upstream_base=base)
            self.assertFalse(await gateway.start(), base)
            self.assertTrue(gateway.error)
            self.assertFalse(gateway.running)
        for base in ("https://api.anthropic.com", "http://localhost:9", "http://127.0.0.1:9", "http://[::1]:9"):
            gateway = self.make_gateway(upstream_base=base)
            self.assertTrue(await gateway.start(), base)
            await gateway.stop()
        self.assertEqual(self.upstream.requests, [])

    async def test_stray_byte_after_the_request_does_not_cut_the_reply(self):
        self.upstream.delay = 0.2
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
        writer.write(self.raw_request() + b"X")
        reply = await asyncio.wait_for(reader.read(), 10)
        writer.close()
        self.assertTrue(reply.startswith(b"HTTP/1.1 200"))
        self.assertIn(b'"id": "m1"', reply)
        self.assertTrue(await self.until(lambda: self.records))

    async def test_https_verifies_certificates_and_host_names(self):
        gateway = self.make_gateway(upstream_base="https://api.anthropic.com")
        self.assertEqual(gateway._ssl.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(gateway._ssl.check_hostname)

    async def test_reservation_uses_the_long_prompt_rate(self):
        self.policy.allowed_models = {"claude-haiku-5-5"}
        self.policy.cap_usd = 0.03  # ~133k tokens: $0.013 at 0.10/M, but $0.067 at the long-prompt 0.50/M
        body = self.message(model="claude-haiku-5-5", messages=[{"role": "user", "content": "a" * 400_000}])
        status, _, reply = await self.call("POST", "/v1/messages", body)
        self.assertEqual(status, 402)
        self.assertEqual(json.loads(reply)["error"]["message"], "This request could go over this Code session's "
                                                                  "spending cap.")
        self.assertEqual(self.upstream.requests, [])
        self.policy.cap_usd = 0.1
        status, _, _ = await self.call("POST", "/v1/messages", body)
        self.assertEqual(status, 200)

    async def test_base64_media_is_not_counted_as_text(self):
        self.policy.cap_usd = 0.1  # 600 KB as text would be ~200k tokens, about $0.40
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 600_000}}
        status, _, _ = await self.call("POST", "/v1/messages",
                                       self.message(messages=[{"role": "user", "content": [image]}]))
        self.assertEqual(status, 200)

    async def test_input_estimate_counts_media_and_is_clamped(self):
        small = {"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64",
                                                                                     "data": "A" * 3000}}]}]}
        self.assertEqual(estimate_input_tokens(small, 3200), 200 // 3 + 1600)
        self.assertEqual(estimate_input_tokens({"messages": []}, 50_000_000), 1_000_000)

    async def test_web_search_reservation_uses_max_uses(self):
        self.policy.allow_web_tools = True
        self.policy.cap_usd = 0.04
        search = {"type": "web_search_20250305", "name": "web_search"}
        status, _, _ = await self.call("POST", "/v1/messages", self.message(tools=[{**search, "max_uses": 5}]))
        self.assertEqual(status, 402)  # 5 searches would be $0.05
        status, _, _ = await self.call("POST", "/v1/messages", self.message(tools=[search]))
        self.assertEqual(status, 402)  # no max_uses: 5 assumed
        status, _, _ = await self.call("POST", "/v1/messages", self.message(tools=[{**search, "max_uses": 1}]))
        self.assertEqual(status, 200)

    async def test_an_abandoned_stream_is_cut_at_finish_seconds_and_recorded(self):
        await self.restart(finish_seconds=0.3)
        start = self.sse_events()[0]
        delta = (b'event: content_block_delta\ndata: ' + json.dumps({
            "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x" * 400}}).encode()
            + b"\n\n")
        self.upstream.content_type = "text/event-stream"
        self.upstream.slow_chunks = [start + delta, b"never sent"]
        self.upstream.hang_after_first = True
        await self.start_stream_and_abort()
        self.assertTrue(await self.until(lambda: self.records, 5))
        self.assertEqual(len(self.records), 1)
        usage = self.records[0][1]
        self.assertEqual(usage.input_tokens, 100)
        self.assertGreaterEqual(usage.output_tokens, 100)  # estimated from the 400 characters
        self.assertTrue(await self.until(lambda: not self.gateway._active))

    async def test_stop_clears_open_calls_and_reservations(self):
        self.policy.cap_usd = 5.0
        self.upstream.hang_before_head = True
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
        writer.write(self.raw_request())
        await writer.drain()
        self.assertTrue(await self.until(lambda: self.upstream.requests))
        self.assertTrue(self.gateway._reserved)
        await self.gateway.stop()
        self.assertEqual((self.gateway._active, self.gateway._reserved, self.gateway._calls), ({}, {}, set()))
        writer.close()


class HelperTests(unittest.TestCase):
    def test_rewrite_does_not_change_the_input(self):
        original = {"model": MODEL, "system": "a", "tools": [{"type": "web_search_1"}]}
        out = rewrite_request(original, allowed_models={MODEL}, allow_web_tools=False, extra_system="rules")
        self.assertEqual(original["system"], "a")
        self.assertEqual(out["tools"], [])
        self.assertEqual(out["system"][0], {"type": "text", "text": "a"})
        self.assertNotIn("cache_control", out["system"][-1])

    def test_filter_headers_allow_list(self):
        got = filter_headers({"Authorization": "x", "Host": "h", "Content-Length": "1", "Connection": "keep-alive",
                              "Accept": "*/*", "User-Agent": "ua", "X-App": "cli", "X-Stainless-OS": "linux"})
        self.assertEqual(got, {"accept": "*/*", "user-agent": "ua", "x-app": "cli", "x-stainless-os": "linux"})

    def test_sse_counter_handles_split_chunks_and_latest_delta(self):
        counter = SSEUsageCounter()
        for i in range(0, len(SSE), 7):
            counter.feed(SSE[i:i + 7])
        usage = counter.usage
        self.assertEqual((usage.upstream_model, usage.input_tokens, usage.output_tokens), (MODEL, 100, 42))


if __name__ == "__main__":
    unittest.main()
