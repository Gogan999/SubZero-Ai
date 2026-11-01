"""Code sandboxes: a disposable virtual machine for the commands of each Code session.

A Code session's commands run inside its own sandbox instead of on the mini PC. The mentor's copy of the repository
stays on the host (reads, approvals and the patch download all use it), and every approved edit is written to both
copies. A sandbox is a Hyper-V virtual machine made from one shared base disk as a differencing disk, so the base is
only ever read. It is attached only to the private switch named in Settings, and it is deleted when its session
finishes, after its time limit, and when no record of it is left in the database.

FakeSandbox does the same job with folders and subprocesses on this computer. It exists for the tests and is not
isolated at all. HyperVSandbox builds its PowerShell and SSH commands with pure functions and takes the two runners
as parameters, so the tests can check every command without touching a real host.

Guest image details that are not known yet are marked TODO (the Linux user, the static address setup, the SSH key
and host key, python3 and the base disk's path). See docs/CODE_SANDBOX_PLAN.md, milestone M3 and M4.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .db import Database, utcnow

log = logging.getLogger("subzero.sandbox")

PREFIX = "szsandbox-"  # every sandbox's name starts with this, so the reaper only ever touches our own
COPY_SKIP = frozenset({".gradle", "build"})  # build output and caches: never copied into or out of a sandbox
OFF_VALUES = ("", "off")  # code_sandbox values that keep Code commands on this computer; anything else needs a sandbox
MAX_OUTPUT_CHARS = 20_000  # the end of a command's output that goes back to Claude
REAP_SECONDS = 5  # after a command exits, how long its output may stay open (a background child process)
STALE_CREATING = timedelta(minutes=30)  # a sandbox still "being made" after this long was left by a crash
MAX_SLOTS = 200  # sandboxes get a numbered slot; the slot picks the guest's static address
GUEST_ADDRESS = "10.77.0.{}"
GUEST_FIRST_SLOT_HOST = 10  # 10.77.0.10 is the first guest address; .1 is the host's
# TODO: the guest account and home folder come from the base image (not built yet).
GUEST_USER = "szrunner"
GUEST_HOME = "/home/szrunner"
GUEST_PATH = "/usr/local/bin:/usr/bin:/bin"
# A PowerShell single quote, including the typographic ones PowerShell also reads as quotes
PS_QUOTES = {"'": "''", "\u2018": "''", "\u2019": "''", "\u201a": "''", "\u201b": "''"}


class SandboxError(Exception):
    """A sandbox couldn't be made, used or deleted. The message is plain enough to show a mentor."""


class SandboxRefused(SandboxError):
    """A new sandbox isn't allowed right now (too many in use, too little disk, or not set up yet)."""


@dataclass(frozen=True)
class SandboxHandle:
    name: str
    backend: str
    created_at: str


@dataclass(frozen=True)
class ExitResult:
    exit_code: int | None
    output: str  # the end of the output, bounded to MAX_OUTPUT_CHARS
    timed_out: bool = False
    stopped: bool = False


class SandboxBackend(Protocol):
    """What the app needs from a sandbox. Paths for files and folders are relative to the sandbox's home folder."""

    name: str

    def create(self, session_id: int | str) -> SandboxHandle: ...
    def run(self, handle: SandboxHandle, argv: list[str], *, cwd: str, env: dict[str, str], timeout: int,
            on_output: Callable[[str], None] | None = None) -> ExitResult: ...
    def kill(self, handle: SandboxHandle) -> None: ...
    def put_file(self, handle: SandboxHandle, rel: str, data: bytes) -> None: ...
    def get_file(self, handle: SandboxHandle, rel: str) -> bytes: ...
    def list_dir(self, handle: SandboxHandle, rel: str) -> list[str]: ...
    def put_archive(self, handle: SandboxHandle, data: bytes, *, dest: str) -> None: ...
    def get_archive(self, handle: SandboxHandle, *, src: str, exclude: frozenset[str]) -> bytes: ...
    def destroy(self, handle: SandboxHandle) -> None: ...
    def list_existing(self) -> list[str]: ...


# --------------------------------------------------------------------------- names and paths
def make_name(session_id: int | str, slot: int) -> str:
    """A sandbox's name: our prefix, the Code session, its slot and a random tail."""
    return f"{PREFIX}{session_id}-s{slot:03d}-{os.urandom(3).hex()}"


def slot_of(name: str) -> int | None:
    match = re.search(r"-s(\d{3})-", name)
    return int(match.group(1)) if match else None


def next_slot(names: list[str]) -> int:
    """The lowest slot no existing sandbox uses."""
    used = {slot for slot in (slot_of(name) for name in names) if slot is not None}
    for slot in range(MAX_SLOTS):
        if slot not in used:
            return slot
    raise SandboxRefused("All sandbox slots are in use.")


def check_name(name: str) -> str:
    """Refuse any name that isn't one of ours. Every destructive command goes through this."""
    if not name.startswith(PREFIX) or not re.fullmatch(r"szsandbox-[A-Za-z0-9-]{1,60}", name):
        raise SandboxError(f"{name!r} isn't a sandbox name.")
    return name


def guest_address(slot: int) -> str:
    """The static address on the private network for a slot (10.77.0.10 and up)."""
    if not 0 <= slot < MAX_SLOTS:
        raise SandboxError("That sandbox slot doesn't exist.")
    return GUEST_ADDRESS.format(GUEST_FIRST_SLOT_HOST + slot)


def safe_rel(rel: str) -> str:
    """A path relative to the sandbox's home folder. Refuses anything that could leave it."""
    text = (rel or "").replace("\\", "/").strip("/")
    if not text:
        return "."
    parts = text.split("/")
    if any(part in ("", ".", "..") or ":" in part or any(ord(ch) < 32 for ch in part) for part in parts):
        raise SandboxError("That path isn't allowed in the sandbox.")
    return "/".join(parts)


# --------------------------------------------------------------------------- archives and output
def pack_tree(root: Path, exclude: frozenset[str] | set[str] = frozenset()) -> bytes:
    """A zip of a folder's files, with their permissions. Folders named in `exclude` are skipped at any depth,
    and symbolic links are never followed (a link could point at the mini PC's own files)."""
    root = Path(root)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in exclude)
                for name in sorted(filenames):
                    path = Path(dirpath) / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    info = zipfile.ZipInfo(path.relative_to(root).as_posix())
                    info.external_attr = (path.stat().st_mode & 0o777) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, path.read_bytes())
    return buf.getvalue()


def _zip_parts(name: str) -> list[str]:
    parts = name.replace("\\", "/").split("/")
    if name.startswith("/") or any(p in ("", ".", "..") or ":" in p for p in parts):
        raise SandboxError("The archive has a path that isn't allowed.")
    return parts


def unpack_tree(data: bytes, dest: Path) -> int:
    """Unpack a zip made by pack_tree into `dest`. Returns the number of files."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SandboxError("The copy sent to the sandbox was damaged.") from exc
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            target = dest.joinpath(*_zip_parts(info.filename))
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            mode = (info.external_attr >> 16) & 0o777
            os.chmod(target, (mode or 0o644) | 0o600)
            count += 1
    return count


def changed_in_archive(data: bytes, host_root: Path, exclude: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Files in a zip that differ from the same files under host_root (or are missing there)."""
    changed = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            parts = _zip_parts(info.filename)
            if any(part in exclude for part in parts[:-1]):
                continue
            target = Path(host_root).joinpath(*parts)
            if not target.is_file() or target.read_bytes() != archive.read(info):
                changed.append(info.filename)
    return sorted(changed)


class OutputTail:
    """Keeps only the end of a command's output, so a chatty command can't fill the app's memory."""

    def __init__(self, limit: int):
        self.limit = limit
        self.data = bytearray()
        self.cut = False

    def add(self, chunk: bytes) -> None:
        self.data += chunk
        if len(self.data) > self.limit:
            del self.data[:len(self.data) - self.limit]
            self.cut = True

    def text(self) -> str:
        text = self.data.decode("utf-8", errors="replace")
        if self.cut or len(text) > MAX_OUTPUT_CHARS:
            return "(…earlier output cut)\n" + text[-MAX_OUTPUT_CHARS:]
        return text


def read_output(stream, tail: OutputTail, on_output: Callable[[str], None] | None) -> None:
    """Read a command's output as it comes, in a worker thread, so nothing waits for the command to end."""
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                return
            tail.add(chunk)
            if on_output is not None:
                on_output(chunk.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Stop a command and everything it started (on Windows, the cmd.exe and JVM under it)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()


# --------------------------------------------------------------------------- FakeSandbox
@dataclass(eq=False)  # compared by identity, so it can live in a set
class _Run:
    proc: subprocess.Popen | None = None
    stopped: bool = False


class FakeSandbox:
    """Folders on this computer and subprocesses. For tests only: nothing is isolated."""

    name = "fake"

    def __init__(self, root: Path):
        self.root = Path(root)
        self._runs: dict[str, set[_Run]] = {}
        self._lock = threading.Lock()

    def _home(self, handle: SandboxHandle) -> Path:
        return self.root / check_name(handle.name)

    def _path(self, handle: SandboxHandle, rel: str) -> Path:
        home = self._home(handle).resolve()
        target = (home / safe_rel(rel)).resolve()
        if target != home and home not in target.parents:
            raise SandboxError("That path isn't allowed in the sandbox.")
        return target

    def create(self, session_id: int | str) -> SandboxHandle:
        name = make_name(session_id, next_slot(self.list_existing()))
        self._home(SandboxHandle(name, self.name, "")).mkdir(parents=True)
        return SandboxHandle(name, self.name, utcnow())

    def run(self, handle, argv, *, cwd, env, timeout, on_output=None) -> ExitResult:
        folder = self._path(handle, cwd)
        if not folder.is_dir():
            raise SandboxError(f"{cwd} doesn't exist in the sandbox.")
        run = _Run()
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            run.proc = subprocess.Popen(argv, cwd=folder, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, **kwargs)
        except OSError as exc:
            raise SandboxError(f"Couldn't start the command: {exc}") from exc
        with self._lock:
            self._runs.setdefault(handle.name, set()).add(run)
        tail = OutputTail(MAX_OUTPUT_CHARS * 4)  # 4 bytes is the most a character takes in UTF-8
        reader = threading.Thread(target=read_output, args=(run.proc.stdout, tail, on_output), daemon=True)
        reader.start()
        timed_out = False
        try:
            run.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(run.proc)
            run.proc.wait()
        finally:
            with self._lock:
                self._runs.get(handle.name, set()).discard(run)
        reader.join(REAP_SECONDS)
        if reader.is_alive():  # a child process still holds the output open
            kill_process_tree(run.proc)
            reader.join(REAP_SECONDS)
        if not reader.is_alive():
            run.proc.stdout.close()
        return ExitResult(run.proc.returncode, tail.text(), timed_out, run.stopped)

    def kill(self, handle) -> None:
        with self._lock:
            runs = list(self._runs.get(handle.name, ()))
        for run in runs:
            run.stopped = True
            if run.proc is not None:
                kill_process_tree(run.proc)

    def put_file(self, handle, rel, data) -> None:
        target = self._path(handle, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get_file(self, handle, rel) -> bytes:
        return self._path(handle, rel).read_bytes()

    def list_dir(self, handle, rel) -> list[str]:
        folder = self._path(handle, rel)
        return sorted(p.name + ("/" if p.is_dir() else "") for p in folder.iterdir())

    def put_archive(self, handle, data, *, dest) -> None:
        target = self._path(handle, dest)
        if target.exists():
            shutil.rmtree(target)
        unpack_tree(data, target)

    def get_archive(self, handle, *, src, exclude) -> bytes:
        return pack_tree(self._path(handle, src), exclude)

    def destroy(self, handle) -> None:
        self.kill(handle)
        home = self._home(handle)
        if home.exists():
            try:
                shutil.rmtree(home)
            except OSError as exc:
                raise SandboxError(f"Couldn't delete the sandbox folder: {exc}") from exc

    def list_existing(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and p.name.startswith(PREFIX))


# --------------------------------------------------------------------------- Hyper-V: pure command builders
def ps_quote(value: str) -> str:
    """A value as a PowerShell single-quoted string. Control characters are refused outright."""
    text = str(value)
    if any(ord(ch) < 32 for ch in text):
        raise SandboxError("A setting has a line break or control character, so it can't be used.")
    return "'" + "".join(PS_QUOTES.get(ch, ch) for ch in text) + "'"


def child_disk_path(base: str | Path, name: str) -> Path:
    """The child disk for a sandbox: next to the base disk, named after the sandbox. Never the base disk itself."""
    base = Path(base)
    check_name(name)
    child = base.with_name(f"{name}.vhdx")
    if child == base or child.suffix.lower() != ".vhdx" or not child.name.startswith(PREFIX):
        raise SandboxError("The sandbox disk would have replaced the base disk, so it wasn't made.")
    return child


def new_vhd_script(child: Path, base: str | Path, disk_gb: int) -> str:
    """Make the child as a differencing disk of the base. The base is only read (its size is checked)."""
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$parent = Get-VHD -Path {ps_quote(str(base))}",
        f"if ($parent.Size -gt {int(disk_gb)}GB) {{ throw 'The base disk is bigger than the sandbox disk limit.' }}",
        f"New-VHD -Path {ps_quote(str(child))} -ParentPath {ps_quote(str(base))} -Differencing | Out-Null",
    ])


def new_vm_script(name: str, child: Path, switch: str, memory_mb: int, cpus: int, vm_dir: Path) -> str:
    """A generation 2 VM with fixed memory and CPUs, one network adapter on the private switch, and the child disk."""
    vm = ps_quote(check_name(name))
    # TODO: Set-VMFirmware -EnableSecureBoot depends on the base image's OS; set once the image is known.
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"New-VM -Name {vm} -Generation 2 -MemoryStartupBytes {int(memory_mb)}MB -VHDPath {ps_quote(str(child))}"
        f" -SwitchName {ps_quote(switch)} -Path {ps_quote(str(vm_dir))} | Out-Null",
        f"Set-VMMemory -VMName {vm} -DynamicMemoryEnabled $false",
        f"Set-VMProcessor -VMName {vm} -Count {int(cpus)}",
        f"Set-VM -Name {vm} -CheckpointType Disabled",
        f"Set-VMNetworkAdapter -VMName {vm} -MacAddressSpoofing Off -AllowTeaming Off",
    ])


def adapters_script(name: str) -> str:
    return (f"Get-VMNetworkAdapter -VMName {ps_quote(check_name(name))} | "
            "Select-Object -Property Name, SwitchName | ConvertTo-Json -Compress")


def check_adapters(output: str, switch: str) -> None:
    """Refuse a VM with no adapter, or with any adapter not on the private switch."""
    text = (output or "").strip()
    rows = json.loads(text) if text else []
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        raise SandboxError("The sandbox has no network adapter, so it wasn't started.")
    wrong = [str(row.get("SwitchName") or "no switch") for row in rows if row.get("SwitchName") != switch]
    if wrong:
        raise SandboxError(f"The sandbox has an adapter on {wrong[0]}, not on {switch}, so it wasn't started.")


def start_script(name: str) -> str:
    return f"$ErrorActionPreference = 'Stop'\nStart-VM -Name {ps_quote(check_name(name))}"


def list_vms_script() -> str:
    return "Get-VM | Select-Object -ExpandProperty Name | ConvertTo-Json -Compress"


def destroy_script(name: str, child: Path) -> str:
    """Turn the VM off, remove it, and delete its child disk. Safe to run twice. The base disk is never named."""
    vm = ps_quote(check_name(name))
    disk = ps_quote(str(child))
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$vm = Get-VM -Name {vm} -ErrorAction SilentlyContinue",
        "if ($vm) {",
        "  if ($vm.State -ne 'Off') { Stop-VM -Name " + vm + " -TurnOff -Force }",
        f"  Remove-VM -Name {vm} -Force",
        "}",
        f"if (Test-Path -LiteralPath {disk}) {{ Remove-Item -LiteralPath {disk} -Force }}",
    ])


def parse_vm_names(output: str) -> list[str]:
    text = (output or "").strip()
    if not text:
        return []
    value = json.loads(text)
    names = [value] if isinstance(value, str) else list(value or [])
    return sorted(str(n) for n in names if str(n).startswith(PREFIX))


# --------------------------------------------------------------------------- Hyper-V: guest commands
def guest_path(rel: str) -> str:
    return posixpath.join(GUEST_HOME, safe_rel(rel))


def guest_run_command(argv: list[str], cwd: str, env: dict[str, str], timeout: int) -> str:
    """The command the guest's shell runs: from the folder, with a clean environment and a time limit."""
    full_env = {"PATH": GUEST_PATH, **env}
    assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(full_env.items()))
    return (f"cd {shlex.quote(guest_path(cwd))} && env -i {assignments} "
            f"timeout -k 5 {int(timeout)} {shlex.join(argv)}")


# The guest runs these with python3 (TODO: the base image must have it). They only use the standard library.
GUEST_PACK = (
    "import io,os,sys,zipfile\n"
    "root=sys.argv[1]; skip=set(sys.argv[2:])\n"
    "buf=io.BytesIO()\n"
    "with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:\n"
    "  for d,dn,fn in os.walk(root):\n"
    "    dn[:]=[x for x in dn if x not in skip]\n"
    "    for f in fn:\n"
    "      p=os.path.join(d,f)\n"
    "      if os.path.islink(p): continue\n"
    "      i=zipfile.ZipInfo(os.path.relpath(p,root).replace(os.sep,'/'))\n"
    "      i.external_attr=(os.stat(p).st_mode&0o777)<<16\n"
    "      z.writestr(i,open(p,'rb').read())\n"
    "sys.stdout.buffer.write(buf.getvalue())\n"
)
GUEST_UNPACK = (
    "import io,os,shutil,sys,zipfile\n"
    "dest=sys.argv[1]\n"
    "shutil.rmtree(dest,ignore_errors=True); os.makedirs(dest)\n"
    "z=zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read()))\n"
    "for i in z.infolist():\n"
    "  if i.is_dir(): continue\n"
    "  parts=i.filename.split('/')\n"
    "  if any(p in ('','.','..') for p in parts): raise SystemExit('bad path')\n"
    "  t=os.path.join(dest,*parts); os.makedirs(os.path.dirname(t),exist_ok=True)\n"
    "  open(t,'wb').write(z.read(i)); os.chmod(t,(((i.external_attr>>16)&0o777) or 0o644)|0o600)\n"
)


def ssh_argv(address: str, remote: str, *, key: str, known_hosts: str) -> list[str]:
    # TODO: pin the guest's host key from the base image instead of accepting the first one seen.
    return ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known_hosts}",
            f"{GUEST_USER}@{address}", remote]


def default_run_powershell(script: str) -> str:
    """Run a PowerShell script on the host and return its output. Needs an elevated PowerShell (M3). The script is
    passed encoded, so its line breaks and quotes survive the trip to powershell.exe."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-EncodedCommand", encoded], capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise SandboxError((proc.stderr or proc.stdout or "PowerShell failed").strip()[-400:])
    return proc.stdout


def make_default_run_ssh(key: str, known_hosts: str) -> Callable[..., tuple[int, bytes, bytes]]:
    def run_ssh(address: str, remote: str, *, stdin: bytes | None, timeout: int) -> tuple[int, bytes, bytes]:
        proc = subprocess.run(ssh_argv(address, remote, key=key, known_hosts=known_hosts), input=stdin,
                              capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    return run_ssh


# --------------------------------------------------------------------------- HyperVSandbox
class HyperVSandbox:
    """One Hyper-V VM per session. The host runs PowerShell; the VM is reached over SSH on the private network.

    The two runners are injected: run_powershell(script) -> output text, and
    run_ssh(address, remote_command, stdin=..., timeout=...) -> (exit code, stdout, stderr)."""

    name = "hyperv"

    def __init__(self, *, base_image: str, switch: str, memory_mb: int, cpus: int, disk_gb: int,
                 vm_dir: str = "", ssh_key: str = "", known_hosts: str = "",
                 run_powershell: Callable[[str], str] | None = None,
                 run_ssh: Callable[..., tuple[int, bytes, bytes]] | None = None,
                 ready_seconds: int = 180, sleep: Callable[[float], None] = time.sleep):
        if not base_image:
            raise SandboxRefused("Set the sandbox base image in Settings first.")
        self.base_image = base_image
        self.switch = switch
        self.memory_mb, self.cpus, self.disk_gb = memory_mb, cpus, disk_gb
        self.vm_dir = Path(vm_dir) if vm_dir else Path(base_image).parent / "szsandbox-vm-config"
        self.run_powershell = run_powershell or default_run_powershell
        self.run_ssh = run_ssh or make_default_run_ssh(ssh_key, known_hosts)
        self.ready_seconds = ready_seconds
        self.sleep = sleep
        self._lock = threading.RLock()
        self._killed: set[str] = set()

    # ---- plumbing
    def _ps(self, script: str) -> str:
        try:
            return self.run_powershell(script)
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Hyper-V command failed: {exc}") from exc

    def _ssh(self, name: str, remote: str, *, stdin: bytes | None = None, timeout: int = 60) -> tuple[int, bytes, bytes]:
        try:
            return self.run_ssh(self._address(name), remote, stdin=stdin, timeout=timeout)
        except Exception as exc:
            raise SandboxError(f"Couldn't reach the sandbox: {exc}") from exc

    def _address(self, name: str) -> str:
        slot = slot_of(check_name(name))
        if slot is None:
            raise SandboxError("That sandbox has no address.")
        return guest_address(slot)

    def _child(self, name: str) -> Path:
        return child_disk_path(self.base_image, check_name(name))

    def _wait_ready(self, name: str) -> None:
        deadline = time.monotonic() + self.ready_seconds
        while True:
            code, _, _ = self._ssh(name, "true", timeout=15)
            if code == 0:
                return
            if time.monotonic() >= deadline:
                raise SandboxError("The sandbox didn't start in time.")
            self.sleep(3)

    def _destroy_name(self, name: str) -> None:
        self._ps(destroy_script(name, self._child(name)))

    # ---- lifecycle
    def create(self, session_id: int | str) -> SandboxHandle:
        with self._lock:
            name = make_name(session_id, next_slot(self.list_existing()))
            try:
                child = self._child(name)
                self._ps(new_vhd_script(child, self.base_image, self.disk_gb))
                self._ps(new_vm_script(name, child, self.switch, self.memory_mb, self.cpus, self.vm_dir))
                check_adapters(self._ps(adapters_script(name)), self.switch)
                self._ps(start_script(name))
                self._wait_ready(name)
            except Exception as exc:
                try:
                    self._destroy_name(name)  # don't leave a half-made VM behind
                except Exception:
                    log.warning("Couldn't clean up sandbox %s after a failed start", name, exc_info=True)
                if isinstance(exc, SandboxError):
                    raise
                raise SandboxError(f"The sandbox couldn't be started: {exc}") from exc
            return SandboxHandle(name, self.name, utcnow())

    def destroy(self, handle: SandboxHandle) -> None:
        with self._lock:
            self._destroy_name(handle.name)

    def list_existing(self) -> list[str]:
        return parse_vm_names(self._ps(list_vms_script()))

    # ---- running and files
    def run(self, handle, argv, *, cwd, env, timeout, on_output=None) -> ExitResult:
        self._killed.discard(handle.name)
        remote = guest_run_command(argv, cwd, env, timeout)
        code, out, err = self._ssh(handle.name, remote, timeout=timeout + 60)
        text = (out + err).decode("utf-8", errors="replace")
        if on_output is not None and text:
            on_output(text)
        tail = OutputTail(MAX_OUTPUT_CHARS * 4)
        tail.add((out + err)[-MAX_OUTPUT_CHARS * 4:])
        return ExitResult(code, tail.text(), timed_out=code == 124, stopped=handle.name in self._killed)

    def kill(self, handle) -> None:
        self._killed.add(handle.name)
        self._ssh(handle.name, f"pkill -KILL -u {GUEST_USER} || true", timeout=30)

    def put_file(self, handle, rel, data) -> None:
        path = guest_path(rel)
        code, _, err = self._ssh(handle.name, f"mkdir -p {shlex.quote(posixpath.dirname(path))} && "
                                              f"cat > {shlex.quote(path)}", stdin=data)
        if code != 0:
            raise SandboxError(f"Couldn't write {rel} in the sandbox: {err.decode(errors='replace')[-200:]}")

    def get_file(self, handle, rel) -> bytes:
        code, out, _ = self._ssh(handle.name, f"cat {shlex.quote(guest_path(rel))}")
        if code != 0:
            raise SandboxError(f"Couldn't read {rel} in the sandbox.")
        return out

    def list_dir(self, handle, rel) -> list[str]:
        code, out, _ = self._ssh(handle.name, f"ls -1Ap {shlex.quote(guest_path(rel))}")
        if code != 0:
            raise SandboxError(f"Couldn't list {rel} in the sandbox.")
        return [line for line in out.decode(errors="replace").splitlines() if line]

    def put_archive(self, handle, data, *, dest) -> None:
        code, _, err = self._ssh(handle.name, f"python3 -c {shlex.quote(GUEST_UNPACK)} {shlex.quote(guest_path(dest))}",
                                 stdin=data, timeout=600)
        if code != 0:
            raise SandboxError(f"Couldn't copy the repository into the sandbox: {err.decode(errors='replace')[-200:]}")

    def get_archive(self, handle, *, src, exclude) -> bytes:
        skip = " ".join(shlex.quote(name) for name in sorted(exclude))
        code, out, _ = self._ssh(handle.name, f"python3 -c {shlex.quote(GUEST_PACK)} {shlex.quote(guest_path(src))} "
                                              f"{skip}", timeout=600)
        if code != 0:
            raise SandboxError("Couldn't copy the sandbox's files out.")
        return out


# --------------------------------------------------------------------------- factory
def make_backend(settings: Settings, kind: str) -> SandboxBackend:
    """The backend for a code_sandbox value. Built from the settings each time it's needed."""
    if kind == "fake":
        return FakeSandbox(settings.data_dir / "sandbox-fake")
    if kind == "hyperv":
        keys = settings.data_dir / "sandbox"  # TODO: create the SSH key pair and bake its public half into the base image
        return HyperVSandbox(base_image=settings.sandbox_base_image, switch=settings.sandbox_switch,
                             memory_mb=settings.sandbox_memory_mb, cpus=settings.sandbox_cpus,
                             disk_gb=settings.sandbox_disk_gb, ssh_key=str(keys / "id_ed25519"),
                             known_hosts=str(keys / "known_hosts"))
    raise SandboxError(f"There's no sandbox kind called {kind}.")


# --------------------------------------------------------------------------- records and manager
class SandboxStore:
    """The sandbox_sessions table. Every write goes through here."""

    def __init__(self, db: Database):
        self.db = db

    def insert(self, code_session_id: int, backend: str, *, state: str, created_at: str, expires_at: str) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute("INSERT INTO sandbox_sessions (code_session_id, backend, name, state, created_at, "
                               "expires_at) VALUES (?, ?, '', ?, ?, ?)",
                               (code_session_id, backend, state, created_at, expires_at))
            return int(cur.lastrowid)

    def update(self, row_id: int, **fields: Any) -> None:
        sql = ", ".join(f"{key} = ?" for key in fields)
        self.db.execute(f"UPDATE sandbox_sessions SET {sql} WHERE id = ?", (*fields.values(), row_id))

    def get(self, row_id: int):
        return self.db.one("SELECT * FROM sandbox_sessions WHERE id = ?", (row_id,))

    def live_for_session(self, code_session_id: int):
        """The session's sandbox that is running and can take commands, if it has one."""
        return self.db.one("SELECT * FROM sandbox_sessions WHERE code_session_id = ? AND state = 'running' "
                           "ORDER BY id DESC LIMIT 1", (code_session_id,))

    def alive_count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM sandbox_sessions WHERE state != 'destroyed'")["n"]

    def known_names(self) -> set[str]:
        rows = self.db.all("SELECT name FROM sandbox_sessions WHERE state != 'destroyed' AND name != ''")
        return {row["name"] for row in rows}

    def kinds_in_use(self) -> set[str]:
        return {row["backend"] for row in self.db.all(
            "SELECT DISTINCT backend FROM sandbox_sessions WHERE state != 'destroyed'")}

    def due(self, now: str, stale_before: str):
        """Sandboxes to reap: past their time limit, failed to delete, stuck while being made, or owned by a
        Code session that has finished."""
        return self.db.all(
            "SELECT sb.* FROM sandbox_sessions sb LEFT JOIN code_sessions cs ON cs.id = sb.code_session_id "
            "WHERE sb.state != 'destroyed' AND (sb.expires_at <= ? OR sb.state = 'failed' "
            "OR (sb.state = 'creating' AND sb.created_at <= ?) OR cs.status IN ('done', 'stopped', 'failed')) "
            "ORDER BY sb.id", (now, stale_before))


class SandboxManager:
    """Makes, uses and deletes sandboxes, and keeps their records in step. One lock covers every change, so
    the reaper never looks for orphans while a sandbox is being made."""

    def __init__(self, settings: Settings, db: Database,
                 backend_factory: Callable[[str], SandboxBackend] | None = None):
        self.settings = settings
        self.db = db
        self.store = SandboxStore(db)
        self.backend_factory = backend_factory or (lambda kind: make_backend(settings, kind))
        self.lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        """True unless code_sandbox is off. An unknown value counts as on, so a typo in config.env fails closed:
        the command is refused, instead of running on this computer."""
        return self.settings.code_sandbox.strip().lower() not in OFF_VALUES

    def backend(self, kind: str) -> SandboxBackend:
        return self.backend_factory(kind)

    def kinds_in_use(self) -> set[str]:
        kinds = self.store.kinds_in_use()
        if self.enabled:
            kinds.add(self.settings.code_sandbox)
        return kinds

    def handle_of(self, row) -> SandboxHandle:
        return SandboxHandle(row["name"], row["backend"], row["created_at"])

    def live_for_session(self, code_session_id: int):
        return self.store.live_for_session(code_session_id)

    def free_gb(self) -> float:
        anchor = Path(self.settings.sandbox_base_image) if self.settings.sandbox_base_image else self.settings.data_dir
        while not anchor.exists() and anchor.parent != anchor:
            anchor = anchor.parent
        return shutil.disk_usage(anchor).free / (1024 ** 3)

    def check_capacity(self) -> None:
        """Refuse a new sandbox when the limit is reached, the disk is low, or the base image isn't set."""
        if self.settings.code_sandbox == "hyperv" and not self.settings.sandbox_base_image:
            raise SandboxRefused("Set the sandbox base image in Settings before Code can use sandboxes.")
        limit = self.settings.sandbox_max_sessions
        if self.store.alive_count() >= limit:
            raise SandboxRefused(f"All {limit} sandboxes are in use right now. Ask a mentor to finish or stop a Code "
                                 "session, then try again.")
        free = self.free_gb()
        if free < self.settings.sandbox_min_free_gb:
            raise SandboxRefused(f"The computer has {free:.0f} GB of free disk space, and it needs "
                                 f"{self.settings.sandbox_min_free_gb} GB before it can start a sandbox. Ask an admin "
                                 "to free some space.")

    def create_for_session(self, code_session_id: int, *, actor_id: int | None) -> SandboxHandle:
        """Make a sandbox for a Code session. Raises SandboxRefused or SandboxError."""
        with self.lock:
            self.check_capacity()
            kind = self.settings.code_sandbox
            now = datetime.now(timezone.utc)
            expires = now + timedelta(hours=self.settings.sandbox_ttl_hours)
            row_id = self.store.insert(code_session_id, kind, state="creating", created_at=now.isoformat(timespec="seconds"),
                                       expires_at=expires.isoformat(timespec="seconds"))
            try:
                handle = self.backend(kind).create(code_session_id)
            except SandboxError as exc:
                self.store.update(row_id, state="failed", note=str(exc)[:300])
                raise
            except Exception as exc:
                self.store.update(row_id, state="failed", note=str(exc)[:300])
                raise SandboxError(f"The sandbox couldn't be made: {exc}") from exc
            self.store.update(row_id, name=handle.name, state="running")
            self.db.audit("sandbox_created", actor_id=actor_id, target_type="sandbox", target_id=row_id,
                          details={"name": handle.name, "backend": kind, "code_session": code_session_id,
                                   "expires_at": expires.isoformat(timespec="seconds")})
            return SandboxHandle(handle.name, kind, handle.created_at)

    def destroy_record(self, row, *, reason: str, actor_id: int | None = None,
                       before: Callable[[SandboxHandle], None] | None = None) -> None:
        """Delete one sandbox and mark its record. `before` saves what the sandbox holds first; if it fails, the
        delete still goes ahead. A failed delete is kept as "failed" so the reaper tries again."""
        with self.lock:
            if row["state"] == "destroyed":
                return
            handle = self.handle_of(row)
            if row["name"] and before is not None:
                try:
                    before(handle)
                except Exception:
                    log.warning("Couldn't save a sandbox's files before deleting it", exc_info=True)
            try:
                if row["name"]:
                    self.backend(row["backend"]).destroy(handle)
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                self.store.update(row["id"], state="failed", note=message[:300])
                raise SandboxError(f"The sandbox couldn't be deleted: {message}") from exc
            self.store.update(row["id"], state="destroyed", destroyed_at=utcnow(), note=reason[:300])
            self.db.audit("sandbox_destroyed", actor_id=actor_id, target_type="sandbox", target_id=row["id"],
                          details={"name": row["name"], "backend": row["backend"], "reason": reason})

    def destroy_for_session(self, code_session_id: int, *, reason: str,
                            before: Callable[[SandboxHandle], None] | None = None) -> bool:
        """Delete the session's running sandbox, if it has one. Returns True if one was deleted."""
        with self.lock:
            row = self.live_for_session(code_session_id)
            if row is None:
                return False
            self.destroy_record(row, reason=reason, before=before)
            return True


def reap_sandboxes(services: Any, now: datetime | None = None) -> dict[str, int]:
    """Delete sandboxes past their time limit, of finished sessions, or that no record owns. Never raises: every
    problem is logged and counted in the result, and the next run tries again."""
    report = {"expired": 0, "orphans": 0, "failed": 0}
    manager: SandboxManager | None = getattr(services, "sandboxes", None)
    if manager is None:
        return report
    try:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        stamp = moment.isoformat(timespec="seconds")
        with manager.lock:
            for row in manager.store.due(stamp, (moment - STALE_CREATING).isoformat(timespec="seconds")):
                _reap_row(manager, row, stamp, report)
            _reap_orphans(manager, report)
    except Exception:
        log.exception("Sandbox cleanup failed")
    return report


def _reap_row(manager: SandboxManager, row, stamp: str, report: dict[str, int]) -> None:
    if row["state"] == "failed":
        reason = "retry after a failed delete"
    elif row["state"] == "creating":
        reason = "left unfinished"
    elif row["expires_at"] <= stamp:
        reason = "time limit reached"
    else:
        reason = "session finished"
    try:
        if reason == "time limit reached" and row["state"] == "running":
            manager.store.update(row["id"], state="expired")
        manager.destroy_record(row, reason=reason)
    except SandboxError as exc:
        report["failed"] += 1
        log.warning("Sandbox %s wasn't deleted: %s", row["name"] or row["id"], exc)
        return
    except Exception:
        report["failed"] += 1
        log.exception("Sandbox %s wasn't deleted", row["name"] or row["id"])
        return
    report["expired"] += 1
    manager.db.audit("sandbox_reaped", actor_id=None, target_type="sandbox", target_id=row["id"],
                     details={"name": row["name"], "reason": reason})


def _reap_orphans(manager: SandboxManager, report: dict[str, int]) -> None:
    known = manager.store.known_names()
    for kind in sorted(manager.kinds_in_use()):
        try:
            backend = manager.backend(kind)
            names = backend.list_existing()
        except Exception:
            report["failed"] += 1
            log.warning("Couldn't list the %s sandboxes", kind, exc_info=True)
            continue
        for name in names:
            if name in known or not name.startswith(PREFIX):
                continue
            try:
                backend.destroy(SandboxHandle(name, kind, ""))
            except Exception:
                report["failed"] += 1
                log.warning("Orphan sandbox %s wasn't deleted", name, exc_info=True)
                continue
            report["orphans"] += 1
            manager.db.audit("sandbox_reaped", actor_id=None, target_type="sandbox",
                             details={"name": name, "reason": "no record", "backend": kind})
