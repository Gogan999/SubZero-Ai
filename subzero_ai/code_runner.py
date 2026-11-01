"""Code sessions: Claude works in a copy of the team's GitHub repository on the mini PC.

A mentor gives Claude a task. Claude can look around the copy on its own (list, read and search files), but
every change to a file and every command waits for a mentor to approve it, with the exact change or command on
screen. Commands must start with a program an admin allowed in Settings (gradlew by default), run in the copy's
folder with a time limit and without the app's API keys or tokens in their environment variables. Nothing is
pushed to GitHub: when the work is done, a mentor downloads the changes as a patch and applies it themselves.

A command still runs as the app's own account, so it could read files the app can read, such as config.env.
That's why Claude can't change build files (anything a build runs before the code itself) and why the approval
card for a command lists the files Claude changed: tests and the robot code run during a build too.

Code is for mentors only. Each session's copy lives in DATA_DIR/code/<session id>: "base" is the repository as
downloaded and "work" is Claude's copy, so the changes are the difference between the two.

When sandboxes are on (Settings, code_sandbox), commands run inside the session's sandbox (sandbox.py) instead, against
a copy of "work"; approved edits are written to both copies, and the patch still comes from the host copy."""

from __future__ import annotations

import asyncio
import difflib
import io
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .ai_backend import capabilities_for, resolve_backend
from .claude import ClaudeError
from .config import Settings
from .connectors import ConnectorError, GitHub
from .db import Database, utcnow
from .live import Tasks
from .sandbox import (
    COPY_SKIP, GUEST_HOME, GUEST_PATH, MAX_OUTPUT_CHARS, ExitResult, OutputTail, SandboxError, SandboxHandle,
    SandboxManager, changed_in_archive, kill_process_tree, pack_tree, read_output,
)
from .store import Viewer

log = logging.getLogger("subzero.code")
_OutputTail = OutputTail  # the output tail used by run_program and the tests

MAX_TURNS = 40  # Claude replies per run, so a confused session can't spend without limit
MAX_REPO_ZIP = 100 * 1024 * 1024
MAX_REPO_FILES = 20_000
MAX_REPO_BYTES = 400 * 1024 * 1024
MAX_READ_BYTES = 200 * 1024
MAX_WRITE_BYTES = 500 * 1024
MAX_TASK_CHARS = 8000
MAX_TOKENS = 16000
KEEP_COPIES_DAYS = 14  # finished sessions' repository copies are deleted after this
MAX_SESSION_BYTES = 800 * 1024 * 1024  # one session's two copies together, build output included
MAX_RUNNING_COMMANDS = 2  # across the whole app: a second build slows the first one down
COMMAND_WAIT_SECONDS = 60  # how long a command waits for a free slot before it is refused
MAX_OPEN_SESSIONS = 5  # per mentor: sessions waiting or running at the same time
STUCK_SESSION_DAYS = 3  # waiting, running or preparing this long with no activity: stopped, copy deleted
REAP_SECONDS = 5  # after a command exits, how long its output may stay open (a background child process)
SKIP_DIRS = {".git", ".gradle", "build", "bin", "node_modules", "__pycache__", ".idea", ".vscode"}
FINISHED = ("done", "stopped", "failed")  # a session in one of these states has no more commands to run
# What a command in a sandbox gets: a fixed, Linux-style environment (the host's Windows variables don't apply)
SANDBOX_ENV = {"PATH": GUEST_PATH, "HOME": GUEST_HOME, "LANG": "C.UTF-8", "GRADLE_OPTS": "-Dorg.gradle.daemon=false"}
# Only what a build needs from the app's environment. API keys, tokens and the rest stay out.
SAFE_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME",
            "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "JAVA_HOME", "GRADLE_USER_HOME", "LANG", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")

READ_TOOLS = {"list_files", "read_file", "search"}
APPROVAL_TOOLS = {"write_file", "edit_file", "run_command"}

SYSTEM_PROMPT = """You are Claude, working with a mentor of a high school robotics team on the team's robot code.
You're in a copy of the team's repository on the team's computer. You can list, read and search files freely.
Every file change (write_file, edit_file) and every command (run_command) is shown to a mentor, who approves or
declines it before it happens, so give each one a short, clear reason. If a mentor declines a step, read their
note and adjust; don't ask for the same thing again.

Work in small steps. Read the relevant code before changing it, keep changes as small as the task allows, and
match the code's existing style. Use edit_file for changes to existing files and write_file only for new files
or full rewrites. You can't change build files (Gradle scripts and settings, the wrapper, vendordeps, scripts):
if one needs a change, say exactly what in your summary. Don't touch credentials, tokens or files outside the
task. Commands run from the repository folder; only these programs are allowed: {commands}. The computer may be Windows, where the Gradle wrapper is
gradlew.bat (asking for gradlew works on both).

Nothing you do is pushed anywhere. When you're done, say plainly what you changed and why, what you checked,
and anything the mentor should test on the robot. The mentor downloads the changes as a patch."""

TOOLS: list[dict[str, Any]] = [
    {"name": "list_files", "description": "List the files and folders in one folder of the repository.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Folder path relative to the repository root; empty for the root."}},
         "required": ["path"]}},
    {"name": "read_file", "description": "Read a text file, with line numbers. Long files can be read in parts.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "start_line": {"type": "integer", "description": "First line to read (1-based). Optional."},
         "end_line": {"type": "integer", "description": "Last line to read. Optional."}},
         "required": ["path"]}},
    {"name": "search", "description": "Search the repository's text files with a regular expression. Returns up "
                                      "to 100 matching lines.",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string"},
         "path": {"type": "string", "description": "Folder to search in; empty for the whole repository."}},
         "required": ["pattern"]}},
    {"name": "write_file", "description": "Create a file or replace its whole content. A mentor approves it first.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"},
         "reason": {"type": "string", "description": "One sentence the mentor reads before approving."}},
         "required": ["path", "content", "reason"]}},
    {"name": "edit_file", "description": "Replace one exact piece of text in a file with new text. old_text must "
                                         "appear exactly once. A mentor approves it first.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
         "reason": {"type": "string", "description": "One sentence the mentor reads before approving."}},
         "required": ["path", "old_text", "new_text", "reason"]}},
    {"name": "run_command", "description": "Run a command in the repository folder, such as a Gradle build or "
                                           "tests. A mentor approves it first. Output is cut to the last 20,000 "
                                           "characters.",
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string", "description": "For example: gradlew build"},
         "reason": {"type": "string", "description": "One sentence the mentor reads before approving."}},
         "required": ["command", "reason"]}},
]


@dataclass
class CodeError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


class ToolProblem(Exception):
    """A tool call that can't be done; the message goes back to Claude as an error result."""


# --------------------------------------------------------------------------- workspace
# Files a build runs or that decide what a build downloads. A command runs as the app's own account, so a
# changed build script could read the app's keys from disk. Claude can't change these; it can suggest changes
# in its summary for a mentor to make by hand.
BUILD_DIRS = ("gradle", "buildsrc", ".gradle", ".wpilib", "vendordeps", ".mvn")  # folders at the repository root
BUILD_NAMES = ("gradlew", "gradlew.bat", "gradle.properties")  # at any depth
BUILD_SUFFIXES = (".groovy", ".gradle", ".kts", ".bat", ".cmd", ".ps1", ".sh", ".exe", ".jar")


def is_build_file(name: str) -> bool:
    """True for a build file or script. Judged on a normalized path: case, slash style, and trailing dots and
    spaces don't matter (Windows drops them, so `gradlew.bat.` is gradlew.bat). `..` and Windows stream
    names (`file:stream`) are refused outright."""
    raw = name.replace("\\", "/").split("/")
    if any(part not in ("", ".") and not part.rstrip(". ") for part in raw) or any(":" in part for part in raw):
        return True
    parts = [part.rstrip(". ").lower() for part in raw if part not in ("", ".")]
    if not parts:
        return False
    return parts[0] in BUILD_DIRS or any(part in BUILD_NAMES or part.endswith(BUILD_SUFFIXES) for part in parts)


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.base = self.root / "base"
        self.work = self.root / "work"

    def ready(self) -> bool:
        return self.work.is_dir()

    def unpack(self, data: bytes) -> int:
        """Unpack GitHub's zip archive (one top folder) into base and work. Returns the number of files."""
        if self.root.exists():
            shutil.rmtree(self.root)
        self.base.mkdir(parents=True)
        count = total = 0
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise CodeError("GitHub sent something that isn't a zip archive.") from exc
        with archive:
            for info in archive.infolist():
                parts = info.filename.replace("\\", "/").split("/")[1:]  # drop GitHub's top folder
                if info.is_dir() or not parts or not parts[-1]:
                    continue
                if any(p in ("", ".", "..") or ":" in p for p in parts):
                    continue
                count += 1
                total += info.file_size
                if count > MAX_REPO_FILES or total > MAX_REPO_BYTES:
                    raise CodeError("The repository is too big to copy to the mini PC.")
                target = self.base.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if info.external_attr >> 16 & 0o100:
                    target.chmod(0o755)  # keep gradlew runnable on Linux and macOS
        shutil.copytree(self.base, self.work)
        return count

    def path(self, relative: str, *, must_exist: bool = True) -> Path:
        """A path inside the work copy. Refuses anything that would reach outside it."""
        relative = (relative or "").strip().replace("\\", "/").strip("/")
        if len(relative) > 400 or any(ord(ch) < 32 for ch in relative):
            raise ToolProblem("That path isn't allowed.")
        target = (self.work / relative).resolve()
        work = self.work.resolve()
        if target != work and work not in target.parents:
            raise ToolProblem("Paths must stay inside the repository.")
        if must_exist and not target.exists():
            raise ToolProblem(f"{relative or '.'} doesn't exist.")
        return target

    def rel(self, path: Path) -> str:
        return path.relative_to(self.work.resolve()).as_posix()

    # ---- read-only tools
    def list_files(self, path: str = "") -> str:
        folder = self.path(path)
        if not folder.is_dir():
            raise ToolProblem(f"{path} is a file, not a folder.")
        lines = []
        for entry in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:500]:
            lines.append(entry.name + "/" if entry.is_dir() else f"{entry.name} ({entry.stat().st_size:,} bytes)")
        return "\n".join(lines) or "(empty folder)"

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        target = self.path(path)
        if target.is_dir():
            raise ToolProblem(f"{path} is a folder. Use list_files.")
        data = target.read_bytes()
        if b"\x00" in data[:8000]:
            raise ToolProblem(f"{path} isn't a text file.")
        lines = data.decode("utf-8", errors="replace").splitlines()
        start = max(1, int(start_line or 1))
        end = min(len(lines), int(end_line or len(lines)))
        out, size = [], 0
        for number in range(start, end + 1):
            line = f"{number:>5}  {lines[number - 1]}"
            size += len(line) + 1
            if size > MAX_READ_BYTES:
                out.append(f"(cut off at line {number - 1} of {len(lines)}; read the rest with start_line)")
                break
            out.append(line)
        return "\n".join(out) if out else f"(no lines; the file has {len(lines)})"

    def files(self, root: Path | None = None):
        root = root or self.work
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                yield Path(dirpath) / name

    def search(self, pattern: str, path: str = "") -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolProblem(f"That isn't a valid regular expression: {exc}") from exc
        hits = []
        for file in self.files(self.path(path)):
            if file.stat().st_size > 1024 * 1024:
                continue
            data = file.read_bytes()
            if b"\x00" in data[:8000]:
                continue
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{self.rel(file.resolve())}:{number}: {line.strip()[:300]}")
                    if len(hits) >= 100:
                        return "\n".join(hits) + "\n(stopped at 100 matches)"
        return "\n".join(hits) or "No matches."

    # ---- changes, after a mentor approves them
    def check_writable(self, target: Path) -> None:
        name = self.rel(target)
        if is_build_file(name):
            raise ToolProblem(f"{name} is a build file, and Code sessions can't change build files, Gradle "
                              "settings, scripts or vendor dependencies, because a build runs them. Describe the "
                              "change in your summary so a mentor can make it by hand.")

    def size_bytes(self) -> int:
        """Everything in the session's folder: both copies and any build output."""
        total = 0
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return total

    def check_room(self, extra: int = 0) -> None:
        """Refuse an approved change or command when the session's folder is already at its size limit."""
        if self.size_bytes() + extra > MAX_SESSION_BYTES:
            raise ToolProblem(f"This session's copies of the repository would go over the "
                              f"{MAX_SESSION_BYTES // (1024 * 1024)} MB limit. Ask a mentor to start a new session.")

    def preview(self, tool: str, args: dict) -> str:
        """What a change would do, as a diff, for the mentor to approve."""
        target = self.path(args.get("path", ""), must_exist=False)
        self.check_writable(target)
        old = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        if tool == "write_file":
            new = str(args.get("content", ""))
        else:
            old_text, new_text = str(args.get("old_text", "")), str(args.get("new_text", ""))
            if not target.is_file():
                raise ToolProblem(f"{args.get('path')} doesn't exist. Use write_file to create it.")
            count = old.count(old_text) if old_text else 0
            if count != 1:
                raise ToolProblem("old_text must appear exactly once in the file; it appears "
                                  f"{count} times. Include more surrounding lines.")
            new = old.replace(old_text, new_text, 1)
        if len(new.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolProblem("That file would be too big.")
        name = self.rel(target)
        diff = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                    fromfile=f"a/{name}" if target.exists() else "/dev/null",
                                    tofile=f"b/{name}")
        return "".join(diff) or "(no change)"

    def apply(self, tool: str, args: dict) -> str:
        target = self.path(args.get("path", ""), must_exist=False)
        self.check_writable(target)
        if tool == "write_file":
            if target.is_dir():
                raise ToolProblem(f"{args.get('path')} is a folder.")
            content = str(args.get("content", ""))
        else:
            if not target.is_file():
                raise ToolProblem(f"{args.get('path')} doesn't exist.")
            old = target.read_text(encoding="utf-8", errors="replace")
            old_text = str(args.get("old_text", ""))
            if not old_text or old.count(old_text) != 1:
                raise ToolProblem("old_text must appear exactly once in the file.")
            content = old.replace(old_text, str(args.get("new_text", "")), 1)
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolProblem("That file would be too big.")
        self.check_room(len(content.encode("utf-8")))
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        return f"Saved {self.rel(target)}."

    def changes(self) -> list[dict[str, str]]:
        """Files that differ between the downloaded copy and Claude's copy."""
        names = set()
        for root in (self.base, self.work):
            if root.is_dir():
                names |= {p.relative_to(root).as_posix() for p in self.files(root)}
        out = []
        for name in sorted(names):
            before, after = self.base / name, self.work / name
            a = before.read_bytes() if before.is_file() else None
            b = after.read_bytes() if after.is_file() else None
            if a == b:
                continue
            kind = "added" if a is None else "deleted" if b is None else "changed"
            if (a and b"\x00" in a[:8000]) or (b and b"\x00" in b[:8000]):
                diff = f"Binary file {name} {kind}.\n"
            else:
                diff = "".join(difflib.unified_diff(
                    (a or b"").decode("utf-8", errors="replace").splitlines(keepends=True),
                    (b or b"").decode("utf-8", errors="replace").splitlines(keepends=True),
                    fromfile="/dev/null" if a is None else f"a/{name}",
                    tofile="/dev/null" if b is None else f"b/{name}"))
                if diff and not diff.endswith("\n"):
                    diff += "\n\\ No newline at end of file\n"
            out.append({"name": name, "kind": kind, "diff": diff})
        return out


def split_command(command: str, allowed: tuple[str, ...]) -> tuple[str, list[str]]:
    """The program's name and its arguments, if the command starts with an allowed program. No shell is involved."""
    try:
        args = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise ToolProblem(f"Couldn't read that command: {exc}") from exc
    if not args:
        raise ToolProblem("The command is empty.")
    if any(ch in command for ch in "|&;<>`$%^") or "\n" in command:
        raise ToolProblem("Run one program at a time, without pipes, redirects or shell syntax.")
    program = args[0].removeprefix("./").removeprefix(".\\")
    name = re.sub(r"\.(bat|cmd|exe|sh)$", "", program, flags=re.I)
    if "/" in name or "\\" in name or name.lower() not in {a.lower() for a in allowed}:
        raise ToolProblem(f"Only these programs can be run: {', '.join(allowed)}. An admin can allow more in "
                          "Settings.")
    for option in args[1:]:
        if _blocked_option(option):
            raise ToolProblem(f"Claude can't pass {option} to the build, because it loads other scripts or picks "
                              "other build files. Describe the change in your summary so a mentor can run it by hand.")
    return name, args[1:]


def parse_command(command: str, allowed: tuple[str, ...], work: Path) -> list[str]:
    """The command as arguments, if it starts with an allowed program. The program is looked up in the
    repository folder first (for the Gradle wrapper), then on PATH. No shell is involved."""
    name, rest = split_command(command, allowed)
    local = [work / (name + ".bat"), work / name] if os.name == "nt" else [work / name]
    found = next((p for p in local if p.is_file()), None)
    resolved = str(found) if found else shutil.which(name)
    if not resolved:
        raise ToolProblem(f"{name} isn't installed on this computer.")
    if found is not None and os.name != "nt" and not os.access(found, os.X_OK):
        return ["sh", resolved, *rest]
    return [resolved, *rest]


def _blocked_option(arg: str) -> bool:
    """Gradle options that load another script or pick another build file: -I, -c, -b, their attached forms
    (-Ipath), and the long forms with or without `=`."""
    if any(arg == opt or arg.startswith(opt + "=") for opt in ("--init-script", "--settings-file", "--build-file", "--gradle-user-home")):
        return True
    if arg == "-g" or arg.startswith("-Dgradle.user.home") or arg.startswith("-Dorg.gradle.user.home"):
        return True
    return arg[:2] in ("-I", "-c", "-b") and not arg.startswith("--")


_COMMAND_SLOTS = threading.BoundedSemaphore(MAX_RUNNING_COMMANDS)
_RUNNING_LOCK = threading.Lock()


class _RunningCommand:
    """A command that is running now, so a mentor's Stop can find and kill it."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.stopped = False


_RUNNING: dict[int, _RunningCommand] = {}  # session id -> the command it is running


def stop_command(session_id: int) -> bool:
    """Kill the command a session is running, with everything it started. True if one was running."""
    with _RUNNING_LOCK:
        running = _RUNNING.get(session_id)
        if running is None:
            return False
        running.stopped = True
    kill_process_tree(running.proc)
    return True


@contextmanager
def command_slot() -> Iterator[None]:
    """Hold one of the MAX_RUNNING_COMMANDS slots for a command, or refuse once the wait is too long."""
    if not _COMMAND_SLOTS.acquire(timeout=COMMAND_WAIT_SECONDS):
        raise ToolProblem("Too many commands are running on the computer right now. Ask Claude to try again in "
                          "a few minutes.")
    try:
        yield
    finally:
        _COMMAND_SLOTS.release()


def command_report(text: str, exit_code: int | None, *, stopped: bool, timed_out: bool, timeout: int) -> str:
    """The end of a command's output, with a line saying how it ended."""
    if stopped:
        return text + "\n(Stopped by a mentor.)"
    if timed_out:
        return text + f"\n(Stopped after {timeout} seconds.)"
    return text + f"\n(Exit code {exit_code}.)"


def run_program(args: list[str], cwd: Path, timeout: int, session_id: int | None = None) -> str:
    """Run one program in cwd for at most `timeout` seconds and return the end of its output. At most
    MAX_RUNNING_COMMANDS run at once across the app; a command waits for a free slot, then is refused."""
    with command_slot():
        return _run_in_slot(args, cwd, timeout, session_id)


def _run_in_slot(args: list[str], cwd: Path, timeout: int, session_id: int | None) -> str:
    env = {key: os.environ[key] for key in SAFE_ENV if key in os.environ}
    # No Gradle daemon: it would outlive the command (for hours) and hold memory on the mini PC
    env["GRADLE_OPTS"] = "-Dorg.gradle.daemon=false"
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, **kwargs)
    except OSError as exc:
        raise ToolProblem(f"Couldn't start the command: {exc}") from exc
    running = _RunningCommand(proc)
    if session_id is not None:
        with _RUNNING_LOCK:
            _RUNNING[session_id] = running
    tail = OutputTail(MAX_OUTPUT_CHARS * 4)  # 4 bytes is the most a character takes in UTF-8
    reader = threading.Thread(target=read_output, args=(proc.stdout, tail, None), daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(proc)
        proc.wait()
    finally:
        if session_id is not None:
            with _RUNNING_LOCK:
                if _RUNNING.get(session_id) is running:
                    del _RUNNING[session_id]
    reader.join(REAP_SECONDS)
    if reader.is_alive():  # a child process still holds the output open: stop everything it started
        kill_process_tree(proc)
        reader.join(REAP_SECONDS)
    if not reader.is_alive():
        proc.stdout.close()
    return command_report(tail.text(), proc.returncode, stopped=running.stopped, timed_out=timed_out, timeout=timeout)


def short_title(task: str) -> str:
    first = " ".join(task.split())
    return first if len(first) <= 70 else first[:67].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------- sessions
class CodeRunner:
    def __init__(self, *, settings: Settings, db: Database, claude, github: GitHub, tasks: Tasks,
                 sandboxes: SandboxManager | None = None):
        self.settings = settings
        self.db = db
        self.claude = claude
        self.github = github
        self.tasks = tasks
        self.sandboxes = sandboxes
        self._busy: set[int] = set()

    @property
    def available(self) -> bool:
        return self.github.configured

    @property
    def sandboxed(self) -> bool:
        """True when commands run in a sandbox (code_sandbox is set to hyperv or fake), not on this computer."""
        return self.sandboxes is not None and self.sandboxes.enabled

    def workspace(self, session_id: int) -> Workspace:
        return Workspace(self.settings.data_dir / "code" / str(session_id))

    def get(self, session_id: int) -> sqlite3.Row | None:
        return self.db.one("SELECT s.*, u.name AS owner_name FROM code_sessions s JOIN users u ON u.id = s.created_by "
                           "WHERE s.id = ?", (session_id,))

    def list(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.db.all(
            "SELECT s.*, u.name AS owner_name, (SELECT COUNT(*) FROM code_steps t WHERE t.session_id = s.id "
            "AND t.status = 'pending') AS pending FROM code_sessions s JOIN users u ON u.id = s.created_by "
            "ORDER BY s.updated_at DESC LIMIT ?", (limit,))

    def steps(self, session_id: int) -> list[sqlite3.Row]:
        return self.db.all("SELECT t.*, u.name AS reviewer_name FROM code_steps t LEFT JOIN users u ON "
                           "u.id = t.reviewed_by WHERE t.session_id = ? ORDER BY t.id", (session_id,))

    def count_waiting(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM code_sessions WHERE status = 'waiting'")["n"]

    def _set(self, session_id: int, **fields) -> None:
        fields["updated_at"] = utcnow()
        sql = ", ".join(f"{key} = ?" for key in fields)
        self.db.execute(f"UPDATE code_sessions SET {sql} WHERE id = ?", (*fields.values(), session_id))

    def _note(self, session_id: int, turn: int, text: str) -> None:
        self.db.execute("INSERT INTO code_steps (session_id, turn, kind, output, status, created_at) "
                        "VALUES (?, ?, 'note', ?, 'done', ?)", (session_id, turn, text, utcnow()))

    # ---- starting and steering
    def start(self, viewer: Viewer, task: str, model: str) -> int:
        if not viewer.is_mentor:
            raise CodeError("Code is for mentors.")
        if not self.available:
            raise CodeError("Code needs the team's GitHub repository. An admin can add it in Settings, Connectors.")
        if not capabilities_for(model, resolve_backend(self.settings)).code:
            raise CodeError("Code sessions need a Claude model. An admin can pick one in Settings.")
        task = (task or "").replace("\r\n", "\n").strip()
        if not task:
            raise CodeError("Describe what Claude should do.")
        if len(task) > MAX_TASK_CHARS:
            raise CodeError(f"Keep the task under {MAX_TASK_CHARS:,} characters.")
        self._check_open_limit(viewer.id)
        if self.sandboxed:
            try:
                self.sandboxes.check_capacity()
            except SandboxError as exc:
                raise CodeError(str(exc)) from exc
        now = utcnow()
        transcript = [{"role": "user", "content": task}]
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO code_sessions (title, task, status, model, repo, ref, transcript, created_by, "
                "created_at, updated_at) VALUES (?, ?, 'preparing', ?, ?, ?, ?, ?, ?, ?)",
                (short_title(task), task, model, self.settings.connector_github_repo,
                 self.settings.connector_github_branch, json.dumps(transcript), viewer.id, now, now))
            session_id = cur.lastrowid
            self.db.audit("code_session_started", actor_id=viewer.id, target_type="code", target_id=session_id,
                          details={"task": task, "repo": self.settings.connector_github_repo, "model": model},
                          conn=conn)
        self.tasks.spawn(self._prepare_and_run(session_id), name=f"code-{session_id}")
        return session_id

    def _check_open_limit(self, user_id: int) -> None:
        open_now = self.db.one("SELECT COUNT(*) AS n FROM code_sessions WHERE created_by = ? "
                               "AND status IN ('preparing', 'running', 'waiting')", (user_id,))["n"]
        if open_now >= MAX_OPEN_SESSIONS:
            raise CodeError(f"You already have {MAX_OPEN_SESSIONS} Code sessions waiting or running. Stop or "
                            "finish one first.")

    def follow_up(self, viewer: Viewer, session_id: int, text: str) -> None:
        row = self.get(session_id)
        text = (text or "").replace("\r\n", "\n").strip()
        if row is None or not viewer.is_mentor:
            raise CodeError("That session doesn't exist.")
        if row["status"] not in ("done", "stopped", "failed") or not self.workspace(session_id).ready():
            raise CodeError("Wait until Claude finishes, or stop the session first.")
        self._check_open_limit(row["created_by"])
        if not text or len(text) > MAX_TASK_CHARS:
            raise CodeError("Write what Claude should do next.")
        if json.loads(row["transcript"])[-1]["role"] == "assistant":
            # A run that ended between Claude's tool calls and their results. Answer them without running
            # anything here: commands run only in the background, never while a page request waits.
            self.db.execute("UPDATE code_steps SET status = 'declined', review_note = 'The session ended before "
                            "this ran. Ask for it again if it is still needed.' WHERE session_id = ? "
                            "AND status IN ('approved', 'pending')", (session_id,))
            self._answer_turn(session_id)
        transcript = json.loads(self.get(session_id)["transcript"])
        if transcript and transcript[-1]["role"] == "user":
            transcript[-1]["content"] = _as_blocks(transcript[-1]["content"]) + [{"type": "text", "text": text}]
        else:
            transcript.append({"role": "user", "content": text})
        turn = sum(1 for m in transcript if m["role"] == "assistant")
        self.db.execute("INSERT INTO code_steps (session_id, turn, kind, output, status, reviewed_by, created_at) "
                        "VALUES (?, ?, 'note', ?, 'done', ?, ?)", (session_id, turn, text, viewer.id, utcnow()))
        self._set(session_id, status="running", transcript=json.dumps(transcript), error=None)
        self.db.audit("code_follow_up", actor_id=viewer.id, target_type="code", target_id=session_id,
                      details={"text": text})
        self.tasks.spawn(self._run(session_id), name=f"code-{session_id}")

    def stop(self, viewer: Viewer, session_id: int) -> None:
        row = self.get(session_id)
        if row is None or not viewer.is_mentor:
            raise CodeError("That session doesn't exist.")
        if row["status"] in ("done", "stopped", "failed"):
            return
        stop_command(session_id)  # kills a build that is running right now
        with self.db.transaction() as conn:
            conn.execute("UPDATE code_steps SET status = 'declined', review_note = 'The session was stopped.', "
                         "reviewed_by = ?, finished_at = ? WHERE session_id = ? AND status = 'pending'",
                         (viewer.id, utcnow(), session_id))
            conn.execute("UPDATE code_steps SET status = 'declined', review_note = 'The session was stopped while "
                         "this ran.', finished_at = ? WHERE session_id = ? AND status = 'running'",
                         (utcnow(), session_id))
            self.db.audit("code_session_stopped", actor_id=viewer.id, target_type="code", target_id=session_id,
                          conn=conn)
        self._set(session_id, status="stopped")
        if row["status"] == "waiting":
            self._answer_turn(session_id)  # so the transcript stays valid for a follow-up
        if self.sandboxes is not None:
            self.tasks.spawn(self._end_sandbox(session_id), name=f"code-{session_id}")

    def decide(self, viewer: Viewer, step_id: int, approve: bool, note: str = "") -> int:
        if not viewer.is_mentor:
            raise CodeError("Only mentors approve Code steps.")
        step = self.db.one("SELECT * FROM code_steps WHERE id = ?", (step_id,))
        if step is None:
            raise CodeError("That step doesn't exist.")
        note = " ".join((note or "").split())[:500]
        changed = self.db.execute(
            "UPDATE code_steps SET status = ?, reviewed_by = ?, review_note = ? WHERE id = ? AND status = 'pending'",
            ("approved" if approve else "declined", viewer.id, note or None, step_id)).rowcount
        if not changed:
            raise CodeError("Someone already handled this step.")
        self.db.audit("code_step_approved" if approve else "code_step_declined", actor_id=viewer.id,
                      target_type="code", target_id=step["session_id"],
                      details={"tool": step["tool"], "input": json.loads(step["input"] or "{}"), "note": note})
        left = self.db.one("SELECT COUNT(*) AS n FROM code_steps WHERE session_id = ? AND status = 'pending'",
                           (step["session_id"],))["n"]
        if not left and self.get(step["session_id"])["status"] == "waiting":
            self._set(step["session_id"], status="running")
            self.tasks.spawn(self._resume(step["session_id"]), name=f"code-{step['session_id']}")
        return step["session_id"]

    # ---- the loop
    async def _prepare_and_run(self, session_id: int) -> None:
        workspace = self.workspace(session_id)
        try:
            data = await asyncio.to_thread(self.github.download_zip, MAX_REPO_ZIP)
            count = await asyncio.to_thread(workspace.unpack, data)
        except (ConnectorError, CodeError, OSError) as exc:
            self._set(session_id, status="failed", error=str(exc))
            return
        self._note(session_id, 0, f"Copied {count:,} files from {self.settings.connector_github_repo}"
                                  f"{' (' + self.settings.connector_github_branch + ')' if self.settings.connector_github_branch else ''}.")
        self._set(session_id, status="running")
        if self.sandboxed:
            try:
                await asyncio.to_thread(self._sandbox_for, session_id, workspace)
            except (SandboxError, CodeError) as exc:
                self._set(session_id, status="failed", error=str(exc))
                await asyncio.to_thread(self._settle_sandbox, session_id)
                return
        await self._run(session_id)

    async def _resume(self, session_id: int) -> None:
        """Do the approved steps of the last turn, give Claude every result, and carry on."""
        # In a worker thread: an approved build can take minutes, and the app must keep answering meanwhile
        if await asyncio.to_thread(self._answer_turn, session_id) and self.get(session_id)["status"] == "running":
            await self._run(session_id)

    def _answer_turn(self, session_id: int) -> bool:
        row = self.get(session_id)
        transcript = json.loads(row["transcript"])
        turn = sum(1 for m in transcript if m["role"] == "assistant") - 1
        steps = self.db.all("SELECT * FROM code_steps WHERE session_id = ? AND turn = ? AND kind = 'tool' "
                            "ORDER BY id", (session_id, turn))
        if not steps or transcript[-1]["role"] != "assistant":
            return False
        workspace = self.workspace(session_id)
        results = []
        for step in steps:
            args = json.loads(step["input"] or "{}")
            output, error = step["output"], step["status"] == "error"
            if step["status"] == "approved" and self.get(session_id)["status"] == "stopped":
                output, error = "The session was stopped before this ran.", True
                self.db.execute("UPDATE code_steps SET status = 'declined', review_note = ?, finished_at = ? "
                                "WHERE id = ?", (output, utcnow(), step["id"]))
            elif step["status"] == "approved":
                self.db.execute("UPDATE code_steps SET status = 'running' WHERE id = ?", (step["id"],))
                try:
                    output = self._execute(workspace, step["tool"], args, session_id)
                    status = "done"
                except ToolProblem as exc:
                    output, status, error = str(exc), "error", True
                # A Stop during the run has already marked this step stopped; keep that
                stopped = not self.db.execute(
                    "UPDATE code_steps SET status = ?, output = ?, finished_at = ? WHERE id = ? AND status = 'running'",
                    (status, output, utcnow(), step["id"])).rowcount
                if stopped:
                    output, error = "The session was stopped while this ran.", True
            elif step["status"] == "declined":
                who = self.db.one("SELECT name FROM users WHERE id = ?", (step["reviewed_by"],))
                output = (f"The mentor ({who['name'] if who else 'a mentor'}) declined this step."
                          + (f" Their note: {step['review_note']}" if step["review_note"] else ""))
                error = True
            results.append({"type": "tool_result", "tool_use_id": step["tool_use_id"], "content": output or "",
                            **({"is_error": True} if error else {})})
        transcript.append({"role": "user", "content": results})
        self._set(session_id, transcript=json.dumps(transcript))
        return True

    def _execute(self, workspace: Workspace, tool: str, args: dict, session_id: int | None = None) -> str:
        try:
            return self._run_tool(workspace, tool, args, session_id)
        except SandboxError as exc:
            raise ToolProblem(str(exc)) from exc

    def _run_tool(self, workspace: Workspace, tool: str, args: dict, session_id: int | None) -> str:
        sandboxed = self.sandboxed and session_id is not None
        if tool == "list_files":
            return workspace.list_files(str(args.get("path", "")))
        if tool == "read_file":
            return workspace.read_file(str(args.get("path", "")), args.get("start_line"), args.get("end_line"))
        if tool == "search":
            return workspace.search(str(args.get("pattern", "")), str(args.get("path", "")))
        if tool in ("write_file", "edit_file"):
            output = workspace.apply(tool, args)  # the host copy holds the approved change, and the patch comes from it
            if sandboxed:
                self._mirror_edit(session_id, workspace, str(args.get("path", "")))
            return output
        if tool == "run_command":
            if sandboxed:
                return self._run_in_sandbox(session_id, workspace, str(args.get("command", "")))
            command = parse_command(str(args.get("command", "")), self.settings.code_commands, workspace.work)
            workspace.check_room()
            return run_program(command, workspace.work, self.settings.code_command_timeout, session_id)
        raise ToolProblem(f"There's no tool called {tool}.")

    # ---- sandboxes (see sandbox.py): commands run in the session's sandbox, the host copy keeps the changes
    def _sandbox_for(self, session_id: int, workspace: Workspace) -> SandboxHandle:
        """The session's running sandbox. With none (the first command, or after the session finished), a new one
        is made with a fresh copy of the host's repository, which already has every approved change."""
        if self.get(session_id)["status"] == "stopped":
            raise SandboxError("The session was stopped.")
        with self.sandboxes.lock:
            row = self.sandboxes.live_for_session(session_id)
            if row is not None:
                return self.sandboxes.handle_of(row)
            owner = self.get(session_id)["created_by"]
            handle = self.sandboxes.create_for_session(session_id, actor_id=owner)
            try:
                self.sandboxes.backend(handle.backend).put_archive(
                    handle, pack_tree(workspace.work, COPY_SKIP), dest="work")
            except Exception as exc:
                self._drop_sandbox(session_id, "the copy failed")
                raise SandboxError("Couldn't copy the repository into the sandbox.") from exc
        self._note(session_id, 0, "Made a sandbox for this session. Commands run there, and it is deleted when the "
                                  "session ends.")
        return handle

    def _mirror_edit(self, session_id: int, workspace: Workspace, path: str) -> None:
        """Copy an approved edit into the sandbox. A sandbox made just now already has it, so it is copied again."""
        handle = self._sandbox_for(session_id, workspace)
        target = workspace.path(path)
        try:
            self.sandboxes.backend(handle.backend).put_file(handle, f"work/{workspace.rel(target)}",
                                                            target.read_bytes())
        except Exception as exc:
            self._drop_sandbox(session_id, "the edit couldn't be copied")
            raise SandboxError("The sandbox couldn't be updated, so it was deleted. The next command makes a new "
                               "one.") from exc

    def _run_in_sandbox(self, session_id: int, workspace: Workspace, command: str) -> str:
        name, rest = split_command(command, self.settings.code_commands)
        workspace.check_room()
        handle = self._sandbox_for(session_id, workspace)
        backend = self.sandboxes.backend(handle.backend)
        with command_slot():
            result: ExitResult = backend.run(handle, [f"./{name}", *rest], cwd="work", env=SANDBOX_ENV,
                                             timeout=self.settings.code_command_timeout)
        return command_report(result.output, result.exit_code, stopped=result.stopped, timed_out=result.timed_out,
                              timeout=self.settings.code_command_timeout)

    def _drop_sandbox(self, session_id: int, why: str) -> None:
        try:
            self.sandboxes.destroy_for_session(session_id, reason=f"deleted: {why}")
        except SandboxError:
            log.warning("Code session %s: its sandbox couldn't be deleted yet", session_id, exc_info=True)

    def _kill_sandbox_command(self, session_id: int) -> None:
        row = self.sandboxes.live_for_session(session_id)
        if row is None:
            return
        try:
            self.sandboxes.backend(row["backend"]).kill(self.sandboxes.handle_of(row))
        except Exception:
            log.warning("Code session %s: couldn't stop the command in its sandbox", session_id, exc_info=True)

    async def _end_sandbox(self, session_id: int) -> None:
        """After a Stop: kill the command running in the sandbox, then delete the sandbox."""
        await asyncio.to_thread(self._kill_sandbox_command, session_id)
        await asyncio.to_thread(self._settle_sandbox, session_id)

    def _settle_sandbox(self, session_id: int) -> None:
        """Delete the session's sandbox once the session has finished. Its files are saved to the host first."""
        row = self.get(session_id)
        if self.sandboxes is None or row is None or row["status"] not in FINISHED:
            return
        box = self.sandboxes.live_for_session(session_id)
        if box is None:
            return
        workspace = self.workspace(session_id)
        try:
            self.sandboxes.destroy_record(box, reason="session finished",
                                          before=lambda handle: self._export_sandbox(session_id, workspace, handle))
        except SandboxError:
            log.warning("Code session %s: its sandbox will be deleted on the next cleanup", session_id, exc_info=True)

    def _export_sandbox(self, session_id: int, workspace: Workspace, handle: SandboxHandle) -> None:
        """Keep a copy of the sandbox's repository on the host, and say which files commands changed there.
        The patch still comes from the host copy, which only holds approved edits."""
        data = self.sandboxes.backend(handle.backend).get_archive(handle, src="work", exclude=COPY_SKIP)
        (workspace.root / "sandbox-export.zip").write_bytes(data)
        drift = changed_in_archive(data, workspace.work, COPY_SKIP)
        if drift:
            listed = ", ".join(drift[:20]) + (" and more" if len(drift) > 20 else "")
            self._note(session_id, 0, f"Commands changed files in the sandbox that the patch doesn't include: {listed}. "
                                      "The patch only has changes made through approved edits, so check these by hand.")

    async def _run(self, session_id: int) -> None:
        if session_id in self._busy:
            return
        self._busy.add(session_id)
        try:
            await self._loop(session_id)
        except Exception as exc:  # keep the session readable whatever went wrong
            log.exception("Code session %s failed", session_id)
            self._set(session_id, status="failed", error=f"Something went wrong ({type(exc).__name__}).")
        finally:
            self._busy.discard(session_id)
        if self.sandboxes is not None:
            await asyncio.to_thread(self._settle_sandbox, session_id)

    async def _loop(self, session_id: int) -> None:
        workspace = self.workspace(session_id)
        system = SYSTEM_PROMPT.format(commands=", ".join(self.settings.code_commands))
        for _ in range(MAX_TURNS):
            row = self.get(session_id)
            if row["status"] != "running":
                return
            transcript = json.loads(row["transcript"])
            try:
                reply = await self.claude.tool_turn(model=row["model"], system=system, messages=transcript,
                                                    tools=TOOLS, max_tokens=MAX_TOKENS, effort="high")
            except ClaudeError as exc:
                self._set(session_id, status="failed", error=str(exc))
                return
            content = reply.get("content") or []
            usage = reply.get("usage") or {}
            transcript.append({"role": "assistant", "content": content})
            turn = sum(1 for m in transcript if m["role"] == "assistant") - 1
            if self.get(session_id)["status"] != "running":
                return  # stopped while Claude was thinking; drop the reply
            self._set(session_id, transcript=json.dumps(transcript),
                      input_tokens=row["input_tokens"] + int(usage.get("input_tokens") or 0),
                      output_tokens=row["output_tokens"] + int(usage.get("output_tokens") or 0))
            uses = [block for block in content if block.get("type") == "tool_use"]
            for block in content:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.db.execute("INSERT INTO code_steps (session_id, turn, kind, output, status, created_at) "
                                    "VALUES (?, ?, 'text', ?, 'done', ?)", (session_id, turn, block["text"], utcnow()))
            if not uses:
                if reply.get("stop_reason") == "max_tokens":
                    self._set(session_id, status="failed", error="Claude's reply was too long. Ask it to continue "
                                                                  "in smaller steps.")
                else:
                    self._set(session_id, status="done")
                return
            pending = False
            for block in uses:
                tool, args = block.get("name", ""), block.get("input") or {}
                status, output = "done", None
                if tool in APPROVAL_TOOLS:
                    try:
                        if tool == "run_command":
                            if self.sandboxed:
                                split_command(str(args.get("command", "")), self.settings.code_commands)
                            else:
                                parse_command(str(args.get("command", "")), self.settings.code_commands, workspace.work)
                            output = str(args.get("command", ""))
                        else:
                            output = workspace.preview(tool, args)
                        status, pending = "pending", True
                    except ToolProblem as exc:
                        status, output = "error", str(exc)
                else:
                    try:
                        output = await asyncio.to_thread(self._execute, workspace, tool, args, session_id)
                    except ToolProblem as exc:
                        status, output = "error", str(exc)
                self.db.execute(
                    "INSERT INTO code_steps (session_id, turn, kind, tool, tool_use_id, input, output, status, "
                    "created_at, finished_at) VALUES (?, ?, 'tool', ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, turn, tool, block.get("id"), json.dumps(args), output, status, utcnow(),
                     None if status == "pending" else utcnow()))
            if pending:
                self._set(session_id, status="waiting")
                return
            await asyncio.to_thread(self._answer_turn, session_id)
        self._set(session_id, status="stopped", error=f"Stopped after {MAX_TURNS} replies so it can't run up costs. "
                                                     "Send a follow-up to let it carry on.")


def prune_workspaces(db: Database, data_dir: Path, days: int = KEEP_COPIES_DAYS) -> int:
    """Delete the repository copies of sessions that finished more than `days` ago (two copies of the repository
    each, plus build output). The steps and transcript stay, so the session can still be read. Sessions left
    waiting, running or preparing for STUCK_SESSION_DAYS are stopped first. Returns how many copies were deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    removed = prune_stuck_workspaces(db, data_dir)
    for row in db.all("SELECT id FROM code_sessions WHERE status IN ('done', 'stopped', 'failed') "
                      "AND updated_at < ?", (cutoff,)):
        folder = Path(data_dir) / "code" / str(row["id"])
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
            db.execute("INSERT INTO code_steps (session_id, turn, kind, output, status, created_at) "
                       "VALUES (?, 0, 'note', ?, 'done', ?)",
                       (row["id"], f"The copy of the repository was deleted after {days} days to save space.",
                        utcnow()))
            removed += 1
    return removed


def prune_stuck_workspaces(db: Database, data_dir: Path, days: int = STUCK_SESSION_DAYS) -> int:
    """Stop sessions that have sat waiting, running or preparing for `days` days, and delete their copies.
    The database rows stay, so the session can still be read."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    removed = 0
    for row in db.all("SELECT id FROM code_sessions WHERE status IN ('preparing', 'running', 'waiting') "
                      "AND updated_at < ?", (cutoff,)):
        session_id = row["id"]
        stop_command(session_id)
        with db.transaction() as conn:
            conn.execute("UPDATE code_steps SET status = 'declined', review_note = ?, finished_at = ? "
                         "WHERE session_id = ? AND status = 'pending'",
                         (f"The session was stopped after {days} days with no activity.", utcnow(), session_id))
            conn.execute("UPDATE code_sessions SET status = 'stopped', error = ?, updated_at = ? WHERE id = ?",
                         (f"Stopped after {days} days with no activity.", utcnow(), session_id))
            db.audit("code_session_expired", actor_id=None, target_type="code", target_id=session_id,
                     details={"days": days}, conn=conn)
        folder = Path(data_dir) / "code" / str(session_id)
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
            db.execute("INSERT INTO code_steps (session_id, turn, kind, output, status, created_at) "
                       "VALUES (?, 0, 'note', ?, 'done', ?)",
                       (session_id, f"The session had no activity for {days} days, so it was stopped and its copy "
                                    "of the repository was deleted to save space.", utcnow()))
        removed += 1
    return removed


def _as_blocks(content) -> list[dict]:
    return content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
