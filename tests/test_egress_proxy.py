"""The sandboxes' egress proxy (subzero_ai/egress_proxy.py), with real sockets on 127.0.0.1. The DNS lookup and the
connection to the internet are replaced by fakes: the proxy is told a public address and is connected to a local
upstream instead, so nothing leaves this computer. The tests check what the proxy allows, refuses, logs and audits."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import shutil
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from subzero_ai import saved_settings
from subzero_ai.config import load_settings
from subzero_ai.db import Database
from subzero_ai.egress_proxy import (
    MESSAGES,
    EgressProxy,
    TokenStore,
    _peer_address,
    basic_credentials,
    host_allowed,
    parse_hosts,
    parse_ports,
    refused_address,
)
from subzero_ai.sandbox import sandbox_address

PUBLIC = "93.184.216.34"  # a public address: the fake DNS answers with it
ALLOWED = ("example.com", "*.example.net", "*.pypi.org")
LOCAL_ADDRESSES = ["10.77.0.1", "192.168.1.5"]  # "this computer" for the tests
SANDBOX = "szsandbox-7-s001-a1b2c3"


def basic(name: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{name}:{token}".encode()).decode()


class ProxyCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="egress-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = Database(self.tmp / "test.db")
        self.now = datetime.now(timezone.utc)
        self.mono = 1000.0
        self.tokens = TokenStore(clock=lambda: self.now)
        # host -> the answers DNS gives, one per lookup (the last answer repeats)
        self.answers: dict[str, list[list[str]]] = {}
        self.resolved: list[str] = []
        self.connects: list[tuple[str, int]] = []
        self.upstream_payload = b""  # if set, the upstream sends this at once and closes
        self.received: list[bytes] = []  # what the upstream got from the client
        self.upstream = await asyncio.start_server(self._upstream, "127.0.0.1", 0)
        self.addCleanup(self._close_upstream)
        self.upstream_port = int(self.upstream.sockets[0].getsockname()[1])
        self.proxy: EgressProxy | None = None

    async def _close_upstream(self):
        self.upstream.close()

    async def _upstream(self, reader, writer):
        try:
            if self.upstream_payload:
                writer.write(self.upstream_payload)
                await writer.drain()
                return
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                self.received.append(data)
                writer.write(data)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    async def _resolve(self, host: str) -> list[str]:
        self.resolved.append(host)
        queue = self.answers.get(host)
        if not queue:
            raise socket.gaierror(f"no such name: {host}")
        return list(queue.pop(0) if len(queue) > 1 else queue[0])

    async def _connect(self, address: str, port: int):
        self.connects.append((address, port))
        return await asyncio.open_connection("127.0.0.1", self.upstream_port)

    async def start_proxy(self, **overrides) -> EgressProxy:
        # The test client connects from 127.0.0.1, so every sandbox name is told it belongs to 127.0.0.1
        options = dict(tokens=self.tokens, db=self.db, host="127.0.0.1", port=0, allowed_hosts=ALLOWED,
                       allowed_ports=(443,), resolver=self._resolve, connector=self._connect,
                       local_addresses=LOCAL_ADDRESSES, head_seconds=2, resolve_seconds=2, connect_seconds=2,
                       idle_seconds=5, clock=lambda: self.mono, address_for=lambda name: "127.0.0.1")
        options.update(overrides)
        self.proxy = EgressProxy(**options)
        self.assertTrue(await self.proxy.start(), self.proxy.error)
        self.addAsyncCleanup(self.proxy.stop)
        self.answers.setdefault("example.com", [[PUBLIC]])
        return self.proxy

    def issue(self, name: str = SANDBOX, *, hours: float = 1) -> str:
        return self.tokens.issue(name, self.now + timedelta(hours=hours))

    async def open_tunnel(self, target: str, auth: str | None):
        """Send a CONNECT. Returns (status, headers, reader, writer); on a refusal the body is read by the caller."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        if auth is not None:
            lines.append(f"Proxy-Authorization: {auth}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        status_line, *rest = head.decode("latin-1").split("\r\n")
        headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in rest if line)}
        return int(status_line.split(" ")[1]), headers, reader, writer

    async def refusal(self, target: str, auth: str | None) -> tuple[int, dict, str]:
        status, headers, reader, writer = await self.open_tunnel(target, auth)
        body = b""
        if "content-length" in headers:
            body = await asyncio.wait_for(reader.readexactly(int(headers["content-length"])), 5)
        writer.close()
        return status, headers, body.decode("utf-8")

    async def send_raw(self, data: bytes) -> tuple[int, str]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        writer.write(data)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        status = int(response.split(b" ", 2)[1]) if response.startswith(b"HTTP/1.") else 0
        return status, response.decode("utf-8", "replace")

    def audit_rows(self, action: str = "sandbox_proxy_blocked") -> list[dict]:
        rows = self.db.all("SELECT details FROM audit_log WHERE action = ? ORDER BY id", (action,))
        return [json.loads(row["details"]) for row in rows]


class TunnelTest(ProxyCase):
    async def test_an_allowed_connection_carries_bytes_both_ways(self):
        await self.start_proxy()
        token = self.issue()
        status, headers, reader, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 200)
        writer.write(b"ping")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 5), b"ping")
        writer.close()
        self.assertEqual(self.connects, [(PUBLIC, 443)], "connected to the checked address")
        self.assertEqual(self.resolved, ["example.com"])

    async def test_a_wildcard_allows_names_under_it_but_not_the_bare_suffix(self):
        await self.start_proxy()
        token = self.issue()
        self.answers["files.pypi.org"] = [[PUBLIC]]
        status, _, _, writer = await self.open_tunnel("files.pypi.org:443", basic(SANDBOX, token))
        writer.close()
        self.assertEqual(status, 200)
        status, _, _ = await self.refusal("pypi.org:443", basic(SANDBOX, token))
        self.assertEqual(status, 403, "*.pypi.org doesn't cover pypi.org itself")

    async def test_the_upstream_gets_what_the_client_sends_only(self):
        await self.start_proxy()
        token = self.issue()
        status, _, reader, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
        writer.write(b"\x16\x03\x01hello")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(8), 5), b"\x16\x03\x01hello")
        writer.close()
        self.assertEqual(b"".join(self.received), b"\x16\x03\x01hello")


class RefusalTest(ProxyCase):
    async def test_requests_other_than_connect_are_refused(self):
        await self.start_proxy()
        token = self.issue()
        for line in ("GET http://example.com/ HTTP/1.1", "POST http://example.com/ HTTP/1.1",
                     "GET ftp://example.com/file HTTP/1.1", "HEAD http://example.com/ HTTP/1.0"):
            with self.subTest(line=line):
                request = f"{line}\r\nProxy-Authorization: {basic(SANDBOX, token)}\r\n\r\n"
                status, reply = await self.send_raw(request.encode("ascii"))
                self.assertEqual(status, 405)
                self.assertIn("only opens secure website connections", reply)
        self.assertEqual(self.connects, [])
        self.assertEqual(self.resolved, [])

    async def test_a_name_not_on_the_list_is_refused_before_any_lookup(self):
        await self.start_proxy()
        token = self.issue()
        status, _, body = await self.refusal("evil.example.org:443", basic(SANDBOX, token))
        self.assertEqual(status, 403)
        self.assertIn("isn't on the team's allowed list", body)
        self.assertEqual(self.resolved, [], "not looked up at all")
        self.assertEqual(self.connects, [])
        self.assertEqual(self.audit_rows(), [{"name": SANDBOX, "host": "evil.example.org", "reason": "not-allowed"}])

    async def test_ip_addresses_are_refused_in_every_form(self):
        await self.start_proxy()
        token = self.issue()
        for target in (f"{PUBLIC}:443", "127.0.0.1:443", "[2001:db8::1]:443", "[::1]:443", "10.0.0.5:443"):
            with self.subTest(target=target):
                status, _, body = await self.refusal(target, basic(SANDBOX, token))
                self.assertEqual(status, 403)
                self.assertIn("website name, not an IP address", body)
        self.assertEqual(self.resolved, [])
        self.assertEqual(self.connects, [])

    async def test_ports_other_than_443_are_refused(self):
        await self.start_proxy()
        token = self.issue()
        for target in ("example.com:80", "example.com:8443", "example.com:0"):
            with self.subTest(target=target):
                status, _, body = await self.refusal(target, basic(SANDBOX, token))
                self.assertEqual(status, 403)
                self.assertIn("port 443", body)
        self.assertEqual(self.resolved, [])

    async def test_a_name_that_points_inside_is_refused_whatever_else_it_points_to(self):
        await self.start_proxy()
        token = self.issue()
        cases = {
            "private-10": [["10.0.0.5"]],
            "loopback": [["127.0.0.1"]],
            "loopback-v6": [["::1"]],
            "link-local": [["169.254.169.254"]],
            "private-192": [["192.168.1.1"]],
            "private-172": [["172.16.0.9"]],
            "cgnat": [["100.64.0.7"]],
            "multicast": [["224.0.0.251"]],
            "unspecified": [["0.0.0.0"]],
            "sandbox-subnet": [["10.77.0.20"]],
            "this-computer": [["192.168.1.5"]],
            "ipv6-link-local": [["fe80::1"]],
            "ipv6-unique-local": [["fc00::1"]],
            "mapped-private": [["::ffff:10.0.0.1"]],
            "mixed": [[PUBLIC, "10.1.2.3"]],
        }
        for label, answer in cases.items():
            with self.subTest(label=label):
                self.answers["example.com"] = answer
                status, _, body = await self.refusal("example.com:443", basic(SANDBOX, token))
                self.assertEqual(status, 403)
                self.assertIn("this computer or a private network", body)
        self.assertEqual(self.connects, [], "nothing was connected")

    async def test_a_name_with_no_addresses_or_no_answer_is_refused_or_reported(self):
        await self.start_proxy()
        token = self.issue()
        self.answers["example.com"] = [[]]
        status, _, _ = await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 403)
        self.answers.pop("example.com")
        status, _, body = await self.refusal("nothing.example.net:443", basic(SANDBOX, token))
        self.assertEqual(status, 502)
        self.assertIn("Couldn't find that website", body)
        self.assertEqual(self.connects, [])

    async def test_a_name_that_changes_its_address_after_the_check_is_not_followed(self):
        """DNS rebinding: the second lookup would give a private address. It never happens, and the connection goes
        to the address that was checked."""
        await self.start_proxy()
        token = self.issue()
        self.answers["example.com"] = [[PUBLIC], ["10.0.0.5"]]
        status, _, _, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
        writer.close()
        self.assertEqual(status, 200)
        self.assertEqual(self.resolved, ["example.com"], "looked up exactly once")
        self.assertEqual(self.connects, [(PUBLIC, 443)])

    async def test_a_long_request_head_is_refused(self):
        await self.start_proxy()
        token = self.issue()
        status, reply = await self.send_raw(
            f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: {basic(SANDBOX, token)}\r\n"
            f"X-Padding: {'a' * 9000}\r\n\r\n".encode("ascii"))
        self.assertEqual(status, 431)
        self.assertEqual(self.connects, [])

    async def test_a_request_that_isnt_http_gets_400(self):
        await self.start_proxy()
        status, _ = await self.send_raw(b"HELLO THERE\r\n\r\n")
        self.assertEqual(status, 400)


class UpstreamTimeoutTest(ProxyCase):
    async def test_a_site_that_does_not_answer_gets_a_504_with_a_plain_message(self):
        async def never(_address: str, _port: int):
            await asyncio.sleep(30)

        await self.start_proxy(connector=never, connect_seconds=0.2)
        token = self.issue()
        status, _headers, body = await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 504)
        self.assertIn("didn't answer", body)


class LoginTest(ProxyCase):
    async def test_a_missing_wrong_or_malformed_login_gets_407(self):
        await self.start_proxy()
        token = self.issue()
        other = self.issue("szsandbox-8-s002-ffffff")
        bad_logins = {
            "missing": None,
            "wrong token": basic(SANDBOX, "not-the-token"),
            "right token, wrong sandbox": basic("szsandbox-9-s009-000000", token),
            "other sandbox's token": basic(SANDBOX, other),
            "not base64": "Basic !!!",
            "not basic": f"Bearer {token}",
            "name only": "Basic " + base64.b64encode(SANDBOX.encode()).decode(),
            "empty token": basic(SANDBOX, ""),
        }
        for label, auth in bad_logins.items():
            with self.subTest(label=label):
                status, headers, _ = await self.refusal("example.com:443", auth)
                self.assertEqual(status, 407)
                self.assertIn("Basic", headers.get("proxy-authenticate", ""))
        self.assertEqual(self.connects, [])
        self.assertEqual(self.resolved, [])

    async def test_a_revoked_token_gets_407_and_open_tunnels_are_cut(self):
        await self.start_proxy()
        token = self.issue()
        status, _, reader, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 200)
        self.tokens.revoke(SANDBOX)
        await asyncio.to_thread(self.proxy.close_sandbox, SANDBOX)  # from another thread, as the app does
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"", "the open tunnel was closed")
        writer.close()
        status, _, _ = await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 407)

    async def test_an_expired_token_gets_407(self):
        await self.start_proxy()
        token = self.issue(hours=1)
        status, _, _, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
        writer.close()
        self.assertEqual(status, 200)
        self.now += timedelta(hours=1, seconds=1)
        status, _, _ = await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 407)

    async def test_a_token_is_checked_again_after_the_connection_is_made(self):
        """A token revoked while the upstream connection was being made is not used for the tunnel."""
        await self.start_proxy()
        token = self.issue()
        real_connect = self._connect

        async def revoke_while_connecting(address, port):
            upstream = await real_connect(address, port)
            self.tokens.revoke(SANDBOX)
            return upstream

        self.proxy._connector = revoke_while_connecting
        status, _, _ = await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 407)


class LimitTest(ProxyCase):
    async def test_connections_per_sandbox_are_capped(self):
        await self.start_proxy(per_sandbox_limit=2)
        token = self.issue()
        held = [await self.open_tunnel("example.com:443", basic(SANDBOX, token)) for _ in range(2)]
        for status, *_ in held:
            self.assertEqual(status, 200)
        status, _, body = await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 503)
        self.assertIn("too many connections open", body)
        for _, _, _, writer in held:
            writer.close()
        await asyncio.sleep(0.05)
        status, _, _, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
        writer.close()
        self.assertEqual(status, 200, "a slot is free again once a tunnel closes")

    async def test_connections_in_total_are_capped_across_sandboxes(self):
        await self.start_proxy(total_limit=1)
        first = self.issue(SANDBOX)
        second = self.issue("szsandbox-8-s002-ffffff")
        status, _, _, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, first))
        self.assertEqual(status, 200)
        status, _, body = await self.refusal("example.com:443", basic("szsandbox-8-s002-ffffff", second))
        self.assertEqual(status, 503)
        self.assertIn("busy right now", body)
        writer.close()

    async def test_a_sandbox_over_its_byte_allowance_is_cut_off(self):
        await self.start_proxy(byte_limit=1000)
        token = self.issue()
        self.upstream_payload = b"x" * 5000
        status, _, reader, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 200)
        received = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        self.assertLessEqual(len(received), 1000)
        self.assertLess(len(received), 5000)

    async def test_one_tunnel_over_the_per_connection_cap_is_cut_off(self):
        await self.start_proxy(connection_byte_limit=1000)
        token = self.issue()
        self.upstream_payload = b"y" * 5000
        status, _, reader, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 200)
        received = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        self.assertLess(len(received), 5000)

    async def test_the_allowance_is_read_from_settings_for_each_request(self):
        settings = load_settings(search_cwd=False, data_dir=self.tmp, sandbox_proxy_mb_limit=64)
        await self.start_proxy(settings=settings, allowed_hosts=())
        token = self.issue()
        status, _, _ = await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 403, "the allow-list is empty in Settings")
        settings.sandbox_allowed_hosts = "example.com"
        status, _, _, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
        writer.close()
        self.assertEqual(status, 200, "a change in Settings applies to the next request")


class AuditTest(ProxyCase):
    async def test_refusals_are_audited_once_a_minute_per_host(self):
        await self.start_proxy()
        token = self.issue()
        for _ in range(3):
            await self.refusal("evil.example.org:443", basic(SANDBOX, token))
        self.assertEqual(len(self.audit_rows()), 1, "a loop of refusals makes one row")
        self.mono += 61
        await self.refusal("evil.example.org:443", basic(SANDBOX, token))
        self.assertEqual(len(self.audit_rows()), 2, "a minute later it is recorded again")
        await self.refusal("other.example.org:443", basic(SANDBOX, token))
        self.assertEqual(len(self.audit_rows()), 3, "another host is recorded at once")
        self.assertEqual({row["host"] for row in self.audit_rows()}, {"evil.example.org", "other.example.org"})

    async def test_the_audit_never_holds_a_token(self):
        await self.start_proxy()
        token = self.issue()
        await self.refusal("evil.example.org:443", basic(SANDBOX, token))
        await self.refusal("10.0.0.5:443", basic(SANDBOX, token))
        rows = self.db.all("SELECT * FROM audit_log")
        dumped = json.dumps([dict(row) for row in rows])
        self.assertNotIn(token, dumped)
        self.assertNotIn(base64.b64encode(f"{SANDBOX}:{token}".encode()).decode(), dumped)

    async def test_the_log_never_holds_a_token(self):
        logging.disable(logging.NOTSET)  # tests/__init__ turns logging off; this test checks the log lines
        self.addCleanup(logging.disable, logging.CRITICAL)
        await self.start_proxy()
        token = self.issue()
        with self.assertLogs("subzero", level=logging.DEBUG) as captured:
            status, _, reader, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
            writer.close()
            await self.refusal("evil.example.org:443", basic(SANDBOX, token))
            await self.refusal("example.com:443", basic(SANDBOX, "wrong-token-value"))
            await asyncio.sleep(0.05)
        text = "\n".join(captured.output)
        self.assertIn("tunnel to example.com", text, "the byte count is logged")
        self.assertNotIn(token, text)
        self.assertNotIn("wrong-token-value", text)


class LifecycleTest(ProxyCase):
    async def test_stop_closes_open_tunnels_and_the_port(self):
        await self.start_proxy()
        token = self.issue()
        status, _, reader, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 200)
        port = self.proxy.port
        await asyncio.wait_for(self.proxy.stop(), 5)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")
        writer.close()
        self.assertFalse(self.proxy.running)
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
        await self.proxy.stop()  # a second stop is harmless

    async def test_an_idle_tunnel_is_closed(self):
        await self.start_proxy(idle_seconds=0.2)
        token = self.issue()
        status, _, reader, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 200)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")
        writer.close()

    async def test_a_port_that_is_taken_fails_closed_with_a_plain_message(self):
        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        self.addCleanup(busy.close)
        port = busy.getsockname()[1]
        proxy = EgressProxy(tokens=self.tokens, db=self.db, host="127.0.0.1", port=port, allowed_hosts=ALLOWED)
        self.assertFalse(await proxy.start())
        self.assertFalse(proxy.running)
        self.assertIn("couldn't start", proxy.error)
        self.assertIn("Check the sandbox host address and port in Settings", proxy.error)

    async def test_closing_a_sandbox_when_nothing_is_running_is_harmless(self):
        proxy = EgressProxy(tokens=self.tokens, db=self.db, host="127.0.0.1", port=0, allowed_hosts=ALLOWED)
        proxy.close_sandbox(SANDBOX)  # never started: nothing to close
        self.assertFalse(proxy.running)

    async def test_a_bad_allow_list_in_settings_refuses_every_request(self):
        settings = load_settings(search_cwd=False, data_dir=self.tmp, sandbox_allowed_hosts="https://example.com/x")
        proxy = EgressProxy.from_settings(settings, tokens=self.tokens, db=self.db)
        self.assertFalse(await proxy.start())
        self.assertIn("allowed websites", proxy.error)


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.store = TokenStore(clock=lambda: self.now)

    def test_only_a_hash_of_each_token_is_kept(self):
        token = self.store.issue(SANDBOX, self.now + timedelta(hours=1))
        stored = [entry.digest for entry in self.store._entries.values()]
        self.assertEqual(stored, [hashlib.sha256(token.encode()).digest()])
        self.assertNotIn(token.encode(), stored)
        self.assertGreaterEqual(len(token), 40, "random, from secrets.token_urlsafe(32)")

    def test_verify_is_true_only_for_the_current_token_of_that_sandbox(self):
        token = self.store.issue(SANDBOX, self.now + timedelta(hours=1))
        self.assertTrue(self.store.verify(SANDBOX, token))
        self.assertFalse(self.store.verify(SANDBOX, token + "x"))
        self.assertFalse(self.store.verify("szsandbox-other", token))
        self.assertFalse(self.store.verify(SANDBOX, ""))
        newer = self.store.issue(SANDBOX, self.now + timedelta(hours=1))
        self.assertFalse(self.store.verify(SANDBOX, token), "a new token replaces the old one")
        self.assertTrue(self.store.verify(SANDBOX, newer))

    def test_revoke_and_expiry_end_a_token(self):
        token = self.store.issue(SANDBOX, self.now + timedelta(minutes=5))
        self.store.revoke(SANDBOX)
        self.assertFalse(self.store.verify(SANDBOX, token))
        token = self.store.issue(SANDBOX, self.now + timedelta(minutes=5))
        self.now += timedelta(minutes=5)
        self.assertFalse(self.store.verify(SANDBOX, token))
        self.assertFalse(self.store.active(SANDBOX), "an expired token is dropped")

    def test_basic_credentials_are_read_strictly(self):
        self.assertEqual(basic_credentials(basic(SANDBOX, "tok")), (SANDBOX, "tok"))
        self.assertEqual(basic_credentials("basic " + base64.b64encode(f"{SANDBOX}:t".encode()).decode()),
                         (SANDBOX, "t"))
        self.assertIsNone(basic_credentials("Basic"))
        self.assertIsNone(basic_credentials(basic("", "tok")))
        self.assertIsNone(basic_credentials("Basic " + base64.b64encode(b"nocolon").decode()))


class PolicyTest(unittest.TestCase):
    def test_allow_list_rules(self):
        rules = parse_hosts("pypi.org, *.Example.net  github.com")
        self.assertEqual(rules, ("pypi.org", "*.example.net", "github.com"))
        self.assertTrue(host_allowed("pypi.org", rules))
        self.assertTrue(host_allowed("a.b.example.net", rules))
        self.assertFalse(host_allowed("example.net", rules))
        self.assertFalse(host_allowed("pypi.org.evil.com", rules))
        self.assertFalse(host_allowed("evilpypi.org", rules))
        self.assertFalse(host_allowed("notgithub.com", rules))

    def test_allow_list_refuses_anything_but_plain_names(self):
        for bad in ("https://pypi.org", "pypi.org/simple", "pypi.org:443", "10.0.0.5", "*.10.0.0.5", "*", "a*.b.com",
                    "pypi.org.", "-bad.com", "under_score.com", "x" * 300, "a" * 64 + ".com", "[::1]"):
            with self.subTest(bad=bad[:40]):
                with self.assertRaises(ValueError):
                    parse_hosts(bad)
        with self.assertRaises(ValueError):
            parse_hosts(", ".join(f"h{i}.example.com" for i in range(51)))

    def test_ports_must_be_numbers_from_1_to_65535(self):
        self.assertEqual(parse_ports("443"), (443,))
        self.assertEqual(parse_ports("443, 8443 443"), (443, 8443))
        for bad in ("", "0", "65536", "https", "44.3", "-1", "١٢٣"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_ports(bad)

    def test_addresses_that_must_never_be_connected_to(self):
        local = frozenset({"192.168.1.5"})
        for address in ("127.0.0.1", "10.1.2.3", "172.31.0.1", "192.168.0.9", "169.254.1.1", "100.127.0.1",
                        "100.64.0.1", "224.0.0.1", "0.0.0.0", "10.77.0.10", "192.168.1.5", "::1", "fe80::1", "fd00::1",
                        "::ffff:127.0.0.1", "ff02::1", "not-an-address"):
            with self.subTest(address=address):
                self.assertTrue(refused_address(address, local))
        for address in (PUBLIC, "2606:4700:4700::1111", "8.8.8.8"):
            with self.subTest(address=address):
                self.assertFalse(refused_address(address, local))


class SettingsValidationTest(unittest.TestCase):
    def check(self, **values):
        form = {"sandbox_host_address": "10.77.0.1", "sandbox_allowed_hosts": "pypi.org",
                "sandbox_allowed_ports": "443", "sandbox_proxy_mb_limit": "2048",
                "sandbox_proxy_max_per_sandbox": "16"}
        form.update(values)
        return saved_settings.validate("code", form, settings=None, db=None)

    def test_good_values_are_saved_in_their_normal_form(self):
        values, errors = self.check(sandbox_allowed_hosts="PyPI.org,  *.Example.net", sandbox_allowed_ports="443,8443")
        for name in ("sandbox_host_address", "sandbox_allowed_hosts", "sandbox_allowed_ports", "sandbox_proxy_mb_limit",
                     "sandbox_proxy_max_per_sandbox"):
            self.assertNotIn(name, errors)
        self.assertEqual(values["sandbox_allowed_hosts"], "pypi.org, *.example.net")
        self.assertEqual(values["sandbox_allowed_ports"], "443, 8443")
        self.assertEqual(values["sandbox_host_address"], "10.77.0.1")

    def test_an_empty_allow_list_is_allowed_and_bad_entries_are_named(self):
        values, errors = self.check(sandbox_allowed_hosts="")
        self.assertEqual(values["sandbox_allowed_hosts"], "")
        self.assertNotIn("sandbox_allowed_hosts", errors)
        _, errors = self.check(sandbox_allowed_hosts="https://pypi.org/simple")
        self.assertIn("sandbox_allowed_hosts", errors)
        self.assertIn("isn't a host name", errors["sandbox_allowed_hosts"])

    def test_the_host_address_must_be_a_private_ipv4_address(self):
        for bad in ("0.0.0.0", "8.8.8.8", "10.77.0.1/24", "not-an-ip", "::1", "example.com"):
            with self.subTest(bad=bad):
                _, errors = self.check(sandbox_host_address=bad)
                self.assertIn("sandbox_host_address", errors)
        values, errors = self.check(sandbox_host_address="10.77.0.1")
        self.assertNotIn("sandbox_host_address", errors)

    def test_ports_and_limits_are_checked(self):
        _, errors = self.check(sandbox_allowed_ports="0")
        self.assertIn("sandbox_allowed_ports", errors)
        _, errors = self.check(sandbox_proxy_mb_limit="10")
        self.assertIn("sandbox_proxy_mb_limit", errors)
        _, errors = self.check(sandbox_proxy_max_per_sandbox="0")
        self.assertIn("sandbox_proxy_max_per_sandbox", errors)

    def test_the_new_settings_are_documented(self):
        doc = (Path(__file__).resolve().parent.parent / "docs" / "SETTINGS.md").read_text(encoding="utf-8")
        for key in ("SANDBOX_HOST_ADDRESS", "SANDBOX_ALLOWED_HOSTS", "SANDBOX_ALLOWED_PORTS",
                    "SANDBOX_PROXY_MB_LIMIT", "SANDBOX_PROXY_MAX_PER_SANDBOX"):
            self.assertIn(key, doc)


class FakePeerWriter:
    """Only what _peer_address and _from_sandbox read from a stream writer."""

    def __init__(self, peer):
        self.peer = peer

    def get_extra_info(self, key):
        return self.peer if key == "peername" else None


class SandboxAddressTest(ProxyCase):
    """A login belongs to one sandbox, so the connection must come from that sandbox's own address. A connection from
    any other address is refused with 403 before anything is looked up or connected."""

    async def test_a_connection_from_another_address_is_refused_before_any_lookup(self):
        await self.start_proxy(address_for=lambda name: "10.77.0.11")
        token = self.issue()
        status, _, body = await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 403)
        self.assertEqual(body.strip(), MESSAGES["sandbox"])
        self.assertEqual(self.resolved, [], "nothing is looked up")
        self.assertEqual(self.connects, [], "nothing is connected")
        self.assertEqual([row["reason"] for row in self.audit_rows()], ["wrong-address"])

    async def test_a_sandbox_with_no_known_address_is_refused(self):
        await self.start_proxy(address_for=lambda name: None)
        token = self.issue()
        status, _, _ = await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 403, "no address for the name fails closed")
        self.assertEqual(self.connects, [])

    async def test_a_failing_address_lookup_is_refused(self):
        def broken(name):
            raise RuntimeError("the lookup broke")

        await self.start_proxy(address_for=broken)
        token = self.issue()
        status, _, _ = await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 403)

    async def test_each_sandbox_name_has_its_own_address(self):
        other = "szsandbox-8-s002-ffffff"
        await self.start_proxy(address_for=lambda name: {SANDBOX: "127.0.0.1", other: "10.77.0.12"}.get(name))
        token_a, token_b = self.issue(SANDBOX), self.issue(other)
        status, _, reader, writer = await self.open_tunnel("example.com:443", basic(SANDBOX, token_a))
        writer.close()
        self.assertEqual(status, 200, "its own address is accepted")
        status, _, _ = await self.refusal("example.com:443", basic(other, token_b))
        self.assertEqual(status, 403, "a login from the wrong address is refused for that name")

    async def test_the_default_address_is_the_one_the_slot_gives(self):
        self.assertEqual(sandbox_address(SANDBOX), "10.77.0.11", "slot 1 is 10.77.0.11")
        self.assertIsNone(sandbox_address("other-vm"))
        await self.start_proxy(address_for=None)  # the default: the sandbox's slot address, not 127.0.0.1
        token = self.issue()
        status, _, _ = await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(status, 403)

    async def test_refusals_from_the_wrong_address_are_audited_once_a_minute(self):
        await self.start_proxy(address_for=lambda name: "10.77.0.11")
        token = self.issue()
        for _ in range(3):
            await self.refusal("example.com:443", basic(SANDBOX, token))
        self.assertEqual(len(self.audit_rows()), 1)

    def test_the_peer_address_is_a_plain_ip_and_mapped_ipv6_is_unwrapped(self):
        self.assertEqual(_peer_address(FakePeerWriter(("10.77.0.11", 5000))), "10.77.0.11")
        self.assertEqual(_peer_address(FakePeerWriter(("::ffff:10.77.0.11", 5000, 0, 0))), "10.77.0.11")
        self.assertIsNone(_peer_address(FakePeerWriter(None)))
        self.assertIsNone(_peer_address(FakePeerWriter(("not-an-address", 1))))

    def test_a_missing_peer_address_fails_closed(self):
        proxy = EgressProxy(tokens=TokenStore(), db=None, host="127.0.0.1", port=0,
                            address_for=lambda name: "10.77.0.11")
        self.assertFalse(proxy._from_sandbox(SANDBOX, FakePeerWriter(None)))
        self.assertTrue(proxy._from_sandbox(SANDBOX, FakePeerWriter(("10.77.0.11", 1))))
        self.assertFalse(proxy._from_sandbox(SANDBOX, FakePeerWriter(("10.77.0.12", 1))))
        self.assertFalse(proxy._from_sandbox(SANDBOX, FakePeerWriter(("0.0.0.0", 1))))


class CloseFromAnyThreadTest(ProxyCase):
    """close_sandbox is called from other threads while tunnels run on the loop. Its usage and audit bookkeeping must
    neither crash nor log an error, and tunnels must still work afterwards."""

    async def echo_once(self, auth: str) -> bool:
        try:
            status, _, reader, writer = await self.open_tunnel("example.com:443", auth)
            if status != 200:
                writer.close()
                return False
            writer.write(b"ping")
            await writer.drain()
            data = await asyncio.wait_for(reader.readexactly(4), 5)
            writer.close()
            return data == b"ping"
        except (OSError, EOFError, asyncio.TimeoutError):
            return False  # a tunnel cut off by close_sandbox ends like this

    async def test_close_sandbox_from_threads_while_tunnels_run(self):
        await self.start_proxy()
        token = self.issue()
        auth = basic(SANDBOX, token)
        self.assertTrue(await self.echo_once(auth), "a tunnel works before the hammering")
        errors: list[BaseException] = []

        def hammer():
            try:
                for _ in range(200):
                    self.proxy.close_sandbox(SANDBOX)
                    self.proxy.close_sandbox("szsandbox-9-s009-aaaaaa")
            except BaseException as exc:  # reported to the test, which fails on it
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        with self.assertNoLogs("subzero.egress", level="ERROR"):
            for thread in threads:
                thread.start()
            results = []
            for _ in range(8):
                results += await asyncio.gather(*(self.echo_once(auth) for _ in range(6)))
            for thread in threads:
                await asyncio.to_thread(thread.join, 30)
        self.assertEqual(errors, [])
        self.assertTrue(all(isinstance(r, bool) for r in results))
        self.assertTrue(await self.echo_once(auth), "tunnels still work after the hammering")


if __name__ == "__main__":
    unittest.main()
