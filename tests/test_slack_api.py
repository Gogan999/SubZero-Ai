"""The Slack Web API client against a local stand-in server."""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import requests

from subzero_ai.slack_api import SlackError, SlackWebApi


class FakeSlackServer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, headers=None):
        self.send_response(status)
        self.send_header("content-type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        method = self.path.rsplit("/", 1)[1]
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["content-length"] or 0)).decode()).items()}
        self.server.calls.append({"method": method, "form": form, "auth": self.headers.get("authorization")})
        if method == "chat.postMessage":
            if self.server.rate_limit_once and not self.server.limited:
                self.server.limited = True
                return self.reply(429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "0"})
            return self.reply(200, {"ok": True, "channel": form["channel"], "ts": "1700000000.000100"})
        if method == "conversations.open":
            return self.reply(200, {"ok": True, "channel": {"id": "D0DM"}})
        if method == "conversations.members":
            if form.get("cursor") == "page2":
                return self.reply(200, {"ok": True, "members": ["U3"], "response_metadata": {"next_cursor": ""}})
            return self.reply(200, {"ok": True, "members": ["U1", "U2"], "response_metadata": {"next_cursor": "page2"}})
        if method == "apps.connections.open":
            return self.reply(200, {"ok": True, "url": "wss://wss-primary.slack.com/link/?ticket=t"})
        return self.reply(200, {"ok": False, "error": "unknown_method"})


class SlackWebApiTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSlackServer)
        self.server.calls, self.server.rate_limit_once, self.server.limited = [], False, False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        session = requests.Session()
        session.trust_env = False
        self.api = SlackWebApi("xoxb-test", base_url=f"http://127.0.0.1:{self.server.server_port}/api/", session=session)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_post_message_encoding(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
        posted = asyncio.run(self.api.post_message("C1", "fallback", blocks))
        self.assertEqual(posted, {"channel": "C1", "ts": "1700000000.000100"})
        call = self.server.calls[0]
        self.assertEqual(call["auth"], "Bearer xoxb-test")
        self.assertEqual(json.loads(call["form"]["blocks"]), blocks)
        self.assertEqual(call["form"]["unfurl_links"], "false")
        self.assertNotIn("thread_ts", call["form"])

    def test_rate_limit_is_retried(self):
        self.server.rate_limit_once = True
        asyncio.run(self.api.post_message("C1", "x"))
        self.assertEqual([c["method"] for c in self.server.calls], ["chat.postMessage", "chat.postMessage"])

    def test_dm_opens_a_conversation_first(self):
        asyncio.run(self.api.dm("U1", "hello"))
        self.assertEqual([c["method"] for c in self.server.calls], ["conversations.open", "chat.postMessage"])
        self.assertEqual(self.server.calls[0]["form"]["users"], "U1")
        self.assertEqual(self.server.calls[1]["form"]["channel"], "D0DM")

    def test_channel_members_follows_pages(self):
        self.assertEqual(asyncio.run(self.api.channel_members("G1")), {"U1", "U2", "U3"})

    def test_socket_url_uses_the_app_token(self):
        url = asyncio.run(self.api.open_socket_url("xapp-test"))
        self.assertTrue(url.startswith("wss://"))
        self.assertEqual(self.server.calls[0]["auth"], "Bearer xapp-test")

    def test_errors(self):
        with self.assertRaises(SlackError) as caught:
            asyncio.run(self.api.call("views.publish"))
        self.assertEqual(caught.exception.error, "unknown_method")


if __name__ == "__main__":
    unittest.main()
