"""The real server (Uvicorn) with live updates: a student watches a reply stream in after a mentor approves."""

from __future__ import annotations

import asyncio
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import requests
import uvicorn

from subzero_ai.claude import FakeClaude
from subzero_ai.config import load_settings
from subzero_ai.slack_api import FakeSlack
from subzero_ai.web import create_app

CSRF_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.port = free_port()
        settings = load_settings(search_cwd=False, data_dir=cls.tmp, dev_login=True,
                                 public_url=f"http://127.0.0.1:{cls.port}", slack_approvals_channel="CAPPROVALS",
                                 slack_team_id="TDEV", approval_window_minutes=0)
        cls.slack = FakeSlack()
        cls.app = create_app(settings, slack=cls.slack, claude=FakeClaude(delay=0.01), background=False)
        config = uvicorn.Config(cls.app, host="127.0.0.1", port=cls.port, log_level="warning",
                                timeout_graceful_shutdown=1)
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 10
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.05)
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def session_for(self, user):
        session = requests.Session()
        session.trust_env = False
        resp = session.post(self.base + "/auth/dev", data={"user": user}, timeout=5, allow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        page = session.get(self.base + "/about", timeout=5)  # open to everyone signed in; carries the CSRF token
        self.assertEqual(page.status_code, 200)
        session.headers["X-CSRF-Token"] = CSRF_RE.search(page.text).group(1)
        session.headers["Accept"] = "application/json"
        return session

    @staticmethod
    def watch(session, url):
        """Open the live-update stream on its own connection, as a browser's EventSource does."""
        watcher = requests.Session()
        watcher.trust_env = False
        watcher.cookies.update(session.cookies)
        return watcher.get(url, stream=True, timeout=15, headers={"Accept": "text/event-stream"})

    def student_dm(self, text, user="DEV-student-ava"):
        """The student messages the bot in Slack. The app handles it on its own loop, as Socket Mode would."""
        services = self.app.state.services
        event = {"type": "message", "channel_type": "im", "channel": "D0" + user, "user": user, "text": text,
                 "ts": f"1700000{int(time.time() * 1000) % 1000000:06d}.000100"}
        envelope = {"type": "events_api", "envelope_id": "e", "payload": {"type": "event_callback", "event": event}}

        async def go():
            await services.interactions.dispatch(envelope)
            await services.tasks.wait_idle()

        asyncio.run(go())
        row = services.db.one("SELECT message_id FROM slack_dm_messages WHERE channel = ? AND ts = ?",
                              (event["channel"], event["ts"]))
        message = services.store.message(row["message_id"])
        return message["id"], message["conversation_id"]

    def test_the_review_queue_stream_tells_a_mentor_about_new_and_decided_messages(self):
        kim = self.session_for("DEV-mentor-kim")
        got: list[str] = []
        connected = threading.Event()
        stream = self.watch(kim, f"{self.base}/queue/events")
        self.assertEqual(stream.headers["content-type"].split(";")[0], "text/event-stream")

        def read():
            try:
                for line in stream.iter_lines(decode_unicode=True):
                    if line.startswith("retry:"):
                        connected.set()
                    elif line.startswith("event:"):
                        got.append(line[6:].strip())
            except Exception:
                pass

        threading.Thread(target=read, daemon=True).start()
        self.assertTrue(connected.wait(5), "the live-update stream didn't start")

        mid, cid = self.student_dm("Explain current limits on a NEO")
        deadline = time.time() + 5
        while "queue" not in got and time.time() < deadline:
            time.sleep(0.05)
        self.assertIn("queue", got, "a new waiting message reaches the mentor's open Review page")
        before = len(got)

        resp = kim.post(f"{self.base}/m/{mid}/approve", timeout=5)
        self.assertEqual(resp.status_code, 200, resp.text)
        deadline = time.time() + 5
        while len(got) == before and time.time() < deadline:
            time.sleep(0.05)
        stream.close()
        self.assertGreater(len(got), before, "approving also reaches it")

        # The read-only chat page shows the reply once it is done
        deadline = time.time() + 10
        html = ""
        while time.time() < deadline:
            html = kim.get(f"{self.base}/c/{cid}?thread=1", timeout=5).text
            if 'class="msg-body prose"' in html:
                break
            time.sleep(0.2)
        self.assertIn('class="msg-body prose"', html)
        self.assertIn('<div class="code">', html)

    def test_a_student_gets_no_chat_page_or_stream_on_the_real_server(self):
        ava = self.session_for("DEV-student-ava")
        _, cid = self.student_dm("A normal question", user="DEV-student-ava")
        for path in (f"/c/{cid}", f"/c/{cid}?thread=1", f"/c/{cid}/events", "/queue/events", "/queue"):
            resp = ava.get(self.base + path, timeout=5, allow_redirects=False,
                           headers={"Accept": "text/html"})
            self.assertNotIn("normal question", resp.text, path)
        resp = ava.get(f"{self.base}/c/{cid}", timeout=5, allow_redirects=False, headers={"Accept": "text/html"})
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/code"))
        self.assertEqual(ava.get(f"{self.base}/c/{cid}/events", timeout=5).status_code, 404)
        self.assertEqual(ava.get(f"{self.base}/queue/events", timeout=5, allow_redirects=False).status_code, 403)


if __name__ == "__main__":
    unittest.main()
