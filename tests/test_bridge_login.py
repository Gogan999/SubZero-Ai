"""Signing in the Claude subscription from the Settings page, with a pretend Claude Code program."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from subzero_ai import bridge_login
from subzero_ai.bridge_login import BridgeLogin, LoginError, failure_text, looks_like_code, sign_in_url
from subzero_ai.pi_client import bridge_credentials_path

from .helpers import ADMIN, MENTOR, STUDENT, Harness
from .test_settings_dashboard import HTML

GOOD_CODE = "goodcode123#state456"
URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz"

# Acts like `claude auth login --claudeai`: prints the address, waits for a code, and signs in only with GOOD_CODE.
FAKE_CLI = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    folder = os.environ["CLAUDE_CONFIG_DIR"]
    with open(os.path.join(folder, "env-seen.json"), "w") as f:
        json.dump({{"args": sys.argv[1:], "env": sorted(os.environ)}}, f)
    if os.environ.get("FAKE_NO_URL"):
        print("Login failed: network is down", flush=True)
        sys.exit(1)
    print("\\x1b[1mOpening browser to sign in\\u2026\\x1b[0m", flush=True)
    print("If the browser didn't open, visit: {URL}", flush=True)
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    with open(os.path.join(folder, "code-seen.txt"), "w") as f:
        f.write(code)
    if code != "{GOOD_CODE}":
        print("Login failed: That code wasn't accepted.", flush=True)
        sys.exit(1)
    with open(os.path.join(folder, ".credentials.json"), "w") as f:
        f.write("{{}}")
    print("Login successful.", flush=True)
""")


def install_fake_cli(folder: Path) -> Path:
    path = folder / "claude"
    path.write_text(FAKE_CLI, encoding="utf-8")
    path.chmod(0o755)
    return path


class PartsTest(unittest.TestCase):
    def test_sign_in_url_only_trusts_anthropic_sites(self):
        self.assertEqual(sign_in_url(f"visit: \x1b[4m{URL}\x1b[0m\n"), URL)
        self.assertEqual(sign_in_url("visit: https://evil.example/claude.com/login"), "")
        self.assertEqual(sign_in_url("visit: https://claude.com.evil.example/x"), "")
        self.assertEqual(sign_in_url("https://platform.claude.com/oauth/code/callback"),
                         "https://platform.claude.com/oauth/code/callback")

    def test_failure_text_and_code_shape(self):
        self.assertEqual(failure_text("Paste code > Login failed: Code expired.\n"), "Code expired.")
        self.assertEqual(failure_text("nothing"), "")
        self.assertTrue(looks_like_code(GOOD_CODE))
        for bad in ("", "short", "has space in it", "two\nlines123", "x" * 3000):
            self.assertFalse(looks_like_code(bad), bad)


@unittest.skipIf(os.name == "nt", "the pretend program is a script with a #! line")
class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cli = install_fake_cli(self.tmp)
        self.env = bridge_login.login_env(self.tmp)
        self.config = Path(self.env["CLAUDE_CONFIG_DIR"])

    def test_good_code_signs_in(self):
        login = BridgeLogin()
        self.assertEqual(login.start(self.cli, self.env, "Ada"), URL)
        self.assertTrue(login.waiting())
        self.assertEqual(login.by, "Ada")
        login.finish(GOOD_CODE, lambda: bridge_credentials_path(self.tmp).is_file())
        self.assertFalse(login.waiting())
        self.assertEqual(login.url, "")
        seen = json.loads((self.config / "env-seen.json").read_text())
        self.assertEqual(seen["args"], ["auth", "login", "--claudeai"])
        self.assertFalse([k for k in seen["env"] if k.upper().startswith(("ANTHROPIC_", "SUBZERO_"))])

    def test_wrong_code_gives_claude_codes_reason(self):
        login = BridgeLogin()
        login.start(self.cli, self.env)
        with self.assertRaises(LoginError) as caught:
            login.finish("wrongcode123", lambda: False)
        self.assertIn("wasn't accepted", str(caught.exception))
        self.assertFalse(login.waiting())

    def test_no_address(self):
        with self.assertRaises(LoginError) as caught:
            BridgeLogin().start(self.cli, {**self.env, "FAKE_NO_URL": "1"})
        self.assertIn("network is down", str(caught.exception))

    def test_old_sign_in_expires_and_cancel_stops_it(self):
        now = [0.0]
        login = BridgeLogin(clock=lambda: now[0])
        login.start(self.cli, self.env)
        now[0] = bridge_login.EXPIRES + 1
        self.assertFalse(login.waiting())
        with self.assertRaises(LoginError):
            login.finish(GOOD_CODE, lambda: True)
        login = BridgeLogin()
        login.start(self.cli, self.env)
        login.cancel()
        self.assertFalse(login.waiting())

    def test_starting_again_replaces_the_first(self):
        login = BridgeLogin()
        login.start(self.cli, self.env)
        first = login._proc
        login.start(self.cli, self.env)
        self.assertIsNotNone(first.poll())
        login.cancel()


@unittest.skipIf(os.name == "nt", "the pretend program is a script with a #! line")
class PageTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(ai_backend="fake")
        self.admin = self.h.login(ADMIN)
        self.cli = install_fake_cli(Path(tempfile.mkdtemp()))
        patcher = mock.patch("subzero_ai.web_admin.claude_cli_path", return_value=self.cli)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.h.services.bridge_login.cancel()
        self.h.close()

    def post(self, path, form=None):
        return self.admin.post(path, form or {}, headers=HTML, follow_redirects=True)

    def audit(self, action):
        return self.h.services.db.all("SELECT details FROM audit_log WHERE action = ?", (action,))

    def test_admin_only(self):
        for path in ("/settings/bridge/sign-in", "/settings/bridge/code", "/settings/bridge/cancel"):
            for who in (STUDENT, MENTOR):
                self.assertEqual(self.h.login(who).post(path, {}).status_code, 403, (path, who))
            self.assertEqual(self.admin.post(path, {}, csrf=False).status_code, 403, path)
        self.assertFalse(self.h.services.bridge_login.waiting())

    def test_box_is_always_there_so_the_link_works(self):
        page = self.admin.get("/settings").text
        self.assertIn('id="bridge"', page)
        self.assertIn('href="#bridge"', page)
        self.assertIn('action="/settings/bridge/sign-in"', page)
        self.assertNotIn("claude-login", page)

    def test_sign_in_from_the_page(self):
        page = self.post("/settings/bridge/sign-in").text
        self.assertIn("Open the Claude sign-in page", page)
        self.assertIn(URL.replace("&", "&amp;"), page)
        self.assertIn('action="/settings/bridge/code"', page)
        self.assertEqual(len(self.audit("bridge_sign_in_started")), 1)
        page = self.post("/settings/bridge/code", {"code": GOOD_CODE}).text
        self.assertIn("Signed in to the Claude subscription.", page)
        self.assertIn("Sign out of the Claude subscription", page)
        self.assertTrue(bridge_credentials_path(self.h.settings.data_dir).is_file())
        self.assertEqual(len(self.audit("bridge_signed_in")), 1)
        rows = self.h.services.db.all("SELECT details FROM audit_log")
        self.assertNotIn(GOOD_CODE, json.dumps([r["details"] for r in rows]))

    def test_bad_code_and_cancel(self):
        self.post("/settings/bridge/sign-in")
        page = self.post("/settings/bridge/code", {"code": "has spaces in it"}).text
        self.assertIn("doesn&#39;t look like the code", page)
        self.assertTrue(self.h.services.bridge_login.waiting())  # still waiting for a real one
        page = self.post("/settings/bridge/code", {"code": "wrongcode999"}).text
        self.assertIn("Not signed in.", page)
        self.assertIn("wasn&#39;t accepted", page)
        self.assertFalse(bridge_credentials_path(self.h.settings.data_dir).exists())
        self.post("/settings/bridge/sign-in")
        page = self.post("/settings/bridge/cancel").text
        self.assertIn("Sign-in stopped.", page)
        self.assertFalse(self.h.services.bridge_login.waiting())
        page = self.post("/settings/bridge/code", {"code": GOOD_CODE}).text
        self.assertIn("isn&#39;t waiting anymore", page)

    def test_without_the_program(self):
        with mock.patch("subzero_ai.web_admin.claude_cli_path", return_value=None):
            self.assertIn("Install the newest SubZero AI zip", self.admin.get("/settings").text)
            page = self.post("/settings/bridge/sign-in").text
        self.assertIn("Couldn&#39;t find the Claude Code program", page)
        self.assertFalse(self.h.services.bridge_login.waiting())


if __name__ == "__main__":
    unittest.main()
