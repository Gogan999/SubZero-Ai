"""The Anthropic gateway for Code sessions. The Claude Agent SDK inside a sandbox VM is pointed here
(ANTHROPIC_BASE_URL) and sends the sandbox's token instead of a real credential. For each request the gateway
checks the token, applies the team's rules (allowed models, safety rules, web tools, spending cap), adds the
real credential and forwards the call to Anthropic. Usage is counted from the reply and handed to a callback.
The real credential never leaves the host.

The server is hand-written on asyncio streams, like egress_proxy.py. The call to Anthropic runs in a worker thread
(http.client, in the gateway's own thread pool) that keeps reading until the reply is finished, even when the sandbox
has gone away, so usage is always counted. The reply is passed on chunk by chunk. The gateway connects to Anthropic
directly: it ignores HTTPS_PROXY and the other proxy settings.

authenticate, policy, record and credential run on the event loop, so they must not block."""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import re
import socket
import ssl
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

from .pricing import WEB_SEARCH_PRICE, cost_usd, price_for

log = logging.getLogger("subzero.anthropic_gateway")

MAX_HEAD_BYTES = 16 * 1024  # the request line and headers together
MAX_BODY_BYTES = 32 * 1024 * 1024  # whole conversations, with images
MAX_TOTAL_CONNECTIONS = 64
MAX_CONCURRENT = 16  # requests being answered at once, in all sessions
PER_SESSION_LIMIT = 8
RATE_PER_MINUTE = 120  # requests per Code session
HEAD_SECONDS = 30.0
BODY_SECONDS = 120.0  # longest wait for the next piece of the request body
BODY_TOTAL_SECONDS = 300.0  # longest wait for a whole request body
WRITE_SECONDS = 60.0  # longest wait for the sandbox to take what is written to it
UPSTREAM_SECONDS = 600.0  # longest wait for Anthropic to send something
FINISH_SECONDS = 60.0  # how long to keep reading a reply after the sandbox has gone, so its usage is counted
STOP_SECONDS = 5.0
DEFAULT_MAX_TOKENS = 32000  # assumed when a request doesn't say
MEDIA_TOKENS = 1600  # assumed per image or document block when estimating a request's input
MAX_INPUT_ESTIMATE = 1_000_000  # tokens
DEFAULT_SEARCH_USES = 5  # web searches assumed for a search tool that sets no max_uses
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
OVER_CAP_MESSAGE = "This request could go over this Code session's spending cap."
TOKEN_BYTES = 3  # bytes per input token when estimating from a body's size (pessimistic)
API_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"
CAP_MESSAGE = "This Code session has reached its spending cap."

# Request headers that are passed on to Anthropic. Everything else (credentials, cookies, host, hop-by-hop) is dropped.
FORWARDED_HEADERS = ("anthropic-version", "anthropic-beta", "content-type", "accept", "user-agent", "x-app",
                     "anthropic-dangerous-direct-browser-access")
FORWARDED_PREFIXES = ("x-stainless-",)
# Reply headers that are passed back to the sandbox.
REPLY_HEADERS = ("content-type", "request-id", "retry-after", "retry-after-ms", "x-should-retry")
REPLY_PREFIXES = ("anthropic-ratelimit-",)

_CHUNK_SIZE = re.compile(rb"[0-9a-fA-F]{1,8}")
_LENGTH = re.compile(r"[0-9]{1,12}")
_UNIQUE_HEADERS = ("content-length", "transfer-encoding", "host", "authorization", "x-api-key", "content-type")
_ROUTES = (("POST", "/v1/messages"), ("POST", "/v1/messages/count_tokens"), ("GET", "/v1/models"))
_NOT_FOUND = (404, b'{"type":"error","error":{"type":"not_found_error","message":"Not found."}}')


class GatewayError(Exception):
    """A refusal, answered in Anthropic's error JSON shape."""

    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


def error_body(kind: str, message: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()


# --------------------------------------------------------------------------- data
@dataclass
class Usage:
    """Token counts for one reply. Input tokens exclude cache reads and writes, as the API reports them. `model` is the
    model that was asked for (it prices the reply); `upstream_model` is the id Anthropic answered with (display
    only)."""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0  # all cache writes, 5-minute and 1-hour
    cache_write_1h_tokens: int = 0  # the part of cache_write_tokens that went to the 1-hour cache (priced at 2x)
    web_searches: int = 0  # web searches and web fetches
    upstream_model: str = ""

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def update(self, raw: Any) -> None:
        """Take the values present in an API `usage` object (later values replace earlier ones: they're cumulative)."""
        if not isinstance(raw, dict):
            return
        for key, attr in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                          ("cache_read_input_tokens", "cache_read_tokens"),
                          ("cache_creation_input_tokens", "cache_write_tokens")):
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self, attr, value)
        created = raw.get("cache_creation")
        if isinstance(created, dict) and isinstance(created.get("ephemeral_1h_input_tokens"), int):
            self.cache_write_1h_tokens = created["ephemeral_1h_input_tokens"]
        server = raw.get("server_tool_use")
        if isinstance(server, dict):
            self.web_searches = sum(v for k, v in server.items()
                                    if k in ("web_search_requests", "web_fetch_requests")
                                    and isinstance(v, int) and not isinstance(v, bool))

    def cost(self) -> float:
        """Estimated dollars; 0.0 when the model's price isn't known (the gateway never forwards such a model)."""
        price = price_for(self.model)
        if price is None:
            return 0.0
        hour = min(max(self.cache_write_1h_tokens, 0), self.cache_write_tokens)
        base = cost_usd(self.model, input_tokens=self.input_tokens, output_tokens=self.output_tokens,
                        cache_read_tokens=self.cache_read_tokens, cache_write_tokens=self.cache_write_tokens - hour,
                        web_searches=self.web_searches) or 0.0
        return base + hour * price.input * 2 / 1_000_000


@dataclass
class Credential:
    kind: str  # "api_key" or "subscription"
    secret: str = field(repr=False)


@dataclass
class SessionPolicy:
    """What one Code session may do. spent_usd may be a callable so the live value is read at each request."""
    allowed_models: set[str] | frozenset[str] | list[str] = field(default_factory=set)
    allow_web_tools: bool = False
    cap_usd: float | None = None
    spent_usd: float | Callable[[], float] = 0.0
    extra_system: str = ""
    on_cap: Callable[[], None] | None = None  # called when a request is refused for the spending cap (D92)

    def spent(self) -> float:
        return float(self.spent_usd() if callable(self.spent_usd) else self.spent_usd)

    def models(self) -> list[str]:
        return sorted({bare_model(m) for m in self.allowed_models if bare_model(m)})


Authenticator = Callable[[str, str], "str | None"]
PolicyProvider = Callable[[str], "SessionPolicy | None"]
Recorder = Callable[[str, Usage, float], None]
CredentialProvider = Callable[[], "Credential | None"]


# --------------------------------------------------------------------------- pure helpers
def bare_model(value: object) -> str:
    """A model id without the provider prefix the app's settings use ('anthropic/claude-x' -> 'claude-x'). Used for
    settings values only; a model id in a request must match exactly."""
    text = str(value or "").strip()
    return text[len("anthropic/"):] if text.startswith("anthropic/") else text


def filter_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Only the allow-listed request headers, lower-cased. Credentials never pass."""
    out: dict[str, str] = {}
    for name, value in headers.items():
        low = name.lower()
        if low in FORWARDED_HEADERS or low.startswith(FORWARDED_PREFIXES):
            out[low] = value
    return out


def apply_credential(headers: dict[str, str], credential: Credential) -> dict[str, str]:
    """Headers with the real credential added. A subscription token also needs the OAuth beta flag."""
    out = dict(headers)
    if credential.kind == "subscription":
        out["authorization"] = f"Bearer {credential.secret}"
        flags = [f.strip() for f in out.get("anthropic-beta", "").split(",") if f.strip()]
        if OAUTH_BETA not in flags:
            flags.append(OAUTH_BETA)
        out["anthropic-beta"] = ",".join(flags)
    else:
        out["x-api-key"] = credential.secret
    out.setdefault("anthropic-version", API_VERSION)
    return out


def _has_url_source(messages: Any) -> bool:
    """True if any content block in the messages (tool results included) loads something from a URL."""
    stack: list[Any] = [messages]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            source = node.get("source")
            if isinstance(source, dict) and source.get("type") == "url":
                return True
            if "content" in node:
                stack.append(node["content"])
    return False


def estimate_input_tokens(payload: dict[str, Any], size: int) -> int:
    """A pessimistic input size for a request of `size` bytes: bytes / 3, but base64 media isn't text, so its bytes are
    left out and each image or document block counts a flat MEDIA_TOKENS instead."""
    media = 0
    stack: list[Any] = [payload.get("messages")]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            source = node.get("source")
            if isinstance(source, dict) and source.get("type") == "base64" and isinstance(source.get("data"), str):
                size -= len(source["data"])
            if node.get("type") in ("image", "document"):
                media += 1
            if "content" in node:
                stack.append(node["content"])
    return min(max(size, 0) // TOKEN_BYTES + media * MEDIA_TOKENS, MAX_INPUT_ESTIMATE)


def search_uses(payload: dict[str, Any]) -> int:
    """Web searches the request's search tools may run (their max_uses, or a default when unset)."""
    uses = 0
    for tool in payload.get("tools") or []:
        if isinstance(tool, dict) and str(tool.get("type", "")).startswith("web_search"):
            wanted = tool.get("max_uses")
            uses += wanted if isinstance(wanted, int) and not isinstance(wanted, bool) and wanted > 0 \
                else DEFAULT_SEARCH_USES
    return uses


def _tool_allowed(tool: Any, allow_web_tools: bool) -> bool:
    """D93: only client tools (a plain definition, or type "custom") pass, and the web search and fetch server tools
    when web is on. Every other server tool type (code execution, MCP, computer use and so on) is removed."""
    if not isinstance(tool, dict):
        return True  # not a tool definition at all: Anthropic refuses it
    kind = tool.get("type")
    if kind is None or kind == "custom":
        return True
    return bool(allow_web_tools) and isinstance(kind, str) and kind.startswith(("web_search", "web_fetch"))


def typical_request_cost(model: str) -> float:
    """What the gateway reserves for the smallest request this model can get: a short prompt and the default
    max_tokens. A session this close to its cap would be refused by the gateway anyway. 0.0 for an unpriced model."""
    price = price_for(model)
    if price is None:
        return 0.0
    return (price.input * 1000 + price.output * DEFAULT_MAX_TOKENS) / 1_000_000


def rewrite_request(payload: dict[str, Any], *, allowed_models: Any, allow_web_tools: bool,
                    extra_system: str = "") -> dict[str, Any]:
    """The request body after the team's rules are applied. Raises GatewayError for a refused request."""
    bad = "invalid_request_error"
    allowed = {bare_model(m) for m in allowed_models if bare_model(m)}
    model = payload.get("model")
    if not isinstance(model, str) or model not in allowed:
        names = ", ".join(sorted(allowed)) or "none"
        raise GatewayError(400, bad, f"This Code session can't use that model. The models it can use are: {names}.")
    system = payload.get("system")
    if system is not None and not isinstance(system, (str, list)):
        raise GatewayError(400, bad, "The system prompt must be text or a list of text blocks.")
    tier = payload.get("service_tier")
    if tier is not None and tier not in ("auto", "standard_only"):
        raise GatewayError(400, bad, "This Code session can't change the service tier.")
    speed = payload.get("speed")
    if speed is not None and speed != "standard":
        raise GatewayError(400, bad, "This Code session can't change the speed.")
    if not allow_web_tools and _has_url_source(payload.get("messages")):
        raise GatewayError(400, bad, "This Code session can't load images or documents from web addresses.")
    out = dict(payload)
    out.pop("mcp_servers", None)  # Anthropic's servers would connect to any address named here
    tools = out.get("tools")
    if isinstance(tools, list):
        out["tools"] = [t for t in tools if _tool_allowed(t, allow_web_tools)]
    if extra_system.strip():
        block = {"type": "text", "text": extra_system}
        if isinstance(system, str):
            out["system"] = [{"type": "text", "text": system}, block] if system else [block]
        elif isinstance(system, list):
            out["system"] = [*system, block]
        else:
            out["system"] = [block]
    return out


class SSEUsageCounter:
    """Reads usage out of a server-sent-event stream as its bytes go past. It never changes them."""

    def __init__(self) -> None:
        self.usage = Usage()
        self._buffer = b""
        self.started = False  # message_start seen
        self.final = False  # message_delta or message_stop seen: the usage is complete
        self.delta_chars = 0  # characters of generated content seen, to estimate output if no message_delta comes

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        *lines, self._buffer = self._buffer.split(b"\n")
        if len(self._buffer) > 8 * 1024 * 1024:  # a line that never ends isn't an event
            self._buffer = b""
        for line in lines:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "message_start" and isinstance(event.get("message"), dict):
                message = event["message"]
                if isinstance(message.get("model"), str):
                    self.usage.upstream_model = message["model"]
                self.usage.update(message.get("usage"))
                self.started = True
            elif kind == "message_delta":
                self.usage.update(event.get("usage"))
                self.final = True
            elif kind == "message_stop":
                self.final = True
            elif kind == "content_block_delta" and isinstance(event.get("delta"), dict):
                for key in ("text", "thinking", "partial_json"):
                    if isinstance(event["delta"].get(key), str):
                        self.delta_chars += len(event["delta"][key])

    def snapshot(self) -> Usage:
        """The usage so far. Without a message_delta, output is estimated from the content seen (about 4 chars each)."""
        usage = Usage(**vars(self.usage))
        if not self.final:
            usage.output_tokens = max(usage.output_tokens, (self.delta_chars + 3) // 4)
        return usage


def json_usage(body: bytes) -> Usage | None:
    """Usage from a non-streaming reply, or None when the body isn't a message."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("usage"), dict):
        return None
    usage = Usage(upstream_model=str(data.get("model") or ""))
    usage.update(data["usage"])
    return usage


def _clean(text: str, limit: int = 200) -> str:
    return "".join(c if c.isprintable() else "?" for c in text)[:limit]


# --------------------------------------------------------------------------- the server
@dataclass
class _Request:
    method: str
    path: str
    query: str
    headers: dict[str, str]


@dataclass(eq=False)
class _Conn:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    sent: bool = False  # a reply has started, so no other reply may be written


@dataclass(eq=False)
class _Call:
    """One request on its way to Anthropic. The worker thread fills it in; the loop thread reads it."""
    key: str
    model: str
    target: str
    headers: dict[str, str]
    data: bytes
    max_tokens: int
    counted: bool
    reserved: float
    queue: asyncio.Queue[tuple[str, Any]] = field(default_factory=asyncio.Queue)
    counter: SSEUsageCounter = field(default_factory=SSEUsageCounter)
    body: bytearray = field(default_factory=bytearray)
    status: int | None = None
    streaming: bool = False
    sent: bool = False  # the request has gone to Anthropic, so it may be billed
    complete: bool = False
    abandoned: bool = False
    conn: http.client.HTTPConnection | None = None
    sock: socket.socket | None = None  # kept here: http.client forgets it once a reply that will close has begun
    timer: asyncio.TimerHandle | None = None
    closed: threading.Event = field(default_factory=threading.Event)

    def close(self) -> None:
        """Cut the connection to Anthropic. Safe from any thread."""
        self.closed.set()
        conn, sock = self.conn, self.sock
        try:
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


class AnthropicGateway:
    """The gateway. Start and stop it on the app's event loop.

    authenticate(token, peer_ip) returns the session key the token belongs to, or None. policy(session_key) returns the
    session's SessionPolicy, or None for an unknown session. record(session_key, usage, cost_usd) is called once per
    counted reply. credential() returns the real Credential, or None when none is configured (the sandbox then gets a
    503 with a plain message). All four run on the event loop and must not block."""

    def __init__(self, *, host: str, port: int, authenticate: Authenticator, policy: PolicyProvider,
                 record: Recorder, credential: CredentialProvider,
                 upstream_base: str = "https://api.anthropic.com", max_body_bytes: int = MAX_BODY_BYTES,
                 per_session_limit: int = PER_SESSION_LIMIT, max_concurrent: int = MAX_CONCURRENT,
                 total_limit: int = MAX_TOTAL_CONNECTIONS, rate_per_minute: int = RATE_PER_MINUTE,
                 head_seconds: float = HEAD_SECONDS, body_seconds: float = BODY_SECONDS,
                 body_total_seconds: float = BODY_TOTAL_SECONDS, write_seconds: float = WRITE_SECONDS,
                 upstream_seconds: float = UPSTREAM_SECONDS, finish_seconds: float = FINISH_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.host = host
        self.port = port  # the port asked for; once started, the port actually bound
        self._authenticate = authenticate
        self._policy = policy
        self._record = record
        self._credential = credential
        self._config_error = ""
        try:
            parts = urlsplit(upstream_base)
            port = parts.port  # raises ValueError for a malformed port
        except ValueError:
            parts, port = urlsplit(""), None
        if not parts.hostname or not (parts.scheme == "https"
                                      or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)):
            self._config_error = ("The Anthropic address for Code sessions must start with https:// (http:// is only "
                                  "allowed for this computer).")
        self._https = parts.scheme == "https"
        self._upstream_host = parts.hostname or ""
        self._upstream_port = port or (443 if self._https else 80)
        self._upstream_prefix = parts.path.rstrip("/")
        self._ssl: ssl.SSLContext | None = None
        if self._https:
            try:
                import certifi  # the same certificate bundle the requests library uses in claude.py
                self._ssl = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                self._ssl = ssl.create_default_context()
        self.max_body_bytes = max_body_bytes
        self.per_session_limit = per_session_limit
        self.max_concurrent = max_concurrent
        self.total_limit = total_limit
        self.rate_per_minute = rate_per_minute
        self.head_seconds = head_seconds
        self.body_seconds = body_seconds
        self.body_total_seconds = body_total_seconds
        self.write_seconds = write_seconds
        self.upstream_seconds = upstream_seconds
        self.finish_seconds = finish_seconds
        self.clock = clock
        self.running = False
        self.error = ""
        self._server: asyncio.Server | None = None
        self._pool: ThreadPoolExecutor | None = None  # upstream calls
        self._prep_pool: ThreadPoolExecutor | None = None  # parsing and rewriting request bodies
        self._handlers: set[asyncio.Task[Any]] = set()
        self._calls: set[_Call] = set()
        self._connections = 0
        self._active: dict[str, int] = {}  # requests in progress per session
        self._reserved: dict[str, float] = {}  # worst-case dollars of the requests in progress per session
        self._hits: dict[str, deque[float]] = {}

    @property
    def address(self) -> tuple[str, int]:
        return self.host, self.port

    async def start(self) -> bool:
        """Listen on host:port. Returns False, with a plain reason in `error`, when that can't be done."""
        if self.running:
            return True
        if self._config_error:
            self.error = self._config_error
            log.warning("%s", self.error)
            return False
        try:
            self._server = await asyncio.start_server(self._on_connect, self.host, self.port, limit=MAX_HEAD_BYTES)
        except OSError as exc:
            self._server = None
            self.error = (f"The Code gateway couldn't start on {self.host}:{self.port} ({exc.strerror or exc}). "
                          "Check the sandbox host address and gateway port in Settings.")
            log.warning("%s", self.error)
            return False
        self.port = int(self._server.sockets[0].getsockname()[1])
        self._pool = ThreadPoolExecutor(max_workers=max(1, self.max_concurrent), thread_name_prefix="anthropic-gw")
        self._prep_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="anthropic-gw-prep")
        self.error = ""
        self.running = True
        log.info("Anthropic gateway is listening on %s:%s", self.host, self.port)
        return True

    async def stop(self) -> None:
        """Stop listening and cut every open connection, including those to Anthropic. Returns within a few seconds."""
        self.running = False
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for call in list(self._calls):
            call.close()
        handlers = list(self._handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.wait(handlers, timeout=STOP_SECONDS)
        for call in list(self._calls):  # count what can be counted and free the slots
            self._finish(call)
        self._active.clear()
        self._reserved.clear()
        self._hits.clear()
        for pool in (self._pool, self._prep_pool):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        self._pool = self._prep_pool = None
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), 2)
            except Exception:
                pass

    # ---- connection handling
    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = _Conn(reader, writer)
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        self._connections += 1
        try:
            if self._connections > self.total_limit:
                await self._send(conn, 503, error_body("overloaded_error", "The Code gateway is busy. Try again soon."))
                return
            await self._handle(conn)
        except (asyncio.CancelledError, OSError, asyncio.IncompleteReadError, TimeoutError):
            pass  # includes the sandbox going away
        except Exception as exc:
            log.warning("Gateway request failed (%s)", type(exc).__name__)
            try:
                await self._send(conn, 500, error_body("api_error", "The gateway had a problem."))
            except Exception:
                pass
        finally:
            self._connections -= 1
            if task is not None:
                self._handlers.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    async def _handle(self, conn: _Conn) -> None:
        try:
            req = await self._read_head(conn.reader)
        except GatewayError as exc:
            await self._send(conn, exc.status, error_body(exc.kind, exc.message))
            return
        if req is None:
            return
        if req.path == "/api/hello" and req.method in ("GET", "HEAD"):
            await self._send(conn, 200, b'{"ok":true}', head_only=req.method == "HEAD")
            return
        route = (req.method, req.path)
        if route not in _ROUTES:
            await self._send(conn, *_NOT_FOUND)
            return
        token = _token(req.headers)
        peer = _peer_ip(conn.writer)
        key = self._lookup(lambda: self._authenticate(token, peer), "authentication") if token else None
        policy = self._lookup(lambda: self._policy(key), "policy") if key is not None else None
        if key is None or policy is None:
            log.info("Gateway: %s %s refused (not signed in)", req.method, req.path)
            await self._send(conn, 401, error_body("authentication_error", "This Code session isn't signed in."))
            return
        if route == ("GET", "/v1/models"):
            models = [{"type": "model", "id": m, "display_name": m, "created_at": "1970-01-01T00:00:00Z"}
                      for m in policy.models()]
            body = {"data": models, "has_more": False, "first_id": models[0]["id"] if models else None,
                    "last_id": models[-1]["id"] if models else None}
            await self._send(conn, 200, json.dumps(body).encode())
            return
        wait = self._rate_wait(key)
        if wait is not None:
            await self._send(conn, 429, error_body("rate_limit_error", "This Code session is sending requests too "
                                                                       "fast. Try again in a moment."),
                             {"retry-after": str(int(wait) + 1)})
            return
        if self._active.get(key, 0) >= self.per_session_limit or sum(self._active.values()) >= self.max_concurrent:
            await self._send(conn, 429, error_body("rate_limit_error", "The Code assistant has too many requests "
                                                                       "running at once. Try again in a moment."),
                             {"retry-after": "5"})
            return
        self._active[key] = self._active.get(key, 0) + 1
        call: _Call | None = None
        try:
            call = await self._prepare_call(conn, req, key, policy)
        finally:
            if call is None:
                self._release_slot(key)
        if call is None:
            return
        self._start(call)
        await self._relay(conn, call)

    def _lookup(self, func: Callable[[], Any], what: str) -> Any:
        try:
            return func()
        except Exception as exc:
            log.warning("Gateway %s check failed (%s)", what, type(exc).__name__)
            return None

    def _rate_wait(self, key: str) -> float | None:
        """None if the session may send a request now (and counts it), else the seconds to wait."""
        now = self.clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= self.rate_per_minute:
            return 60 - (now - hits[0])
        hits.append(now)
        if len(self._hits) > 1000:
            self._hits = {k: v for k, v in self._hits.items() if v}
        return None

    def _release_slot(self, key: str) -> None:
        self._active[key] = self._active.get(key, 1) - 1
        if self._active[key] <= 0:
            self._active.pop(key, None)

    async def _read_head(self, reader: asyncio.StreamReader) -> _Request | None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.head_seconds)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return None
        except (asyncio.LimitOverrunError, ValueError):
            raise GatewayError(431, "invalid_request_error", "The request headers are too large.")
        bad = GatewayError(400, "invalid_request_error", "That isn't a valid request.")
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise bad
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            name = name.strip().lower()
            if not sep or not name:
                raise bad
            if name in headers:
                if name in _UNIQUE_HEADERS:
                    raise bad
                value = headers[name] + ", " + value.strip()
            headers[name] = value.strip()
        target = parts[1]
        if not target.startswith("/") or target.startswith("//"):
            return _Request(parts[0].upper(), "", "", headers)  # no route matches, so it's a 404
        path, _, query = target.partition("?")
        return _Request(parts[0].upper(), path, query, headers)

    async def _read_body(self, conn: _Conn, req: _Request) -> bytes:
        bad = GatewayError(400, "invalid_request_error", "That isn't a valid request.")
        encoding, length_text = req.headers.get("transfer-encoding"), req.headers.get("content-length")
        if encoding is not None and (length_text is not None or encoding.lower() != "chunked"):
            raise bad
        if length_text is not None and not _LENGTH.fullmatch(length_text):
            raise bad
        if req.headers.get("expect", "").lower() == "100-continue":
            conn.writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            await self._drain(conn)
        try:
            return await asyncio.wait_for(self._read_body_parts(conn.reader, encoding is not None, length_text),
                                          self.body_total_seconds)
        except (asyncio.LimitOverrunError, ValueError):
            raise bad

    async def _read_body_parts(self, reader: asyncio.StreamReader, chunked: bool, length_text: str | None) -> bytes:
        bad = GatewayError(400, "invalid_request_error", "That isn't a valid request.")
        too_big = GatewayError(413, "request_too_large", "The request is too large.")
        limit = self.max_body_bytes

        async def read(n: int) -> bytes:
            return await asyncio.wait_for(reader.readexactly(n), self.body_seconds)

        async def line() -> bytes:
            return await asyncio.wait_for(reader.readline(), self.body_seconds)

        if chunked:
            body = bytearray()
            while True:
                head = await line()
                if not head.endswith(b"\r\n"):
                    raise bad
                size_text = head[:-2].split(b";", 1)[0].strip()
                if not _CHUNK_SIZE.fullmatch(size_text):
                    raise bad
                size = int(size_text, 16)
                if size == 0:
                    for _ in range(32):  # trailers: a few short lines at most
                        trailer = await line()
                        if trailer == b"\r\n":
                            return bytes(body)
                        if not trailer.endswith(b"\r\n"):
                            raise bad
                    raise bad
                if len(body) + size > limit:
                    raise too_big
                body += await read(size)
                if await read(2) != b"\r\n":
                    raise bad
        length = int(length_text or "0")
        if length > limit:
            raise too_big
        data = bytearray()
        while len(data) < length:
            piece = await asyncio.wait_for(reader.read(min(1024 * 1024, length - len(data))), self.body_seconds)
            if not piece:
                raise asyncio.IncompleteReadError(bytes(data), length)
            data += piece
        return bytes(data)

    # ---- preparing a call
    def _prepare(self, body: bytes, policy: SessionPolicy, counted: bool) -> tuple[bytes, str, float, int]:
        """Parse, check and rewrite a request body (runs off the event loop). Returns the new body, the model, the
        worst-case cost in dollars and max_tokens. Raises GatewayError."""
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            payload = None
        if not isinstance(payload, dict):
            raise GatewayError(400, "invalid_request_error", "The request body must be a JSON object.")
        out = rewrite_request(payload, allowed_models=policy.allowed_models, allow_web_tools=policy.allow_web_tools,
                              extra_system=policy.extra_system)
        data = json.dumps(out).encode()
        model = out["model"]
        estimate, max_tokens = 0.0, 0
        if counted:
            price = price_for(model)
            if price is None:
                raise GatewayError(400, "invalid_request_error",
                                   "The price of that model isn't known, so this Code session can't use it.")
            asked = out.get("max_tokens")
            max_tokens = asked if isinstance(asked, int) and not isinstance(asked, bool) and asked > 0 \
                else DEFAULT_MAX_TOKENS
            tokens = estimate_input_tokens(out, len(data))
            rate_in, rate_out = price.input, price.output
            if price.long_prompt_over is not None and tokens > price.long_prompt_over:
                rate_in, rate_out = price.long_input, price.long_output
            estimate = (rate_in * tokens + rate_out * max_tokens) / 1_000_000
            # Server-side searches add their results to the input, which this can't foresee, so it can still overshoot.
            estimate += search_uses(out) * WEB_SEARCH_PRICE
        return data, model, estimate, max_tokens

    @staticmethod
    def _tell_cap(policy: SessionPolicy) -> None:
        if policy.on_cap is not None:
            try:
                policy.on_cap()
            except Exception:
                log.exception("Gateway: the cap callback failed")

    async def _prepare_call(self, conn: _Conn, req: _Request, key: str, policy: SessionPolicy) -> _Call | None:
        """Read and check the request. Sends the refusal and returns None if it can't go to Anthropic. When it returns
        a call, the cost estimate is already reserved against the session's cap."""
        counted = req.path == "/v1/messages"
        try:
            body = await self._read_body(conn, req)
            if self._prep_pool is None:
                raise GatewayError(503, "api_error", "The Code gateway is stopping.")
            data, model, estimate, max_tokens = await asyncio.get_running_loop().run_in_executor(
                self._prep_pool, self._prepare, body, policy, counted)
            reserve = 0.0
            if counted and policy.cap_usd is not None:
                spent = policy.spent()
                if spent >= policy.cap_usd:
                    log.info("Gateway: session %s is at its cap", key)
                    self._tell_cap(policy)
                    raise GatewayError(402, "billing_error", CAP_MESSAGE)
                if spent + self._reserved.get(key, 0.0) + estimate > policy.cap_usd:
                    log.info("Gateway: session %s could go over its cap", key)
                    self._tell_cap(policy)
                    raise GatewayError(402, "billing_error", OVER_CAP_MESSAGE)
                reserve = estimate
            credential = self._lookup(self._credential, "credential")
            if credential is None or not credential.secret:
                raise GatewayError(503, "api_error", "The Code assistant isn't set up yet: no Anthropic sign-in is "
                                                     "configured on the team's computer.")
        except GatewayError as exc:
            await self._send(conn, exc.status, error_body(exc.kind, exc.message))
            return None
        except asyncio.TimeoutError:
            await self._send(conn, 408, error_body("invalid_request_error", "The request took too long to arrive."))
            return None
        headers = apply_credential(filter_headers(req.headers), credential)
        headers["content-type"] = "application/json"
        headers["accept-encoding"] = "identity"
        # Only the route and ?beta=true go upstream; the client's own target and other query values never do.
        query = "?beta=true" if ("beta", "true") in parse_qsl(req.query) else ""
        call = _Call(key=key, model=model, target=self._upstream_prefix + req.path + query, headers=headers, data=data,
                     max_tokens=max_tokens, counted=counted, reserved=reserve)
        if reserve:
            self._reserved[key] = self._reserved.get(key, 0.0) + reserve
        return call

    # ---- talking to Anthropic
    def _start(self, call: _Call) -> None:
        """Hand the call to a worker thread. From here on, _finish counts it and frees its slot."""
        self._calls.add(call)
        loop = asyncio.get_running_loop()
        try:
            if self._pool is None:
                raise RuntimeError("stopped")
            self._pool.submit(self._pump, call, loop)
        except RuntimeError:
            call.closed.set()
            self._finish(call)

    def _connect(self) -> http.client.HTTPConnection:
        if self._https:
            return http.client.HTTPSConnection(self._upstream_host, self._upstream_port, timeout=10,
                                               context=self._ssl)
        return http.client.HTTPConnection(self._upstream_host, self._upstream_port, timeout=10)

    @staticmethod
    def _put(call: _Call, loop: asyncio.AbstractEventLoop, item: tuple[str, Any]) -> None:
        if call.abandoned:
            return
        try:
            loop.call_soon_threadsafe(call.queue.put_nowait, item)
        except RuntimeError:  # the loop has closed
            pass

    def _pump(self, call: _Call, loop: asyncio.AbstractEventLoop) -> None:
        """Worker thread: send the request, read the whole reply and hand it on. It keeps reading when the sandbox has
        gone, until the usage is final, so the cost is always counted."""
        conn = None
        try:
            conn = call.conn = self._connect()
            if call.closed.is_set():
                raise OSError("closed")
            conn.connect()  # a name lookup in here can't be interrupted; the 10 second timeout bounds the rest
            if call.closed.is_set():
                raise OSError("closed")
            call.sock = conn.sock
            if conn.sock is not None:
                conn.sock.settimeout(self.upstream_seconds)
            if call.closed.is_set():
                raise OSError("closed")
            conn.request("POST", call.target, body=call.data, headers=call.headers)
            call.sent = True
            resp = conn.getresponse()
            call.status = resp.status
            headers = {k.lower(): v for k, v in resp.getheaders()}
            call.streaming = "text/event-stream" in headers.get("content-type", "")
            self._put(call, loop, ("head", (resp.status, headers)))
            while True:
                chunk = resp.read1(8192)
                if not chunk or call.closed.is_set():
                    break
                if call.streaming:
                    call.counter.feed(chunk)
                    self._put(call, loop, ("data", chunk))
                    if call.abandoned and call.counter.final:
                        break
                else:
                    call.body += chunk
                    if len(call.body) > 64 * 1024 * 1024:
                        raise OSError("reply too large")
            if call.closed.is_set():
                raise OSError("closed")
            call.complete = True
            self._put(call, loop, ("end", None))
        except Exception as exc:
            log.warning("Gateway: the call to Anthropic failed (%s)", type(exc).__name__)
            self._put(call, loop, ("error", type(exc).__name__))
        finally:
            call.close()
            try:
                loop.call_soon_threadsafe(self._finish, call)
            except RuntimeError:
                pass

    def _final_usage(self, call: _Call) -> Usage | None:
        """What to count for a call that has ended: the real usage, or an estimate when the reply was cut off."""
        if not call.counted or not call.sent or (call.status is not None and call.status >= 400):
            return None
        if call.streaming:
            if call.counter.started or call.counter.final:
                return call.counter.snapshot()
        elif call.complete:
            return json_usage(bytes(call.body))
        return Usage(input_tokens=len(call.data) // TOKEN_BYTES, output_tokens=call.max_tokens)

    def _finish(self, call: _Call) -> None:
        """On the loop thread, when the worker has ended: count the usage once and free the call's slot."""
        if call not in self._calls:
            return
        self._calls.discard(call)
        if call.timer is not None:
            call.timer.cancel()
        usage = self._final_usage(call)
        if usage is not None and usage.total:
            usage.model = call.model  # priced by the model asked for, not the id Anthropic answers with
            try:
                self._record(call.key, usage, usage.cost())
            except Exception as exc:
                log.warning("Gateway couldn't record usage for session %s (%s)", call.key, type(exc).__name__)
        if call.reserved:
            left = self._reserved.get(call.key, 0.0) - call.reserved
            if left > 1e-12:
                self._reserved[call.key] = left
            else:
                self._reserved.pop(call.key, None)
        self._release_slot(call.key)
        log.info("Gateway: POST %s session=%s status=%s tokens=%s", call.target.split("?")[0], call.key, call.status,
                 usage.total if usage else 0)

    def _abandon(self, call: _Call) -> None:
        """The sandbox has gone. Keep reading from Anthropic for a while so the reply's usage is counted."""
        if call.abandoned or call not in self._calls:
            return
        call.abandoned = True
        call.timer = asyncio.get_running_loop().call_later(self.finish_seconds, call.close)

    async def _relay(self, conn: _Conn, call: _Call) -> None:
        """Pass the reply to the sandbox as it arrives. If the sandbox hangs up, stop sending and let the call finish
        in the background (see _abandon)."""
        handler = asyncio.current_task()

        def gone(done: asyncio.Future[bytes]) -> None:
            # Only EOF or a reset counts as a hang-up. A half-closed client counts too: the SDK's fetch never
            # half-closes while it waits for the reply. Stray data is ignored and watching stops.
            if done.cancelled() or (done.exception() is None and done.result()):
                return
            self._abandon(call)
            if handler is not None:
                handler.cancel()

        watch = asyncio.ensure_future(conn.reader.read(1))  # the sandbox sends nothing more, so this ends at hang-up
        watch.add_done_callback(gone)
        try:
            kind, value = await call.queue.get()
            if kind == "error":
                await self._send(conn, 502, error_body("api_error", "Couldn't reach Anthropic. Try again soon."))
                return
            status, upstream_headers = value
            reply_headers = {n: v for n, v in upstream_headers.items()
                             if n in REPLY_HEADERS or n.startswith(REPLY_PREFIXES)}
            if call.streaming:
                await self._write(conn, _head(status, reply_headers, chunked=True))
            while True:
                kind, value = await call.queue.get()
                if kind == "end":
                    break
                if kind == "error":
                    if conn.sent:
                        conn.writer.transport.abort()  # a streamed reply is cut off, not ended cleanly
                    else:
                        await self._send(conn, 502, error_body("api_error", "Anthropic's reply was cut off."))
                    return
                await self._write(conn, b"%x\r\n" % len(value) + value + b"\r\n")
            if call.streaming:
                await self._write(conn, b"0\r\n\r\n")
            else:
                await self._write(conn, _head(status, reply_headers, length=len(call.body)) + bytes(call.body))
        finally:
            watch.cancel()
            self._abandon(call)  # does nothing once the call has ended

    # ---- replies
    async def _drain(self, conn: _Conn) -> None:
        await asyncio.wait_for(conn.writer.drain(), self.write_seconds)

    async def _write(self, conn: _Conn, data: bytes) -> None:
        conn.sent = True
        conn.writer.write(data)
        await self._drain(conn)

    async def _send(self, conn: _Conn, status: int, body: bytes, headers: Mapping[str, str] | None = None,
                    head_only: bool = False) -> None:
        """A complete reply, unless one has already started."""
        if conn.sent:
            return
        reply = {"content-type": "application/json", **(headers or {})}
        try:
            await self._write(conn, _head(status, reply, length=len(body)) + (b"" if head_only else body))
        except (OSError, asyncio.TimeoutError):
            pass


# --------------------------------------------------------------------------- small helpers
def _peer_ip(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    if not peer:
        return ""
    ip = str(peer[0])
    return ip[len("::ffff:"):] if ip.lower().startswith("::ffff:") else ip


def _token(headers: Mapping[str, str]) -> str:
    auth = headers.get("authorization", "")
    scheme, _, rest = auth.partition(" ")
    if scheme.lower() == "bearer" and rest.strip():
        return rest.strip()
    return headers.get("x-api-key", "").strip()


def _head(status: int, headers: Mapping[str, str], *, length: int | None = None, chunked: bool = False) -> bytes:
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "Status"
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines += [f"{n}: {_clean(v, 1000)}" for n, v in headers.items()]
    if chunked:
        lines.append("Transfer-Encoding: chunked")
        lines.append("Cache-Control: no-cache")
    elif length is not None:
        lines.append(f"Content-Length: {length}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")
