"""Signing in the Claude subscription (Claude Code) from the Settings page.

`claude auth login --claudeai` prints a sign-in address and then waits for a code on its input. The admin opens the
address on their own computer or phone, signs in, copies the code Claude shows, and pastes it on the Settings page;
we hand it to the waiting program. Nothing has to happen on the mini PC itself. The code is never logged or saved.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

URL_WAIT = 30.0  # seconds to wait for Claude Code to print its sign-in address
CODE_WAIT = 60.0  # seconds to wait for Claude Code to finish after it gets the code
EXPIRES = 15 * 60  # a sign-in that nobody finished is stopped after this many seconds
# Only addresses on Anthropic's own sites are shown as the sign-in link
SIGN_IN_HOSTS = ("claude.com", "claude.ai", "anthropic.com")
_URL = re.compile(r"https://[^\s\"'<>]+")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def login_env(data_dir: Path) -> dict[str, str]:
    """What Claude Code gets when it signs in: only the system variables it needs, plus the bridge's own config
    folder. Never an API key or token, which would make it bill the API (or skip the login) instead."""
    from .pi_client import child_env
    config_dir = Path(data_dir) / "bridge" / "claude-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    env = child_env({})
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


class LoginError(Exception):
    """A sign-in step that didn't work, with a sentence to show the admin."""


def sign_in_url(output: str) -> str:
    """The first Anthropic sign-in address in Claude Code's output, or an empty string."""
    for match in _URL.finditer(clean(output)):
        url = match.group(0).rstrip(".,)")
        host = (urlsplit(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in SIGN_IN_HOSTS):
            return url
    return ""


def clean(text: str) -> str:
    """Claude Code's output without colours and cursor codes."""
    return _ANSI.sub("", text).replace("\r", "")


def failure_text(output: str) -> str:
    """Claude Code's own reason a sign-in failed, if it gave one, as one plain sentence."""
    for line in clean(output).splitlines():
        line = line.strip()
        if "Login failed:" in line:
            return line.split("Login failed:", 1)[1].strip()
    return ""


def looks_like_code(code: str) -> bool:
    """True if a pasted code could be one: one line of letters, digits and a few symbols. Never says why not."""
    return bool(re.fullmatch(r"[A-Za-z0-9_\-#.~=+/]{8,2048}", code))


class BridgeLogin:
    """One sign-in at a time: the Claude Code program waiting for its code, and the address it printed."""

    def __init__(self, popen: Callable[..., Any] = subprocess.Popen, clock: Callable[[], float] = time.monotonic):
        self._popen = popen
        self._clock = clock
        self._lock = threading.Lock()
        self._proc: Any = None
        self._output: list[str] = []
        self._changed = threading.Condition(self._lock)
        self.url = ""
        self.started = 0.0
        self.by = ""  # who started it, shown on the page

    # --- state --------------------------------------------------------------------------------------------------

    def waiting(self) -> bool:
        """True while a sign-in is waiting for its code. Stops one that's been waiting too long."""
        with self._lock:
            if self._proc is None:
                return False
            if self._proc.poll() is None and self._clock() - self.started < EXPIRES:
                return True
        self.cancel()
        return False

    def _text(self) -> str:
        return "".join(self._output)

    def _read(self, proc: Any) -> None:
        """Copy the program's output into memory as it comes, so its pipe never fills up."""
        stream = proc.stdout
        while True:
            try:
                chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            except (OSError, ValueError):
                chunk = b""
            with self._changed:
                if chunk:
                    self._output.append(chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk)
                self._changed.notify_all()
                if not chunk:
                    return

    def _wait_for(self, test: Callable[[], bool], timeout: float) -> bool:
        deadline = self._clock() + timeout
        with self._changed:
            while not test():
                left = deadline - self._clock()
                if left <= 0:
                    return False
                self._changed.wait(min(left, 0.5))
            return True

    # --- steps --------------------------------------------------------------------------------------------------

    def start(self, cli: Path, env: dict[str, str], by: str = "") -> str:
        """Start Claude Code's sign-in and return the address to open. Replaces a sign-in that was waiting."""
        self.cancel()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        # BROWSER=none keeps Claude Code from opening a browser on the mini PC, where nobody is looking
        try:
            proc = self._popen([str(cli), "auth", "login", "--claudeai"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env={**env, "BROWSER": "none"},
                               creationflags=flags)
        except OSError as exc:
            raise LoginError(f"Couldn't start Claude Code on the mini PC ({type(exc).__name__}).") from exc
        with self._lock:
            self._proc, self._output, self.url, self.started, self.by = proc, [], "", self._clock(), by
        threading.Thread(target=self._read, args=(proc,), name="bridge-login", daemon=True).start()
        found = self._wait_for(lambda: bool(sign_in_url(self._text())) or proc.poll() is not None, URL_WAIT)
        url = sign_in_url(self._text())
        if not found or not url:
            reason = failure_text(self._text())
            self.cancel()
            raise LoginError("Claude Code didn't give a sign-in address" + (f": {reason}" if reason else ".")
                             + " Try again in a minute.")
        with self._lock:
            self.url = url
        return url

    def finish(self, code: str, signed_in: Callable[[], bool]) -> None:
        """Give Claude Code the pasted code and wait until it's done. Raises LoginError if it didn't sign in."""
        with self._lock:
            proc = self._proc
            before = len(self._text())
        if proc is None or proc.poll() is not None:
            self.cancel()
            raise LoginError("That sign-in isn't waiting anymore. Press Sign in again to get a new address.")
        try:
            proc.stdin.write((code + "\n").encode())
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            self.cancel()
            raise LoginError("Claude Code stopped before it got the code. Press Sign in again.") from exc
        done = self._wait_for(lambda: proc.poll() is not None, CODE_WAIT)
        if done:
            self._wait_for(lambda: False, 0.2)  # let the last of the output arrive
        reason = failure_text(self._text()[before:])
        ok = done and proc.poll() == 0 and signed_in()
        self.cancel()
        if not ok:
            raise LoginError(reason or ("Claude Code didn't finish signing in. " if not done else
                                        "The sign-in didn't work. ") + "Press Sign in again to get a new address.")

    def cancel(self) -> None:
        """Stop a sign-in that's waiting, if any."""
        with self._lock:
            proc, self._proc, self.url, self.by = self._proc, None, "", ""
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError, AttributeError):
                pass
