"""The real server (Uvicorn) with live updates: a student watches a reply stream in after a mentor approves."""

from __future__ import annotations

import json
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
                                 slack_team_id="TDEV")
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
        page = session.post(self.base + "/auth/dev", data={"user": user}, timeout=5)
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

    def test_reply_streams_to_the_student(self):
        ava = self.session_for("DEV-student-ava")
        kim = self.session_for("DEV-mentor-kim")
        created = ava.post(self.base + "/chats/new", data={"text": "Explain current limits on a NEO"}, timeout=5).json()
        cid = created["url"].rsplit("/", 1)[1]
        mid = created["message_id"]

        events: list[tuple[str, dict]] = []
        connected = threading.Event()
        finished = threading.Event()
        stream = self.watch(ava, f"{self.base}/c/{cid}/events")
        self.assertEqual(stream.headers["content-type"].split(";")[0], "text/event-stream")

        def read():
            name = None
            try:
                for line in stream.iter_lines(decode_unicode=True):
                    if line.startswith("retry:"):
                        connected.set()
                    elif line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        events.append((name, json.loads(line[5:])))
                        if name == "title":
                            finished.set()
                            return
            except Exception:
                pass

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        self.assertTrue(connected.wait(5), "the live-update stream didn't start")

        resp = kim.post(f"{self.base}/m/{mid}/approve", timeout=5)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(finished.wait(15), f"reply didn't finish streaming; got {[e[0] for e in events]}")
        stream.close()

        kinds = [name for name, _ in events]
        self.assertEqual(kinds[0], "changed")
        self.assertEqual(events[0][1]["id"], mid)
        self.assertIn("delta", kinds)
        self.assertLess(kinds.index("delta"), kinds.index("done"))
        deltas = [data for name, data in events if name == "delta"]
        self.assertEqual([d["seq"] for d in deltas], list(range(len(deltas))), "chunks arrive in order")
        text = "".join(d["text"] for d in deltas)
        self.assertEqual(text, FakeClaude.reply_for("Explain current limits on a NEO"))

        reply_id = deltas[0]["id"]
        html = ava.get(f"{self.base}/c/{cid}/m/{reply_id}", timeout=5).text
        self.assertIn('class="msg-body prose"', html)
        self.assertIn('<div class="code">', html)

    def test_other_students_are_cut_off_when_a_chat_turns_private(self):
        ava = self.session_for("DEV-student-ava")
        ben = self.session_for("DEV-student-ben")
        kim = self.session_for("DEV-mentor-kim")
        created = ava.post(self.base + "/chats/new", data={"text": "A normal question"}, timeout=5).json()
        cid = created["url"].rsplit("/", 1)[1]
        kim.post(f"{self.base}/m/{created['message_id']}/approve", timeout=5)
        time.sleep(1.5)  # let the reply finish
        follow = ava.post(f"{self.base}/c/{cid}/messages", data={"text": "Something private"}, timeout=5).json()

        got = []
        stream = self.watch(ben, f"{self.base}/c/{cid}/events")
        self.assertEqual(stream.status_code, 200)

        def read():
            try:
                for line in stream.iter_lines(decode_unicode=True):
                    if line.startswith("event:"):
                        got.append(line[6:].strip())
                        if got[-1] == "gone":
                            return
            except Exception:
                pass

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        time.sleep(0.3)
        kim.post(f"{self.base}/m/{follow['message_id']}/hold", timeout=5)
        reader.join(5)
        stream.close()
        self.assertIn("gone", got)
        self.assertEqual(ben.get(f"{self.base}/c/{cid}", timeout=5).status_code, 404)


if __name__ == "__main__":
    unittest.main()
