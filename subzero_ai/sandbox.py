"""Code sandboxes: a disposable virtual machine for the commands of each Code session.

A Code session's commands run inside its own sandbox instead of on the mini PC. The mentor's copy of the repository
stays on the host (reads, approvals and the patch download all use it), and every approved edit is written to both
copies. A sandbox is a Hyper-V virtual machine made from one shared base disk as a differencing disk, so the base is
only ever read. It is attached only to the internal switch named in Settings (default szsandbox-internal), whose host
vNIC is 10.77.0.1/24. The host must firewall that adapter; see docs/CODE_SANDBOX_PLAN.md. Each VM also gets its own
network ACLs, DHCP and router guards, a static MAC and a private VLAN (see new_vm_script and isolation_script). ACLs
alone don't stop non-IP frames, so the private VLAN is what keeps sandboxes apart. A sandbox is deleted when its
session finishes, after its time limit, and when no record of it is left in the database.

Each sandbox gets its own SSH host key pair, made on the host. The public half goes into the sandbox's known_hosts
file, and the private half reaches the guest on a small NoCloud seed disk (a CIDATA disk that cloud-init reads on
first boot). Nothing is read over the network, so ssh-keyscan isn't used (it fails with Windows OpenSSH). Once SSH
answers, the seed disk is detached and deleted and the host's copy of the private key is deleted.

FakeSandbox does the same job with folders and subprocesses on this computer. It exists for the tests and is not
isolated at all. HyperVSandbox builds its PowerShell, SSH and key commands with pure functions and takes its runners
as parameters, so the tests can check every command without touching a real host.

Guest image details that are not known yet are marked TODO (python3 and the base disk's path). The base image must
have NoCloud enabled (the image script does this once). See docs/CODE_SANDBOX_PLAN.md, milestones M3, M3b and M4.
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
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .config import Settings, fake_sandbox_allowed
from .db import Database, utcnow
from .egress_proxy import EgressProxy, TokenStore

log = logging.getLogger("subzero.sandbox")

PREFIX = "szsandbox-"  # every sandbox's name starts with this, so the reaper only ever touches our own
COPY_SKIP = frozenset({".gradle", "build"})  # build output and caches: never copied into or out of a sandbox
OFF_VALUES = ("", "off")  # code_sandbox values that keep Code commands on this computer; anything else needs a sandbox
MAX_OUTPUT_CHARS = 20_000  # the end of a command's output that goes back to Claude
REAP_SECONDS = 5  # after a command exits, how long its output may stay open (a background child process)
STALE_CREATING = timedelta(minutes=30)  # a sandbox still "being made" after this long was left by a crash
EXTEND_LIMIT = timedelta(days=7)  # a sandbox can be extended by mentors, but never lives longer than this
MAX_SLOTS = 200  # sandboxes get a numbered slot; the slot picks the guest's static address
GUEST_ADDRESS = "10.77.0.{}"
GUEST_FIRST_SLOT_HOST = 10  # 10.77.0.10 is the first guest address; .1 is the host's
HOST_ADDRESS = "10.77.0.1"  # the host's vNIC on the internal switch
GUEST_SUBNET = "10.77.0.0/24"
GUEST_GRADLE_OPTS = "-Dorg.gradle.daemon=false"  # a sandbox's Gradle runs without its daemon
REDACTED = "[hidden]"  # what a proxy token becomes in output that goes back to Claude or a mentor
RECENT_TOKENS = 64  # tokens kept (in memory) so output of a command still running at deletion is redacted
IPV6_ANY = "::/0"  # every IPv6 address: the sandboxes get no IPv6 path at all
MAX_SESSION_BYTES = 800 * 1024 * 1024  # one session's two copies together, build output included; also the archive cap
SSH = "ssh"  # the ssh program on the host (a test can point it at a fake)
SSH_KEYGEN = "ssh-keygen.exe"  # makes each sandbox's host key pair and the app's client key (on the host)
ICACLS = "icacls"  # locks a key file to the account that runs the app
PUBLIC_KEY_LINE = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/=]+(?: [A-Za-z0-9._@-]+)?$")
PRIVATE_KEY_BEGIN = "-----BEGIN OPENSSH PRIVATE KEY-----"
PRIVATE_KEY_END = "-----END OPENSSH PRIVATE KEY-----"
CLIENT_KEY_COMMENT = "szsandbox-client"
HOST_KEY_FILE = "ssh_host_ed25519_key"  # the host key pair's file name inside a sandbox's staging folder
SEED_LABEL = "CIDATA"  # the NoCloud seed's volume label, which cloud-init looks for
SEED_DIR = "seed"  # the staging subfolder that becomes the seed image's root
SEED_FILES = ("meta-data", "network-config", "user-data")  # the files on the seed disk, in this order
SEED_SIZE_MB = 64
PRIVATE_VLAN_PRIMARY = 100  # the private VLAN pair every sandbox adapter and the host's adapter use
PRIVATE_VLAN_SECONDARY = 200
# Waits in the guest until cloud-init has read the seed, so the seed can be detached without breaking its reads
CLOUD_INIT_WAIT = "cloud-init status --wait >/dev/null 2>&1; true"
# The guest account, from the seed's user-data. It is the same in every sandbox, so the base image can rely on it.
GUEST_USER = "sandbox"
GUEST_HOME = "/home/sandbox"
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
    expires_at: str = ""  # when the sandbox is due for deletion; its proxy token expires then too


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


def sandbox_address(name: str) -> str | None:
    """The address that belongs to a sandbox name: its slot's static address, which the VM's MAC and network config
    are set from. None for anything that isn't a sandbox name with a slot (the egress proxy refuses those)."""
    try:
        check_name(name)
    except SandboxError:
        return None
    slot = slot_of(name)
    return guest_address(slot) if slot is not None and 0 <= slot < MAX_SLOTS else None


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


def proxy_env(name: str, token: str, host: str, port: int) -> dict[str, str]:
    """The environment that lets a command in a sandbox reach the internet through the egress proxy
    (egress_proxy.py). The token is in it: never log, audit or show the result. Java and Gradle don't read
    HTTPS_PROXY, so their proxy settings are passed as JVM options. The empty Basic-scheme override lets Java send the
    login for the HTTPS tunnel (the JDK refuses Basic for tunnels by default)."""
    url = f"http://{name}:{token}@{host}:{port}"
    java = (f"-Dhttps.proxyHost={host} -Dhttps.proxyPort={port} -Dhttps.proxyUser={name} "
            f"-Dhttps.proxyPassword={token} -Djdk.http.auth.tunneling.disabledSchemes=")
    return {"HTTPS_PROXY": url, "https_proxy": url, "NO_PROXY": "", "no_proxy": "",
            "JAVA_TOOL_OPTIONS": java, "GRADLE_OPTS": f"{GUEST_GRADLE_OPTS} {java}"}


# --------------------------------------------------------------------------- Hyper-V: pure command builders
def ps_quote(value: str) -> str:
    """A value as a PowerShell single-quoted string. Control characters are refused outright."""
    text = str(value)
    if any(ord(ch) < 32 for ch in text):
        raise SandboxError("A setting has a line break or control character, so it can't be used.")
    return "'" + "".join(PS_QUOTES.get(ch, ch) for ch in text) + "'"


def child_disk_path(base: str | Path, name: str, folder: str | Path | None = None) -> Path:
    """The child disk for a sandbox, named after the sandbox: in `folder` when one is given (the app's own folder, which
    the service can create files in), else next to the base disk. Never the base disk itself."""
    base = Path(base)
    check_name(name)
    child = Path(folder) / f"{name}.vhdx" if folder else base.with_name(f"{name}.vhdx")
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
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"New-VM -Name {vm} -Generation 2 -MemoryStartupBytes {int(memory_mb)}MB -VHDPath {ps_quote(str(child))}"
        f" -SwitchName {ps_quote(switch)} -Path {ps_quote(str(vm_dir))} | Out-Null",
        # Ubuntu's boot loader is signed by Microsoft's third-party CA, not by the default Windows template
        f"Set-VMFirmware -VMName {vm} -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority",
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
    (docs/CODE_SANDBOX_PLAN.md). Last, the adapter goes into the private VLAN pair (100 primary, 200 secondary, isolated
    mode). ACLs don't stop non-IP frames, so the VLAN is what keeps one sandbox's frames away from another's. The
    host's adapter is promiscuous on the same pair (sandbox-network.ps1 Install). Measured on the mini PC, see
    docs/CODE_SANDBOX_PLAN.md (M3b). The VLAN mode is read back before the VM starts and again after it starts
    (check_vm_vlan, in HyperVSandbox._make)."""
    vm = ps_quote(check_name(name))
    host = ps_quote(f"{HOST_ADDRESS}/32")
    subnet = ps_quote(GUEST_SUBNET)
    def both(action: str, remote: str, weight: int) -> list[str]:
        # The cmdlet takes Inbound or Outbound only (there is no Both), so every rule is added once per direction
        return [f"Add-VMNetworkAdapterExtendedAcl -VMName {vm} -Action {action} -Direction {direction} "
                f"-RemoteIPAddress {remote} -Weight {weight}" for direction in ("Inbound", "Outbound")]

    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"Set-VMNetworkAdapter -VMName {vm} -StaticMacAddress {ps_quote(mac_for_slot(slot))} "
        "-DhcpGuard On -RouterGuard On",
        *both("Allow", host, 200),
        *both("Deny", subnet, 100),
        *both("Deny", ps_quote(IPV6_ANY), 150),
        f"Set-VMNetworkAdapterVlan -VMName {vm} -Isolated -PrimaryVlanId {PRIVATE_VLAN_PRIMARY} "
        f"-SecondaryVlanId {PRIVATE_VLAN_SECONDARY}",
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


def destroy_script(name: str, child: Path, seed: Path, vm_folder: Path | None = None) -> str:
    """Turn the VM off, remove it, and delete its child disk and its seed disk (if one was left). Safe to run twice.
    The base disk is never named."""
    vm = ps_quote(check_name(name))
    seed_disk = ps_quote(check_path(seed))
    disks = f"@({ps_quote(check_path(child))}, {seed_disk})"
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$vm = Get-VM -Name {vm} -ErrorAction SilentlyContinue",
        "if ($vm) {",
        "  if ($vm.State -ne 'Off') { Stop-VM -Name " + vm + " -TurnOff -Force }",
        f"  Remove-VM -Name {vm} -Force",
        "}",
        *wipe_file_lines(seed_disk),  # the seed may hold a copy of the host's private key; the VM no longer uses it
        f"foreach ($disk in {disks}) {{ if (Test-Path -LiteralPath $disk) {{ "
        "Remove-Item -LiteralPath $disk -Force } }",
        *([f"$folder = {ps_quote(check_path(vm_folder))}",  # Hyper-V leaves the VM's own (empty) folders behind
           "if (Test-Path -LiteralPath $folder) { Remove-Item -LiteralPath $folder -Recurse -Force "
           "-ErrorAction SilentlyContinue }"] if vm_folder else []),
    ])


def parse_vm_names(output: str) -> list[str]:
    text = (output or "").strip()
    if not text:
        return []
    value = json.loads(text)
    names = [value] if isinstance(value, str) else list(value or [])
    return sorted(str(n) for n in names if str(n).startswith(PREFIX))


# --------------------------------------------------------------------------- Hyper-V: VLAN, seed disk and host keys
def vm_vlan_script(name: str) -> str:
    """Read back one VM's adapters' VLAN settings, as a JSON list (one object per adapter)."""
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"ConvertTo-Json -Compress -InputObject @(Get-VMNetworkAdapterVlan -VMName {ps_quote(check_name(name))} | "
        "ForEach-Object { [pscustomobject]@{ OperationMode = [string]$_.OperationMode; "
        "PrivateVlanMode = [string]$_.PrivateVlanMode; PrimaryVlanId = [string]$_.PrimaryVlanId; "
        "SecondaryVlanId = [string]$_.SecondaryVlanId } })",
    ])


def check_vm_vlan(output: str) -> None:
    """Refuse a VM whose adapters are not all isolated in the private VLAN pair. Nothing is accepted by default: an
    empty answer, or any adapter that reads back differently, stops the VM."""
    text = (output or "").strip()
    rows = json.loads(text) if text else []
    if isinstance(rows, dict):
        rows = [rows]
    good = bool(rows) and all(
        isinstance(row, dict) and str(row.get("OperationMode")) == "Private"
        and str(row.get("PrivateVlanMode")) == "Isolated" and str(row.get("PrimaryVlanId")) == str(PRIVATE_VLAN_PRIMARY)
        and str(row.get("SecondaryVlanId")) == str(PRIVATE_VLAN_SECONDARY) for row in rows)
    if not good:
        raise SandboxError("The sandbox's network isn't on its own isolated private VLAN, so it wasn't started.")


def check_path(value: str | Path) -> str:
    """A host path for a PowerShell script or a file write. Refuses an empty path, control characters and '..'."""
    text = str(value)
    if not text or any(ord(ch) < 32 for ch in text) or ".." in re.split(r"[\\/]", text):
        raise SandboxError("A file path for the sandbox has a character that isn't allowed, so it wasn't used.")
    return text


def seed_disk_path(base: str | Path, name: str, folder: str | Path | None = None) -> Path:
    """The NoCloud seed for a sandbox: an ISO image in `folder` (else next to the base disk), never the base disk."""
    base = Path(base)
    check_name(name)
    seed = Path(folder) / f"{name}-seed.iso" if folder else base.with_name(f"{name}-seed.iso")
    if seed == base or seed.suffix.lower() != ".iso" or not seed.name.startswith(PREFIX):
        raise SandboxError("The seed disk would have replaced the base disk, so it wasn't made.")
    return seed


# Windows' own IMAPI2 image writer, called from PowerShell. It needs no extra privilege (mounting a disk image does)
ISO_WRITER_SOURCE = """
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public static class SzIsoWriter {
    public static void Write(object comStream, string path) {
        IStream stream = (IStream)comStream;
        System.Runtime.InteropServices.ComTypes.STATSTG stat;
        stream.Stat(out stat, 1);
        long left = stat.cbSize;
        byte[] buffer = new byte[65536];
        IntPtr read = Marshal.AllocHGlobal(4);
        try {
            using (FileStream file = new FileStream(path, FileMode.CreateNew, FileAccess.Write)) {
                while (left > 0) {
                    int want = (int)Math.Min((long)buffer.Length, left);
                    stream.Read(buffer, want, read);
                    int got = Marshal.ReadInt32(read);
                    if (got <= 0) { break; }
                    file.Write(buffer, 0, got);
                    left -= got;
                }
            }
        } finally { Marshal.FreeHGlobal(read); }
    }
}
"""


def seed_disk_script(seed: Path, staging: Path) -> str:
    """Build the seed as an ISO image with Windows' IMAPI2 (no mounting, so no extra privilege): the label cidata and
    the three seed files from the staging folder at its root. A seed that already exists is never overwritten."""
    disk = ps_quote(check_path(seed))
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"if (Test-Path -LiteralPath {disk}) {{ throw 'The seed disk is already there.' }}",
        "Add-Type -TypeDefinition @'" + ISO_WRITER_SOURCE + "'@",
        "$image = New-Object -ComObject IMAPI2FS.MsftFileSystemImage",
        "$image.FileSystemsToCreate = 3",  # ISO 9660 and Joliet, so the file names keep their case and hyphens
        f"$image.VolumeName = {ps_quote(SEED_LABEL.lower())}",
    ]
    # Only the seed folder goes onto the image (the host key's own file sits beside it, never in it)
    lines.append(f"$image.Root.AddTree({ps_quote(check_path(Path(staging) / SEED_DIR))}, $false)")
    lines += ["$result = $image.CreateResultImage()",
              f"[SzIsoWriter]::Write($result.ImageStream, {disk})"]
    return "\n".join(lines)


def attach_seed_script(name: str, seed: Path) -> str:
    return "\n".join(["$ErrorActionPreference = 'Stop'",
                      f"Add-VMDvdDrive -VMName {ps_quote(check_name(name))} -Path {ps_quote(check_path(seed))}"])


def wipe_file_lines(disk: str) -> list[str]:
    """PowerShell lines that overwrite a file with zeros, best effort (a file that is locked or can't be opened is
    left to the delete that follows), and do nothing when it is missing. `disk` is the quoted path."""
    return [
        f"if (Test-Path -LiteralPath {disk}) {{",
        "  try {",
        f"    $stream = [System.IO.File]::OpenWrite({disk})",
        "    try {",
        "      $zeros = New-Object byte[] 1048576",
        "      $left = $stream.Length",
        "      $stream.Position = 0",
        "      while ($left -gt 0) {",
        "        $n = [int][Math]::Min([long]$zeros.Length, $left)",
        "        $stream.Write($zeros, 0, $n)",
        "        $left -= $n",
        "      }",
        "      $stream.Flush()",
        "    } finally { $stream.Dispose() }",
        "  } catch { }",
        "}",
    ]


def release_seed_script(name: str, seed: Path) -> str:
    """Detach the seed disk from the VM, overwrite the seed file with zeros (best effort: it holds a copy of the host's
    private key, in its user-data), then delete it. Safe to run twice."""
    vm = ps_quote(check_name(name))
    disk = ps_quote(check_path(seed))
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"Get-VMDvdDrive -VMName {vm} | Where-Object {{ $_.Path -eq {disk} }} | Remove-VMDvdDrive",
        *wipe_file_lines(disk),
        f"if (Test-Path -LiteralPath {disk}) {{ Remove-Item -LiteralPath {disk} -Force }}",
    ])


def lf_text(text: str) -> str:
    """Line endings as the guest reads them: LF only."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_lf(path: Path, text: str) -> None:
    """Write a seed or known_hosts file as UTF-8 without a byte order mark, with LF line endings only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(lf_text(text).encode("utf-8"))


def public_key_text(value: str) -> str:
    """An ed25519 public key line (as ssh-keygen writes it). Anything else is refused."""
    text = (value or "").strip()
    if not PUBLIC_KEY_LINE.match(text):
        raise SandboxError("The sandbox's SSH public key isn't an ed25519 key, so it wasn't used.")
    return text


def private_key_text(value: str) -> str:
    """An OpenSSH private key file, LF line endings, one key only. Anything else is refused."""
    lines = lf_text(value or "").strip("\n").split("\n")
    body = lines[1:-1]
    if (lines[0] != PRIVATE_KEY_BEGIN or lines[-1] != PRIVATE_KEY_END or not body
            or not all(line and line.isascii() and line.isprintable() for line in body)):
        raise SandboxError("The sandbox's SSH host key couldn't be read, so it wasn't used.")
    return "\n".join(lines) + "\n"


def meta_data(name: str) -> str:
    check_name(name)
    return f"instance-id: {name}\nlocal-hostname: {name}\n"


def network_config(slot: int) -> str:
    """Network config (version 2): the one wired adapter gets the sandbox's static address and no gateway."""
    return "\n".join([
        "version: 2",
        "ethernets:",
        "  eth0:",
        "    match:",
        '      name: "e*"',
        "    dhcp4: false",
        "    dhcp6: false",
        "    addresses:",
        f"      - {guest_address(slot)}/24",
        "",
    ])


def user_data(client_public: str, host_public: str, host_private: str, *, guest_sudo: bool) -> str:
    """The cloud-init user-data for a sandbox. The guest user accepts only the app's client key. Passwords are never
    enabled (locked password, password login off). The host key pair is the one this sandbox's known_hosts file was
    written from. sudo is off unless guest_sudo is set."""
    private = private_key_text(host_private).rstrip("\n").split("\n")
    lines = [
        "#cloud-config",
        "users:",
        f"  - name: {GUEST_USER}",
        "    lock_passwd: true",
        *(["    sudo: ALL=(ALL) NOPASSWD:ALL"] if guest_sudo else []),  # no sudo line: the user gets no sudo rights
        "    ssh_authorized_keys:",
        f"      - {public_key_text(client_public)}",
        "ssh_pwauth: false",
        # IPv6 off in the guest, as early as cloud-init allows and again from sysctl.d on every later boot
        "bootcmd:",
        "  - [sh, -c, \"sysctl -w net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1\"]",
        # The Azure agent waits for Azure data that never comes in a sandbox, which slowed the last boot stage by ~30 s
        "  - [sh, -c, \"systemctl mask walinuxagent.service walinuxagent-network-setup.service || true\"]",
        "write_files:",
        "  - path: /etc/sysctl.d/99-sandbox-no-ipv6.conf",
        "    content: |",
        "      net.ipv6.conf.all.disable_ipv6 = 1",
        "      net.ipv6.conf.default.disable_ipv6 = 1",
        "ssh_deletekeys: true",
        "ssh_genkeytypes: []",
        "ssh_keys:",
        "  ed25519_private: |",
        *(f"    {line}" for line in private),
        f"  ed25519_public: {public_key_text(host_public)}",
    ]
    return "\n".join(lines) + "\n"


def known_hosts_line(host_public: str, address: str) -> str:
    """The known_hosts line that pins a guest's host key: its address, the key type and the key (no comment)."""
    key_type, blob = public_key_text(host_public).split(" ")[:2]
    return f"{address} {key_type} {blob}"


def keygen_argv(path: Path, comment: str) -> list[str]:
    """ssh-keygen for one ed25519 pair with no passphrase, written quietly to path and path.pub."""
    return [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", check_path(path)]


def parse_whoami_sid(output: str) -> str:
    """The SID from `whoami /user /fo csv /nh` output (`"domain\\user","S-1-5-..."`) as the `*S-1-5-...` form icacls
    takes, or '' when the output has no SID. A SID is used because %USERNAME% is the computer account (MINIPC$) for a
    virtual service account such as NT SERVICE\\SubZeroAI."""
    match = re.search(r'"(S-1-[0-9-]+)"', output or "")
    return f"*{match.group(1)}" if match else ""


def default_whoami_sid() -> str:
    """The SID of the account this process runs as, from whoami, or '' if it can't be read."""
    try:
        proc = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return parse_whoami_sid(proc.stdout) if proc.returncode == 0 else ""


def icacls_argv(path: Path, account: str) -> list[str]:
    """Lock a file to one account: inherited permissions removed, full control for that account only."""
    if not account or any(ord(ch) < 32 for ch in account):
        raise SandboxError("The account that runs the app has a name that can't be used to lock a key file.")
    return [ICACLS, check_path(path), "/inheritance:r", "/grant:r", f"{account}:F"]


def shred_file(path: Path) -> None:
    """Overwrite a file with zeros, then delete it. A missing file is fine."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    try:
        with open(path, "r+b") as handle:
            handle.write(b"\0" * size)
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink()
    except OSError as exc:
        raise SandboxError(f"Couldn't delete a sandbox key file: {exc}") from exc


# --------------------------------------------------------------------------- Hyper-V: host network check
NETWORK_OK_SECONDS = 60  # a passing host check is trusted this long, so each create doesn't run PowerShell again
NETWORK_CHECK_MARK = "# szsandbox: host network check (read-only, changes nothing)"  # first line of the check script
NETWORK_CHECK_SECONDS = 60  # the Settings page's check gives up after this long
HOST_VNIC_NAME = "vEthernet ({})"  # the host's adapter for an Internal switch is named after it
BLOCK_TCP_BELOW = "szsandbox-block-tcp-below"  # blocks TCP ports 1 to proxy port minus 1
BLOCK_TCP_ABOVE = "szsandbox-block-tcp-above"  # blocks TCP ports proxy port plus 1 to 65535
BLOCK_UDP = "szsandbox-block-udp"  # blocks every UDP port
BLOCK_ICMP4 = "szsandbox-block-icmp4"  # blocks ICMPv4
BLOCK_ICMP6 = "szsandbox-block-icmp6"  # blocks ICMPv6
# IP protocols with no ports that TCP, UDP and ICMP blocks don't cover: IGMP (2), IPv6 inside IPv4 (41), GRE (47),
# ESP (50) and AH (51). Each gets one block rule, BLOCK_PROTO_RULE with the number.
BLOCK_PROTOCOLS = (2, 41, 47, 50, 51)
BLOCK_PROTO_RULE = "szsandbox-block-proto-{}"
PROXY_RULE = "szsandbox-allow-proxy"  # the one allow: TCP to the proxy port from the guest subnet
# The PowerShell list of BLOCK_PROTOCOLS that sandbox-network.ps1 must contain word for word (a test checks it)
NETWORK_BLOCK_PROTOCOLS_PS = ("$BlockProtocols = @(" + ", ".join(str(n) for n in BLOCK_PROTOCOLS) + ")  "
                              "# IP protocols with no ports, blocked whole: generated from sandbox.BLOCK_PROTOCOLS")
# Problem codes that carry a name after the colon (a rule's name, or an error message)
NETWORK_CODE_PREFIXES = ("block-rule-missing:", "check-failed:")
NETWORK_PROBLEMS = {
    "switch-missing": "The sandbox switch doesn't exist yet",
    "switch-not-internal": "The sandbox switch isn't an Internal switch",
    "host-adapter-missing": "The host has no network adapter for the sandbox switch",
    "host-vlan-missing": "The host's adapter for the sandboxes isn't on private VLAN 100 with 200 in promiscuous mode, "
                         "so sandboxes could still reach each other",
    "host-address": "The host's adapter for the sandboxes doesn't have the address 10.77.0.1/24",
    "host-gateway": "The host's adapter for the sandboxes has a default gateway, and it must have none",
    "forwarding-on": "IP forwarding is on for the host's adapter for the sandboxes, and it must be off",
    "profile-not-public": "The network profile of the sandbox adapter isn't Public",
    "ipv6-enabled": "IPv6 is still on for the host's adapter for the sandboxes, and it must be off (uncheck TCP/IPv6 "
                    "on that adapter)",
    "proxy-rule-wrong": "The firewall rule szsandbox-allow-proxy must exist and allow only TCP to the proxy port from "
                        "10.77.0.0/24 on the sandbox adapter",
    "firewall-off": "Windows Firewall is turned off for the Public profile, so none of the sandbox rules would apply. "
                    "Turn it on (Windows Security, Firewall and network protection)",
    "default-inbound-allow": "Windows Firewall's default for incoming traffic on the Public profile is Allow, and it "
                             "must be Block",
}
UNREADABLE_CHECK = "The check gave an answer the app couldn't read"


def block_rule_specs(proxy_port: int) -> list[tuple[str, str, str | None]]:
    """The firewall block rules for the sandbox adapter, as (name, protocol, local ports or None for every port). With
    PROXY_RULE they cover all inbound traffic: TCP is blocked below and above the proxy port, UDP, both ICMP versions
    and BLOCK_PROTOCOLS are blocked whole, and the one allow rule opens only the proxy port. Windows Firewall lets a
    block rule beat an allow rule, which is why the blocks skip the proxy port. A range that would be empty is left
    out. Allow rules that already exist on the host need no change: these interface-scoped blocks take precedence."""
    port = int(proxy_port)
    specs: list[tuple[str, str, str | None]] = []
    if port > 1:
        specs.append((BLOCK_TCP_BELOW, "TCP", f"1-{port - 1}"))
    if port < 65535:
        specs.append((BLOCK_TCP_ABOVE, "TCP", f"{port + 1}-65535"))
    specs += [(BLOCK_UDP, "UDP", None), (BLOCK_ICMP4, "ICMPv4", None), (BLOCK_ICMP6, "ICMPv6", None)]
    specs += [(BLOCK_PROTO_RULE.format(number), str(number), None) for number in BLOCK_PROTOCOLS]
    return specs


def describe_network_problem(code: str) -> str:
    """The plain-language text for one problem code from host_network_check_script (no final full stop)."""
    if code.startswith("block-rule-missing:"):
        name = code.split(":", 1)[1][:120]
        return (f"The firewall rule {name} is missing, turned off or set up wrong. The sandbox adapter needs its block "
                "rules for every port except the proxy's, and for the other IP protocols")
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
    M3b). The same checks, with messages, are in packaging/windows/scripts/sandbox-network.ps1; keep them in step.
    It reads only the szsandbox-* firewall rules, and each one's filters once, so it stays fast."""
    sw = ps_quote(switch)
    alias = ps_quote(HOST_VNIC_NAME.format(switch))
    subnet = ps_quote(GUEST_SUBNET)
    port = int(proxy_port)
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
        f"    $vlan = Get-VMNetworkAdapterVlan -ManagementOS -VMNetworkAdapterName {sw} -ErrorAction SilentlyContinue",
        "    $secondary = @($vlan.SecondaryVlanIdList | ForEach-Object { [string]$_ })",
        "    $vlanOk = $vlan -and ([string]$vlan.OperationMode -eq 'Private') -and "
        "([string]$vlan.PrivateVlanMode -eq 'Promiscuous') -and ([string]$vlan.PrimaryVlanId -eq '100') -and "
        "($secondary -contains '200')",
        "    if (-not $vlanOk) { $problems.Add('host-vlan-missing') }",
        "  }",
        "  $publicProfile = Get-NetFirewallProfile -Profile Public -PolicyStore ActiveStore -ErrorAction Stop",
        "  if ($publicProfile.Enabled -ne 'True') { $problems.Add('firewall-off') }",
        "  if ($publicProfile.DefaultInboundAction -eq 'Allow') { $problems.Add('default-inbound-allow') }",
        "  # Only the szsandbox-* rules are read, and each rule's filters are fetched once, into $infos",
        "  $rules = @(Get-NetFirewallRule -DisplayName 'szsandbox-*' -ErrorAction SilentlyContinue | Where-Object {",
        "    $_.Enabled -eq 'True' })",
        "  $infos = @()",
        "  foreach ($rule in $rules) {",
        "    $protocols = Get-RuleValues $rule 'Get-NetFirewallPortFilter' 'Protocol'",
        "    $localPorts = Get-RuleValues $rule 'Get-NetFirewallPortFilter' 'LocalPort'",
        "    $ifaces = Get-RuleValues $rule 'Get-NetFirewallInterfaceFilter' 'InterfaceAlias'",
        "    $remotes = Get-RemoteAddresses $rule",
        "    $infos += [pscustomobject]@{ Name = $rule.DisplayName; Direction = $rule.Direction;"
        " Action = $rule.Action; Protocols = $protocols; LocalPorts = $localPorts; Interfaces = $ifaces;"
        " Remotes = $remotes }",
        "  }",
        f"  $blockSpecs = @({specs})",
        "  foreach ($spec in $blockSpecs) {",
        "    $ok = $false",
        "    foreach ($info in @($infos | Where-Object { $_.Name -eq $spec.Name -and $_.Direction -eq 'Inbound'"
        " -and $_.Action -eq 'Block' })) {",
        "      $portOk = ($null -eq $spec.LocalPort) -or ($info.LocalPorts -contains $spec.LocalPort)",
        "      if (($info.Protocols -contains $spec.Protocol) -and $portOk -and ($info.Interfaces -contains $alias)"
        " -and ($info.Remotes -contains 'Any')) { $ok = $true }",
        "    }",
        "    if (-not $ok) { $problems.Add('block-rule-missing:' + $spec.Name) }",
        "  }",
        "  $proxyRules = @($infos | Where-Object { $_.Name -eq 'szsandbox-allow-proxy' })",
        "  $proxyBad = $proxyRules.Count -eq 0",
        "  foreach ($info in $proxyRules) {",
        "    $ok = ($info.Direction -eq 'Inbound') -and ($info.Action -eq 'Allow')"
        " -and ($info.Protocols -contains 'TCP') `",
        f"      -and ($info.LocalPorts -contains '{port}') -and ($info.LocalPorts.Count -eq 1) `",
        f"      -and ($info.Interfaces -contains $alias) -and ($info.Remotes -contains {subnet})",
        "    if (-not $ok) { $proxyBad = $true }",
        "  }",
        "  if ($proxyBad) { $problems.Add('proxy-rule-wrong') }",
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
    full_env = {"PATH": GUEST_PATH, "HOME": GUEST_HOME, **env}
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


def default_run_keygen(argv: list[str]) -> None:
    """Run ssh-keygen (on Windows, ssh-keygen.exe from OpenSSH). Raises SandboxError with its reason on failure."""
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxError(f"ssh-keygen couldn't run: {exc}") from exc
    if proc.returncode != 0:
        raise SandboxError((proc.stderr or "ssh-keygen failed").strip()[-200:])


def default_run_icacls(argv: list[str]) -> None:
    """Run icacls to lock a file. Raises SandboxError with its reason on failure."""
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxError(f"icacls couldn't run: {exc}") from exc
    if proc.returncode != 0:
        raise SandboxError((proc.stderr or proc.stdout or "icacls failed").strip()[-200:])


def plain_powershell_error(text: str) -> str:
    """An error message as plain text: if powershell.exe still wrapped it in CLIXML, take the error lines out of it."""
    if "#< CLIXML" in text or "<Objs" in text:
        parts = re.findall(r'<S S="Error">(.*?)</S>', text, re.S)
        if parts:
            joined = "".join(parts)
            text = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), joined)
            text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    return text.strip()[-1500:]


def default_run_powershell(script: str, timeout: int = 900) -> str:
    """Run a PowerShell script on the host and return its output. Needs an elevated PowerShell (M3). The script is
    passed encoded, so its line breaks and quotes survive the trip to powershell.exe."""
    # Errors are caught and written as plain text (powershell.exe wraps its own error stream in CLIXML otherwise)
    script = ("$ProgressPreference = 'SilentlyContinue'\ntry {\n" + script +
              "\n} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-OutputFormat", "Text", "-EncodedCommand", encoded], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise SandboxError(plain_powershell_error(proc.stderr or proc.stdout or "PowerShell failed"))
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

    Making a sandbox: a fresh ed25519 host key pair is made on the host (ssh-keygen) and locked to the app's account.
    Its public half is written to the sandbox's own known_hosts file. Its private half goes to the guest on a NoCloud
    seed disk, with the app's client public key for the guest user. The seed is attached before the VM starts. Once SSH
    answers (with strict host key checking, so the pinned key must be the one in use), cloud-init is allowed to finish
    reading the seed, then the seed is detached and deleted, and the host's copies of the private key (the staged host
    key and the seed's user-data) are overwritten with zeros and deleted.
    Destroy deletes whatever is left: the VM, its disks, the seed, the staged files and the known_hosts file.

    The runners are injected: run_powershell(script) -> output text; run_keygen(argv) makes a key pair (ssh-keygen);
    run_icacls(argv) locks a file to an account; and run_ssh(address, remote_command, *, known_hosts, stdin, timeout,
    on_output=None, limit=None) -> (exit code, stdout, stderr). With `limit`, stdout is cut off past limit + 1 bytes and
    the caller refuses it."""

    name = "hyperv"

    def __init__(self, *, base_image: str, switch: str, memory_mb: int, cpus: int, disk_gb: int,
                 vm_dir: str = "", disks_dir: str = "", ssh_key: str = "", known_dir: str = "", staging_dir: str = "", account: str = "",
                 guest_sudo: bool = False, run_powershell: Callable[[str], str] | None = None,
                 run_keygen: Callable[[list[str]], None] | None = None,
                 run_icacls: Callable[[list[str]], None] | None = None,
                 run_whoami: Callable[[], str] | None = None,
                 run_ssh: Callable[..., tuple[int, bytes, bytes]] | None = None,
                 ready_seconds: int = 180, sleep: Callable[[float], None] = time.sleep,
                 archive_limit: int = MAX_SESSION_BYTES, proxy_port: int = 3128):
        if not base_image:
            raise SandboxRefused("Set the sandbox base image in Settings first.")
        self.base_image = base_image
        self.switch = switch
        self.memory_mb, self.cpus, self.disk_gb = memory_mb, cpus, disk_gb
        base_dir = Path(base_image).parent
        # Disks and VM files go in a folder the service makes itself: it has no right to set permissions in the base
        # image's folder, and New-VM needs to
        self.disks_dir = Path(disks_dir) if disks_dir else base_dir / "szsandbox-disks"
        self.vm_dir = Path(vm_dir) if vm_dir else self.disks_dir / "config"
        self.known_dir = Path(known_dir) if known_dir else base_dir / "szsandbox-known-hosts"
        self.staging_dir = Path(staging_dir) if staging_dir else base_dir / "szsandbox-staging"
        self.ssh_key = ssh_key or str(base_dir / "szsandbox-keys" / "id_ed25519")
        self.account = account  # '' means: look up this process's own SID when the first key is locked
        self.run_whoami = run_whoami or default_whoami_sid
        self.guest_sudo = bool(guest_sudo)
        self.run_powershell = run_powershell or default_run_powershell
        self.run_keygen = run_keygen or default_run_keygen
        self.run_icacls = run_icacls or default_run_icacls
        self.run_ssh = run_ssh or make_default_run_ssh(self.ssh_key)
        self.ready_seconds = ready_seconds
        self.sleep = sleep
        self.archive_limit = archive_limit
        self.proxy_port = proxy_port
        self._lock = threading.Lock()  # guards _killed, _reserved and _network_ok_until; never held across a VM call
        self._key_lock = threading.Lock()  # makes the app's client key once, even when two sandboxes start together
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

    def _staging(self, name: str) -> Path:
        """The sandbox's folder of files for the seed (they include the host private key, so it is removed early)."""
        return self.staging_dir / check_name(name)

    def _seed(self, name: str) -> Path:
        return seed_disk_path(self.base_image, check_name(name), self.disks_dir)

    def _ssh(self, name: str, remote: str, *, stdin: bytes | None = None, timeout: int = 60,
             on_output: Callable[[str], None] | None = None, limit: int | None = None) -> tuple[int, bytes, bytes]:
        try:
            return self.run_ssh(self._address(name), remote, known_hosts=self._known_hosts(name), stdin=stdin,
                                timeout=timeout, on_output=on_output, limit=limit)
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Couldn't reach the sandbox: {exc}") from exc

    def _address(self, name: str) -> str:
        slot = slot_of(check_name(name))
        if slot is None:
            raise SandboxError("That sandbox has no address.")
        return guest_address(slot)

    def _child(self, name: str) -> Path:
        return child_disk_path(self.base_image, check_name(name), self.disks_dir)

    def _run_keygen(self, path: Path, comment: str) -> None:
        """Make one key pair at path (path.pub is the public half) and lock the private file to the app's account."""
        path.parent.mkdir(parents=True, exist_ok=True)
        for stale in (path, Path(str(path) + ".pub")):
            stale.unlink(missing_ok=True)  # ssh-keygen would ask before overwriting
        try:
            self.run_keygen(keygen_argv(path, comment))
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Couldn't make the sandbox's SSH keys: {exc}") from exc
        if not self.account:
            self.account = self.run_whoami()
        if not self.account:
            raise SandboxRefused("The app's Windows account isn't known, so the sandbox key files can't be locked.")
        try:
            self.run_icacls(icacls_argv(path, self.account))
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Couldn't lock a sandbox key file: {exc}") from exc

    def _client_public_key(self) -> str:
        """The app-wide client key that signs in to every sandbox. It is made the first time it is needed."""
        key = Path(self.ssh_key)
        with self._key_lock:
            if not (key.is_file() and Path(str(key) + ".pub").is_file()):
                self._run_keygen(key, CLIENT_KEY_COMMENT)
        return Path(str(key) + ".pub").read_text(encoding="ascii", errors="replace")

    def _stage_seed(self, name: str, slot: int) -> None:
        """Make the sandbox's host key pair, and write the seed's three files and its known_hosts file. Nothing goes to
        the guest yet."""
        client_public = self._client_public_key()
        staging = self._staging(name)
        host_key = staging / HOST_KEY_FILE
        self._run_keygen(host_key, name)
        host_public = Path(str(host_key) + ".pub").read_text(encoding="ascii", errors="replace")
        host_private = host_key.read_bytes().decode("ascii", errors="replace")
        seed = staging / SEED_DIR
        write_lf(seed / "meta-data", meta_data(name))
        write_lf(seed / "network-config", network_config(slot))
        write_lf(seed / "user-data", user_data(client_public, host_public, host_private,
                                                  guest_sudo=self.guest_sudo))
        write_lf(Path(self._known_hosts(name)), known_hosts_line(host_public, guest_address(slot)) + "\n")

    def _wait_ready(self, name: str) -> None:
        """Wait until the guest's SSH answers. Every try is strict about the pinned host key, so a guest that presents
        any other key is never trusted."""
        deadline = time.monotonic() + self.ready_seconds
        while True:
            try:
                code, _, _ = self._ssh(name, "true", timeout=15)
                if code == 0:
                    return
            except SandboxError:
                pass  # not up yet; the deadline below decides when to give up
            if time.monotonic() >= deadline:
                raise SandboxError("The sandbox didn't start in time.")
            self.sleep(3)

    def _release_seed(self, name: str) -> None:
        """Once the guest is up: detach the seed disk and delete it, then wipe the host's copies of the private key (the
        staged host key and the seed's user-data) and delete the staged files. The known_hosts file stays, since every
        later connection checks it."""
        try:
            self._ps(release_seed_script(name, self._seed(name)))
        finally:
            self._wipe_staged_keys(name)  # the copies go even if the seed disk couldn't be deleted
        self._remove_staging(name)

    def _wipe_staged_keys(self, name: str) -> None:
        """Overwrite with zeros, then delete, each staged file that holds a copy of the host's private key. Best effort:
        a file that can't be overwritten is still deleted, and one that can't be deleted is left for _remove_staging,
        which reports it."""
        staging = self._staging(name)
        for path in (staging / HOST_KEY_FILE, staging / SEED_DIR / "user-data"):
            try:
                shred_file(path)
            except (SandboxError, OSError):
                log.warning("Couldn't overwrite a staged sandbox file before deleting it", exc_info=True)
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.warning("Couldn't delete a staged sandbox file yet", exc_info=True)

    def _remove_staging(self, name: str) -> None:
        folder = self._staging(name)
        try:
            if folder.exists():
                shutil.rmtree(folder)
        except OSError as exc:
            raise SandboxError(f"Couldn't delete the sandbox's staged files: {exc}") from exc

    def _destroy_name(self, name: str) -> None:
        try:
            self._ps(destroy_script(name, self._child(name), self._seed(name), self.vm_dir / check_name(name)))
        finally:
            try:
                self._wipe_staged_keys(name)  # the staged files hold a private key: wiped even if the VM didn't go
                self._remove_staging(name)
            finally:
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
            child, seed = self._child(name), self._seed(name)
            self.disks_dir.mkdir(parents=True, exist_ok=True)  # made by the app itself, so the app owns it
            self.vm_dir.mkdir(parents=True, exist_ok=True)
            self._stage_seed(name, slot)
            self._ps(new_vhd_script(child, self.base_image, self.disk_gb))
            self._ps(new_vm_script(name, child, self.switch, self.memory_mb, self.cpus, self.vm_dir))
            self._ps(isolation_script(name, slot))
            check_adapters(self._ps(adapters_script(name)), self.switch)
            check_vm_vlan(self._ps(vm_vlan_script(name)))  # before the start: a wrong VLAN destroys the VM unstarted
            self._ps(seed_disk_script(seed, self._staging(name)))
            self._ps(attach_seed_script(name, seed))
            self._ps(start_script(name))
            check_vm_vlan(self._ps(vm_vlan_script(name)))  # and again after the start, the read-back of the running VM
            self._wait_ready(name)
            self._ssh(name, CLOUD_INIT_WAIT, timeout=180)
            self._release_seed(name)
        except Exception as exc:
            try:
                self._destroy_name(name)  # don't leave a half-made VM, or its key files, behind
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

    def disk_bytes(self, handle: SandboxHandle) -> int | None:
        """How big the sandbox's own disk file has grown (a differencing disk grows as the guest writes)."""
        out = self._ps("$ErrorActionPreference = 'Stop'\n"
                       f"(Get-Item -LiteralPath {ps_quote(check_path(self._child(handle.name)))}).Length").strip()
        return int(out) if out.isdigit() else None

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
                settings.sandbox_proxy_port, settings.sandbox_guest_sudo)
    return ()


def make_backend(settings: Settings, kind: str) -> SandboxBackend:
    """The backend for a code_sandbox value, built from the current settings (see SandboxManager.backend)."""
    if kind == "fake":
        if not fake_sandbox_allowed():
            raise SandboxRefused("The Fake sandbox is only for testing. Choose the Hyper-V sandbox or turn sandboxes "
                                 "off in Settings.")
        return FakeSandbox(settings.data_dir / "sandbox-fake")
    if kind == "hyperv":
        # The app's client key and every sandbox's host key are made on the host, on first use (see HyperVSandbox)
        keys = settings.data_dir / "sandbox"
        return HyperVSandbox(base_image=settings.sandbox_base_image, switch=settings.sandbox_switch,
                             memory_mb=settings.sandbox_memory_mb, cpus=settings.sandbox_cpus,
                             disk_gb=settings.sandbox_disk_gb, ssh_key=str(keys / "id_ed25519"), disks_dir=str(keys / "disks"),
                             known_dir=str(keys / "known_hosts"), staging_dir=str(keys / "staging"),
                             guest_sudo=settings.sandbox_guest_sudo, proxy_port=settings.sandbox_proxy_port)
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

    def is_running(self, name: str) -> bool:
        return self.db.one("SELECT 1 AS found FROM sandbox_sessions WHERE name = ? AND state = 'running' LIMIT 1",
                           (name,)) is not None

    def live_for_session(self, code_session_id: int):
        """The session's sandbox that is running and can take commands, if it has one."""
        return self.db.one("SELECT * FROM sandbox_sessions WHERE code_session_id = ? AND state = 'running' "
                           "ORDER BY id DESC LIMIT 1", (code_session_id,))

    def alive_count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM sandbox_sessions WHERE state NOT IN ('destroyed', 'failed')")["n"]

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
                 backend_factory: Callable[[str], SandboxBackend] | None = None, *,
                 tokens: TokenStore | None = None, egress: EgressProxy | None = None):
        self.settings = settings
        self.db = db
        self.store = SandboxStore(db)
        self.backend_factory = backend_factory or (lambda kind: make_backend(settings, kind))
        self.lock = threading.RLock()
        self.tokens = tokens or TokenStore()  # the proxy checks these; the proxy itself shares this store
        self.egress = egress  # the proxy, started by the app for Hyper-V sandboxes (None in tests that don't need it)
        self._backends: dict[str, tuple[tuple, SandboxBackend]] = {}
        self._session_locks: dict[int, list] = {}  # code session -> [RLock, threads inside or waiting]
        self._state_lock = threading.Lock()  # guards _backends and _session_locks only
        # Proxy tokens, in memory only. _secrets holds each running sandbox's token; _recent keeps the last few, so
        # output from a command still running when its sandbox was deleted is redacted too.
        self._token_lock = threading.Lock()
        self._secrets: dict[str, str] = {}
        self._recent: deque[tuple[str, str]] = deque(maxlen=RECENT_TOKENS)

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

    @contextmanager
    def session_lock(self, code_session_id: int) -> Iterator[None]:
        """Serializes the sandbox work of one Code session (making its sandbox and copying the repository in), so two
        commands can't each make one. Other sessions are not held up by it. The entry counts the threads inside or
        waiting, so _forget_session_lock never drops a lock that is in use."""
        with self._state_lock:
            entry = self._session_locks.setdefault(code_session_id, [threading.RLock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._state_lock:
                entry[1] -= 1

    def _forget_session_lock(self, code_session_id: int) -> None:
        """Drop a Code session's lock entry once its sandbox is gone, so the table doesn't grow with every session.
        A lock still in use is kept, and a later session_lock call makes a new one."""
        with self._state_lock:
            entry = self._session_locks.get(code_session_id)
            if entry is not None and entry[1] == 0:
                del self._session_locks[code_session_id]

    def kinds_in_use(self) -> set[str]:
        kinds = self.store.kinds_in_use()
        if self.enabled:
            kinds.add(self.settings.code_sandbox)
        return kinds

    def handle_of(self, row) -> SandboxHandle:
        return SandboxHandle(row["name"], row["backend"], row["created_at"], row["expires_at"])

    def proxy_ready(self) -> bool:
        return self.egress is not None and self.egress.running

    def proxy_problem(self) -> str:
        """Why sandboxes can't be made because the egress proxy isn't running, in plain words."""
        reason = self.egress.error if self.egress is not None and self.egress.error else "It hasn't started"
        return (f"The sandbox's internet proxy isn't running, so no sandbox can be made. {reason.rstrip('.')}. "
                "Restart SubZero AI after changing sandbox settings, then try again.")

    # ---- proxy tokens (see egress_proxy.py)
    def _issue_locked(self, handle: SandboxHandle) -> str:
        """A new token for a sandbox, kept in memory for its commands. Caller holds _token_lock."""
        if not handle.expires_at:
            raise SandboxError("That sandbox has no time limit recorded.")
        token = self.tokens.issue(handle.name, datetime.fromisoformat(handle.expires_at))
        self._secrets[handle.name] = token
        self._recent.append((handle.name, token))
        return token

    def _token_for(self, handle: SandboxHandle) -> str:
        """The sandbox's token. One that was lost (the app restarted) is issued again, but only while the sandbox is
        still running."""
        with self._token_lock:
            token = self._secrets.get(handle.name)
            if token is not None:
                return token
            if not self.store.is_running(handle.name):
                raise SandboxError("That sandbox is no longer running.")
            return self._issue_locked(handle)

    def _revoke(self, name: str) -> None:
        """Refuse the sandbox at the proxy from now on, and cut off its open connections."""
        self.tokens.revoke(name)
        with self._token_lock:
            self._secrets.pop(name, None)
        if self.egress is not None:
            self.egress.close_sandbox(name)

    def command_env(self, handle: SandboxHandle) -> dict[str, str]:
        """The extra environment for a command in this sandbox: its proxy login and the proxy settings. It is passed
        only with the command, never written to the guest's disk."""
        token = self._token_for(handle)
        host, port = self.egress.address if self.egress is not None else (self.settings.sandbox_host_address,
                                                                          self.settings.sandbox_proxy_port)
        return proxy_env(handle.name, token, host, port)

    def redact(self, text: str) -> str:
        """Replace every proxy token this app issued recently (in the plain or the Basic-login form) with [hidden]."""
        with self._token_lock:
            recent = list(self._recent)
        for name, token in recent:
            if token in text:
                text = text.replace(token, REDACTED)
            basic = base64.b64encode(f"{name}:{token}".encode()).decode()
            if basic in text:
                text = text.replace(basic, REDACTED)
        return text

    def live_for_session(self, code_session_id: int):
        return self.store.live_for_session(code_session_id)

    def free_gb(self) -> float:
        anchor = Path(self.settings.sandbox_base_image) if self.settings.sandbox_base_image else self.settings.data_dir
        while not anchor.exists() and anchor.parent != anchor:
            anchor = anchor.parent
        return shutil.disk_usage(anchor).free / (1024 ** 3)

    def check_room(self) -> None:
        """Before a command in a real sandbox: refuse when the drive is nearly full (below half the free-space floor)."""
        if self.settings.code_sandbox == "hyperv" and self.free_gb() < self.settings.sandbox_min_free_gb / 2:
            raise SandboxRefused("The computer is nearly out of disk space, so commands are paused. Ask an admin to "
                                 "free some space.")

    def check_capacity(self) -> None:
        """Refuse a new sandbox when the limit is reached, the disk is low, or the base image isn't set."""
        if self.settings.code_sandbox == "hyperv" and not self.settings.sandbox_base_image:
            raise SandboxRefused("Set the sandbox base image in Settings before Code can use sandboxes.")
        if self.settings.code_sandbox == "hyperv" and not self.proxy_ready():  # fail closed: no proxy, no sandbox
            raise SandboxRefused(self.proxy_problem())
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
            made = self.backend(kind).create(code_session_id)
            handle = replace(made, expires_at=expires.isoformat(timespec="seconds"))
        except SandboxError as exc:
            self.store.update(row_id, state="failed", note=str(exc)[:300])
            raise
        except Exception as exc:
            self.store.update(row_id, state="failed", note=str(exc)[:300])
            raise SandboxError(f"The sandbox couldn't be made: {exc}") from exc
        with self.lock:
            recorded = self.store.start(row_id, handle.name)
            if recorded:
                with self._token_lock:
                    self._issue_locked(handle)  # the sandbox's commands get this token from now on
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
        return handle

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
                    self._revoke(row["name"])  # refused at the proxy even if the delete below fails
                    self.backend(row["backend"]).destroy(handle)
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                self.store.update(row["id"], state="failed", note=message[:300])
                raise SandboxError(f"The sandbox couldn't be deleted: {message}") from exc
            self.store.update(row["id"], state="destroyed", destroyed_at=utcnow(), note=reason[:300])
            self._forget_session_lock(row["code_session_id"])
            self.db.audit("sandbox_destroyed", actor_id=actor_id, target_type="sandbox", target_id=row["id"],
                          details={"name": row["name"], "backend": row["backend"], "reason": reason})

    def extend_for_session(self, code_session_id: int, *, actor_id: int | None) -> str:
        """Give the session's running sandbox another sandbox_ttl_hours, up to EXTEND_LIMIT after it was made. Its
        proxy token expires at the same time. Returns the new expiry (ISO text)."""
        with self.lock:
            row = self.live_for_session(code_session_id)
            if row is None:
                raise SandboxError("There's no sandbox to extend.")
            now = datetime.now(timezone.utc)
            expires = datetime.fromisoformat(row["expires_at"])
            cap = datetime.fromisoformat(row["created_at"]) + EXTEND_LIMIT
            new = min(max(expires, now) + timedelta(hours=self.settings.sandbox_ttl_hours), cap)
            if new <= expires:
                raise SandboxError("This sandbox has reached its longest time, so it can't be extended. Its files "
                                   "are deleted when it ends.")
            stamp = new.isoformat(timespec="seconds")
            self.store.update(row["id"], expires_at=stamp)
            self.tokens.extend(row["name"], new)
            self.db.audit("sandbox_extended", actor_id=actor_id, target_type="sandbox", target_id=row["id"],
                          details={"name": row["name"], "expires_at": stamp})
            return stamp

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
            _reap_oversize(manager, report)
    except Exception:
        log.exception("Sandbox cleanup failed")
    return report


def watch_sandbox_disks(services: Any) -> dict[str, int]:
    """The quick check, run every minute: only the disk checks, not the full cleanup. Never raises."""
    report = {"expired": 0, "orphans": 0, "failed": 0}
    manager: SandboxManager | None = getattr(services, "sandboxes", None)
    if manager is None:
        return report
    try:
        with manager.lock:
            _reap_oversize(manager, report)
    except Exception:
        log.exception("Sandbox disk check failed")
    return report


def _reap_oversize(manager: SandboxManager, report: dict[str, int]) -> None:
    """Delete a running sandbox whose disk has grown past sandbox_disk_cap_gb, so a few busy sandboxes can't fill the
    drive the app's database is on."""
    cap = int(getattr(manager.settings, "sandbox_disk_cap_gb", 0) or 0) * 1024 ** 3
    low = manager.settings.code_sandbox == "hyperv" and manager.free_gb() < manager.settings.sandbox_min_free_gb / 2
    if cap <= 0 and not low:
        return
    sizes = []
    for row in manager.store.db.all("SELECT * FROM sandbox_sessions WHERE state = 'running' AND name != ''"):
        try:
            size = getattr(manager.backend(row["backend"]), "disk_bytes", lambda handle: None)(manager.handle_of(row))
        except Exception:
            report["failed"] += 1
            log.warning("Couldn't read the disk size of sandbox %s", row["name"], exc_info=True)
            continue
        if size is not None:
            sizes.append((size, row))
    doomed = [(row, "disk limit reached") for size, row in sizes if cap > 0 and size > cap]
    if low and not doomed and sizes:  # the drive is nearly full: the biggest sandbox goes
        doomed = [(max(sizes, key=lambda pair: pair[0])[1], "the computer is nearly out of disk space")]
    for row, reason in doomed:
        try:
            manager.destroy_record(row, reason=reason)
        except Exception:
            report["failed"] += 1
            log.warning("Couldn't check or delete sandbox %s for its disk size", row["name"], exc_info=True)
            continue
        report["expired"] += 1
        manager.db.audit("sandbox_reaped", actor_id=None, target_type="sandbox", target_id=row["id"],
                         details={"name": row["name"], "reason": reason})


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
