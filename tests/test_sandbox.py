"""Code sandboxes: the fake sandbox's lifecycle, Hyper-V command building (with fake PowerShell and SSH), the reaper,
the sandbox table migration, the Settings fields, and a Code session that runs its command in a sandbox. Offline:
no PowerShell, no SSH and no Hyper-V are ever started."""

from __future__ import annotations

import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from subzero_ai import code_runner, saved_settings, sandbox
from subzero_ai.code_runner import SANDBOX_ENV
from subzero_ai.config import Settings, load_settings
from subzero_ai.db import MIGRATIONS, Database
from subzero_ai.sandbox import (
    PREFIX,
    FakeSandbox,
    HyperVSandbox,
    SandboxError,
    SandboxHandle,
    SandboxRefused,
    check_adapters,
    child_disk_path,
    guest_address,
    pack_tree,
    ps_quote,
    reap_sandboxes,
    unpack_tree,
)

from .helpers import MENTOR, Harness
from .test_features import FakeResponse, FakeWeb, repo_zip

BASE = "/srv/sandbox/base.vhdx"
SWITCH = "szsandbox-private"
GOOD_ADAPTERS = '[{"Name":"Network Adapter","SwitchName":"szsandbox-private"}]'
DESTRUCTIVE = ("Remove-Item", "Remove-VM", "Stop-VM", "Set-VHD", "Optimize-VHD", "Convert-VHD", "Resize-VHD")


# --------------------------------------------------------------------------- the fake sandbox
class FakeSandboxLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sandbox-fake-test-"))
        self.backend = FakeSandbox(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_create_files_and_listing(self):
        handle = self.backend.create(7)
        self.assertTrue(handle.name.startswith(PREFIX))
        self.assertTrue((self.root / handle.name).is_dir())
        self.backend.put_file(handle, "work/src/a.txt", b"hello")
        self.assertEqual(self.backend.get_file(handle, "work/src/a.txt"), b"hello")
        self.assertEqual(self.backend.list_dir(handle, "work/src"), ["a.txt"])
        self.assertEqual(self.backend.list_existing(), [handle.name])

    def test_other_folders_are_never_listed_or_touched(self):
        (self.root / "not-ours").mkdir()
        handle = self.backend.create(1)
        self.assertEqual(self.backend.list_existing(), [handle.name])
        self.backend.destroy(handle)
        self.assertTrue((self.root / "not-ours").is_dir())

    def test_paths_cannot_leave_the_sandbox(self):
        handle = self.backend.create(1)
        for rel in ("../escape.txt", "work/../../x", "work/a:b"):
            with self.assertRaises(SandboxError, msg=rel):
                self.backend.put_file(handle, rel, b"x")
        self.backend.put_file(handle, "/etc/passwd", b"x")  # a leading slash is read as "from the sandbox's home"
        self.assertEqual(self.backend.get_file(handle, "etc/passwd"), b"x")
        self.assertFalse((self.root / "escape.txt").exists())

    def test_archive_round_trip_keeps_permissions_and_skips_build_output(self):
        source = Path(tempfile.mkdtemp(prefix="sandbox-src-"))
        try:
            (source / "gradlew").write_text("#!/bin/sh\n")
            (source / "gradlew").chmod(0o755)
            (source / "build").mkdir()
            (source / "build" / "out.class").write_text("x")
            (source / "src").mkdir()
            (source / "src" / "Robot.java").write_text("class Robot {}\n")
            data = pack_tree(source, frozenset({"build"}))
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
            self.assertEqual(sorted(names), ["gradlew", "src/Robot.java"])
            handle = self.backend.create(2)
            self.backend.put_archive(handle, data, dest="work")
            copied = self.root / handle.name / "work"
            self.assertTrue(os.access(copied / "gradlew", os.X_OK))
            self.assertEqual((copied / "src" / "Robot.java").read_text(), "class Robot {}\n")
            back = pack_tree(copied)
            self.assertEqual(sorted(zipfile.ZipFile(io.BytesIO(back)).namelist()), ["gradlew", "src/Robot.java"])
        finally:
            shutil.rmtree(source, ignore_errors=True)

    def test_symbolic_links_are_never_packed(self):
        source = Path(tempfile.mkdtemp(prefix="sandbox-link-"))
        try:
            secret = source.parent / f"secret-{os.getpid()}.txt"
            secret.write_text("keys")
            (source / "link.txt").symlink_to(secret)
            names = zipfile.ZipFile(io.BytesIO(pack_tree(source))).namelist()
            self.assertEqual(names, [])
        finally:
            shutil.rmtree(source, ignore_errors=True)
            secret.unlink(missing_ok=True)

    def test_zip_entries_that_climb_out_are_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../evil.txt", "x")
        with self.assertRaises(SandboxError):
            unpack_tree(buf.getvalue(), Path(tempfile.mkdtemp(prefix="sandbox-zip-")))
        self.assertFalse((self.root.parent / "evil.txt").exists())

    def test_run_streams_output_and_reports_the_exit_code(self):
        handle = self.backend.create(3)
        self.backend.put_file(handle, "work/x.txt", b"")
        chunks: list[str] = []
        result = self.backend.run(handle, [sys.executable, "-c", "print('hello from the sandbox')"], cwd="work",
                                  env={"PATH": os.environ.get("PATH", "")}, timeout=60, on_output=chunks.append)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "hello from the sandbox\n")
        self.assertEqual("".join(chunks), "hello from the sandbox\n")
        self.assertFalse(result.timed_out or result.stopped)

    def test_run_is_bounded_and_times_out(self):
        handle = self.backend.create(4)
        self.backend.put_file(handle, "work/x.txt", b"")
        started = time.monotonic()
        code = "import sys,time; sys.stdout.write('a' * 300000); sys.stdout.flush(); time.sleep(30)"
        result = self.backend.run(handle, [sys.executable, "-c", code], cwd="work", env={}, timeout=1)
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(result.timed_out)
        self.assertLessEqual(len(result.output), sandbox.MAX_OUTPUT_CHARS + 40)

    def test_kill_stops_a_running_command(self):
        handle = self.backend.create(5)
        self.backend.put_file(handle, "work/x.txt", b"")
        holder: dict[str, sandbox.ExitResult] = {}
        worker = threading.Thread(target=lambda: holder.update(r=self.backend.run(
            handle, [sys.executable, "-c", "import time; time.sleep(60)"], cwd="work", env={}, timeout=120)))
        worker.start()
        for _ in range(500):  # wait until the command is registered as running
            if self.backend._runs.get(handle.name):
                break
            time.sleep(0.01)
        self.backend.kill(handle)
        worker.join(20)
        self.assertFalse(worker.is_alive())
        self.assertTrue(holder["r"].stopped)

    def test_destroy_is_idempotent_and_names_are_checked(self):
        handle = self.backend.create(6)
        self.backend.destroy(handle)
        self.assertFalse((self.root / handle.name).exists())
        self.backend.destroy(handle)  # already gone: fine
        with self.assertRaises(SandboxError):
            self.backend.destroy(SandboxHandle("Windows Server", "fake", ""))

    def test_sandbox_names_are_checked_before_use(self):
        self.assertEqual(sandbox.check_name("szsandbox-12-s001-abcdef"), "szsandbox-12-s001-abcdef")
        for bad in ("other-vm", "szsandbox-", "szsandbox-../x", "szsandbox-a b"):
            with self.assertRaises(SandboxError, msg=bad):
                sandbox.check_name(bad)

    def test_next_slot_fills_the_lowest_free_slot(self):
        self.assertEqual(sandbox.next_slot([]), 0)
        self.assertEqual(sandbox.next_slot(["szsandbox-1-s000-aaaaaa", "szsandbox-2-s002-bbbbbb"]), 1)


# --------------------------------------------------------------------------- Hyper-V: fakes and command checks
class FakeHost:
    """Stands in for PowerShell and SSH. Records every script and command; never runs anything."""

    def __init__(self, adapters: str = GOOD_ADAPTERS, vms: str = "[]", ssh_codes: dict[str, int] | None = None):
        self.scripts: list[str] = []
        self.remote: list[tuple[str, str]] = []
        self.adapters, self.vms = adapters, vms
        self.ssh_codes = ssh_codes or {}

    def powershell(self, script: str) -> str:
        self.scripts.append(script)
        if script.startswith("Get-VMNetworkAdapter") or "Get-VMNetworkAdapter -VMName" in script:
            return self.adapters
        if "Select-Object -ExpandProperty Name" in script:
            return self.vms
        return ""

    def ssh(self, address: str, remote: str, *, stdin: bytes | None, timeout: int) -> tuple[int, bytes, bytes]:
        self.remote.append((address, remote))
        for prefix, code in self.ssh_codes.items():
            if remote.startswith(prefix):
                return code, b"", b""
        return 0, b"ok", b""

    def sent(self, word: str) -> list[str]:
        return [s for s in self.scripts if word in s]


def hyperv(host: FakeHost, **overrides) -> HyperVSandbox:
    options = dict(base_image=BASE, switch=SWITCH, memory_mb=2048, cpus=2, disk_gb=10, run_powershell=host.powershell,
                   run_ssh=host.ssh, ready_seconds=0, sleep=lambda _s: None)
    options.update(overrides)
    return HyperVSandbox(**options)


class HyperVCommandTest(unittest.TestCase):
    def test_the_child_disk_is_a_differencing_disk_of_the_base_and_never_the_base(self):
        host = FakeHost()
        backend = hyperv(host)
        backend.create(7)
        [new_vhd] = host.sent("New-VHD")
        self.assertIn("-Differencing", new_vhd)
        self.assertIn(f"-ParentPath {ps_quote(BASE)}", new_vhd)
        child = re.search(r"New-VHD -Path '([^']+)'", new_vhd).group(1)
        self.assertEqual(Path(child).parent, Path(BASE).parent)
        self.assertTrue(Path(child).name.startswith(PREFIX) and child.endswith(".vhdx"))
        self.assertNotEqual(child, BASE)
        # The base disk is only read (Get-VHD) and used as a parent; no script changes or deletes it
        for script in host.scripts:
            for line in script.splitlines():
                if BASE in line:
                    self.assertTrue("Get-VHD" in line or "-ParentPath" in line, line)
                    self.assertFalse(any(word in line for word in DESTRUCTIVE), line)

    def test_the_vm_is_attached_only_to_the_private_switch_with_caps(self):
        host = FakeHost()
        hyperv(host, memory_mb=3072, cpus=3).create(7)
        [new_vm] = host.sent("New-VM ")
        self.assertEqual(new_vm.count("-SwitchName"), 1)
        self.assertIn(f"-SwitchName {ps_quote(SWITCH)}", new_vm)
        self.assertIn("-MemoryStartupBytes 3072MB", new_vm)
        self.assertIn("-DynamicMemoryEnabled $false", host.sent("Set-VMMemory")[0])
        self.assertIn("-Count 3", host.sent("Set-VMProcessor")[0])
        for script in host.scripts:
            self.assertFalse(re.search(r"Connect-VMNetworkAdapter|Add-VMNetworkAdapter|-SwitchName(?!.*szsandbox)",
                                       script), script)

    def test_a_vm_with_an_adapter_on_another_switch_is_never_started(self):
        host = FakeHost(adapters='[{"Name":"Network Adapter","SwitchName":"TrueNAS"}]')
        with self.assertRaises(SandboxError) as caught:
            hyperv(host).create(7)
        self.assertIn("TrueNAS", str(caught.exception))
        self.assertEqual(host.sent("Start-VM"), [], "the VM is never started")
        self.assertTrue(host.sent("Remove-VM"), "the half-made VM is removed again")

    def test_check_adapters_refuses_no_adapter_and_any_other_switch(self):
        check_adapters(GOOD_ADAPTERS, SWITCH)
        check_adapters('{"Name":"a","SwitchName":"szsandbox-private"}', SWITCH)
        for bad in ("", "[]", '[{"SwitchName":"Default Switch"}]', '[{"SwitchName":null}]',
                    f'[{GOOD_ADAPTERS[1:-1]},{{"SwitchName":"External"}}]'):
            with self.assertRaises(SandboxError, msg=bad):
                check_adapters(bad, SWITCH)

    def test_the_vm_is_started_and_reached_on_its_static_address(self):
        host = FakeHost()
        handle = hyperv(host).create(7)
        self.assertEqual(handle.backend, "hyperv")
        self.assertTrue(host.sent("Start-VM"))
        self.assertEqual(host.remote[0], ("10.77.0.10", "true"), "the first sandbox gets 10.77.0.10")

    def test_a_second_sandbox_takes_the_next_address(self):
        host = FakeHost(vms='["szsandbox-1-s000-aaaaaa"]')
        hyperv(host).create(2)
        self.assertEqual(host.remote[0][0], "10.77.0.11")

    def test_destroy_stops_removes_then_deletes_only_the_child_disk(self):
        host = FakeHost(vms='["szsandbox-7-s000-abcdef"]')
        backend = hyperv(host)
        backend.destroy(SandboxHandle("szsandbox-7-s000-abcdef", "hyperv", ""))
        [script] = host.scripts
        self.assertLess(script.index("Stop-VM"), script.index("Remove-VM"))
        self.assertLess(script.index("Remove-VM"), script.index("Remove-Item"))
        child = str(child_disk_path(BASE, "szsandbox-7-s000-abcdef"))
        self.assertIn(ps_quote(child), script)
        self.assertNotIn(ps_quote(BASE), script.split("Remove-Item")[1])
        self.assertIn("if ($vm.State -ne 'Off')", script, "a VM that is already off isn't asked to stop")

    def test_names_outside_our_prefix_never_reach_powershell(self):
        host = FakeHost()
        with self.assertRaises(SandboxError):
            hyperv(host).destroy(SandboxHandle("Production DB", "hyperv", ""))
        self.assertEqual(host.scripts, [])

    def test_the_child_disk_is_refused_for_a_name_without_our_prefix(self):
        with self.assertRaises(SandboxError):
            child_disk_path(BASE, "base")
        self.assertEqual(child_disk_path(BASE, "szsandbox-1-s000-aaaaaa"),
                         Path("/srv/sandbox/szsandbox-1-s000-aaaaaa.vhdx"))

    def test_disk_size_is_checked_against_the_limit(self):
        host = FakeHost()
        hyperv(host, disk_gb=12).create(7)
        self.assertIn("-gt 12GB", host.sent("New-VHD")[0])

    def test_the_guest_command_has_a_clean_environment_and_a_time_limit(self):
        host = FakeHost(ssh_codes={"cd ": 124})
        backend = hyperv(host)
        handle = backend.create(7)
        result = backend.run(handle, ["./gradlew", "build"], cwd="work", env=SANDBOX_ENV, timeout=600)
        remote = host.remote[-1][1]
        self.assertIn("cd /home/szrunner/work", remote)
        self.assertIn("env -i", remote)
        self.assertIn("timeout -k 5 600 ./gradlew build", remote)
        self.assertEqual(remote.count("PATH="), 1, "one PATH only")
        self.assertTrue(result.timed_out, "exit code 124 means the time limit stopped it")

    def test_guest_addresses_stay_in_the_private_network(self):
        addresses = {guest_address(slot) for slot in range(sandbox.MAX_SLOTS)}
        self.assertEqual(len(addresses), sandbox.MAX_SLOTS)
        for address in addresses:
            self.assertTrue(address.startswith("10.77.0."))
            self.assertNotIn(int(address.rsplit(".", 1)[1]), (0, 1, 255))
        with self.assertRaises(SandboxError):
            guest_address(sandbox.MAX_SLOTS)

    def test_powershell_quoting(self):
        self.assertEqual(ps_quote("C:\\it's"), "'C:\\it''s'")
        self.assertEqual(ps_quote("a\u2019b"), "'a''b'")
        with self.assertRaises(SandboxError):
            ps_quote("line\nbreak")

    def test_a_base_image_must_be_set(self):
        with self.assertRaises(SandboxRefused):
            HyperVSandbox(base_image="", switch=SWITCH, memory_mb=1024, cpus=1, disk_gb=5)


# --------------------------------------------------------------------------- database
class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sandbox-db-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_new_database_has_the_sandbox_table_and_index(self):
        db = Database(self.tmp / "new.db")
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))
            self.assertEqual(len(MIGRATIONS), 14)
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(sandbox_sessions)")]
            self.assertEqual(columns, ["id", "code_session_id", "backend", "name", "state", "created_at", "expires_at",
                                       "destroyed_at", "note"])
            indexes = [row["name"] for row in conn.execute("PRAGMA index_list(sandbox_sessions)")]
            self.assertIn("idx_sandbox_sessions_state", indexes)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO sandbox_sessions (backend, state, created_at, expires_at) "
                             "VALUES ('fake', 'lost', 'x', 'y')")

    def test_an_existing_version_13_database_gains_the_table_and_keeps_its_data(self):
        path = self.tmp / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode = WAL")
        for script in MIGRATIONS[:13]:
            conn.executescript("BEGIN;" + script + "COMMIT;")
        conn.execute("PRAGMA user_version = 13")
        conn.execute("INSERT INTO kv (key, value) VALUES ('keep', 'me')")
        conn.commit()
        conn.close()
        db = Database(path)
        self.assertEqual(db.get_kv("keep"), "me")
        self.assertIsNotNone(db.one("SELECT name FROM sqlite_master WHERE name = 'sandbox_sessions'"))
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 14)


# --------------------------------------------------------------------------- settings
class SandboxSettingsTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(code_sandbox="fake")
        self.settings = self.h.services.settings
        self.db = self.h.services.db

    def tearDown(self):
        self.h.close()

    def form(self, **changes) -> dict[str, str]:
        values = {f.name: saved_settings.current(self.settings, f.name) for f in saved_settings.SECTIONS["code"][1]}
        values.update(changes)
        return values

    def test_defaults(self):
        fresh = Settings()
        self.assertEqual(fresh.code_sandbox, "off")
        self.assertEqual((fresh.sandbox_ttl_hours, fresh.sandbox_max_sessions), (48, 3))
        self.assertEqual((fresh.sandbox_memory_mb, fresh.sandbox_cpus, fresh.sandbox_disk_gb), (2048, 2, 10))
        self.assertEqual((fresh.sandbox_min_free_gb, fresh.sandbox_switch), (20, "szsandbox-private"))

    def test_valid_values_are_accepted(self):
        values, errors = saved_settings.validate("code", self.form(
            code_sandbox="hyperv", sandbox_ttl_hours="24", sandbox_max_sessions="2", sandbox_memory_mb="4096",
            sandbox_cpus="4", sandbox_disk_gb="12", sandbox_min_free_gb="25",
            sandbox_base_image="C:\\SubZeroAI\\sandbox\\base.vhdx", sandbox_switch="szsandbox-private"),
            settings=self.settings, db=self.db)
        self.assertEqual(errors, {})
        self.assertEqual(values["code_sandbox"], "hyperv")
        self.assertEqual(values["sandbox_base_image"], "C:\\SubZeroAI\\sandbox\\base.vhdx")

    def test_bad_values_are_refused_with_a_message(self):
        cases = {
            "code_sandbox": ["vm", "HyperV "],
            "sandbox_ttl_hours": ["0", "169", "soon"],
            "sandbox_max_sessions": ["0", "11"],
            "sandbox_memory_mb": ["512", "99999"],
            "sandbox_cpus": ["0", "64"],
            "sandbox_disk_gb": ["1"],
            "sandbox_min_free_gb": ["0"],
            "sandbox_base_image": ["relative\\base.vhdx", "C:\\x;del *", "C:\\it's.vhdx", "C:\\a\nb.vhdx"],
            "sandbox_switch": ["", "bad;name", "a" * 64, "Default Switch;"],
        }
        for name, bad_values in cases.items():
            for bad in bad_values:
                values, errors = saved_settings.validate("code", self.form(**{name: bad}), settings=self.settings,
                                                         db=self.db)
                self.assertIn(name, errors, (name, bad))
                self.assertNotIn(name, values, (name, bad))

    def test_the_base_image_can_be_left_empty(self):
        values, errors = saved_settings.validate("code", self.form(sandbox_base_image=""), settings=self.settings,
                                                 db=self.db)
        self.assertEqual(errors, {})
        self.assertEqual(values["sandbox_base_image"], "")

    def test_saving_applies_now_and_is_logged(self):
        values, errors = saved_settings.validate("code", self.form(sandbox_max_sessions="5"), settings=self.settings,
                                                 db=self.db)
        self.assertEqual(errors, {})
        saved_settings.save(self.db, self.settings, values, actor_id=None)
        self.assertEqual(self.settings.sandbox_max_sessions, 5)
        actions = [row["action"] for row in self.db.all("SELECT action FROM audit_log")]
        self.assertIn("setting_changed", actions)

    def test_config_env_values_are_read(self):
        tmp = Path(tempfile.mkdtemp(prefix="sandbox-env-"))
        try:
            with mock.patch.dict(os.environ, {"CODE_SANDBOX": "hyperv", "SANDBOX_TTL_HOURS": "12",
                                              "SANDBOX_SWITCH": "lab-private"}):
                loaded = load_settings(search_cwd=False, data_dir=tmp)
            self.assertEqual((loaded.code_sandbox, loaded.sandbox_ttl_hours, loaded.sandbox_switch),
                             ("hyperv", 12, "lab-private"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- manager and reaper
class ReaperTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(code_sandbox="fake", sandbox_min_free_gb=0, sandbox_max_sessions=5)
        self.services = self.h.services
        self.db = self.services.db
        self.manager = self.services.sandboxes
        self.owner = self.h.login(MENTOR).user["id"]
        self.root = self.services.settings.data_dir / "sandbox-fake"

    def tearDown(self):
        self.h.close()

    def make_session(self, status: str = "running") -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            return conn.execute("INSERT INTO code_sessions (title, task, status, model, repo, created_by, created_at, "
                                "updated_at) VALUES ('t', 't', ?, 'anthropic/x', 'team/robot', ?, ?, ?)",
                                (status, self.owner, now, now)).lastrowid

    def records(self, session_id: int | None = None):
        if session_id is None:
            return self.db.all("SELECT * FROM sandbox_sessions ORDER BY id")
        return self.db.all("SELECT * FROM sandbox_sessions WHERE code_session_id = ? ORDER BY id", (session_id,))

    def actions(self) -> list[str]:
        return [row["action"] for row in self.db.all("SELECT action FROM audit_log ORDER BY id")]

    def test_a_sandbox_is_created_with_a_record_and_an_audit_entry(self):
        sid = self.make_session()
        handle = self.manager.create_for_session(sid, actor_id=self.owner)
        [row] = self.records(sid)
        self.assertEqual((row["state"], row["name"], row["backend"]), ("running", handle.name, "fake"))
        self.assertTrue((self.root / handle.name).is_dir())
        self.assertIn("sandbox_created", self.actions())

    def test_a_sandbox_past_its_time_limit_is_deleted_and_recorded(self):
        sid = self.make_session()
        handle = self.manager.create_for_session(sid, actor_id=self.owner)
        later = datetime.now(timezone.utc) + timedelta(hours=49)
        report = reap_sandboxes(self.services, now=later)
        self.assertEqual(report["expired"], 1)
        [row] = self.records(sid)
        self.assertEqual(row["state"], "destroyed")
        self.assertFalse((self.root / handle.name).exists())
        self.assertIn("sandbox_reaped", self.actions())
        self.assertIn("sandbox_destroyed", self.actions())

    def test_a_sandbox_within_its_limit_is_left_alone(self):
        sid = self.make_session()
        self.manager.create_for_session(sid, actor_id=self.owner)
        self.assertEqual(reap_sandboxes(self.services), {"expired": 0, "orphans": 0, "failed": 0})
        self.assertEqual(self.records(sid)[0]["state"], "running")

    def test_the_time_limit_follows_the_setting(self):
        self.services.settings.sandbox_ttl_hours = 1
        sid = self.make_session()
        self.manager.create_for_session(sid, actor_id=self.owner)
        self.assertEqual(reap_sandboxes(self.services, now=datetime.now(timezone.utc) + timedelta(hours=2))["expired"], 1)

    def test_a_sandbox_of_a_finished_session_is_deleted(self):
        sid = self.make_session(status="done")
        self.manager.create_for_session(sid, actor_id=self.owner)
        self.assertEqual(reap_sandboxes(self.services)["expired"], 1)
        self.assertEqual(self.records(sid)[0]["state"], "destroyed")

    def test_an_orphan_with_no_record_is_deleted_and_nothing_else_is(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "szsandbox-99-s000-abcdef").mkdir()
        (self.root / "someone-else").mkdir()
        sid = self.make_session()
        kept = self.manager.create_for_session(sid, actor_id=self.owner)
        report = reap_sandboxes(self.services)
        self.assertEqual(report["orphans"], 1)
        self.assertFalse((self.root / "szsandbox-99-s000-abcdef").exists())
        self.assertTrue((self.root / "someone-else").is_dir())
        self.assertTrue((self.root / kept.name).is_dir())
        orphan = self.db.one("SELECT details FROM audit_log WHERE action = 'sandbox_reaped' AND details LIKE "
                             "'%no record%'")
        self.assertIsNotNone(orphan)

    def test_a_failed_delete_is_recorded_retried_and_never_raised(self):
        sid = self.make_session()
        handle = self.manager.create_for_session(sid, actor_id=self.owner)
        real = self.manager.backend_factory
        fails = {"left": 1}

        class Flaky(FakeSandbox):
            def destroy(self, h):
                if fails["left"]:
                    fails["left"] -= 1
                    raise SandboxError("the host is busy")
                super().destroy(h)

        self.manager.backend_factory = lambda kind: Flaky(self.root)
        try:
            report = reap_sandboxes(self.services, now=datetime.now(timezone.utc) + timedelta(hours=49))
            self.assertEqual(report["failed"], 1)
            self.assertEqual(self.records(sid)[0]["state"], "failed")
            self.assertTrue((self.root / handle.name).exists())
            report = reap_sandboxes(self.services)  # the next run retries it
            self.assertEqual(report["expired"], 1)
            self.assertEqual(self.records(sid)[0]["state"], "destroyed")
        finally:
            self.manager.backend_factory = real

    def test_a_backend_that_cannot_be_listed_does_not_stop_the_reaper(self):
        sid = self.make_session()
        self.manager.create_for_session(sid, actor_id=self.owner)
        broken = SimpleNamespace(list_existing=mock.Mock(side_effect=OSError("no host")), destroy=mock.Mock())
        real = self.manager.backend_factory
        self.manager.backend_factory = lambda kind: broken
        try:
            report = reap_sandboxes(self.services, now=datetime.now(timezone.utc) + timedelta(hours=49))
        finally:
            self.manager.backend_factory = real
        self.assertEqual(report["failed"], 1, "the failed listing is counted")
        self.assertEqual(report["expired"], 1, "expired sandboxes are still deleted")
        self.assertTrue(broken.destroy.called, "the expired sandbox is still deleted")

    def test_the_reaper_never_raises_even_without_a_manager(self):
        self.assertEqual(reap_sandboxes(SimpleNamespace(sandboxes=None)), {"expired": 0, "orphans": 0, "failed": 0})

        def boom(*_args, **_kwargs):
            raise RuntimeError("database on fire")

        services = SimpleNamespace(sandboxes=SimpleNamespace(lock=threading.RLock(), store=SimpleNamespace(due=boom)))
        self.assertEqual(reap_sandboxes(services), {"expired": 0, "orphans": 0, "failed": 0})

    def test_at_most_max_sessions_sandboxes_can_be_alive(self):
        self.services.settings.sandbox_max_sessions = 2
        for _ in range(2):
            self.manager.create_for_session(self.make_session(), actor_id=self.owner)
        with self.assertRaises(SandboxRefused) as caught:
            self.manager.create_for_session(self.make_session(), actor_id=self.owner)
        self.assertIn("All 2 sandboxes are in use", str(caught.exception))
        self.assertEqual(len(self.records()), 2, "the refused sandbox is not recorded")

    def test_a_sandbox_is_refused_when_the_disk_is_low(self):
        self.services.settings.sandbox_min_free_gb = 20
        usage = SimpleNamespace(total=100, used=95, free=5 * 1024 ** 3)
        with mock.patch.object(sandbox.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(SandboxRefused) as caught:
                self.manager.create_for_session(self.make_session(), actor_id=self.owner)
        self.assertIn("free disk space", str(caught.exception))
        self.assertEqual(self.records(), [])

    def test_hyperv_needs_a_base_image_before_it_starts(self):
        self.services.settings.code_sandbox = "hyperv"
        self.services.settings.sandbox_base_image = ""
        with self.assertRaises(SandboxRefused):
            self.manager.check_capacity()
        self.services.settings.code_sandbox = "fake"


# --------------------------------------------------------------------------- Code sessions on the fake sandbox
class SandboxCodeSessionTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(connector_github_repo="team/robot-code", code_sandbox="fake", sandbox_min_free_gb=0)
        self.kim = self.h.login(MENTOR)
        self.web = FakeWeb({"https://api.github.com/repos/team/robot-code/zipball": FakeResponse(200, repo_zip())})
        self.h.services.github.session_factory = self.web
        self.code = self.h.services.code
        self.manager = self.h.services.sandboxes
        self.db = self.h.services.db
        self.root = self.h.services.settings.data_dir / "sandbox-fake"

    def tearDown(self):
        self.h.close()

    def begin(self, task="Add a notes file and run the build."):
        resp = self.kim.post("/code", {"task": task}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        self.h.idle()
        return int(resp.headers["location"].rsplit("/", 1)[1])

    def pending(self, sid):
        return [s for s in self.code.steps(sid) if s["status"] == "pending"]

    def act(self, step_id, kind, note=""):
        self.kim.post(f"/code/steps/{step_id}/{kind}", {"note": note}, follow_redirects=False)
        self.h.idle()

    def live(self, sid):
        return self.manager.live_for_session(sid)

    def actions(self) -> list[str]:
        return [row["action"] for row in self.db.all("SELECT action FROM audit_log ORDER BY id")]

    def test_a_mentor_approves_a_command_that_runs_in_the_sandbox(self):
        with mock.patch.object(code_runner, "run_program", side_effect=AssertionError("ran on the host")):
            sid = self.begin()
            self.assertEqual(self.code.get(sid)["status"], "waiting")
            box = self.live(sid)
            self.assertIsNotNone(box, "a sandbox was made when the session started")
            sandbox_root = self.root / box["name"] / "work"
            self.assertTrue((sandbox_root / "src" / "Robot.java").is_file(), "the repository was copied in")
            self.assertTrue(os.access(sandbox_root / "gradlew", os.X_OK))

            [write] = self.pending(sid)
            self.act(write["id"], "approve")
            self.assertEqual((sandbox_root / "NOTES.md").read_text().splitlines()[0], "# Notes")
            self.assertTrue((self.code.workspace(sid).work / "NOTES.md").is_file(), "the host copy has the change too")

            [run] = self.pending(sid)
            self.assertEqual(run["tool"], "run_command")
            self.act(run["id"], "approve")
            ran = self.code.db.one("SELECT * FROM code_steps WHERE id = ?", (run["id"],))
            self.assertIn("BUILD SUCCESSFUL build", ran["output"])
            self.assertIn("Exit code 0", ran["output"])

        self.assertEqual(self.code.get(sid)["status"], "done")
        self.assertIsNone(self.live(sid), "the sandbox is gone once the session is done")
        [record] = self.db.all("SELECT * FROM sandbox_sessions WHERE code_session_id = ?", (sid,))
        self.assertEqual(record["state"], "destroyed")
        self.assertFalse((self.root / record["name"]).exists())
        self.assertTrue((self.code.workspace(sid).root / "sandbox-export.zip").is_file(), "its files were saved first")
        self.assertIn("+++ b/NOTES.md", self.kim.get(f"/code/{sid}/changes.patch").text)
        for action in ("sandbox_created", "sandbox_destroyed", "code_step_approved"):
            self.assertIn(action, self.actions())

    def test_a_command_that_changes_other_files_is_reported_not_put_in_the_patch(self):
        sid = self.begin()
        [write] = self.pending(sid)
        self.act(write["id"], "approve")
        box = self.live(sid)
        (self.root / box["name"] / "work" / "src" / "Robot.java").write_text("class Changed {}\n")
        [run] = self.pending(sid)
        self.act(run["id"], "approve")
        notes = " ".join(step["output"] for step in self.code.steps(sid) if step["kind"] == "note")
        self.assertIn("src/Robot.java", notes)
        self.assertNotIn("Changed", self.kim.get(f"/code/{sid}/changes.patch").text)

    def test_stop_while_waiting_deletes_the_sandbox(self):
        sid = self.begin()
        self.assertIsNotNone(self.live(sid))
        self.kim.post(f"/code/{sid}/stop", follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.code.get(sid)["status"], "stopped")
        self.assertIsNone(self.live(sid))
        self.assertEqual(list(self.root.glob("szsandbox-*")), [])

    def test_a_new_session_waits_when_every_sandbox_is_in_use(self):
        self.h.services.settings.sandbox_max_sessions = 1
        self.begin()
        resp = self.kim.post("/code", {"task": "a second task"}, follow_redirects=True)
        self.assertIn("All 1 sandboxes are in use", resp.text)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM code_sessions")["n"], 1)

    def test_an_unknown_sandbox_setting_refuses_to_run_on_this_computer(self):
        self.h.services.settings.code_sandbox = "vm"  # a typo in config.env
        with mock.patch.object(code_runner, "run_program", side_effect=AssertionError("ran on the host")):
            sid = self.begin()
        self.assertEqual(self.code.get(sid)["status"], "failed")
        self.assertIn("no sandbox kind called vm", self.code.get(sid)["error"])

    def test_a_follow_up_without_commands_makes_no_sandbox(self):
        sid = self.begin()
        [write] = self.pending(sid)
        self.act(write["id"], "approve")
        [run] = self.pending(sid)
        self.act(run["id"], "approve")
        self.assertIsNone(self.live(sid))
        self.kim.post(f"/code/{sid}/follow-up", {"text": "Thanks, that is all."}, follow_redirects=False)
        self.h.idle()
        self.assertEqual(self.code.get(sid)["status"], "done")
        self.assertIsNone(self.live(sid), "nothing ran, so no sandbox was needed")


if __name__ == "__main__":
    unittest.main()
