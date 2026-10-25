"""Slack Socket Mode client.

The app opens a WebSocket to Slack (so nothing needs to reach into the mini PC), receives
button clicks and modal submissions as envelopes, and acknowledges each one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

log = logging.getLogger("subzero.socket")

ACK_DEADLINE_SECONDS = 2.5


class Transport(Protocol):
    async def recv(self) -> str | None: ...
    async def send(self, data: str) -> None: ...
    async def close(self) -> None: ...


Dispatcher = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


class SocketModeClient:
    def __init__(self, *, open_url: Callable[[], Awaitable[str]], connect: Callable[[str], Awaitable[Transport]],
                 dispatch: Dispatcher, max_backoff: float = 60.0):
        self.open_url = open_url
        self.connect = connect
        self.dispatch = dispatch
        self.max_backoff = max_backoff
        self.connected = asyncio.Event()

    async def run_forever(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            transport: Transport | None = None
            try:
                url = await self.open_url()
                transport = await self.connect(url)
                backoff = 1.0
                await self._session(transport, stop)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Slack connection failed; retrying in %.0f seconds", backoff)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self.max_backoff)
            finally:
                self.connected.clear()
                if transport is not None:
                    try:
                        await transport.close()
                    except Exception:
                        pass

    async def _session(self, transport: Transport, stop: asyncio.Event) -> None:
        while not stop.is_set():
            raw = await transport.recv()
            if raw is None:
                log.info("Slack closed the connection; reconnecting")
                return
            try:
                message = json.loads(raw)
            except ValueError:
                log.warning("Ignoring a non-JSON frame from Slack")
                continue
            kind = message.get("type")
            if kind == "hello":
                self.connected.set()
                log.info("Connected to Slack (Socket Mode)")
                continue
            if kind == "disconnect":
                log.info("Slack asked to reconnect (%s)", message.get("reason"))
                return
            envelope_id = message.get("envelope_id")
            if not envelope_id:
                continue
            ack: dict[str, Any] = {"envelope_id": envelope_id}
            try:
                payload = await asyncio.wait_for(self.dispatch(message), timeout=ACK_DEADLINE_SECONDS)
                if payload:
                    ack["payload"] = payload
            except asyncio.TimeoutError:
                log.error("Handling a Slack %s took too long to acknowledge in time", kind)
            except Exception:
                log.exception("Error while handling a Slack %s", kind)
            await transport.send(json.dumps(ack))


class WebsocketsTransport:
    """Adapter for the `websockets` package (installed on the server; version 13 or newer)."""

    def __init__(self, ws: Any):
        self.ws = ws

    @classmethod
    async def connect(cls, url: str) -> WebsocketsTransport:
        from websockets.asyncio.client import (
            connect,  # imported lazily so tests don't need it
        )

        ws = await connect(url, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024)
        return cls(ws)

    async def recv(self) -> str | None:
        from websockets.exceptions import ConnectionClosed

        try:
            data = await self.ws.recv()
        except ConnectionClosed:
            return None
        return data.decode("utf-8") if isinstance(data, bytes) else data

    async def send(self, data: str) -> None:
        await self.ws.send(data)

    async def close(self) -> None:
        await self.ws.close()
