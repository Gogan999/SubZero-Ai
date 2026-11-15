"""The sandbox base image's tools: what the Code engine needs inside every sandbox, how to check that a sandbox has
them, and the one-time admin step that bakes them into a new base disk.

Per-session installs would take minutes, so Node.js, Java 17, git, Python and the runner (`sandbox_runner/`, started
inside each sandbox as `node /opt/subzero-runner/runner.mjs`) live in the base disk. `check_tools` tells a session
whether its sandbox has them. `provision_image` (run by `python -m subzero_ai sandbox-provision`, from
prepare-sandbox-image.cmd) makes a NEW base disk with them and switches the `sandbox_base_image` setting to it:

  1. A temporary builder VM is made from the current base disk, as a differencing disk, with guest sudo for this one VM.
  2. The provisioning script (sandbox_provision.sh) and the runner folder are uploaded and the script is run, with its
     output streamed to the console and to a log file.
  3. The tools are checked inside the VM, then the script cleans the machine and powers it off.
  4. The VM's differencing disk becomes a standalone disk (Convert-VHD, which merges the old base into the copy), next
     to the old base. The old base is only ever read.
  5. The new disk gets the same permissions as the old base, and the setting points at it.
  6. The builder VM and its files are always removed, also on failure, and then the old base stays in use.

The builder can only reach the package sites through its own egress proxy, which runs inside the provisioning process
(not the app's proxy) and holds one token, for the builder only. That is why the app must be stopped while this runs:
the app's proxy owns the same host address and port. The extra package hosts are in PROVISION_HOSTS and are never part
of a session's allow-list.

Everything is injectable, so the tests drive it with FakeHost (tests/test_sandbox.py) and fakes; no PowerShell, SSH or
Hyper-V is used. Not yet run on a real PC: see docs/SANDBOX_IMAGE.md.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import saved_settings
from .config import Settings
from .db import Database
from .egress_proxy import EgressProxy, TokenStore, parse_hosts, parse_ports
from .sandbox import (
    GUEST_USER,
    REDACTED,
    FakeSandbox,
    HyperVSandbox,
    SandboxBackend,
    SandboxError,
    SandboxHandle,
    SandboxStore,
    check_name,
    check_path,
    child_disk_path,
    lf_text,
    ps_quote,
)

log = logging.getLogger("subzero.sandbox_image")

# Bump this whenever anything baked into the image changes (the script, a package list, the Node.js pin, the runner's
# or the Agent SDK's version). It is written into the image as /opt/subzero-runner/TOOLS_VERSION, it names the new base
# disk, and a sandbox whose number is lower is "out of date". A test fails when the pins below drift without a bump.
TOOLS_VERSION = 1
# The same Node.js the Windows build pins (packaging/build_windows.py NODE_VERSION; a test keeps them equal). The hash
# is the linux-x64 .tar.gz line of https://nodejs.org/dist/v<version>/SHASUMS256.txt for that version.
NODE_VERSION = "22.23.4"
NODE_LINUX_X64_SHA256 = "b8492284c76a54baf58d61668dd0e41ee0fc98adabf9503d30f05d08f55b4406"
# Sites only the builder may reach (all over HTTPS, port 443): the Ubuntu package archive and security mirrors, Node.js
# downloads, and the npm registry. Session sandboxes never get these; the builder's allow-list is the normal list plus
# these, in a proxy that only the provisioning run starts.
PROVISION_HOSTS = ("azure.archive.ubuntu.com", "archive.ubuntu.com", "security.ubuntu.com", "nodejs.org",
                   "registry.npmjs.org")
RUNNER_PATH = "/opt/subzero-runner/runner.mjs"
TOOLS_VERSION_PATH = "/opt/subzero-runner/TOOLS_VERSION"
PROVISION_SCRIPT = Path(__file__).resolve().parent / "sandbox_provision.sh"
RUNNER_SOURCE = Path(__file__).resolve().parent.parent / "sandbox_runner"  # the source tree, and the install's app folder
RUNNER_FILES = ("package.json", "package-lock.json", "runner.mjs")  # plus lib/; never node_modules or test/
IMAGE_NAME = "sandbox-base-{}.vhdx"  # next to the old base disk; later rebuilds of the same version add -r2, -r3
SIDECAR_SUFFIX = ".tools.json"  # written beside the new disk once it is verified: what it was built from
SERVICE_ACCOUNT = "NT SERVICE\\SubZeroAI"  # the account that runs the app; it only needs to read the base disk
PREPARE_COMMAND = "prepare-sandbox-image.cmd"
BUILDER_SESSION = "build"  # the builder VM is named szsandbox-build-s<slot>-<random>
BUILDER_PREFIX = "szsandbox-build-s"
NEED_FREE_GB = 15  # the builder's own disk grows by a few GB, and the new base disk is about as big again
TOKEN_HOURS = 3  # the builder's proxy login expires after this long, whatever happens
MIN_PROXY_MB = 4096  # the builder's byte allowance through its proxy (the normal one is for a session, not a build)
SCRIPT_SECONDS = 3600  # the install step may take this long
STEP_SECONDS = 120
SHUTDOWN_WAIT_SECONDS = 240
LOCK_STALE = timedelta(hours=6)
CURRENT_MESSAGE = "The sandbox image already has the current Code tools (version {}). Nothing to do. Use --force to build it again."
MISSING_MESSAGE = ("The sandbox image doesn't have the Code tools yet. Run prepare-sandbox-image.cmd on the server, "
                   "then try again.")
OUTDATED_MESSAGE = ("The sandbox image's Code tools are out of date. Run prepare-sandbox-image.cmd on the server, "
                    "then try again.")
PROXY_KEYS = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")


class ProvisionError(Exception):
    """A step of the provisioning failed. The message is plain enough to show an admin."""


# --------------------------------------------------------------------------- what the app expects
def _runner_package(root: Path | None = None) -> dict[str, Any]:
    path = Path(root or RUNNER_SOURCE) / "package.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SandboxError(f"Couldn't read the Code runner's package.json ({path}): {exc}") from exc


def expected_runner_version(root: Path | None = None) -> str:
    """The runner version this app ships, from sandbox_runner/package.json. It works from the source tree and from the
    Windows install, where sandbox_runner sits beside subzero_ai in the app folder."""
    return str(_runner_package(root).get("version") or "")


def expected_sdk_version(root: Path | None = None) -> str:
    """The Agent SDK version the runner pins (an exact version in package.json)."""
    spec = str(_runner_package(root).get("dependencies", {}).get("@anthropic-ai/claude-agent-sdk") or "")
    return spec.lstrip("^~=v ")


def provision_hosts(normal: str) -> tuple[str, ...]:
    """The builder's allow-list: the normal list from Settings, then PROVISION_HOSTS that aren't already in it."""
    rules = list(parse_hosts(normal))
    rules += [host for host in PROVISION_HOSTS if host not in rules]
    return tuple(rules)


# --------------------------------------------------------------------------- the check a session runs
@dataclass(frozen=True)
class ToolsStatus:
    state: str  # "ok", "missing" or "outdated"
    message: str  # plain words for an admin
    tools_version: int | None = None
    runner_version: str = ""
    sdk_version: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def _expected_ok() -> ToolsStatus:
    return ToolsStatus("ok", f"The sandbox image has the Code tools (version {TOOLS_VERSION}).", TOOLS_VERSION,
                       expected_runner_version(), expected_sdk_version())


def judge_tools(tools_version: int | None, runner: str, sdk: str) -> ToolsStatus:
    """ok, missing or outdated from what a sandbox reported. A version of None means the sandbox had none."""
    if tools_version is None or not runner:
        return ToolsStatus("missing", MISSING_MESSAGE)
    if tools_version < TOOLS_VERSION or runner != expected_runner_version() or sdk != expected_sdk_version():
        return ToolsStatus("outdated", OUTDATED_MESSAGE, tools_version, runner, sdk)
    return ToolsStatus("ok", _expected_ok().message, tools_version, runner, sdk)


def parse_runner_version(output: str) -> tuple[str, str]:
    """(runner_version, sdk_version) from the runner's `--version` line, or ('', '') if there isn't one."""
    for line in reversed((output or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                return "", ""
            return str(data.get("runner_version") or ""), str(data.get("sdk_version") or "")
    return "", ""


def check_tools(backend: SandboxBackend, handle: SandboxHandle) -> ToolsStatus:
    """Does this sandbox have the Code tools, at the version this app expects? Runs two quick commands in it. A
    sandbox that can't be reached raises SandboxError as usual; that isn't the same as missing tools. The fake sandbox
    answers from its `tools` marker ("ok" by default, "missing" or "outdated" to simulate an old image)."""
    simulated = getattr(backend, "tools", None)
    if isinstance(backend, FakeSandbox) or simulated is not None:
        if simulated == "missing":
            return ToolsStatus("missing", MISSING_MESSAGE)
        if simulated == "outdated":
            return ToolsStatus("outdated", OUTDATED_MESSAGE, TOOLS_VERSION - 1, expected_runner_version(),
                               expected_sdk_version())
        return _expected_ok()
    version = backend.run(handle, ["cat", TOOLS_VERSION_PATH], cwd="", env={}, timeout=60)
    text = version.output.strip()
    if version.exit_code != 0 or not text.isdigit():
        return ToolsStatus("missing", MISSING_MESSAGE)
    ran = backend.run(handle, ["node", RUNNER_PATH, "--version"], cwd="", env={}, timeout=60)
    runner, sdk = parse_runner_version(ran.output) if ran.exit_code == 0 else ("", "")
    return judge_tools(int(text), runner, sdk)


# --------------------------------------------------------------------------- files that go to the builder
def runner_archive(root: Path | None = None) -> bytes:
    """A zip of the runner's source files only (package.json, package-lock.json, runner.mjs and lib/). No node_modules,
    no tests: the lock file is installed inside the builder."""
    root = Path(root or RUNNER_SOURCE)
    paths = [root / name for name in RUNNER_FILES]
    lib = root / "lib"
    if lib.is_dir():
        paths += sorted(p for p in lib.rglob("*") if p.is_file() and not p.is_symlink())
    missing = [p.name for p in paths if not p.is_file()]
    if missing or not lib.is_dir():
        raise SandboxError(f"The Code runner's files are missing from {root}: {', '.join(missing) or 'lib'}.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            info = zipfile.ZipInfo(path.relative_to(root).as_posix())
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return buf.getvalue()


def script_text() -> str:
    return lf_text(PROVISION_SCRIPT.read_text(encoding="utf-8"))


def script_env(proxy_url: str) -> dict[str, str]:
    """The environment of the provisioning script: the pins, and the builder's proxy login (only the variables that
    apt, curl, npm and git read; not the Java options, which would make `java` print the login)."""
    return {"HTTPS_PROXY": proxy_url, "https_proxy": proxy_url, "NO_PROXY": "", "no_proxy": "",
            "TOOLS_VERSION": str(TOOLS_VERSION), "NODE_VERSION": NODE_VERSION, "NODE_SHA256": NODE_LINUX_X64_SHA256,
            "DEBIAN_FRONTEND": "noninteractive"}


# --------------------------------------------------------------------------- PowerShell for the disk steps
def vm_state_script(name: str) -> str:
    return ("$ErrorActionPreference = 'Stop'\n"
            f"$vm = Get-VM -Name {ps_quote(check_name(name))} -ErrorAction SilentlyContinue\n"
            "if ($vm) { [string]$vm.State } else { 'Gone' }")


def convert_script(child: Path, temp: Path, vm_name: str) -> str:
    """Turn the builder's differencing disk into a standalone dynamic disk (Convert-VHD copies the whole chain, so the
    old base is only read). The VM must be off. The new disk is named *.building.vhdx until it is checked."""
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$src = {ps_quote(check_path(child))}",
        f"$dst = {ps_quote(check_path(temp))}",
        "if (Test-Path -LiteralPath $dst) { throw 'The new disk file is already there, so nothing was written.' }",
        f"$vm = Get-VM -Name {ps_quote(check_name(vm_name))} -ErrorAction SilentlyContinue",
        "if ($vm -and $vm.State -ne 'Off') { throw 'The builder VM is not turned off yet, so its disk was not copied.' }",
        "Convert-VHD -Path $src -DestinationPath $dst -VHDType Dynamic -BlockSizeBytes 1MB",
        "$out = Get-VHD -Path $dst",
        "if ($out.ParentPath) { throw 'The new disk still depends on another disk, so it was not used.' }",
        "if ([string]$out.VhdType -ne 'Dynamic') { throw 'The new disk is not a dynamic disk, so it was not used.' }",
    ])


def finalize_script(temp: Path, final: Path) -> str:
    """Name the new disk, make it read-only and give it the permissions the plain base disk was given when it was built (SYSTEM and
    Administrators full control, the app's service and the virtual machines' group read only), then save its SHA-256."""
    src, dst = ps_quote(check_path(temp)), ps_quote(check_path(final))
    sums = ps_quote(check_path(f"{final}.sha256"))
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"if (Test-Path -LiteralPath {dst}) {{ throw 'The new base disk name is already used, so nothing was replaced.' }}",
        f"Move-Item -LiteralPath {src} -Destination {dst}",
        f"Set-ItemProperty -LiteralPath {dst} -Name IsReadOnly -Value $true",
        f"icacls.exe {dst} /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null",
        "if ($LASTEXITCODE -ne 0) { throw \"icacls couldn't limit access to the new base disk (exit code $LASTEXITCODE).\" }",
        "$ErrorActionPreference = 'Continue'  # the app's service account may not exist, which is not an error",
        f"icacls.exe {dst} /grant {ps_quote(SERVICE_ACCOUNT + ':(R)')} 2>&1 | Out-Null",
        "$ErrorActionPreference = 'Stop'",
        f"icacls.exe {dst} /grant '*S-1-5-83-0:(R)' | Out-Null",
        "if ($LASTEXITCODE -ne 0) { throw \"icacls couldn't give virtual machines read access (exit code $LASTEXITCODE).\" }",
        f"$hash = (Get-FileHash -LiteralPath {dst} -Algorithm SHA256).Hash",
        f"Set-Content -LiteralPath {sums} -Value $hash -Encoding ASCII",
        "$hash",
    ])


def remove_new_disk_script(paths: list[Path], keep: Path) -> str:
    """Delete the files of a new disk that was not switched to. The current base disk is never named."""
    lines = ["$ErrorActionPreference = 'Stop'"]
    for path in paths:
        if Path(path) == Path(keep):
            raise SandboxError("The cleanup would have removed the current base disk, so it wasn't run.")
        lines.append(f"if (Test-Path -LiteralPath {ps_quote(check_path(path))}) {{ "
                     f"Remove-Item -LiteralPath {ps_quote(check_path(path))} -Force }}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- logs, locks and the proxy
class LineSink:
    """Passes a command's output on one whole line at a time, with the proxy login hidden. Whole lines, because a login
    could otherwise be split across two chunks and slip past the redaction."""

    def __init__(self, emit: Callable[[str], None], secrets: Callable[[], list[str]]):
        self.emit, self.secrets, self.buffer = emit, secrets, ""

    def redact(self, text: str) -> str:
        for secret in self.secrets():
            if secret:
                text = text.replace(secret, REDACTED)
        return text

    def feed(self, chunk: str) -> None:
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split("\n")
        for line in lines:
            self.emit(self.redact(line.rstrip("\r")))

    def close(self) -> None:
        if self.buffer:
            self.emit(self.redact(self.buffer.rstrip("\r")))
            self.buffer = ""


class RunLock:
    """One provisioning run at a time: a lock file made with O_EXCL. A lock older than LOCK_STALE was left by a crash."""

    def __init__(self, path: Path, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if self._stale():
                    self.path.unlink(missing_ok=True)
                    continue
                raise ProvisionError("Another sandbox image run is already going. Wait for it to finish (it takes "
                                     "about 20 minutes), then try again.") from None
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "started": self.clock().isoformat(timespec="seconds")}, handle)
            self.held = True
            return
        raise ProvisionError("The sandbox image lock couldn't be taken. Delete the file " + str(self.path) + " and try again.")

    def _stale(self) -> bool:
        try:
            started = datetime.fromisoformat(json.loads(self.path.read_text(encoding="utf-8"))["started"])
        except (OSError, ValueError, KeyError, TypeError):
            return True  # unreadable: nobody is writing it any more
        return self.clock() - started > LOCK_STALE

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


class ProxyThread:
    """Runs an EgressProxy on its own event loop in a background thread, so the (blocking) provisioning code can use it."""

    def __init__(self, proxy: EgressProxy):
        self.proxy = proxy
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self.proxy.address

    def start(self) -> None:
        ready = threading.Event()
        outcome: dict[str, Any] = {}

        def main() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                outcome["ok"] = loop.run_until_complete(self.proxy.start())
            except Exception as exc:
                outcome["ok"], outcome["error"] = False, str(exc)
            ready.set()
            if outcome["ok"]:
                loop.run_forever()
            loop.close()

        self._thread = threading.Thread(target=main, name="sandbox-image-proxy", daemon=True)
        self._thread.start()
        if not ready.wait(30) or not outcome.get("ok"):
            reason = outcome.get("error") or self.proxy.error or "It didn't start."
            raise ProvisionError(f"The builder's internet proxy couldn't start. {reason.rstrip('.')}. If SubZero AI is "
                                 "still running, stop it first (prepare-sandbox-image.cmd does that for you).")

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            if self.proxy.running:
                asyncio.run_coroutine_threadsafe(self.proxy.stop(), loop).result(20)
        except Exception:
            log.warning("The builder's proxy didn't stop cleanly", exc_info=True)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(10)
            self._loop = self._thread = None


def make_proxy(settings: Settings, tokens: TokenStore, db: Database | None) -> ProxyThread:
    """The builder's own proxy: the same host address and port as the app's (so the app must be stopped), the normal
    allow-list plus PROVISION_HOSTS, and only the builder's token. Fixed rules: it doesn't follow Settings."""
    try:
        rules = provision_hosts(settings.sandbox_allowed_hosts)
        ports = parse_ports(settings.sandbox_allowed_ports)
    except ValueError as exc:
        raise ProvisionError(f"The sandbox's allowed websites or ports in Settings are wrong: {exc}") from exc
    mb = max(int(settings.sandbox_proxy_mb_limit), MIN_PROXY_MB)
    proxy = EgressProxy(tokens=tokens, db=db, host=settings.sandbox_host_address, port=settings.sandbox_proxy_port,
                        allowed_hosts=rules, allowed_ports=ports, byte_limit=mb * 1024 * 1024)
    return ProxyThread(proxy)


def app_answers(port: int) -> bool:
    """Is SubZero AI running on this computer? (Its /healthz answers.)"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2):
            return True
    except Exception:
        return False


# --------------------------------------------------------------------------- the builder backend
def work_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / "sandbox" / "provision"


def make_builder(settings: Settings) -> HyperVSandbox:
    """A backend for the builder VM only. It has its own key, known_hosts, staging and disks folders (so it never touches
    the app's client key or its file permissions), and guest sudo on. A session's backend is never changed."""
    work = work_dir(settings)
    return HyperVSandbox(base_image=settings.sandbox_base_image, switch=settings.sandbox_switch,
                         memory_mb=settings.sandbox_memory_mb, cpus=settings.sandbox_cpus,
                         disk_gb=settings.sandbox_disk_gb, ssh_key=str(work / "keys" / "id_ed25519"),
                         disks_dir=str(work / "disks"), known_dir=str(work / "known_hosts"),
                         staging_dir=str(work / "staging"), guest_sudo=True, proxy_port=settings.sandbox_proxy_port)


# --------------------------------------------------------------------------- what is on disk
def sidecar_path(image: str | Path) -> Path:
    return Path(f"{image}{SIDECAR_SUFFIX}")


def image_is_current(image: str | Path) -> bool:
    """Is this base disk one this app built with the current tools: its record exists and names the current tools
    version, runner version and SDK version?"""
    if not image or not Path(image).is_file():
        return False
    try:
        info = json.loads(sidecar_path(image).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(info, dict) and info.get("tools_version") == TOOLS_VERSION
            and info.get("runner_version") == expected_runner_version()
            and info.get("sdk_version") == expected_sdk_version()
            and info.get("node_version") == NODE_VERSION)


def new_image_path(old: str | Path) -> Path:
    """Where the new base disk goes: next to the old one, named for the tools version, never an existing file."""
    folder = Path(old).parent
    path = folder / IMAGE_NAME.format(TOOLS_VERSION)
    number = 2
    while path.exists() or sidecar_path(path).exists():
        path = folder / IMAGE_NAME.format(f"{TOOLS_VERSION}-r{number}")
        number += 1
    return path


# --------------------------------------------------------------------------- provisioning
@dataclass
class ProvisionResult:
    status: str  # "built", "current" or "failed"
    message: str
    path: str = ""
    log_path: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.status in ("built", "current") else 1


@dataclass
class _Run:
    """What one run has made so far, so cleanup knows what to remove."""
    handle: SandboxHandle | None = None
    token: str = ""
    new_files: list[Path] = field(default_factory=list)
    committed: bool = False
    stage: str = "starting"


def provision_image(settings: Settings, db: Database, *, force: bool = False, say: Callable[[str], None] = print,
                    builder: HyperVSandbox | None = None, proxy_factory: Callable[[TokenStore], Any] | None = None,
                    app_running: Callable[[], bool] | None = None, free_gb: Callable[[Path], float] | None = None,
                    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], datetime] | None = None
                    ) -> ProvisionResult:
    """Make a new base disk with the Code tools and switch the setting to it. Never raises for a failed step: the
    result says what went wrong, the builder VM and its files are gone, and the old base disk and setting are as they
    were. Returns at once, with status "current", when the image is already current (unless force)."""
    now = clock or (lambda: datetime.now(timezone.utc))
    stamp = now().strftime("%Y%m%d-%H%M%S")
    log_dir = Path(settings.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"sandbox-image-{stamp}.log"
    logfile = open(log_path, "a", encoding="utf-8")
    run = _Run()
    secrets: list[str] = []
    lock = RunLock(Path(settings.data_dir) / "sandbox" / "provision.lock", now)
    proxy = None
    tokens: TokenStore | None = None
    runner = Runner(settings, db, say=say, logfile=logfile, secrets=secrets, sleep=sleep)
    old = settings.sandbox_base_image
    try:
        if not old or not Path(old).is_file():
            raise ProvisionError("There is no sandbox base disk to build from. Set up the base disk first "
                                 "(docs/SETTINGS.md, Sandbox base image), then run this again.")
        if not force and image_is_current(old):
            message = CURRENT_MESSAGE.format(TOOLS_VERSION)
            runner.say(message)
            return ProvisionResult("current", message, old, str(log_path))
        if app_running is not None and app_running():
            raise ProvisionError("SubZero AI is running. Stop it first (prepare-sandbox-image.cmd does this for you), "
                                 "because the builder uses the same internet proxy address.")
        busy = SandboxStore(db).alive_count()
        if busy:
            raise ProvisionError(f"{busy} sandbox{'es are' if busy != 1 else ' is'} still in use. Wait until the Code "
                                 "sessions have finished and their sandboxes are deleted, then try again.")
        lock.acquire()
        run.stage = "checking the computer"
        builder = builder or make_builder(settings)
        runner.check_ready(builder, Path(old), free_gb)
        tokens = TokenStore()
        proxy = (proxy_factory(tokens) if proxy_factory else make_proxy(settings, tokens, db))
        path = runner.build(builder, proxy, tokens, run, Path(old), now, force)
        message = (f"The sandbox image is ready: {path}. SubZero AI will use it for new sandboxes. "
                   f"The old base disk, {old}, was kept.")
        runner.say(message)
        return ProvisionResult("built", message, str(path), str(log_path))
    except (ProvisionError, SandboxError) as exc:
        reason = runner.clean_text(str(exc))
        stage = run.stage
        runner.say(f"Stopped while {stage}: {reason}")
        _audit_failure(db, stage, reason)
        return ProvisionResult("failed", f"The sandbox image wasn't prepared: {reason} The old base disk is still in use.",
                               "", str(log_path))
    except Exception as exc:
        reason = runner.clean_text(str(exc) or type(exc).__name__)
        # No traceback: it could hold the ssh command line
        log.error("Preparing the sandbox image failed (%s): %s", type(exc).__name__, reason)
        runner.say(f"Stopped while {run.stage}: {reason}")
        _audit_failure(db, run.stage, reason)
        return ProvisionResult("failed", f"The sandbox image wasn't prepared: {reason} The old base disk is still in use.",
                               "", str(log_path))
    finally:
        runner.cleanup(builder, proxy, tokens, run, Path(old) if old else None, own=lock.held)
        lock.release()
        logfile.close()


def _audit_failure(db: Database, stage: str, reason: str) -> None:
    try:
        db.audit("sandbox_image_failed", actor_id=None, target_type="sandbox_image",
                 details={"stage": stage, "error": reason[:300], "tools_version": TOOLS_VERSION})
    except Exception:
        log.warning("Couldn't write the audit entry for a failed sandbox image run", exc_info=True)


class Runner:
    """The steps of one provisioning run. Kept apart from provision_image so that function stays a readable outline."""

    def __init__(self, settings: Settings, db: Database, *, say: Callable[[str], None], logfile, secrets: list[str],
                 sleep: Callable[[float], None]):
        self.settings, self.db, self._say, self.logfile, self.secrets, self.sleep = settings, db, say, logfile, secrets, sleep

    # ---- output
    def clean_text(self, text: str) -> str:
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, REDACTED)
        return text

    def say(self, text: str) -> None:
        line = self.clean_text(text)
        self._say(line)
        self._log(line)

    def step(self, text: str) -> None:
        self.say("")
        self.say(f"== {text}")

    def _log(self, line: str) -> None:
        try:
            self.logfile.write(f"{datetime.now(timezone.utc):%H:%M:%S} {line}\n")
            self.logfile.flush()
        except (OSError, ValueError):
            pass

    def sink(self) -> LineSink:
        return LineSink(self.say, lambda: list(self.secrets))

    # ---- steps
    def check_ready(self, builder: HyperVSandbox, old: Path, free_gb: Callable[[Path], float] | None) -> None:
        self.step("Checking the sandbox network and the disk")
        builder.check_network()  # raises SandboxRefused (a SandboxError) in plain words
        free = (free_gb or (lambda p: shutil.disk_usage(p).free / 1024 ** 3))(old.parent)
        if free < NEED_FREE_GB:
            raise ProvisionError(f"There is {free:.0f} GB free next to the base disk and this needs {NEED_FREE_GB} GB. "
                                 "Free some space and try again.")
        self.say(f"{free:.0f} GB free. The sandbox network is ready.")
        for name in builder.list_existing():  # a builder left by a run that was cut off (power loss, closed window)
            if name.startswith(BUILDER_PREFIX):
                self.say(f"Removing a builder VM left by an earlier run ({name}).")
                builder.destroy(SandboxHandle(name, builder.name, ""))

    def build(self, builder: HyperVSandbox, proxy: Any, tokens: TokenStore, run: _Run, old: Path, now, force: bool) -> Path:
        ps = builder.run_powershell
        run.stage = "starting the internet proxy for the builder"
        self.step("Starting the builder's internet proxy")
        proxy.start()
        host, port = proxy.address
        self.say(f"The builder may reach: {', '.join(provision_hosts(self.settings.sandbox_allowed_hosts))} "
                 "(HTTPS only).")

        run.stage = "making the builder VM"
        self.step("Making the builder VM from the current base disk (about two minutes)")
        run.handle = builder.create(BUILDER_SESSION)
        handle = run.handle
        self.say(f"Builder VM {handle.name} is up (sudo is on for this VM only).")
        run.token = tokens.issue(handle.name, now() + timedelta(hours=TOKEN_HOURS))
        self.secrets += [run.token, _basic(handle.name, run.token)]
        proxy_url = f"http://{handle.name}:{run.token}@{host}:{port}"
        self.secrets.append(proxy_url)

        run.stage = "uploading the provisioning script and the runner"
        self.step("Uploading the provisioning script and the runner")
        builder.put_file(handle, "provision/provision.sh", script_text().encode("utf-8"))
        builder.put_archive(handle, runner_archive(), dest="provision/runner")

        run.stage = "installing the tools in the builder VM"
        self.step("Installing the tools (this is the long part: about 10 to 15 minutes)")
        self.run_script(builder, handle, "install", script_env(proxy_url), SCRIPT_SECONDS)

        run.stage = "checking the tools in the builder VM"
        self.step("Checking the tools")
        self.verify(builder, handle)

        run.stage = "cleaning and shutting down the builder VM"
        self.step("Cleaning the machine and shutting it down")
        self.run_script(builder, handle, "finish", {"DEBIAN_FRONTEND": "noninteractive"}, STEP_SECONDS * 2)
        self.wait_off(builder, handle)

        run.stage = "copying the builder's disk into a new base disk"
        self.step("Making the new base disk (a few minutes)")
        final = new_image_path(old)
        temp = final.with_name(final.stem + ".building.vhdx")
        run.new_files = [temp, final, Path(f"{final}.sha256"), sidecar_path(final)]
        child = child_disk_path(builder.base_image, handle.name, builder.disks_dir)
        ps(convert_script(child, temp, handle.name))
        run.stage = "setting the new base disk's permissions"
        self.step("Giving the new base disk the same permissions as the old one")
        digest = ps(finalize_script(temp, final)).strip().splitlines()
        sha = digest[-1].strip() if digest else ""
        self.say(f"New base disk: {final}")
        self.say(f"SHA-256: {sha}")
        info = {"tools_version": TOOLS_VERSION, "runner_version": expected_runner_version(),
                "sdk_version": expected_sdk_version(), "node_version": NODE_VERSION, "sha256": sha,
                "built_from": old.name, "built_at": now().isoformat(timespec="seconds")}
        sidecar_path(final).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")

        run.stage = "switching the sandbox base disk setting"
        self.step("Switching the sandbox base disk setting")
        saved_settings.save(self.db, self.settings, {"sandbox_base_image": str(final)}, None)
        run.committed = True
        self.db.audit("sandbox_image_prepared", actor_id=None, target_type="sandbox_image",
                      details={"path": str(final), "previous": str(old), "forced": bool(force), **info})
        return final

    def run_script(self, builder: HyperVSandbox, handle: SandboxHandle, mode: str, env: dict[str, str],
                   timeout: int) -> None:
        sink = self.sink()
        try:
            result = builder.run(handle, ["sudo", "-E", "bash", "provision.sh", mode], cwd="provision", env=env,
                                 timeout=timeout, on_output=sink.feed)
        finally:
            sink.close()
        if result.timed_out:
            raise ProvisionError(f"The provisioning step '{mode}' took too long and was stopped.")
        if result.exit_code != 0:
            raise ProvisionError(f"The provisioning step '{mode}' failed (exit code {result.exit_code}). The last lines "
                                 f"of its output are above and in the log.")

    def verify(self, builder: HyperVSandbox, handle: SandboxHandle) -> None:
        checks = (("node", ["node", "--version"], f"v{NODE_VERSION}"), ("java", ["java", "-version"], "17."),
                  ("git", ["git", "--version"], "git version"), ("python3", ["python3", "--version"], "Python 3"))
        for label, argv, expect in checks:
            result = builder.run(handle, argv, cwd="", env={}, timeout=60)
            output = self.clean_text(result.output.strip())
            if result.exit_code != 0 or expect not in output:
                raise ProvisionError(f"{label} is missing or the wrong version in the builder VM "
                                     f"(expected {expect}, got: {output[-120:] or 'nothing'}).")
            self.say(f"{label}: {output.splitlines()[0] if output else ''}")
        status = check_tools(builder, handle)
        if not status.ok:
            raise ProvisionError(f"The runner didn't check out in the builder VM: {status.message}")
        self.say(f"runner {status.runner_version}, Agent SDK {status.sdk_version}, tools version {status.tools_version}")

    def wait_off(self, builder: HyperVSandbox, handle: SandboxHandle) -> None:
        for _ in range(SHUTDOWN_WAIT_SECONDS // 3):
            state = builder.run_powershell(vm_state_script(handle.name)).strip()
            if state == "Off":
                self.say("The builder VM shut down cleanly.")
                return
            self.sleep(3)
        raise ProvisionError("The builder VM didn't shut down on its own, so its disk wasn't copied.")

    def cleanup(self, builder: HyperVSandbox | None, proxy: Any, tokens: TokenStore | None, run: _Run,
                old: Path | None, *, own: bool) -> None:
        """Always: stop the proxy, forget the token, delete the builder VM and its files, and (when the setting wasn't
        switched) any new disk files. Each part is tried even if an earlier one fails. Nothing is touched unless this
        run holds the lock (`own`): a run that was refused must not remove the files of the run that is going."""
        if not own:
            return
        if proxy is not None:
            try:
                proxy.stop()
            except Exception:
                log.warning("The builder's proxy didn't stop", exc_info=True)
        if tokens is not None and run.handle is not None:
            tokens.revoke(run.handle.name)
        if builder is not None and run.handle is not None:
            try:
                builder.destroy(run.handle)
            except Exception:
                log.warning("The builder VM %s wasn't deleted", run.handle.name, exc_info=True)
                self.say(f"The builder VM {run.handle.name} couldn't be deleted. Delete it by hand in Hyper-V Manager.")
        if builder is not None and run.new_files and not run.committed and old is not None:
            try:
                builder.run_powershell(remove_new_disk_script(run.new_files, old))
            except Exception:
                log.warning("The unfinished new base disk wasn't deleted", exc_info=True)
                self.say("The unfinished new disk couldn't be deleted. Delete the sandbox-base-*.building.vhdx file by hand.")
        if builder is not None:
            try:
                shutil.rmtree(work_dir(self.settings), ignore_errors=True)  # the builder's throwaway key and folders
            except OSError:
                pass


def _basic(name: str, token: str) -> str:
    return base64.b64encode(f"{name}:{token}".encode()).decode()
