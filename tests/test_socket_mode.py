"""The Slack Socket Mode client with a scripted stand-in for the WebSocket."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from subzero_ai import socket_mode
from subzero_ai.socket_mode import SocketModeClient


class ScriptedTransport:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False

    async def recv(self):
        await asyncio.sleep(0)
        return self.frames.pop(0) if self.frames else None

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.closed = True


def envelope(envelope_id, payload_type, **payload):
    return json.dumps({"envelope_id": envelope_id, "type": "interactive", "accepts_response_payload": True,
                       "payload": {"type": payload_type, **payload}})


class SocketModeTest(unittest.TestCase):
    def run_client(self, transports, dispatch, max_connects=None):
        transports = list(transports)
        opened = []

        async def main():
            stop = asyncio.Event()

            async def open_url():
                return "wss://example.invalid/link"

            async def connect(url):
                if not transports or (max_connects and len(opened) >= max_connects):
                    stop.set()
                    raise ConnectionError("no more connections in this test")
                t = transports.pop(0)
                opened.append(t)
                return t

            client = SocketModeClient(open_url=open_url, connect=connect, dispatch=dispatch, max_backoff=0.01)
            await asyncio.wait_for(client.run_forever(stop), timeout=5)

        asyncio.run(main())
        return opened

    def test_acknowledges_every_envelope_and_returns_modal_responses(self):
        seen = []

        async def dispatch(env):
            seen.append(env["payload"]["type"])
            if env["payload"]["type"] == "view_submission":
                return {"response_action": "clear"}
            return None

        first = ScriptedTransport([
            json.dumps({"type": "hello", "num_connections": 1}),
            envelope("e1", "block_actions", actions=[{"action_id": "approve", "value": "3"}]),
            "not json",
            envelope("e2", "view_submission", view={"callback_id": "edit_submit"}),
            json.dumps({"type": "disconnect", "reason": "refresh_requested"}),
        ])
        second = ScriptedTransport([json.dumps({"type": "hello"})])
        opened = self.run_client([first, second], dispatch)
        self.assertEqual(seen, ["block_actions", "view_submission"])
        self.assertEqual(first.sent, [{"envelope_id": "e1"},
                                      {"envelope_id": "e2", "payload": {"response_action": "clear"}}])
        self.assertTrue(first.closed)
        self.assertEqual(len(opened), 2, "reconnects after Slack asks it to")

    def test_slow_or_failing_handlers_still_get_acknowledged(self):
        async def dispatch(env):
            if env["envelope_id"] == "slow":
                await asyncio.sleep(1)
            raise RuntimeError("boom")

        transport = ScriptedTransport([envelope("slow", "block_actions"), envelope("bad", "block_actions")])
        with mock.patch.object(socket_mode, "ACK_DEADLINE_SECONDS", 0.05):
            self.run_client([transport], dispatch)
        self.assertEqual(transport.sent, [{"envelope_id": "slow"}, {"envelope_id": "bad"}])

    def test_keeps_retrying_when_connecting_fails(self):
        attempts = []

        async def main():
            stop = asyncio.Event()

            async def open_url():
                attempts.append(1)
                if len(attempts) >= 3:
                    stop.set()
                raise ConnectionError("Slack is down")

            client = SocketModeClient(open_url=open_url, connect=None, dispatch=None, max_backoff=0.01)
            await asyncio.wait_for(client.run_forever(stop), timeout=5)

        asyncio.run(main())
        self.assertEqual(len(attempts), 3)


if __name__ == "__main__":
    unittest.main()
