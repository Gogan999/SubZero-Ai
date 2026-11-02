"""The egress proxy: the only way a Code sandbox reaches the internet (docs/CODE_SANDBOX_PLAN.md, "Network design").

A sandbox's guest can reach only one TCP port on the host, and it is this proxy's port (the firewall rules are in
sandbox.py). The proxy is an HTTP CONNECT-only forward proxy: it opens a tunnel to a name on the allow-list, on port
443, and copies bytes both ways. Everything else is refused in plain words.

Each request must carry its sandbox's name and that sandbox's own random token (Proxy-Authorization: Basic). Tokens are
issued when a sandbox is made and revoked when it is deleted. TokenStore keeps only a SHA-256 hash of each token, in
memory.

Before anything is connected, the destination is checked: the name must be on the allow-list, it is resolved once, and
every address it resolves to must be a public one (not loopback, private, link-local, the sandbox subnet, carrier-grade
NAT, or this computer's own). The connection is made to an address that was checked, never to a second lookup, so a
name can't be changed to point somewhere else between the check and the connection (DNS rebinding).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .db import Database

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger("subzero.egress")

GUEST_NETWORK = ipaddress.ip_network("10.77.0.0/24")  # the sandboxes' subnet on the internal switch
CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")  # carrier-grade NAT: not public, and not checked by is_global
REALM = "SubZero sandbox"
MAX_HEAD_BYTES = 8 * 1024  # the request line and headers together
HEAD_SECONDS = 30  # how long a client has to send its request
RESOLVE_SECONDS = 10
CONNECT_SECONDS = 15
IDLE_SECONDS = 120  # a tunnel with no bytes either way for this long is closed
CHUNK_BYTES = 64 * 1024
MAX_TOTAL_CONNECTIONS = 64  # open tunnels across all sandboxes
PER_CONNECTION_BYTES = 512 * 1024 * 1024  # one tunnel, both directions
BLOCK_AUDIT_SECONDS = 60  # at most one audit row per sandbox and host in this long
MAX_RULES = 50
MAX_PORTS = 10
MAX_HOST_CHARS = 253
HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST_RE = re.compile(rf"{HOST_LABEL}(?:\.{HOST_LABEL})*")
REASONS = {400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 407: "Proxy Authentication Required",
           431: "Request Header Fields Too Large", 502: "Bad Gateway", 503: "Service Unavailable"}
# What a student or mentor sees when a request is refused
MESSAGES = {
    "method": "This proxy only opens secure website connections (HTTPS). Other kinds of requests aren't allowed.",
    "bad": "That request isn't valid, so it was refused.",
    "too_big": "That request is too big, so it was refused.",
    "login": "This sandbox's proxy login is missing or no longer valid. Start a new Code session to get a new one.",
    "port": "Only secure websites are allowed (port {ports}).",
    "ip": "Use a website name, not an IP address. That isn't allowed.",
    "name": "That address isn't allowed.",
    "list": "That website isn't on the team's allowed list.",
    "private": "That address points at this computer or a private network, which sandboxes can't reach.",
    "allowance": "This sandbox has used up its internet allowance. Ask a mentor to start a new Code session.",
    "busy_sandbox": "This sandbox has too many connections open at once. Wait for some to finish, then try again.",
    "busy": "The proxy is busy right now. Try again in a moment.",
    "dns": "Couldn't find that website. Check the spelling, or try again later.",
    "connect": "Couldn't connect to that website.",
    "timeout": "That website didn't answer in time.",
}


# --------------------------------------------------------------------------- settings and addresses
def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    return True


def parse_hosts(raw: str) -> tuple[str, ...]:
    """The allow-list as typed in Settings: names separated by commas or spaces. `*.example.com` matches any name
    under example.com (not example.com itself). Raises ValueError with a plain message."""
    items = [part.strip().lower() for part in re.split(r"[\s,]+", raw) if part.strip()]
    if len(items) > MAX_RULES:
        raise ValueError(f"Use up to {MAX_RULES} names.")
    rules: list[str] = []
    for item in items:
        name = item[2:] if item.startswith("*.") else item
        if len(item) > MAX_HOST_CHARS or not HOST_RE.fullmatch(name) or _is_ip(name):
            raise ValueError(f"{item} isn't a host name. Use a name like pypi.org or *.example.com, with no "
                             "http://, slash, port or IP address.")
        rules.append(item)
    return tuple(dict.fromkeys(rules))


def parse_ports(raw: str) -> tuple[int, ...]:
    """The ports a tunnel may use, from Settings. Raises ValueError with a plain message."""
    items = [part for part in re.split(r"[\s,]+", raw.strip()) if part]
    if not items:
        raise ValueError("Give at least one port, like 443.")
    if len(items) > MAX_PORTS:
        raise ValueError(f"Use up to {MAX_PORTS} ports.")
    ports: list[int] = []
    for item in items:
        if not re.fullmatch(r"[0-9]{1,5}", item) or not 1 <= int(item) <= 65535:
            raise ValueError(f"{item} isn't a port number. Use numbers from 1 to 65535, like 443.")
        ports.append(int(item))
    return tuple(dict.fromkeys(ports))


def host_allowed(host: str, rules: Iterable[str]) -> bool:
    """True when the name is on the allow-list: exactly, or under a `*.` rule."""
    for rule in rules:
        if rule.startswith("*."):
            if host.endswith(rule[1:]) and len(host) > len(rule) - 1:
                return True
        elif host == rule:
            return True
    return False


def refused_address(address: str, local: frozenset[str]) -> bool:
    """True when a resolved address must never be connected to."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_unspecified
            or ip.is_reserved or not ip.is_global):
        return True
    if ip in GUEST_NETWORK or ip in CGNAT_NETWORK:
        return True
    return str(ip) in local


def _normal_address(text: str) -> str | None:
    try:
        return str(ipaddress.ip_address(text.split("%", 1)[0]))
    except ValueError:
        return None


def this_computer_addresses(extra: Iterable[str] = ()) -> frozenset[str]:
    """This computer's own addresses, so a name that resolves to one of them is refused."""
    found: set[str] = set()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
    except OSError:
        infos = []
    for info in infos:
        found.add(info[4][0])
    found.update(extra)
    return frozenset(n for n in (_normal_address(a) for a in found) if n is not None)


# --------------------------------------------------------------------------- tokens
def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "replace")).digest()


@dataclass
class _Entry:
    digest: bytes
    expires: datetime


class TokenStore:
    """One proxy token per sandbox. Only a SHA-256 hash of each token is kept, in memory. A restart forgets them all;
    the app then issues new ones for the sandboxes that are still running."""

    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def issue(self, name: str, expires_at: datetime) -> str:
        """A new token for the sandbox. It replaces any earlier token for that name. Returns the token itself, which is
        the only time it is ever available in plain text here."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._entries[name] = _Entry(_digest(token), expires_at)
        return token

    def revoke(self, name: str) -> None:
        with self._lock:
            self._entries.pop(name, None)

    def active(self, name: str) -> bool:
        with self._lock:
            return name in self._entries

    def verify(self, name: str, token: str) -> bool:
        """True only for the sandbox's current, unexpired token. The comparison takes the same time either way."""
        with self._lock:
            entry = self._entries.get(name)
        if entry is None:
            return False
        matches = hmac.compare_digest(entry.digest, _digest(token))
        if self._clock() >= entry.expires:
            with self._lock:
                if self._entries.get(name) is entry:
                    del self._entries[name]
            return False
        return matches


def basic_credentials(value: str) -> tuple[str, str] | None:
    """The sandbox name and token from a Proxy-Authorization header, or None if it isn't a usable Basic login."""
    scheme, _, rest = value.strip().partition(" ")
    if scheme.lower() != "basic" or not rest.strip():
        return None
    try:
        decoded = base64.b64decode(rest.strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return None
    name, sep, token = decoded.partition(":")
    if not sep or not name or not token or len(token) > 256 or len(name) > 128:
        return None
    return name, token


def _split_target(target: str) -> tuple[str, int] | None:
    """host and port from a CONNECT target (host:port, or [ipv6]:port). None if it isn't that shape."""
    host, sep, port_text = target.rpartition(":")
    if not sep or not re.fullmatch(r"[0-9]{1,5}", port_text):
        return None
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or len(host) > MAX_HOST_CHARS:
        return None
    return host.lower(), int(port_text)


def _clean_for_log(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.:\[\]_-]", "?", text)[:MAX_HOST_CHARS]


# --------------------------------------------------------------------------- the proxy
@dataclass
class _Usage:
    bytes: int = 0  # both directions, over the sandbox's whole life (until it is deleted)


@dataclass(eq=False)
class _Tunnel:
    name: str
    host: str
    client: asyncio.StreamWriter
    upstream: asyncio.StreamWriter
    tasks: set[asyncio.Future[Any]] = field(default_factory=set)
    bytes: int = 0
    reason: str = ""

    def abort(self) -> None:
        for task in self.tasks:
            task.cancel()
        for writer in (self.client, self.upstream):
            writer.transport.abort()


Resolver = Callable[[str], Awaitable[Iterable[str]]]
Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class EgressProxy:
    """The HTTP CONNECT proxy. Start and stop it on the app's event loop. close_sandbox may be called from any thread.

    resolver(name) and connector(address, port) are replaceable so the tests can use fake DNS and a local upstream;
    the app uses the defaults. The defaults resolve with the loop's getaddrinfo and connect with open_connection."""

    def __init__(self, *, tokens: TokenStore, db: Database | None, host: str, port: int,
                 allowed_hosts: Iterable[str] = (), allowed_ports: Iterable[int] = (443,),
                 byte_limit: int = 2048 * 1024 * 1024, per_sandbox_limit: int = 16,
                 total_limit: int = MAX_TOTAL_CONNECTIONS, connection_byte_limit: int = PER_CONNECTION_BYTES,
                 resolver: Resolver | None = None, connector: Connector | None = None,
                 local_addresses: Iterable[str] | None = None, head_seconds: float = HEAD_SECONDS,
                 resolve_seconds: float = RESOLVE_SECONDS, connect_seconds: float = CONNECT_SECONDS,
                 idle_seconds: float = IDLE_SECONDS, clock: Callable[[], float] = time.monotonic,
                 config_error: str = "", settings: Settings | None = None):
        self.tokens = tokens
        self.db = db
        self.host = host
        self.port = port  # the port asked for; once started, the port actually bound
        # The allow-list, ports and limits. With settings, they are read again for each request, so a change on the
        # Settings page applies at once. The bind address and port apply after a restart.
        self._settings = settings
        self._rules = tuple(allowed_hosts)
        self._ports = tuple(allowed_ports)
        self._byte_limit = byte_limit
        self._per_sandbox_limit = per_sandbox_limit
        self.total_limit = total_limit
        self.connection_byte_limit = connection_byte_limit
        self.head_seconds = head_seconds
        self.resolve_seconds = resolve_seconds
        self.connect_seconds = connect_seconds
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._resolver = resolver
        self._connector = connector or self._open_connection
        self._local_fixed = None if local_addresses is None else frozenset(
            n for n in (_normal_address(a) for a in local_addresses) if n is not None)
        self._config_error = config_error
        self.running = False
        self.error = config_error  # a plain message when the proxy isn't running
        self._server: asyncio.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._local: frozenset[str] = frozenset()
        self._handlers: set[asyncio.Task[Any]] = set()
        self._tunnels: dict[str, set[_Tunnel]] = {}
        self._active: dict[str, int] = {}  # open tunnels per sandbox
        self._total = 0
        self._usage: dict[str, _Usage] = {}
        self._blocked_at: dict[tuple[str, str], float] = {}

    @classmethod
    def from_settings(cls, settings: Settings, *, tokens: TokenStore, db: Database | None) -> EgressProxy:
        """The proxy as Settings describe it. A bad allow-list or port list doesn't stop the app: the proxy then
        refuses to start, and the Settings check says why."""
        error = ""
        try:
            rules = parse_hosts(settings.sandbox_allowed_hosts)
            ports = parse_ports(settings.sandbox_allowed_ports)
        except ValueError as exc:
            rules, ports, error = (), (443,), f"The sandbox's allowed websites or ports in Settings are wrong: {exc}"
        return cls(tokens=tokens, db=db, host=settings.sandbox_host_address, port=settings.sandbox_proxy_port,
                   allowed_hosts=rules, allowed_ports=ports, config_error=error, settings=settings)

    def _policy(self) -> tuple[tuple[str, ...], tuple[int, ...], int, int]:
        """The rules in force now: (allowed names, allowed ports, byte allowance, connections per sandbox)."""
        if self._settings is None:
            return self._rules, self._ports, self._byte_limit, self._per_sandbox_limit
        try:
            rules = parse_hosts(self._settings.sandbox_allowed_hosts)
            ports = parse_ports(self._settings.sandbox_allowed_ports)
        except ValueError:
            return (), (), 0, 1  # a bad list refuses everything until it is fixed in Settings
        mb = self._settings.sandbox_proxy_mb_limit
        return rules, ports, mb * 1024 * 1024, self._settings.sandbox_proxy_max_per_sandbox

    @property
    def address(self) -> tuple[str, int]:
        return self.host, self.port

    async def start(self) -> bool:
        """Listen on host:port. Returns False, with a plain reason in `error`, when that can't be done."""
        if self.running:
            return True
        if self._config_error:
            return False
        try:
            self._server = await asyncio.start_server(self._on_connect, self.host, self.port, limit=MAX_HEAD_BYTES)
        except OSError as exc:
            self._server = None
            self.error = (f"The sandbox proxy couldn't start on {self.host}:{self.port} ({exc.strerror or exc}). "
                          "Check the sandbox host address and port in Settings.")
            log.warning("%s", self.error)
            return False
        self.port = int(self._server.sockets[0].getsockname()[1])
        self._loop = asyncio.get_running_loop()
        if self._local_fixed is not None:
            self._local = self._local_fixed
        else:  # a name lookup, so it runs off the event loop
            self._local = await asyncio.to_thread(this_computer_addresses, [self.host])
        self.error = ""
        self.running = True
        log.info("Sandbox proxy is listening on %s:%s", self.host, self.port)
        return True

    async def stop(self) -> None:
        """Stop listening and close every open tunnel."""
        self.running = False
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for name in list(self._tunnels):
            self._drop(name)
        handlers = list(self._handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), 5)
            except (asyncio.TimeoutError, OSError):
                pass
        self._loop = None
        log.info("Sandbox proxy stopped")

    def close_sandbox(self, name: str) -> None:
        """Cut off a sandbox's open tunnels and forget its byte count. Call after its token is revoked. Safe from any
        thread; it does not wait for the tunnels to close."""
        loop = self._loop
        if loop is None or not self.running:
            self._usage.pop(name, None)
            return
        try:
            loop.call_soon_threadsafe(self._drop, name)
        except RuntimeError:  # the loop has closed, so nothing is open
            self._usage.pop(name, None)

    def _drop(self, name: str) -> None:
        for tunnel in list(self._tunnels.get(name, ())):
            tunnel.reason = tunnel.reason or "revoked"
            tunnel.abort()
        self._usage.pop(name, None)

    # ---- one client
    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            await self._serve(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Sandbox proxy request failed")
        finally:
            if task is not None:
                self._handlers.discard(task)
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.head_seconds)
        except asyncio.LimitOverrunError:
            await self._reply(writer, 431, MESSAGES["too_big"])
            return
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
            return
        lines = head[:-4].split(b"\r\n")
        try:
            parts = lines[0].decode("ascii").split(" ")
        except UnicodeDecodeError:
            parts = []
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            await self._reply(writer, 400, MESSAGES["bad"])
            return
        method, target = parts[0], parts[1]
        if method != "CONNECT":
            await self._reply(writer, 405, MESSAGES["method"], extra=[("Allow", "CONNECT")])
            return
        headers: list[tuple[str, str]] = []
        for raw in lines[1:]:
            name, sep, value = raw.partition(b":")
            if not sep:
                await self._reply(writer, 400, MESSAGES["bad"])
                return
            headers.append((name.strip().lower().decode("latin-1"), value.strip().decode("latin-1")))
        auth = [value for key, value in headers if key == "proxy-authorization"]
        if len(auth) > 1:
            await self._reply(writer, 400, MESSAGES["bad"])
            return
        credentials = basic_credentials(auth[0]) if auth else None
        if credentials is None or not self.tokens.verify(*credentials):
            log.info("Sandbox proxy refused a request with a missing or invalid login")
            await self._reply_login(writer)
            return
        name, token = credentials
        parsed = _split_target(target)
        if parsed is None:
            await self._reply(writer, 400, MESSAGES["bad"])
            return
        host, port = parsed
        rules, ports, byte_limit, per_sandbox_limit = self._policy()
        if port not in ports:
            listed = ", ".join(str(p) for p in ports) or "none"
            await self._refuse(writer, name, host, "port", 403, MESSAGES["port"].format(ports=listed))
            return
        if _is_ip(host):
            await self._refuse(writer, name, host, "ip-address", 403, MESSAGES["ip"])
            return
        if not HOST_RE.fullmatch(host):
            await self._refuse(writer, name, host, "name", 403, MESSAGES["name"])
            return
        if not host_allowed(host, rules):
            await self._refuse(writer, name, host, "not-allowed", 403, MESSAGES["list"])
            return
        if self._usage.get(name, _Usage()).bytes >= byte_limit:
            await self._reply(writer, 403, MESSAGES["allowance"])
            return
        reason = self._acquire(name, per_sandbox_limit)
        if reason:
            await self._reply(writer, 503, MESSAGES["busy_sandbox" if reason == "sandbox" else "busy"])
            return
        try:
            await self._tunnel(writer, reader, name, token, host, port, byte_limit)
        finally:
            self._release(name)

    async def _tunnel(self, writer: asyncio.StreamWriter, reader: asyncio.StreamReader, name: str, token: str,
                      host: str, port: int, byte_limit: int) -> None:
        """Resolve and check the name, connect to a checked address, then copy bytes until one side closes."""
        try:
            addresses = list(dict.fromkeys(await asyncio.wait_for(self._resolve(host), self.resolve_seconds)))
        except (OSError, UnicodeError, asyncio.TimeoutError, ValueError):
            await self._reply(writer, 502, MESSAGES["dns"])
            return
        if not addresses or any(refused_address(address, self._local) for address in addresses):
            await self._refuse(writer, name, host, "private-address", 403, MESSAGES["private"])
            return
        upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        timed_out = False
        for address in addresses:  # only addresses that were checked above, and no second lookup
            try:
                upstream = await asyncio.wait_for(self._connector(address, port), self.connect_seconds)
                break
            except asyncio.TimeoutError:
                timed_out = True
            except OSError:
                continue
        if upstream is None:
            await self._reply(writer, 504 if timed_out else 502, MESSAGES["timeout" if timed_out else "connect"])
            return
        up_reader, up_writer = upstream
        if not self.tokens.verify(name, token):  # revoked while it was being connected
            up_writer.transport.abort()
            await self._reply_login(writer)
            return
        usage = self._usage.setdefault(name, _Usage())
        tunnel = _Tunnel(name, host, writer, up_writer)
        self._tunnels.setdefault(name, set()).add(tunnel)
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            tunnel.tasks = {asyncio.ensure_future(self._pump(reader, up_writer, tunnel, usage, byte_limit)),
                            asyncio.ensure_future(self._pump(up_reader, writer, tunnel, usage, byte_limit))}
            await asyncio.wait(tunnel.tasks, return_when=asyncio.FIRST_COMPLETED)
        except OSError:
            pass
        finally:
            tunnel.abort()
            self._unregister(tunnel)
            log.info("Sandbox %s: a tunnel to %s closed (%s). The sandbox has moved %d bytes through the proxy.",
                     name, _clean_for_log(host), tunnel.reason or "finished", usage.bytes)

    async def _pump(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter, tunnel: _Tunnel,
                    usage: _Usage, byte_limit: int) -> None:
        while True:
            try:
                data = await asyncio.wait_for(source.read(CHUNK_BYTES), self.idle_seconds)
            except asyncio.TimeoutError:
                tunnel.reason = "idle"
                return
            except OSError:
                return
            if not data:
                return
            size = len(data)
            if usage.bytes + size > byte_limit:
                tunnel.reason = "sandbox allowance used up"
                return
            if tunnel.bytes + size > self.connection_byte_limit:
                tunnel.reason = "connection too big"
                return
            usage.bytes += size
            tunnel.bytes += size
            try:
                sink.write(data)
                await sink.drain()
            except OSError:
                return

    def _unregister(self, tunnel: _Tunnel) -> None:
        bucket = self._tunnels.get(tunnel.name)
        if bucket is not None:
            bucket.discard(tunnel)
            if not bucket:
                del self._tunnels[tunnel.name]

    def _acquire(self, name: str, per_sandbox_limit: int) -> str:
        """Reserve a tunnel for the sandbox. Returns "" on success, else which limit was hit."""
        if self._active.get(name, 0) >= per_sandbox_limit:
            return "sandbox"
        if self._total >= self.total_limit:
            return "total"
        self._active[name] = self._active.get(name, 0) + 1
        self._total += 1
        return ""

    def _release(self, name: str) -> None:
        left = self._active.get(name, 0) - 1
        if left > 0:
            self._active[name] = left
        else:
            self._active.pop(name, None)
        self._total = max(0, self._total - 1)

    async def _resolve(self, host: str) -> list[str]:
        if self._resolver is not None:
            return [str(a) for a in await self._resolver(host)]
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return [str(info[4][0]) for info in infos]

    @staticmethod
    async def _open_connection(address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(address, port)

    # ---- refusals and audit
    async def _refuse(self, writer: asyncio.StreamWriter, name: str, host: str, reason: str, status: int,
                      text: str) -> None:
        self._audit_blocked(name, host, reason)
        await self._reply(writer, status, text)

    def _audit_blocked(self, name: str, host: str, reason: str) -> None:
        """One audit row per sandbox and host each minute, so a loop of refused requests can't flood the log."""
        clean = _clean_for_log(host)
        key = (name, clean)
        now = self.clock()
        last = self._blocked_at.get(key)
        if last is not None and now - last < BLOCK_AUDIT_SECONDS:
            return
        if len(self._blocked_at) > 1000:
            self._blocked_at.clear()
        self._blocked_at[key] = now
        log.info("Sandbox %s: refused a connection to %s (%s)", name, clean, reason)
        if self.db is None:
            return
        try:
            self.db.audit("sandbox_proxy_blocked", actor_id=None, target_type="sandbox",
                          details={"name": name, "host": clean, "reason": reason})
        except Exception:
            log.warning("Couldn't write the audit row for a refused sandbox connection", exc_info=True)

    async def _reply_login(self, writer: asyncio.StreamWriter) -> None:
        await self._reply(writer, 407, MESSAGES["login"], extra=[("Proxy-Authenticate", f'Basic realm="{REALM}"')])

    async def _reply(self, writer: asyncio.StreamWriter, status: int, text: str,
                     extra: Iterable[tuple[str, str]] = ()) -> None:
        body = (text + "\n").encode("utf-8")
        head = [f"HTTP/1.1 {status} {REASONS[status]}", "Content-Type: text/plain; charset=utf-8",
                f"Content-Length: {len(body)}", "Connection: close", *(f"{k}: {v}" for k, v in extra)]
        try:
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body)
            await asyncio.wait_for(writer.drain(), 10)
        except (OSError, asyncio.TimeoutError):
            pass
