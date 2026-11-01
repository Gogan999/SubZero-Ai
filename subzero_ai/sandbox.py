"""Code sandboxes: a disposable virtual machine for the commands of each Code session.

A Code session's commands run inside its own sandbox instead of on the mini PC. The mentor's copy of the repository
stays on the host (reads, approvals and the patch download all use it), and every approved edit is written to both
copies. A sandbox is a Hyper-V virtual machine made from one shared base disk as a differencing disk, so the base is
only ever read. It is attached only to the internal switch named in Settings (default szsandbox-internal), whose host
vNIC is 10.77.0.1/24. The host must firewall that adapter; see docs/CODE_SANDBOX_PLAN.md. Each VM also gets its own
network ACLs, DHCP and router guards and a static MAC (see new_vm_script and isolation_script). A sandbox is deleted
when its session finishes, after its time limit, and when no record of it is left in the database.

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
from collections.abc import Callable, Iterable
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
HOST_ADDRESS = "10.77.0.1"  # the host's vNIC on the internal switch
GUEST_SUBNET = "10.77.0.0/24"
IPV6_ANY = "::/0"  # every IPv6 address: the sandboxes get no IPv6 path at all
MAX_SESSION_BYTES = 800 * 1024 * 1024  # one session's two copies together, build output included; also the archive cap
SSH = "ssh"  # the ssh program on the host (a test can point it at a fake)
SSH_KEYSCAN = "ssh-keyscan"  # reads a guest's host key over the internal switch, once, right after it boots
HOST_KEY_LINE = re.compile(r"^\S+ ssh-ed25519 [A-Za-z0-9+/=]+$")
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
    def check_network(self) -> None: ...  # raises SandboxRefused unless the sandbox network is ready


# --------------------------------------------------------------------------- names and paths
def make_name(session_id: int | str, slot: int) -> str:
    """A sandbox's name: our prefix, the Code session, its slot and a random tail."""
    return f"{PREFIX}{session_id}-s{slot:03d}-{os.urandom(3).hex()}"


def slot_of(name: str) -> int | None:
    match = re.search(r"-s(\d{3})-", name)
    return int(match.group(1)) if match else None


def next_slot(names: list[str], reserved: Iterable[int] = ()) -> int:
    """The lowest slot no existing sandbox uses and no sandbox that is being made has reserved."""
    used = {slot for slot in (slot_of(name) for name in names) if slot is not None} | set(reserved)
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
    """The static address on the internal switch for a slot (10.77.0.10 and up)."""
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
def archive_too_big() -> SandboxError:
    return SandboxError(f"The sandbox's files are bigger than {MAX_SESSION_BYTES // (1024 * 1024)} MB, so they weren't "
                        "copied out. Ask a mentor to check them by hand.")


def pack_tree(root: Path, exclude: frozenset[str] | set[str] = frozenset(), limit: int | None = None) -> bytes:
    """A zip of a folder's files, with their permissions. Folders named in `exclude` are skipped at any depth,
    and symbolic links are never followed (a link could point at the mini PC's own files). With `limit`, the
    files' total size is checked before anything is read, and too much raises SandboxError."""
    root = Path(root)
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in exclude)
                for name in sorted(filenames):
                    path = Path(dirpath) / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    if limit is not None:
                        total += path.stat().st_size
                        if total > limit:
                            raise archive_too_big()
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


def read_capped(stream, limit: int, sink: bytearray) -> None:
    """Read a stream into `sink` until it ends or holds more than `limit` bytes. Reading stops there, so the caller
    can refuse the data without ever holding all of it."""
    try:
        while len(sink) <= limit:
            chunk = stream.read1(65536)
            if not chunk:
                return
            sink += chunk
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
        self._reserved: set[int] = set()  # slots of sandboxes being made right now
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
        with self._lock:
            slot = next_slot(self.list_existing(), self._reserved)
            self._reserved.add(slot)
        try:
            name = make_name(session_id, slot)
            self._home(SandboxHandle(name, self.name, "")).mkdir(parents=True)
        finally:
            with self._lock:
                self._reserved.discard(slot)
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
        return pack_tree(self._path(handle, src), exclude, limit=MAX_SESSION_BYTES)

    def destroy(self, handle) -> None:
        self.kill(handle)
        home = self._home(handle)
        if home.exists():
            try:
                shutil.rmtree(home)
            except OSError as exc:
                raise SandboxError(f"Couldn't delete the sandbox folder: {exc}") from exc

    def check_network(self) -> None:
        """Nothing to check: the fake sandbox runs on this computer and has no network of its own."""

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
    """A generation 2 VM with fixed memory and CPUs, one network adapter on the internal switch, and the child disk."""
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


def mac_for_slot(slot: int) -> str:
    """A static MAC for a slot: Microsoft's 00:15:5D prefix, then the guest's last address octet, so the MAC and the
    address always go together. Twelve hex digits, no separators, as Set-VMNetworkAdapter expects."""
    return f"00155D77{GUEST_FIRST_SLOT_HOST + slot:04X}"


def isolation_script(name: str, slot: int) -> str:
    """Lock one VM's adapter down before it starts: a static MAC, DHCP and router messages from the guest dropped, and
    traffic limited to the host's address. The host is allowed (IPv4 only); the rest of the guest subnet, where other
    sandboxes live, is denied in both directions. All IPv6 is denied too (weight 150, below the host allow), so a
    guest can't reach another sandbox over fe80:: link-local addresses. The base image should also turn IPv6 off
    (docs/CODE_SANDBOX_PLAN.md).

    UNVERIFIED: none of these cmdlets, the -Weight ordering (higher weight is meant to win) or the CIDR form of
    -RemoteIPAddress (including ::/0 for IPv6) has been run yet. Test them on the mini PC (docs/CODE_SANDBOX_PLAN.md,
    M3) and read the rules back with Get-VMNetworkAdapterExtendedAcl before trusting this. Until then, the host
    firewall is the real boundary."""
    vm = ps_quote(check_name(name))
    host = ps_quote(f"{HOST_ADDRESS}/32")
    subnet = ps_quote(GUEST_SUBNET)
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"Set-VMNetworkAdapter -VMName {vm} -StaticMacAddress {ps_quote(mac_for_slot(slot))} "
        "-DhcpGuard On -RouterGuard On",
        f"Add-VMNetworkAdapterExtendedAcl -VMName {vm} -Action Allow -Direction Both -RemoteIPAddress {host} "
        "-Weight 200",
        f"Add-VMNetworkAdapterExtendedAcl -VMName {vm} -Action Deny -Direction Both -RemoteIPAddress {subnet} "
        "-Weight 100",
        f"Add-VMNetworkAdapterExtendedAcl -VMName {vm} -Action Deny -Direction Both "
        f"-RemoteIPAddress {ps_quote(IPV6_ANY)} -Weight 150",
    ])


def adapters_script(name: str) -> str:
    return (f"Get-VMNetworkAdapter -VMName {ps_quote(check_name(name))} | "
            "Select-Object -Property Name, SwitchName | ConvertTo-Json -Compress")


def check_adapters(output: str, switch: str) -> None:
    """Refuse a VM with no adapter, or with any adapter not on the internal switch."""
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


# --------------------------------------------------------------------------- Hyper-V: host network check
NETWORK_OK_SECONDS = 60  # a passing host check is trusted this long, so each create doesn't run PowerShell again
NETWORK_CHECK_MARK = "# szsandbox: host network check (read-only, changes nothing)"  # first line of the check script
NETWORK_CHECK_SECONDS = 60  # the Settings page's check gives up after this long
HOST_VNIC_NAME = "vEthernet ({})"  # the host's adapter for an Internal switch is named after it
RISKY_PORTS = (135, 139, 445, 3389, 5985)  # SMB, RPC, RDP and WinRM: never open to the sandboxes
BLOCK_TCP_BELOW = "szsandbox-block-tcp-below"  # blocks TCP ports 1 to proxy port minus 1
BLOCK_TCP_ABOVE = "szsandbox-block-tcp-above"  # blocks TCP ports proxy port plus 1 to 65535
BLOCK_UDP = "szsandbox-block-udp"  # blocks every UDP port
BLOCK_ICMP4 = "szsandbox-block-icmp4"  # blocks ICMPv4
BLOCK_ICMP6 = "szsandbox-block-icmp6"  # blocks ICMPv6
PROXY_RULE = "szsandbox-allow-proxy"  # the one allow: TCP to the proxy port from the guest subnet
# Problem codes that carry a name after the colon (a rule's name, or an error message)
NETWORK_CODE_PREFIXES = ("block-rule-missing:", "open-allow:", "check-failed:")
NETWORK_PROBLEMS = {
    "switch-missing": "The sandbox switch doesn't exist yet",
    "switch-not-internal": "The sandbox switch isn't an Internal switch",
    "host-adapter-missing": "The host has no network adapter for the sandbox switch",
    "host-address": "The host's adapter for the sandboxes doesn't have the address 10.77.0.1/24",
    "host-gateway": "The host's adapter for the sandboxes has a default gateway, and it must have none",
    "forwarding-on": "IP forwarding is on for the host's adapter for the sandboxes, and it must be off",
    "profile-not-public": "The network profile of the sandbox adapter isn't Public",
    "ipv6-enabled": "IPv6 is still on for the host's adapter for the sandboxes, and it must be off (uncheck TCP/IPv6 "
                    "on that adapter)",
    "proxy-rule-wrong": "The firewall rule szsandbox-allow-proxy must exist and allow only TCP to the proxy port from "
                        "10.77.0.0/24 on the sandbox adapter",
}
UNREADABLE_CHECK = "The check gave an answer the app couldn't read"


def block_rule_specs(proxy_port: int) -> list[tuple[str, str, str | None]]:
    """The firewall block rules for the sandbox adapter, as (name, protocol, local ports or None for every port). With
    PROXY_RULE they cover all inbound traffic: TCP is blocked below and above the proxy port, UDP and both ICMP
    versions are blocked whole, and the one allow rule opens only the proxy port. Windows Firewall lets a block rule
    beat an allow rule, which is why the blocks skip the proxy port. A range that would be empty is left out."""
    port = int(proxy_port)
    specs: list[tuple[str, str, str | None]] = []
    if port > 1:
        specs.append((BLOCK_TCP_BELOW, "TCP", f"1-{port - 1}"))
    if port < 65535:
        specs.append((BLOCK_TCP_ABOVE, "TCP", f"{port + 1}-65535"))
    specs += [(BLOCK_UDP, "UDP", None), (BLOCK_ICMP4, "ICMPv4", None), (BLOCK_ICMP6, "ICMPv6", None)]
    return specs


def describe_network_problem(code: str) -> str:
    """The plain-language text for one problem code from host_network_check_script (no final full stop)."""
    if code.startswith("open-allow:"):
        name = code.split(":", 1)[1][:120]
        return (f"The firewall rule '{name}' lets other computers reach this PC over the sandbox network. Turn it off, "
                "or limit it so it doesn't apply to the sandbox adapter")
    if code.startswith("block-rule-missing:"):
        name = code.split(":", 1)[1][:120]
        return (f"The firewall rule {name} is missing, turned off or set up wrong. The sandbox adapter needs its block "
                "rules for every port except the proxy's")
    if code.startswith("check-failed:"):
        return f"The check couldn't finish ({code.split(':', 1)[1].strip()[:200] or 'no reason given'})"
    return NETWORK_PROBLEMS.get(code, f"The check found a problem it doesn't know how to describe ({code[:60]})")


# PowerShell helpers shared by the check script and sandbox-network.ps1. The two copies must stay identical; a test
# compares them. A remote address written as a subnet mask (10.77.0.0/255.255.255.0) is turned into the prefix form
# (10.77.0.0/24) so one comparison accepts either.
NETWORK_CHECK_HELPERS = r'''function Get-RuleValues($Rule, [string]$Cmdlet, [string]$Field) {
    return @($Rule | & $Cmdlet -ErrorAction SilentlyContinue | ForEach-Object { $_.$Field })
}
function ConvertTo-Cidr([string]$Text) {
    if ($Text -match '^(\d+\.\d+\.\d+\.\d+)/(\d+\.\d+\.\d+\.\d+)$') {
        $address = $Matches[1]
        $bits = 0
        foreach ($octet in [System.Net.IPAddress]::Parse($Matches[2]).GetAddressBytes()) {
            $bits += ([Convert]::ToString([int]$octet, 2) -replace '0', '').Length
        }
        return "$address/$bits"
    }
    return $Text
}
function Get-RemoteAddresses($Rule) {
    return @(Get-RuleValues $Rule "Get-NetFirewallAddressFilter" "RemoteAddress" |
        ForEach-Object { ConvertTo-Cidr ([string]$_) })
}'''


def host_network_check_script(switch: str, proxy_port: int) -> str:
    """A read-only PowerShell check of the host's side of the sandbox network. It prints a JSON list of problem codes
    (an empty list when everything is in place); parse_host_network_check turns them into plain words. It changes
    nothing. UNVERIFIED on a real host: the cmdlets and their property names are untested (docs/CODE_SANDBOX_PLAN.md,
    M3b). The same checks, with messages, are in packaging/windows/scripts/sandbox-network.ps1; keep them in step."""
    sw = ps_quote(switch)
    alias = ps_quote(HOST_VNIC_NAME.format(switch))
    subnet = ps_quote(GUEST_SUBNET)
    port = int(proxy_port)
    risky = ", ".join(str(p) for p in RISKY_PORTS)
    specs = ", ".join(
        "@{ Name = " + ps_quote(name) + "; Protocol = " + ps_quote(protocol) + "; LocalPort = "
        + (ps_quote(ports) if ports else "$null") + " }"
        for name, protocol, ports in block_rule_specs(port))
    lines = [
        NETWORK_CHECK_MARK,
        "$ErrorActionPreference = 'Stop'",
        NETWORK_CHECK_HELPERS,
        "$problems = New-Object System.Collections.Generic.List[string]",
        "try {",
        f"  $vswitch = Get-VMSwitch -Name {sw} -ErrorAction SilentlyContinue",
        "  if (-not $vswitch) { $problems.Add('switch-missing') }",
        "  elseif ($vswitch.SwitchType -ne 'Internal') { $problems.Add('switch-not-internal') }",
        f"  $alias = {alias}",
        "  $adapter = Get-NetAdapter -Name $alias -ErrorAction SilentlyContinue",
        "  if (-not $adapter) { $problems.Add('host-adapter-missing') } else {",
        "    $ips = @(Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue)",
        "    if (-not ($ips | Where-Object { $_.IPAddress -eq '10.77.0.1' -and $_.PrefixLength -eq 24 })) {",
        "      $problems.Add('host-address')",
        "    }",
        "    $routes = @(Get-NetRoute -InterfaceAlias $alias -DestinationPrefix '0.0.0.0/0'"
        " -ErrorAction SilentlyContinue)",
        "    if ($routes.Count -gt 0) { $problems.Add('host-gateway') }",
        "    $ipif = Get-NetIPInterface -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue",
        "    if ($ipif -and $ipif.Forwarding -ne 'Disabled') { $problems.Add('forwarding-on') }",
        "    $netProfile = Get-NetConnectionProfile -InterfaceAlias $alias -ErrorAction SilentlyContinue",
        "    $isPublic = $netProfile -and ($netProfile.NetworkCategory -eq 'Public')",
        "    if (-not $isPublic) { $problems.Add('profile-not-public') }",
        "    $binding = Get-NetAdapterBinding -Name $alias -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue",
        "    if (-not $binding -or $binding.Enabled) { $problems.Add('ipv6-enabled') }",
        "  }",
        "  $rules = @(Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {",
        "    $_.DisplayName -like 'szsandbox-*' -and $_.Enabled -eq 'True' })",
        f"  $blockSpecs = @({specs})",
        "  foreach ($spec in $blockSpecs) {",
        "    $ok = $false",
        "    foreach ($rule in @($rules | Where-Object { $_.DisplayName -eq $spec.Name -and"
        " $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' })) {",
        "      $protocols = Get-RuleValues $rule 'Get-NetFirewallPortFilter' 'Protocol'",
        "      $localPorts = Get-RuleValues $rule 'Get-NetFirewallPortFilter' 'LocalPort'",
        "      $ifaces = Get-RuleValues $rule 'Get-NetFirewallInterfaceFilter' 'InterfaceAlias'",
        "      $remotes = Get-RemoteAddresses $rule",
        "      $portOk = ($null -eq $spec.LocalPort) -or ($localPorts -contains $spec.LocalPort)",
        "      if (($protocols -contains $spec.Protocol) -and $portOk -and ($ifaces -contains $alias)"
        " -and ($remotes -contains 'Any')) { $ok = $true }",
        "    }",
        "    if (-not $ok) { $problems.Add('block-rule-missing:' + $spec.Name) }",
        "  }",
        "  $proxyRules = @($rules | Where-Object { $_.DisplayName -eq 'szsandbox-allow-proxy' })",
        "  $proxyBad = $proxyRules.Count -eq 0",
        "  foreach ($rule in $proxyRules) {",
        "    $protocols = Get-RuleValues $rule 'Get-NetFirewallPortFilter' 'Protocol'",
        "    $localPorts = Get-RuleValues $rule 'Get-NetFirewallPortFilter' 'LocalPort'",
        "    $ifaces = Get-RuleValues $rule 'Get-NetFirewallInterfaceFilter' 'InterfaceAlias'",
        "    $remotes = Get-RemoteAddresses $rule",
        "    $ok = ($rule.Direction -eq 'Inbound') -and ($rule.Action -eq 'Allow') -and ($protocols -contains 'TCP') `",
        f"      -and ($localPorts -contains '{port}') -and ($localPorts.Count -eq 1) `",
        f"      -and ($ifaces -contains $alias) -and ($remotes -contains {subnet})",
        "    if (-not $ok) { $proxyBad = $true }",
        "  }",
        "  if ($proxyBad) { $problems.Add('proxy-rule-wrong') }",
        "  $allows = @(Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True"
        " -ErrorAction SilentlyContinue)",
        "  foreach ($rule in $allows) {",
        "    if ($rule.DisplayName -like 'szsandbox-*') { continue }",
        "    $profileText = [string]$rule.Profile",
        "    if (($profileText -notmatch 'Public') -and ($profileText -notmatch 'Any')) { continue }",
        "    $protocols = Get-RuleValues $rule 'Get-NetFirewallPortFilter' 'Protocol'",
        "    if (@($protocols | Where-Object { $_ -match '^ICMP' }).Count -gt 0) { continue }",
        "    $ifaces = Get-RuleValues $rule 'Get-NetFirewallInterfaceFilter' 'InterfaceAlias'",
        "    $ifaceOk = ($ifaces.Count -eq 0) -or ($ifaces -contains 'Any') -or ($ifaces -contains $alias)",
        "    if (-not $ifaceOk) { continue }",
        "    $remotes = Get-RemoteAddresses $rule",
        "    $remoteOk = ($remotes.Count -eq 0) -or ($remotes -contains 'Any') -or ($remotes -contains 'LocalSubnet')",
        "    if (-not $remoteOk) { continue }",
        "    $wide = $false",
        "    foreach ($portText in (Get-RuleValues $rule 'Get-NetFirewallPortFilter' 'LocalPort')) {",
        "      if (($portText -eq 'Any') -or ($portText -eq '')) { $wide = $true }",
        "      elseif ($portText -match '^(\\d+)(?:-(\\d+))?$') {",
        "        $lo = [int]$Matches[1]",
        "        $hi = $lo",
        "        if ($Matches[2]) { $hi = [int]$Matches[2] }",
        f"        foreach ($p in @({risky})) {{ if (($p -ge $lo) -and ($p -le $hi)) {{ $wide = $true }} }}",
        "      }",
        "    }",
        "    if ($wide) { $problems.Add('open-allow:' + $rule.DisplayName) }",
        "  }",
        "} catch {",
        "  $problems.Add('check-failed:' + ($_.Exception.Message -replace '[\\r\\n]+', ' '))",
        "}",
        "ConvertTo-Json -Compress -InputObject @($problems.ToArray())",
    ]
    return "\n".join(lines)


def parse_host_network_check(output: str) -> list[str]:
    """Plain-language problems from the check's output. An empty list means the sandbox network is ready. Output that
    isn't a JSON list of codes (or nothing at all) is a problem too: the check fails closed."""
    text = (output or "").strip()
    try:
        codes = json.loads(text) if text else None
    except ValueError:
        codes = None
    if isinstance(codes, str):
        codes = [codes]
    if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
        return [UNREADABLE_CHECK]
    return [describe_network_problem(code) for code in codes]


def host_network_problems(switch: str, proxy_port: int, run_powershell: Callable[[str], str]) -> list[str]:
    """Run the host check and return its problems. Any error while running it is itself a problem (fail closed)."""
    try:
        output = run_powershell(host_network_check_script(switch, proxy_port))
    except Exception as exc:
        return [describe_network_problem(f"check-failed:{str(exc) or type(exc).__name__}")]
    return parse_host_network_check(output)


def network_refusal(problems: list[str]) -> str:
    """The message a sandbox gets when the host network check doesn't pass."""
    reasons = "; ".join(problem.rstrip(".") for problem in problems)
    return (f"The sandbox network isn't locked down yet: {reasons}; an administrator needs to run the sandbox network "
            "setup script (sandbox-network.ps1 in the SubZero AI folder, with -Action Install).")


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
    """The ssh command to a guest. The host key must match the one pinned in this sandbox's own known_hosts file."""
    return [SSH, "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
            f"{GUEST_USER}@{address}", remote]


def keyscan_argv(address: str) -> list[str]:
    return [SSH_KEYSCAN, "-T", "10", "-t", "ed25519", address]


def host_key_line(output: bytes, address: str) -> str:
    """The known_hosts line for a guest, from ssh-keyscan's output. Refuses anything that isn't one ed25519 key."""
    for raw in output.decode("ascii", errors="replace").splitlines():
        line = raw.strip()
        if HOST_KEY_LINE.match(line) and line.split(" ", 1)[0] == address:
            return line
    raise SandboxError("The sandbox's host key couldn't be read yet.")


def _run_pipes(argv: list[str], *, stdin: bytes | None, timeout: int, on_output: Callable[[str], None] | None,
               limit: int | None) -> tuple[int, bytes, bytes]:
    """Run a command with pipes. Its stderr and stdout are read as they arrive: with on_output, each chunk is passed on
    as it comes; without it, only the last MAX_OUTPUT_CHARS * 4 bytes of each stream are kept. With `limit`, stdout is
    kept only up to limit + 1 bytes (the archive case), and the command is killed once it goes past that."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                               else {"start_new_session": True}))
    keep = MAX_OUTPUT_CHARS * 4
    out_tail, err_tail, archive = OutputTail(keep), OutputTail(keep), bytearray()
    emit_lock = threading.Lock()  # the two readers and the callback never run at the same time

    def emit(text: str) -> None:
        if on_output is not None:
            with emit_lock:
                on_output(text)

    def feed() -> None:
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except (OSError, ValueError):
            pass  # the command ended before it read everything it was sent

    def capture_archive() -> None:
        read_capped(proc.stdout, limit, archive)
        if len(archive) > limit:
            kill_process_tree(proc)  # stop the guest sending more; the caller refuses the archive

    threads = [threading.Thread(target=read_output, args=(proc.stderr, err_tail, emit), daemon=True)]
    if limit is None:
        threads.append(threading.Thread(target=read_output, args=(proc.stdout, out_tail, emit), daemon=True))
    else:
        threads.append(threading.Thread(target=capture_archive, daemon=True))
    if stdin is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for thread in threads:
        thread.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        proc.wait()
        raise
    finally:
        for thread in threads:
            thread.join(REAP_SECONDS)
        if any(thread.is_alive() for thread in threads):  # something still holds a pipe open
            kill_process_tree(proc)
            for thread in threads:
                thread.join(REAP_SECONDS)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    if limit is None:
        return proc.returncode, bytes(out_tail.data), bytes(err_tail.data)
    return proc.returncode, bytes(archive[:limit + 1]), bytes(err_tail.data)


def default_run_keyscan(address: str) -> bytes:
    """ssh-keyscan's output for a guest. Empty when nothing answers yet."""
    proc = subprocess.run(keyscan_argv(address), capture_output=True, timeout=30)
    return proc.stdout[:4096] if proc.returncode == 0 else b""


def default_run_powershell(script: str, timeout: int = 900) -> str:
    """Run a PowerShell script on the host and return its output. Needs an elevated PowerShell (M3). The script is
    passed encoded, so its line breaks and quotes survive the trip to powershell.exe."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-EncodedCommand", encoded], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise SandboxError((proc.stderr or proc.stdout or "PowerShell failed").strip()[-400:])
    return proc.stdout


def make_default_run_ssh(key: str) -> Callable[..., tuple[int, bytes, bytes]]:
    """The real runner. See _run_pipes: output is streamed and bounded, and an archive is capped at `limit`."""
    def run_ssh(address: str, remote: str, *, known_hosts: str, stdin: bytes | None, timeout: int,
                on_output: Callable[[str], None] | None = None, limit: int | None = None) -> tuple[int, bytes, bytes]:
        return _run_pipes(ssh_argv(address, remote, key=key, known_hosts=known_hosts), stdin=stdin, timeout=timeout,
                          on_output=on_output, limit=limit)
    return run_ssh


# --------------------------------------------------------------------------- HyperVSandbox
class HyperVSandbox:
    """One Hyper-V VM per session. The host runs PowerShell; the VM is reached over SSH on the internal switch.

    Each sandbox has its own known_hosts file in known_dir (named after the sandbox). Its host key is read once,
    right after the VM boots, with ssh-keyscan over the host-only link, and then checked on every connection. That
    first read is trust on first use, so it is only as good as the link: the host must firewall the internal adapter
    (docs/CODE_SANDBOX_PLAN.md). The base image should also carry a fixed host key, which would remove that step.

    The runners are injected: run_powershell(script) -> output text; run_keyscan(address) -> bytes; and
    run_ssh(address, remote_command, *, known_hosts, stdin, timeout, on_output=None, limit=None) -> (exit code,
    stdout, stderr). With `limit`, stdout is cut off past limit + 1 bytes and the caller refuses it."""

    name = "hyperv"

    def __init__(self, *, base_image: str, switch: str, memory_mb: int, cpus: int, disk_gb: int,
                 vm_dir: str = "", ssh_key: str = "", known_dir: str = "",
                 run_powershell: Callable[[str], str] | None = None,
                 run_keyscan: Callable[[str], bytes] | None = None,
                 run_ssh: Callable[..., tuple[int, bytes, bytes]] | None = None,
                 ready_seconds: int = 180, sleep: Callable[[float], None] = time.sleep,
                 archive_limit: int = MAX_SESSION_BYTES, proxy_port: int = 3128):
        if not base_image:
            raise SandboxRefused("Set the sandbox base image in Settings first.")
        self.base_image = base_image
        self.switch = switch
        self.memory_mb, self.cpus, self.disk_gb = memory_mb, cpus, disk_gb
        self.vm_dir = Path(vm_dir) if vm_dir else Path(base_image).parent / "szsandbox-vm-config"
        self.known_dir = Path(known_dir) if known_dir else Path(base_image).parent / "szsandbox-known-hosts"
        self.run_powershell = run_powershell or default_run_powershell
        self.run_keyscan = run_keyscan or default_run_keyscan
        self.run_ssh = run_ssh or make_default_run_ssh(ssh_key)
        self.ready_seconds = ready_seconds
        self.sleep = sleep
        self.archive_limit = archive_limit
        self.proxy_port = proxy_port
        self._lock = threading.Lock()  # guards _killed, _reserved and _network_ok_until; never held across a VM call
        self._killed: set[str] = set()
        self._reserved: set[int] = set()  # slots of sandboxes being made right now
        self._network_ok_until = 0.0  # monotonic time until which a passing host network check is trusted

    # ---- plumbing
    def _ps(self, script: str) -> str:
        try:
            return self.run_powershell(script)
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Hyper-V command failed: {exc}") from exc

    def _known_hosts(self, name: str) -> str:
        return str(self.known_dir / check_name(name))

    def _ssh(self, name: str, remote: str, *, stdin: bytes | None = None, timeout: int = 60,
             on_output: Callable[[str], None] | None = None, limit: int | None = None) -> tuple[int, bytes, bytes]:
        try:
            return self.run_ssh(self._address(name), remote, known_hosts=self._known_hosts(name), stdin=stdin,
                                timeout=timeout, on_output=on_output, limit=limit)
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Couldn't reach the sandbox: {exc}") from exc

    def _pin_host_key(self, name: str) -> None:
        """Read the guest's host key once and write it to the sandbox's own known_hosts file. Until this succeeds,
        nothing else connects to the guest."""
        address = self._address(name)
        try:
            line = host_key_line(self.run_keyscan(address), address)
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Couldn't reach the sandbox: {exc}") from exc
        self.known_dir.mkdir(parents=True, exist_ok=True)
        Path(self._known_hosts(name)).write_text(line + "\n", encoding="ascii")

    def _address(self, name: str) -> str:
        slot = slot_of(check_name(name))
        if slot is None:
            raise SandboxError("That sandbox has no address.")
        return guest_address(slot)

    def _child(self, name: str) -> Path:
        return child_disk_path(self.base_image, check_name(name))

    def _wait_ready(self, name: str) -> None:
        """Wait until the guest's SSH answers. The host key is pinned on the first answer that gives one, and every
        check after that is strict, so a different key later is refused rather than trusted."""
        deadline = time.monotonic() + self.ready_seconds
        pinned = False
        while True:
            try:
                if not pinned:
                    self._pin_host_key(name)
                    pinned = True
                code, _, _ = self._ssh(name, "true", timeout=15)
                if code == 0:
                    return
            except SandboxError:
                pass  # not up yet; the deadline below decides when to give up
            if time.monotonic() >= deadline:
                raise SandboxError("The sandbox didn't start in time.")
            self.sleep(3)

    def _destroy_name(self, name: str) -> None:
        self._ps(destroy_script(name, self._child(name)))
        Path(self._known_hosts(name)).unlink(missing_ok=True)

    # ---- lifecycle
    def check_network(self) -> None:
        """Refuse unless the host's side of the sandbox network passes the read-only check. A pass is trusted for
        NETWORK_OK_SECONDS, so each create doesn't run PowerShell again. A failure is never kept, so a fix counts at
        once. Any PowerShell error counts as a failure."""
        with self._lock:
            if time.monotonic() < self._network_ok_until:
                return
        problems = host_network_problems(self.switch, self.proxy_port, self._ps)
        if problems:
            raise SandboxRefused(network_refusal(problems))
        with self._lock:
            self._network_ok_until = time.monotonic() + NETWORK_OK_SECONDS

    def create(self, session_id: int | str) -> SandboxHandle:
        self.check_network()
        # Only the slot is chosen under the lock. The VM takes minutes to boot, so it is made outside it, and the
        # reaper and other sandboxes are never held up by it.
        with self._lock:
            slot = next_slot(self.list_existing(), self._reserved)
            self._reserved.add(slot)
        try:
            return self._make(make_name(session_id, slot), slot)
        finally:
            with self._lock:
                self._reserved.discard(slot)

    def _make(self, name: str, slot: int) -> SandboxHandle:
        try:
            child = self._child(name)
            self._ps(new_vhd_script(child, self.base_image, self.disk_gb))
            self._ps(new_vm_script(name, child, self.switch, self.memory_mb, self.cpus, self.vm_dir))
            self._ps(isolation_script(name, slot))
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
        self._destroy_name(handle.name)

    def list_existing(self) -> list[str]:
        return parse_vm_names(self._ps(list_vms_script()))

    # ---- running and files
    def run(self, handle, argv, *, cwd, env, timeout, on_output=None) -> ExitResult:
        with self._lock:
            self._killed.discard(handle.name)
        remote = guest_run_command(argv, cwd, env, timeout)
        # The runner streams the output to on_output as it comes and keeps only its end, so this stays bounded.
        code, out, err = self._ssh(handle.name, remote, timeout=timeout + 60, on_output=on_output)
        tail = OutputTail(MAX_OUTPUT_CHARS * 4)
        tail.add(out + err)
        with self._lock:
            stopped = handle.name in self._killed
        return ExitResult(code, tail.text(), timed_out=code == 124, stopped=stopped)

    def kill(self, handle) -> None:
        with self._lock:
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
                                              f"{skip}", timeout=600, limit=self.archive_limit)
        if len(out) > self.archive_limit:  # the runner stopped reading here and killed the command
            raise SandboxError(f"The sandbox's files are bigger than {self.archive_limit // (1024 * 1024)} MB, so they "
                               "weren't copied out. Ask a mentor to check them by hand.")
        if code != 0:
            raise SandboxError("Couldn't copy the sandbox's files out.")
        return out


# --------------------------------------------------------------------------- factory
def backend_fingerprint(settings: Settings, kind: str) -> tuple:
    """The settings a backend of this kind is built from. A cached backend is rebuilt when these change."""
    if kind == "fake":
        return (str(settings.data_dir),)
    if kind == "hyperv":
        return (str(settings.data_dir), settings.sandbox_base_image, settings.sandbox_switch,
                settings.sandbox_memory_mb, settings.sandbox_cpus, settings.sandbox_disk_gb,
                settings.sandbox_proxy_port)
    return ()


def make_backend(settings: Settings, kind: str) -> SandboxBackend:
    """The backend for a code_sandbox value, built from the current settings (see SandboxManager.backend)."""
    if kind == "fake":
        return FakeSandbox(settings.data_dir / "sandbox-fake")
    if kind == "hyperv":
        # TODO: create the SSH key pair and bake its public half into the base image
        keys = settings.data_dir / "sandbox"
        return HyperVSandbox(base_image=settings.sandbox_base_image, switch=settings.sandbox_switch,
                             memory_mb=settings.sandbox_memory_mb, cpus=settings.sandbox_cpus,
                             disk_gb=settings.sandbox_disk_gb, ssh_key=str(keys / "id_ed25519"),
                             known_dir=str(keys / "known_hosts"), proxy_port=settings.sandbox_proxy_port)
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

    def start(self, row_id: int, name: str) -> bool:
        """Record a made sandbox as running. False if its record was closed meanwhile (the reaper gave up on it)."""
        with self.db.transaction() as conn:
            cur = conn.execute("UPDATE sandbox_sessions SET name = ?, state = 'running' "
                               "WHERE id = ? AND state = 'creating'", (name, row_id))
            return cur.rowcount == 1

    def any_creating(self) -> bool:
        return self.db.one("SELECT 1 AS found FROM sandbox_sessions WHERE state = 'creating' LIMIT 1") is not None

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
    """Makes, uses and deletes sandboxes, and keeps their records in step. `lock` covers the records and every
    destroy, and it is never held while a VM boots: a sandbox's slot is reserved under the lock as a record in state
    'creating' (which counts toward the limit), and the VM is made after the lock is released."""

    def __init__(self, settings: Settings, db: Database,
                 backend_factory: Callable[[str], SandboxBackend] | None = None):
        self.settings = settings
        self.db = db
        self.store = SandboxStore(db)
        self.backend_factory = backend_factory or (lambda kind: make_backend(settings, kind))
        self.lock = threading.RLock()
        self._backends: dict[str, tuple[tuple, SandboxBackend]] = {}
        self._session_locks: dict[int, threading.RLock] = {}
        self._state_lock = threading.Lock()  # guards _backends and _session_locks only

    @property
    def enabled(self) -> bool:
        """True unless code_sandbox is off. An unknown value counts as on, so a typo in config.env fails closed:
        the command is refused, instead of running on this computer."""
        return self.settings.code_sandbox.strip().lower() not in OFF_VALUES

    def backend(self, kind: str) -> SandboxBackend:
        """The one backend of this kind that everyone shares, so its state (a running command, a stop request) is
        seen by every caller. It is rebuilt only when the settings it is built from, or the factory, change."""
        key = (self.backend_factory, backend_fingerprint(self.settings, kind))
        with self._state_lock:
            cached = self._backends.get(kind)
            if cached is None or cached[0] != key:
                cached = (key, self.backend_factory(kind))
                self._backends[kind] = cached
            return cached[1]

    def session_lock(self, code_session_id: int) -> threading.RLock:
        """Serializes the sandbox work of one Code session (making its sandbox and copying the repository in), so two
        commands can't each make one. Other sessions are not held up by it."""
        with self._state_lock:
            return self._session_locks.setdefault(code_session_id, threading.RLock())

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
        """Make a sandbox for a Code session. Raises SandboxRefused or SandboxError. The lock is held only to check
        the limit and record the reservation; the VM is made outside it, so the reaper is never held up."""
        kind = self.settings.code_sandbox
        self.backend(kind).check_network()  # first, and outside the lock: a refused sandbox leaves no record
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=self.settings.sandbox_ttl_hours)
        with self.lock:
            self.check_capacity()  # counts sandboxes being made too, so parallel requests can't exceed the limit
            row_id = self.store.insert(code_session_id, kind, state="creating",
                                       created_at=now.isoformat(timespec="seconds"),
                                       expires_at=expires.isoformat(timespec="seconds"))
        try:
            handle = self.backend(kind).create(code_session_id)
        except SandboxError as exc:
            self.store.update(row_id, state="failed", note=str(exc)[:300])
            raise
        except Exception as exc:
            self.store.update(row_id, state="failed", note=str(exc)[:300])
            raise SandboxError(f"The sandbox couldn't be made: {exc}") from exc
        with self.lock:
            recorded = self.store.start(row_id, handle.name)
        if not recorded:  # the reaper closed this record while the VM was being made: don't keep the VM
            try:
                self.backend(kind).destroy(handle)
            except Exception:
                log.warning("Couldn't delete sandbox %s, which was made after its record was closed", handle.name,
                            exc_info=True)
            raise SandboxError("The sandbox took too long to start, so it was deleted. Try again.")
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
    if manager.store.any_creating():
        return  # a VM can exist before its record has a name, so nothing is an orphan until the makes finish
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
