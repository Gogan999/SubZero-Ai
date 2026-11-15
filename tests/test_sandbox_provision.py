"""Preparing the sandbox image (subzero_ai/sandbox_image.py): the whole provisioning run against a fake host. Offline: no
PowerShell, no SSH, no Hyper-V and no network. FakeHost (tests/test_sandbox.py) records every PowerShell script and
SSH command in order, and these tests check that order, that every failure cleans up and leaves the old base disk and
its setting alone, and that the builder's proxy login never reaches the console or the log."""

from __future__ import annotations

import io
import json
import re
import shlex
import shutil
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from subzero_ai import sandbox_image
from subzero_ai.config import Settings
from subzero_ai.db import Database
from subzero_ai.egress_proxy import EgressProxy, TokenStore
from subzero_ai.sandbox import SandboxStore, ps_quote
from subzero_ai.sandbox_image import (
    NODE_LINUX_X64_SHA256,
    NODE_VERSION,
    PROVISION_HOSTS,
    TOOLS_VERSION,
    LineSink,
    ProxyThread,
    RunLock,
    image_is_current,
    make_builder,
    make_proxy,
    provision_hosts,
    provision_image,
)

from .test_sandbox import PASSING_NETWORK, FakeHost, hyperv

RUNNER_VERSION_LINE = json.dumps({"runner_version": sandbox_image.expected_runner_version(),
                                  "sdk_version": sandbox_image.expected_sdk_version()})
OLD_BASE_NAME = "sandbox-base.vhdx"


class ProvisionHost(FakeHost):
    """FakeHost that also answers what the provisioning asks: VM state, the disk scripts, and the tools' checks. It
    makes and removes the new disk's files on the host, as the PowerShell would."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.state = "Off"  # what the VM reports after the script's power-off
        self.fail_contains: dict[str, int] = {}  # an ssh command that holds this text exits with this code
        self.fail_ps: dict[str, str] = {}  # a PowerShell script that holds this text raises this message
        self.verify: dict[str, str] = {
            "node --version": f"v{NODE_VERSION}", "java -version": 'openjdk version "17.0.13" 2024-10-15',
            "git --version": "git version 2.43.0", "python3 --version": "Python 3.12.3",
            "cat /opt/subzero-runner/TOOLS_VERSION": str(TOOLS_VERSION),
            "runner.mjs --version": RUNNER_VERSION_LINE}
        self.uploads: list[tuple[str, bytes]] = []
        self.install_output = ""
        self.install_chunks: list[str] = []
        self.removed: list[str] = []

    def powershell(self, script: str) -> str:
        for text, message in self.fail_ps.items():
            if text in script:
                self.scripts.append(script)
                self.events.append(("ps", script))
                raise sandbox_image.SandboxError(message)
        if "Get-VM -Name" in script and "[string]$vm.State" in script:
            self.scripts.append(script)
            self.events.append(("ps", script))
            return self.state + "\n"
        out = super().powershell(script)
        if "Convert-VHD -Path" in script:
            return ""
        if "Move-Item -LiteralPath" in script:
            final = re.search(r"-Destination '([^']+)'", script).group(1)
            Path(final).write_bytes(b"new base disk")
            return "A" * 64 + "\n"
        if "Remove-Item -LiteralPath" in script and "sandbox-base-" in script and "Remove-VM -Name" not in script:
            for path in re.findall(r"if \(Test-Path -LiteralPath '([^']+)'\) \{ Remove-Item", script):
                self.removed.append(path)
                Path(path).unlink(missing_ok=True)
        return out

    def ssh(self, address, remote, *, known_hosts, stdin, timeout, on_output=None, limit=None):
        if remote.startswith("mkdir -p") and stdin is not None or "python3 -c" in remote and stdin is not None:
            self.uploads.append((remote, stdin))
        for text, code in self.fail_contains.items():
            if text in remote:
                self.remote.append((address, remote))
                self.events.append(("ssh", remote))
                if on_output:
                    on_output("something went wrong\n")
                return code, b"", b"failed"
        if "provision.sh install" in remote:
            self.remote.append((address, remote))
            self.events.append(("ssh", remote))
            if on_output:
                for chunk in self.install_chunks or ["== Installing packages\n", self.install_output]:
                    on_output(chunk)
            return 0, b"", b""
        for key, answer in self.verify.items():
            if key in remote and "provision.sh" not in remote:
                self.remote.append((address, remote))
                self.events.append(("ssh", remote))
                return 0, (answer + "\n").encode(), b""
        return super().ssh(address, remote, known_hosts=known_hosts, stdin=stdin, timeout=timeout,
                           on_output=on_output, limit=limit)


class FakeProxy:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.started = self.stopped = False
        self.tokens: TokenStore | None = None
        self.address = ("10.77.0.1", 3128)

    def start(self) -> None:
        if self.fail:
            raise sandbox_image.ProvisionError("The builder's internet proxy couldn't start. Address in use.")
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class ProvisionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="provision-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.images = self.tmp / "sandbox" / "images"
        self.images.mkdir(parents=True)
        self.old = self.images / OLD_BASE_NAME
        self.old.write_bytes(b"old base disk")
        self.settings = Settings(data_dir=self.tmp, code_sandbox="hyperv", sandbox_base_image=str(self.old))
        self.db = Database(self.tmp / "app.db")
        self.host = ProvisionHost()
        self.addCleanup(shutil.rmtree, self.host.root, True)
        self.lines: list[str] = []
        self.proxy = FakeProxy()
        self.tokens: TokenStore | None = None

    def factory(self, tokens):
        self.tokens = tokens
        return self.proxy

    def builder(self, **overrides):
        return hyperv(self.host, base_image=str(self.old), guest_sudo=True, disks_dir=str(self.host.root / "disks"),
                      **overrides)

    def run_it(self, *, force=False, builder=None, settings_base=None, **overrides):
        if settings_base is not None:
            self.settings.sandbox_base_image = settings_base
        options = dict(builder=builder or self.builder(), proxy_factory=self.factory, app_running=lambda: False,
                       free_gb=lambda _p: 100, sleep=lambda _s: None, say=self.lines.append)
        options.update(overrides)
        return provision_image(self.settings, self.db, force=force, **options)

    def audits(self, action: str) -> list:
        return self.db.all("SELECT * FROM audit_log WHERE action = ?", (action,))

    def saved_base(self):
        return self.db.one("SELECT value FROM settings WHERE key = 'sandbox_base_image'")

    def assert_untouched(self):
        self.assertEqual(self.settings.sandbox_base_image, str(self.old))
        self.assertIsNone(self.saved_base())
        self.assertEqual(self.old.read_bytes(), b"old base disk")
        self.assertEqual(sorted(p.name for p in self.images.iterdir()), [OLD_BASE_NAME], "no new disk files left")
        self.assertEqual(self.audits("sandbox_image_prepared"), [])
        self.assertFalse((self.tmp / "sandbox" / "provision.lock").exists(), "the lock is released")

    def ssh_index(self, text: str) -> int:
        for position, (kind, body) in enumerate(self.host.events):
            if kind == "ssh" and text in body:
                return position
        raise AssertionError(f"no ssh command with {text!r}")

    def ps_index(self, text: str, start: int = 0) -> int:
        for position, (kind, body) in enumerate(self.host.events):
            if position >= start and kind == "ps" and text in body:
                return position
        raise AssertionError(f"no PowerShell script with {text!r}")


class HappyPathTest(ProvisionBase):
    def test_a_run_builds_the_new_disk_in_the_expected_order(self):
        result = self.run_it()
        self.assertEqual(result.status, "built", result.message)
        self.assertEqual(result.exit_code, 0)
        new = self.images / f"sandbox-base-{TOOLS_VERSION}.vhdx"
        self.assertEqual(result.path, str(new))
        self.assertTrue(new.is_file())
        events = self.host
        # The builder is a differencing disk of the OLD base, made before anything is uploaded
        new_vhd = self.ps_index("-Differencing")
        self.assertIn(f"-ParentPath {ps_quote(str(self.old))}", events.events[new_vhd][1])
        order = [new_vhd, self.ps_index("New-VM"), self.ps_index("Start-VM"), self.ssh_index("provision/provision.sh"),
                 self.ssh_index("provision/runner"), self.ssh_index("provision.sh install"),
                 self.ssh_index("node --version"), self.ssh_index("java -version"), self.ssh_index("git --version"),
                 self.ssh_index("python3 --version"), self.ssh_index("runner.mjs --version"),
                 self.ssh_index("provision.sh finish"), self.ps_index("[string]$vm.State"),
                 self.ps_index("Convert-VHD -Path"), self.ps_index("Move-Item"), self.ps_index("Remove-VM -Name")]
        self.assertEqual(order, sorted(order), "the steps ran out of order")
        self.assertEqual(len(set(order)), len(order))

    def test_sudo_is_for_the_builder_only(self):
        self.run_it()
        self.assertIn("sudo: ALL=(ALL) NOPASSWD:ALL", self.host.seed_files["user-data"].decode())
        self.assertTrue(make_builder(self.settings).guest_sudo)
        from subzero_ai.sandbox import make_backend
        self.assertFalse(make_backend(self.settings, "hyperv").guest_sudo, "a session's backend never gets sudo")

    def test_the_script_and_the_runner_are_uploaded_without_node_modules_or_tests(self):
        self.run_it()
        [(script_remote, script)] = [u for u in self.host.uploads if "provision.sh" in u[0]]
        self.assertEqual(script.decode(), sandbox_image.script_text())
        self.assertNotIn(b"\r", script)
        [(archive_remote, data)] = [u for u in self.host.uploads if "provision/runner" in u[0]]
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertEqual(sorted(n for n in names if "/" not in n), ["package-lock.json", "package.json", "runner.mjs"])
        self.assertTrue(any(n.startswith("lib/") for n in names))
        self.assertFalse([n for n in names if "node_modules" in n or n.startswith("test/")])

    def test_the_install_runs_with_sudo_the_pins_and_only_the_proxy_login(self):
        self.run_it()
        remote = next(r for _a, r in self.host.remote if "provision.sh install" in r)
        self.assertIn("sudo -E bash provision.sh install", remote)
        words = shlex.split(remote.split("&& env -i ", 1)[1])
        env = dict(w.split("=", 1) for w in words if "=" in w and not w.startswith("-"))
        self.assertEqual(env["TOOLS_VERSION"], str(TOOLS_VERSION))
        self.assertEqual(env["NODE_VERSION"], NODE_VERSION)
        self.assertEqual(env["NODE_SHA256"], NODE_LINUX_X64_SHA256)
        self.assertEqual(env["HTTPS_PROXY"], env["https_proxy"])
        self.assertRegex(env["HTTPS_PROXY"], r"^http://szsandbox-build-s\d{3}-[0-9a-f]{6}:[^@]+@10\.77\.0\.1:3128$")
        self.assertNotIn("JAVA_TOOL_OPTIONS", env)
        self.assertNotIn("GRADLE_OPTS", env)

    def test_only_the_builder_has_a_token_and_it_is_revoked_at_the_end(self):
        seen = {}
        original = self.factory

        def spy(tokens):
            seen["store"] = tokens
            return original(tokens)

        self.run_it(proxy_factory=spy)
        store = seen["store"]
        builder_names = [n for _a, r in self.host.remote for n in re.findall(r"szsandbox-build-s\d{3}-[0-9a-f]{6}", r)]
        self.assertTrue(builder_names)
        self.assertFalse(store.active(builder_names[0]), "revoked when the run ended")
        self.assertTrue(self.proxy.started and self.proxy.stopped)

    def test_the_finished_disk_gets_the_base_disks_permissions_and_a_record(self):
        self.run_it()
        script = next(s for s in self.host.scripts if "Move-Item -LiteralPath" in s)
        for text in ("IsReadOnly -Value $true", "/inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F'",
                     "*S-1-5-83-0:(R)", "NT SERVICE\\SubZeroAI:(R)", "Get-FileHash", ".sha256"):
            self.assertIn(text, script)
        self.assertLess(script.index("Move-Item"), script.index("IsReadOnly"))
        new = self.images / f"sandbox-base-{TOOLS_VERSION}.vhdx"
        info = json.loads(sandbox_image.sidecar_path(new).read_text())
        self.assertEqual((info["tools_version"], info["node_version"], info["built_from"]),
                         (TOOLS_VERSION, NODE_VERSION, OLD_BASE_NAME))
        self.assertTrue(image_is_current(new))

    def test_the_old_base_disk_is_only_read(self):
        self.run_it()
        words = ("Remove-Item", "Remove-VM", "Stop-VM", "Set-VHD", "Optimize-VHD", "Convert-VHD", "Resize-VHD",
                 "Move-Item", "icacls", "Set-ItemProperty", "Set-Content")
        for script in self.host.scripts:
            for line in script.splitlines():
                if str(self.old) in line:
                    self.assertFalse(any(w in line for w in words), line)
        converted = next(s for s in self.host.scripts if "Convert-VHD -Path" in s)
        self.assertNotIn(str(self.old), converted)
        self.assertIn("-VHDType Dynamic", converted)
        self.assertIn("if ($out.ParentPath)", converted)

    def test_the_setting_is_switched_and_audited(self):
        result = self.run_it()
        self.assertEqual(self.settings.sandbox_base_image, result.path)
        self.assertEqual(self.saved_base()["value"], result.path)
        [row] = self.audits("sandbox_image_prepared")
        details = json.loads(row["details"])
        self.assertEqual((details["path"], details["previous"], details["tools_version"]),
                         (result.path, str(self.old), TOOLS_VERSION))
        self.assertNotIn("token", row["details"].lower())

    def test_the_builder_and_its_files_are_removed_at_the_end(self):
        self.run_it()
        destroy = next(s for s in self.host.scripts if "Remove-VM -Name" in s)
        self.assertIn("szsandbox-build-s", destroy)
        self.assertFalse((self.tmp / "sandbox" / "provision.lock").exists())
        self.assertEqual(sorted(p.name for p in self.images.iterdir() if p.suffix == ".vhdx"),
                         sorted([OLD_BASE_NAME, f"sandbox-base-{TOOLS_VERSION}.vhdx"]), "no building file left behind")

    def test_the_new_disk_is_made_after_the_shutdown_and_it_must_be_off(self):
        self.run_it()
        convert = next(s for s in self.host.scripts if "Convert-VHD -Path" in s)
        self.assertIn("$vm.State -ne 'Off'", convert)

    def test_output_is_streamed_to_the_console_and_a_log_with_the_login_hidden(self):
        self.host.install_chunks = ["== Installing packages\n", "Picked up JAVA_TOOL_OPTIONS -D", "TOKENHERE\n"]
        # The fake can't know the token ahead of time, so it reads it from the command the app sent
        original = self.host.ssh

        def ssh(address, remote, **kw):
            if "provision.sh install" in remote:
                token = re.search(r"HTTPS_PROXY=http://[^:]+:([^@]+)@", remote).group(1)
                self.token = token
                half = len(token) // 2
                self.host.install_chunks = [f"proxy login {token[:half]}", f"{token[half:]} end\n", f"basic {token}\n"]
            return original(address, remote, **kw)

        self.host.ssh = ssh
        result = self.run_it()
        self.assertEqual(result.status, "built")
        console = "\n".join(self.lines)
        log_text = Path(result.log_path).read_text(encoding="utf-8")
        self.assertIn("[hidden]", console)
        self.assertIn("proxy login [hidden] end", console)
        self.assertGreater(len(self.token), 30)
        for text in (console, log_text, result.message):
            self.assertNotIn(self.token, text)
            self.assertNotIn(self.token[:12], text, "not even a piece of it")
        self.assertEqual(Path(result.log_path).parent, self.tmp / "logs")
        self.assertIn("proxy login [hidden] end", log_text)


class NoOpAndForceTest(ProvisionBase):
    def test_a_second_run_does_nothing_and_says_so(self):
        self.run_it()
        before = len(self.host.events)
        second = self.run_it()
        self.assertEqual(second.status, "current")
        self.assertEqual(second.exit_code, 0)
        self.assertEqual(len(self.host.events), before, "no PowerShell or ssh on a no-op")
        self.assertIn("Nothing to do", second.message)
        self.assertIn(f"version {TOOLS_VERSION}", second.message)

    def test_force_builds_again_under_a_new_name_and_keeps_the_first(self):
        first = self.run_it()
        self.host = ProvisionHost()
        self.addCleanup(shutil.rmtree, self.host.root, True)
        second = self.run_it(force=True, builder=hyperv(self.host, base_image=first.path, guest_sudo=True))
        self.assertEqual(second.status, "built", second.message)
        self.assertEqual(Path(second.path).name, f"sandbox-base-{TOOLS_VERSION}-r2.vhdx")
        self.assertTrue(Path(first.path).is_file(), "the disk that was in use is kept")
        self.assertTrue(any(f"-ParentPath {ps_quote(first.path)}" in s for s in self.host.scripts),
                        "the rebuild starts from the disk in use")
        [row] = self.audits("sandbox_image_prepared")[1:]
        self.assertTrue(json.loads(row["details"])["forced"])

    def test_a_disk_without_a_record_or_with_an_old_one_is_not_current(self):
        self.assertFalse(image_is_current(self.old))
        sidecar = sandbox_image.sidecar_path(self.old)
        sidecar.write_text(json.dumps({"tools_version": TOOLS_VERSION - 1}), encoding="utf-8")
        self.assertFalse(image_is_current(self.old))
        sidecar.write_text("not json", encoding="utf-8")
        self.assertFalse(image_is_current(self.old))
        sidecar.write_text(json.dumps({"tools_version": TOOLS_VERSION,
                                       "runner_version": sandbox_image.expected_runner_version(),
                                       "sdk_version": "0.0.1", "node_version": NODE_VERSION}), encoding="utf-8")
        self.assertFalse(image_is_current(self.old), "a different SDK version means rebuild")


class RefusalTest(ProvisionBase):
    def assert_refused(self, result, text: str):
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 1)
        self.assertIn(text, result.message)
        self.assertEqual(self.host.events, [] if "network" not in text else self.host.events)
        self.assertEqual(self.old.read_bytes(), b"old base disk")
        self.assertIsNone(self.saved_base())

    def test_it_refuses_while_another_run_is_active(self):
        lock = self.tmp / "sandbox" / "provision.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": 1, "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
        result = self.run_it()
        self.assert_refused(result, "already going")
        self.assertTrue(lock.exists(), "the other run's lock is not removed")
        self.assertFalse(self.proxy.started)

    def test_a_lock_left_by_a_crash_does_not_block_forever(self):
        lock = self.tmp / "sandbox" / "provision.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": 1, "started": "2020-01-01T00:00:00+00:00"}))
        self.assertEqual(self.run_it().status, "built")

    def test_it_refuses_while_the_app_is_running(self):
        result = self.run_it(app_running=lambda: True)
        self.assert_refused(result, "SubZero AI is running")

    def test_it_refuses_while_sandboxes_are_in_use(self):
        SandboxStore(self.db).insert(None, "hyperv", state="running", created_at="2026-01-01T00:00:00+00:00",
                                     expires_at="2099-01-01T00:00:00+00:00")
        result = self.run_it()
        self.assert_refused(result, "still in use")

    def test_it_refuses_without_a_base_disk(self):
        self.old.unlink()
        for result in (self.run_it(), self.run_it(settings_base="")):
            self.assertEqual(result.status, "failed")
            self.assertIn("no sandbox base disk", result.message)
        self.assertEqual(self.host.events, [])
        self.assertIsNone(self.saved_base())

    def test_it_refuses_when_the_sandbox_network_is_not_ready(self):
        self.host.network = '["switch-missing"]'
        result = self.run_it()
        self.assertEqual(result.status, "failed")
        self.assertIn("sandbox network", result.message)
        self.assertFalse(self.host.sent("New-VM"))
        self.assertFalse(self.proxy.started)
        self.assert_untouched()

    def test_it_refuses_when_there_is_too_little_disk(self):
        result = self.run_it(free_gb=lambda _p: 3)
        self.assertIn("3 GB free", result.message)
        self.assertFalse(self.host.sent("New-VM"))
        self.assert_untouched()

    def test_a_builder_left_by_an_earlier_run_is_removed_first(self):
        self.host.vms = '["szsandbox-build-s000-aaaaaa"]'
        self.assertEqual(self.run_it().status, "built")
        first_destroy = next(s for s in self.host.scripts if "Remove-VM -Name" in s)
        self.assertIn("szsandbox-build-s000-aaaaaa", first_destroy)
        self.assertLess(self.host.scripts.index(first_destroy), self.ps_index("New-VM"))


class FailureTest(ProvisionBase):
    """A failure at any stage leaves the old base and its setting alone and removes the builder and its files."""

    def check_failed(self, result, stage_text: str, *, builder_made: bool = True):
        self.assertEqual(result.status, "failed", result.message)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("The old base disk is still in use", result.message)
        self.assertIn(stage_text, "\n".join(self.lines))
        self.assert_untouched()
        self.assertTrue(self.proxy.stopped or not self.proxy.started)
        self.assertEqual(len(self.audits("sandbox_image_failed")), 1)
        if builder_made:
            self.assertTrue(self.host.sent("Remove-VM -Name"), "the builder VM is removed")
        else:
            self.assertFalse(self.host.sent("New-VM"))

    def test_the_proxy_does_not_start(self):
        self.proxy.fail = True
        result = self.run_it()
        self.check_failed(result, "internet proxy", builder_made=False)

    def test_the_builder_cannot_be_made(self):
        host = self.host
        host.vlan = '[{"OperationMode":"Untagged"}]'
        result = self.run_it()
        self.check_failed(result, "making the builder VM")
        self.assertTrue(self.proxy.stopped)

    def test_the_upload_fails(self):
        self.host.fail_contains["cat > "] = 1
        self.check_failed(self.run_it(), "uploading")

    def test_the_install_fails(self):
        self.host.fail_contains["provision.sh install"] = 100
        result = self.run_it()
        self.check_failed(result, "installing the tools")
        self.assertIn("exit code 100", result.message)
        self.assertFalse(self.host.sent("Convert-VHD"), "nothing is copied after a failed install")

    def test_a_tool_is_missing_after_the_install(self):
        self.host.verify["java -version"] = "bash: java: command not found"
        result = self.run_it()
        self.check_failed(result, "checking the tools")
        self.assertIn("java", result.message)
        self.assertFalse(self.host.sent("Convert-VHD"))

    def test_the_wrong_node_version_fails_the_check(self):
        self.host.verify["node --version"] = "v20.1.0"
        self.check_failed(self.run_it(), "checking the tools")

    def test_the_runner_is_the_wrong_version(self):
        self.host.verify["runner.mjs --version"] = json.dumps({"runner_version": "9.9.9", "sdk_version": "1.0.0"})
        result = self.run_it()
        self.check_failed(result, "checking the tools")
        self.assertIn("out of date", result.message)

    def test_the_cleaning_step_fails(self):
        self.host.fail_contains["provision.sh finish"] = 1
        self.check_failed(self.run_it(), "cleaning and shutting down")

    def test_the_builder_never_turns_off(self):
        self.host.state = "Running"
        result = self.run_it()
        self.check_failed(result, "cleaning and shutting down")
        self.assertIn("shut down on its own", result.message)
        self.assertFalse(self.host.sent("Convert-VHD -Path"))

    def test_the_disk_copy_fails(self):
        self.host.fail_ps["Convert-VHD -Path"] = "Convert-VHD failed for the test"
        result = self.run_it()
        self.check_failed(result, "copying the builder's disk")
        self.assertIn("Convert-VHD failed for the test", result.message)

    def test_the_permissions_step_fails_and_the_new_disk_is_removed(self):
        self.host.fail_ps["Move-Item -LiteralPath"] = "icacls failed for the test"
        result = self.run_it()
        self.check_failed(result, "new base disk's permissions")
        remove = next(s for s in self.host.scripts if "Remove-Item" in s and "sandbox-base-" in s)
        self.assertIn(".building.vhdx", remove)
        self.assertNotIn(f"'{self.old}'", remove, "the old base disk is never named in a cleanup")

    def test_the_setting_cannot_be_saved(self):
        from unittest import mock
        with mock.patch("subzero_ai.sandbox_image.saved_settings.save", side_effect=RuntimeError("database is locked")):
            result = self.run_it()
        self.check_failed(result, "switching the sandbox base disk setting")
        self.assertIn("database is locked", result.message)

    def test_a_crash_inside_the_run_still_cleans_up(self):
        self.host.fail_contains["provision.sh install"] = 1
        self.host.fail_ps["Remove-VM -Name"] = "the first delete fails"  # cleanup is attempted and its failure is survived
        result = self.run_it()
        self.assertEqual(result.status, "failed")
        self.assertFalse((self.tmp / "sandbox" / "provision.lock").exists())
        self.assertIn("couldn't be deleted", "\n".join(self.lines))


class ProvisioningHostsTest(unittest.TestCase):
    def test_the_builder_gets_the_package_hosts_and_a_session_does_not(self):
        settings = Settings()
        normal = settings.sandbox_allowed_hosts
        for host in PROVISION_HOSTS:
            if host != "registry.npmjs.org":  # already allowed in the normal list
                self.assertNotIn(host, normal, f"{host} must not be in a session's allow-list")
        builder_rules = provision_hosts(normal)
        for host in PROVISION_HOSTS:
            self.assertIn(host, builder_rules)
        self.assertEqual(len(builder_rules), len(set(builder_rules)))
        for rule in normal.split(","):
            self.assertIn(rule.strip(), builder_rules)

    def test_the_https_mirrors_and_nodejs_are_the_extra_hosts(self):
        self.assertTrue({"archive.ubuntu.com", "security.ubuntu.com", "nodejs.org"} <= set(PROVISION_HOSTS))
        self.assertFalse([h for h in PROVISION_HOSTS if "://" in h or "*" in h], "exact names only")

    def test_the_builders_proxy_has_fixed_rules_and_the_sessions_proxy_is_unchanged(self):
        settings = Settings()
        thread = make_proxy(settings, TokenStore(), None)
        self.assertEqual(set(thread.proxy._rules), set(provision_hosts(settings.sandbox_allowed_hosts)))
        self.assertEqual(thread.proxy._ports, (443,))
        self.assertIsNone(thread.proxy._settings, "rules are fixed for the run")
        self.assertEqual((thread.proxy.host, thread.proxy.port), (settings.sandbox_host_address, settings.sandbox_proxy_port))
        session = EgressProxy.from_settings(settings, tokens=TokenStore(), db=None)
        rules, _ports, _bytes, _per = session._policy()
        self.assertNotIn("archive.ubuntu.com", rules)
        self.assertNotIn("nodejs.org", rules)

    def test_the_proxy_thread_starts_and_stops_a_real_proxy(self):
        proxy = EgressProxy(tokens=TokenStore(), db=None, host="127.0.0.1", port=0, allowed_hosts=["nodejs.org"],
                            local_addresses=[])
        thread = ProxyThread(proxy)
        thread.start()
        try:
            self.assertTrue(proxy.running)
            self.assertNotEqual(thread.address[1], 0)
        finally:
            thread.stop()
        self.assertFalse(proxy.running)

    def test_a_proxy_that_cannot_bind_says_to_stop_the_app(self):
        taken = EgressProxy(tokens=TokenStore(), db=None, host="127.0.0.1", port=0, local_addresses=[])
        first = ProxyThread(taken)
        first.start()
        try:
            clash = ProxyThread(EgressProxy(tokens=TokenStore(), db=None, host="127.0.0.1", port=first.address[1],
                                            local_addresses=[]))
            with self.assertRaises(sandbox_image.ProvisionError) as caught:
                clash.start()
            self.assertIn("stop it first", str(caught.exception))
        finally:
            first.stop()


class HelpersTest(unittest.TestCase):
    def test_the_line_sink_hides_a_secret_split_over_chunks(self):
        out: list[str] = []
        sink = LineSink(out.append, lambda: ["SECRETTOKEN123"])
        sink.feed("a SECRET")
        sink.feed("TOKEN123 b\nsecond SECRETTOKEN123")
        sink.close()
        self.assertEqual(out, ["a [hidden] b", "second [hidden]"])

    def test_the_run_lock_is_exclusive_and_released(self):
        tmp = Path(tempfile.mkdtemp(prefix="lock-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        first, second = RunLock(tmp / "x.lock"), RunLock(tmp / "x.lock")
        first.acquire()
        with self.assertRaises(sandbox_image.ProvisionError):
            second.acquire()
        second.release()
        self.assertTrue((tmp / "x.lock").exists(), "a run that never held the lock can't release it")
        first.release()
        self.assertFalse((tmp / "x.lock").exists())
        second.acquire()
        second.release()


class CommandLineTest(unittest.TestCase):
    def test_the_command_prints_the_message_and_returns_its_exit_code(self):
        from subzero_ai import __main__ as cli
        tmp = Path(tempfile.mkdtemp(prefix="cli-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        seen = {}

        def fake(settings, db, *, force, say, app_running):
            seen.update(force=force, running=app_running())
            return sandbox_image.ProvisionResult("failed", "It stopped.", "", "x.log")

        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.sandbox_provision_tool(Settings(data_dir=tmp), force=True, provision=fake)
        self.assertEqual(code, 1)
        self.assertEqual(seen["force"], True)
        self.assertIn("It stopped.", out.getvalue())
        self.assertIn("x.log", out.getvalue())
        self.assertIn("sandbox-provision", cli.COMMANDS)
        args = cli.build_parser().parse_args(["sandbox-provision", "--force"])
        self.assertTrue(args.force)
        self.assertEqual(PASSING_NETWORK, "[]")


if __name__ == "__main__":
    unittest.main()
