"""The Windows base-image script (packaging/windows/scripts/sandbox-image.ps1) and its docs block. Text-level only:
no PowerShell, no Hyper-V and no network are used. The tests check the pitfalls, the safety rules and the steps that
must stay in the script, in the order they must run."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "packaging" / "windows" / "scripts"
SCRIPT = SCRIPTS / "sandbox-image.ps1"
SETTINGS = ROOT / "docs" / "SETTINGS.md"


def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def function_body(text: str, name: str) -> str:
    """The text of one PowerShell function: from its 'function Name' line to the next top-level function or the end."""
    match = re.search(rf"^function {re.escape(name)}\b.*?(?=^function |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"no function {name} in the script")
    return match.group(0)


class SandboxImageScriptTest(unittest.TestCase):
    def test_the_script_ships_and_is_ascii_only(self):
        self.assertIn("sandbox-image.ps1", [p.name for p in sorted(SCRIPTS.glob("*.ps1"))])
        # Windows PowerShell 5.1 reads this file as ASCII; the build refuses anything else
        SCRIPT.read_bytes().decode("ascii")

    def test_it_needs_an_administrator(self):
        first = next(line for line in script_text().splitlines() if line.strip())
        self.assertEqual(first.strip(), "#Requires -RunAsAdministrator")
        self.assertIn("if (-not (Test-Admin))", script_text())

    def test_it_takes_three_actions_and_defaults_to_check(self):
        text = script_text()
        self.assertIn('[ValidateSet("Build", "Check", "Remove")]', text)
        self.assertIn('[string]$Action = "Check"', text)
        self.assertIn('[string]$Folder = "C:\\ProgramData\\SubZeroAI\\sandbox\\images"', text)
        self.assertIn('[string]$ParentName = "sandbox-base.vhdx"', text)

    def test_no_switch_statement_and_no_switch_parameter(self):
        text = script_text()
        # -Switch would be PowerShell's automatic $switch variable, so no `switch (...)` statement may appear at all
        self.assertIsNone(re.search(r"\bswitch\s*[({]", text, flags=re.IGNORECASE))
        self.assertNotIn("$Switch", text)
        self.assertIn('if ($Action -eq "Build")', text)

    def test_the_release_date_is_validated_and_current_is_refused(self):
        text = script_text()
        self.assertIn("$ReleasePattern = '^[0-9]{8}(\\.[0-9]+)?$'", text)
        self.assertIn("if ($ReleaseDate -notmatch $ReleasePattern)", text)
        # The word may appear in the refusal message, but never as a value the script could use
        self.assertNotIn('$ReleaseDate = "current"', text)
        self.assertNotIn("$ReleaseDate = 'current'", text)

        pattern = re.compile(r"^[0-9]{8}(\.[0-9]+)?$")
        for good in ("20261015", "20261015.1", "20260101"):
            self.assertIsNotNone(pattern.match(good), good)
        for bad in ("current", "Current", "", "2026101", "2026-10-15", "20261015.", "20261015/../x", "latest",
                    " 20261015"):
            self.assertIsNone(pattern.match(bad), bad)

    def test_the_download_url_uses_only_the_checked_release_date(self):
        text = script_text()
        self.assertIn('$ImageRoot = "https://cloud-images.ubuntu.com/noble"', text)
        self.assertIn('"$ImageRoot/$ReleaseDate/$ImageName"', text)
        self.assertIn('"$ImageRoot/$ReleaseDate/SHA256SUMS"', text)

    def test_the_download_is_checked_against_sha256sums_before_it_is_unpacked(self):
        text = script_text()
        self.assertIn("Get-FileHash -LiteralPath $tar -Algorithm SHA256", text)
        check = text.index("Get-ExpectedHash $sums $ImageName")
        self.assertLess(check, text.index("& tar.exe -xzf $tar"))
        self.assertIn("'^\\s*([0-9a-fA-F]{64})\\s+\\*?(\\S+)\\s*$'", text)

    def test_the_sparse_flag_is_cleared_before_the_conversion(self):
        text = script_text()
        self.assertIn("& fsutil.exe sparse setflag $vhd 0", text)
        self.assertLess(text.index("fsutil.exe sparse setflag"), text.index("Convert-VHD -Path $vhd"))

    def test_the_conversion_is_a_dynamic_disk_with_1mb_blocks(self):
        self.assertIn("Convert-VHD -Path $vhd -DestinationPath $Parent -VHDType Dynamic -BlockSizeBytes 1MB",
                      script_text())

    def test_the_base_disk_is_made_read_only_last(self):
        text = script_text()
        self.assertIn("Set-ItemProperty -LiteralPath $Parent -Name IsReadOnly -Value $true", text)
        self.assertLess(text.index("Set-EspBootMenu $Parent"), text.index("-Name IsReadOnly -Value $true"))
        self.assertLess(text.index("Convert-VHD -Path $vhd"), text.index("-Name IsReadOnly -Value $true"))
        # The hash is computed only after the disk is read-only and unmounted
        self.assertLess(text.index("-Name IsReadOnly -Value $true"), text.index("Computing the SHA-256 of the base disk"))

    def test_the_boot_menu_step_finds_the_esp_and_uses_the_ci_ds_nocloud_line(self):
        text = script_text()
        self.assertIn('$EspType = "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}"', text)
        self.assertIn("$_.GptType -eq $EspType", text)
        self.assertIn("ci.ds=NoCloud", text)
        self.assertIn("set timeout=0", text)
        self.assertIn('"set default=0"', text)
        self.assertIn("menuentry 'sandbox' {", text)
        self.assertIn("linux `$sandbox_kernel root=PARTUUID=$PartUuid ro console=tty1 console=ttyS0 "
                      "ci.ds=NoCloud", text)
        self.assertIn("'  initrd $sandbox_initrd'", text)

    def test_grub_picks_the_kernel_and_initrd_at_boot_with_its_own_loops(self):
        body = function_body(script_text(), "New-BootMenu")
        self.assertIn("'for k in /vmlinuz-*; do set sandbox_kernel=$k; done'", body)
        self.assertIn("'for i in /initrd.img-*; do set sandbox_initrd=$i; done'", body)
        # The variables are cleared first, so an empty glob leaves nothing from an earlier run in them
        self.assertIn('"set sandbox_kernel="', body)
        self.assertIn('"set sandbox_initrd="', body)
        # The search line comes first, so the root is set before the glob runs
        self.assertLess(body.index("$SearchLine,"), body.index("for k in /vmlinuz-*"))
        self.assertLess(body.index("for k in /vmlinuz-*"), body.index("menuentry 'sandbox'"))
        # Single quotes keep PowerShell from expanding GRUB's $variables; no double-quoted line holds one
        self.assertNotIn('"$sandbox', body)
        self.assertNotIn('"  linux $sandbox', body)

    def test_no_kernel_version_or_flavour_is_written_into_the_script(self):
        text = script_text()
        # The kernel is whatever the boot partition holds at boot; the script never names a version
        self.assertIsNone(re.search(r"vmlinuz-\d", text))
        self.assertIsNone(re.search(r"initrd\.img-\d", text))
        self.assertNotIn("1017", text)
        self.assertNotIn(r"vmlinuz-\S*azure", text)
        self.assertNotIn(r"initrd\.img-\S*azure", text)
        self.assertNotIn("PARTUUID=894056a3", text)

    def test_the_search_line_is_read_and_a_missing_one_stops_the_step(self):
        text = script_text()
        read = function_body(text, "Read-SearchLine")
        self.assertIn(r"search\.fs_uuid\s+\S+\s+root", read)
        self.assertIn("return $line", read)
        self.assertIn("has no search.fs_uuid line", read)
        self.assertIn("The boot menu was left as it was.", read)
        self.assertIn("throw", read)
        self.assertNotIn("vmlinuz", read)
        self.assertNotIn("root=PARTUUID", read)

    def test_the_root_partuuid_is_the_largest_linux_partition_from_windows(self):
        text = script_text()
        self.assertIn('$LinuxFsType = "{0fc63daf-8483-4772-8e79-3d69d8477de4}"', text)
        body = function_body(text, "Get-RootPartUuid")
        self.assertIn("Get-Partition -DiskNumber $DiskNumber", body)
        self.assertIn("$_.GptType -eq $LinuxFsType", body)
        self.assertIn("Measure-Object -Property Size -Maximum", body)
        self.assertIn("$_.Size -eq $biggest", body)
        # The PARTUUID of a GPT partition is its unique GUID: no braces, lower case, checked against the GUID shape
        self.assertIn("$top[0].Guid -replace '[{}]', ''", body)
        self.assertIn("ToLowerInvariant()", body)
        self.assertIn("'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'", body)
        # Stops for no candidate and for a tie for the largest size
        self.assertIn("no Linux filesystem partition", body)
        self.assertIn("of the same largest size", body)
        self.assertIn("throw", body)

    def test_the_root_partuuid_and_the_search_line_are_read_before_grub_is_written(self):
        body = function_body(script_text(), "Set-EspBootMenu")
        self.assertIn("Read-SearchLine ([System.IO.File]::ReadAllText($grubPath))", body)
        self.assertIn("Get-RootPartUuid $diskNumber", body)
        write = body.index("WriteAllText($grubPath")
        self.assertLess(body.index("Read-SearchLine"), body.index("Copy-Item -LiteralPath $grubPath"))
        self.assertLess(body.index("Get-RootPartUuid $diskNumber"), body.index("Copy-Item -LiteralPath $grubPath"))
        self.assertLess(body.index("Copy-Item -LiteralPath $grubPath"), write)
        self.assertIn("New-BootMenu $searchLine $partUuid", body)

    def test_the_grub_file_is_backed_up_and_written_as_utf8_without_bom_and_lf(self):
        text = script_text()
        self.assertIn('"$($letter):\\EFI\\ubuntu\\grub.cfg.orig"', text)
        self.assertIn("UTF8Encoding($false)", text)
        self.assertIn('-join "`n"', text)
        self.assertIn('[System.IO.File]::WriteAllText($grubPath', text)

    def test_the_esp_is_unmounted_and_the_drive_letter_removed_on_every_path(self):
        body = function_body(script_text(), "Set-EspBootMenu")
        self.assertIn("Add-PartitionAccessPath", body)
        self.assertIn("finally {", body)
        self.assertIn("Remove-PartitionAccessPath", body)
        self.assertIn("Dismount-VHD -Path $Parent", body)
        self.assertIn(".Attached", body, "the base disk must be unmounted before it is made read-only")

    def test_a_failure_after_the_disk_exists_removes_the_partial_disk(self):
        text = script_text()
        rollback = re.search(
            r"try \{\s*\n\s*Write-Step \"Converting.*?\n\s*\} catch \{\s*\n\s*Remove-PartialParent \$Parent\s*\n\s*throw",
            text, flags=re.DOTALL)
        self.assertIsNotNone(rollback, "the conversion and read-only steps must sit inside a catch that rolls back")
        body = function_body(text, "Remove-PartialParent")
        self.assertIn("Dismount-VHD -Path $Parent", body)
        self.assertIn("Remove-Item -LiteralPath $Parent -Force", body)

    def test_the_downloads_and_the_extracted_disk_are_deleted_whatever_happens(self):
        text = script_text()
        self.assertIn('$work = Join-Path $Dir ("build-"', text)
        self.assertIn("finally {\n        Remove-Item -LiteralPath $work -Recurse -Force", text)

    def test_the_free_disk_check_needs_45_gb_and_says_why(self):
        text = script_text()
        self.assertIn("$NeedFreeGB = 45", text)
        self.assertIn("Get-FreeGB $Dir", text)
        self.assertIn("The Ubuntu disk image is 30 GB while it is converted", text)
        self.assertLess(text.index("Get-FreeGB $Dir"), text.index("& curl.exe -fsSL -o $sums"),
                        "the space is checked before anything is downloaded")

    def test_build_refuses_to_overwrite_an_existing_base_disk(self):
        text = script_text()
        refusal = re.search(r"if \(Test-Path -LiteralPath \$Parent\) \{\s*\n\s*throw", text)
        self.assertIsNotNone(refusal)
        self.assertLess(refusal.start(), text.index("& curl.exe -fsSL -o $sums"))
        self.assertLess(refusal.start(), text.index("Convert-VHD -Path $vhd"))

    def test_the_folder_must_be_inside_the_sandbox_folder_and_has_no_dot_dot(self):
        text = script_text()
        body = function_body(text, "Get-SafeFolder")
        self.assertIn("'\\.\\.'", body)
        self.assertIn("IsPathRooted", body)
        self.assertIn("OrdinalIgnoreCase", body)
        self.assertIn('$SandboxRoot = Join-Path $DataDir "sandbox"', text)

    def test_the_base_disk_name_is_a_plain_vhdx_name(self):
        body = function_body(script_text(), "Get-ParentPath")
        self.assertIn("'^[A-Za-z0-9][A-Za-z0-9._-]*\\.vhdx$'", body)

    def test_hyper_v_tools_are_checked_before_any_action(self):
        text = script_text()
        self.assertIn('"Convert-VHD", "Mount-VHD"', text)
        self.assertLess(text.index("Assert-HyperVTools\n"), text.index('if ($Action -eq "Build")'))

    def test_check_only_reads(self):
        body = function_body(script_text(), "Show-ImageCheck")
        for changer in ("Remove-Item", "Set-ItemProperty", "Convert-VHD", "Mount-VHD", "Copy-Item", "New-Item"):
            self.assertNotIn(changer, body)
        self.assertIn("IsReadOnly", body)
        self.assertIn("VhdFormat", body)
        self.assertIn("VhdType", body)

    def test_remove_checks_every_vm_disk_and_deletes_only_the_base_disk(self):
        text = script_text()
        body = function_body(text, "Get-ChildDisks")
        self.assertIn("Get-VM -ErrorAction Stop | Get-VMHardDiskDrive", body)
        self.assertIn("Get-VHD -Path $drive.Path", body)
        self.assertIn("ParentPath", body)
        remove = function_body(text, "Remove-SandboxImage")
        self.assertIn("Get-ChildDisks $Parent", remove)
        self.assertIn("Remove-Item -LiteralPath $Parent -Force", remove)
        self.assertLess(remove.index("Get-ChildDisks $Parent"), remove.index("Remove-Item -LiteralPath $Parent"))

    def test_every_remove_item_names_only_the_script_s_own_files(self):
        lines = [line for line in script_text().splitlines() if "Remove-Item" in line and not line.strip().startswith("#")]
        self.assertGreaterEqual(len(lines), 4)
        for line in lines:
            self.assertTrue(any(name in line for name in ("$work", "$Parent", "$vhd")), line)

    def test_the_parent_is_removed_on_failure_only_when_build_made_it(self):
        # Build refuses an existing disk before anything is written, so a partial disk is always one Build made
        text = script_text()
        self.assertLess(text.index("if (Test-Path -LiteralPath $Parent) {"), text.index("Remove-PartialParent $Parent"))

    def test_the_script_is_never_run_by_the_app_or_the_other_scripts(self):
        for other in list(SCRIPTS.glob("*.ps1")) + list(SCRIPTS.parent.glob("cmd/*.cmd")) + [ROOT / "packaging" / "build_windows.py"]:
            if other.name == SCRIPT.name:
                continue
            # prepare-sandbox-image.* is a different script (it bakes the Code tools in; docs/SANDBOX_IMAGE.md)
            self.assertIsNone(re.search(r"(?<!prepare-)sandbox-image", other.read_text(encoding="utf-8", errors="replace")),
                              other.name)
        for py in (ROOT / "subzero_ai").rglob("*.py"):
            self.assertNotIn("sandbox-image.ps1", py.read_text(encoding="utf-8"), py.name)

    def test_the_guest_ipv6_claim_is_not_made_by_the_script(self):
        # The script edits only the EFI boot menu; IPv6 is not turned off in the guest here (see the settings doc)
        self.assertNotIn("sysctl", script_text())
        self.assertNotIn("bootcmd", script_text())


class SandboxImageDocsTest(unittest.TestCase):
    def block(self) -> str:
        text = SETTINGS.read_text(encoding="utf-8")
        start = text.index("## Sandbox base image")
        return text[start:]

    def test_the_settings_doc_has_one_block_with_the_exact_command(self):
        text = SETTINGS.read_text(encoding="utf-8")
        self.assertEqual(text.count("## Sandbox base image"), 1)
        self.assertIn("powershell -ExecutionPolicy Bypass -File scripts\\sandbox-image.ps1 -Action Build "
                      "-ReleaseDate 20261015", self.block())
        self.assertIn("-Action Check", self.block())
        self.assertIn("-Action Remove", self.block())

    def test_the_block_says_it_is_one_time_and_names_the_setting(self):
        block = self.block()
        self.assertIn("one-time administrator step", block.lower())
        self.assertIn("SANDBOX_BASE_IMAGE", block)
        self.assertIn("never", block.lower())
        self.assertIn("current", block)

    def test_the_block_describes_the_boot_menu_and_its_limit(self):
        block = self.block()
        self.assertIn("search.fs_uuid", block)
        self.assertIn("PARTUUID", block)
        self.assertIn("alphabetical", block)
        self.assertNotIn("vmlinuz-7", block)

    def test_the_block_is_honest_about_ipv6(self):
        block = self.block()
        self.assertIn("IPv6", block)
        # The app's cloud-init user-data does not disable IPv6 today; the block must not claim it does
        self.assertNotIn("user-data disables", block)
        self.assertIn("does not", block)


class SignatureAndHashTest(unittest.TestCase):
    """The signature check of SHA256SUMS (with a pinned key, before the big download, skipped with a note when gpg.exe
    is missing) and the SHA-256 that Build saves and Check -Deep compares. Text-level only."""

    def test_the_ubuntu_key_is_pinned_and_marked_for_re_verification(self):
        text = script_text()
        self.assertIn('$CloudKeyId = "1A5D6C4C7DB87C81"', text)
        self.assertIn('$CloudKeyFingerprint = "D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81"', text)
        self.assertTrue(cloud_key_matches_id())
        self.assertIn("MUST BE RE-VERIFIED", text)
        self.assertIn("https://ubuntu.com/tutorials/how-to-verify-ubuntu", text)

    def test_verify_signature_defaults_to_true_and_is_a_bool_not_a_switch(self):
        text = script_text()
        self.assertIn("[bool]$VerifySignature = $true", text)
        self.assertNotIn("[switch]$VerifySignature", text)
        self.assertNotIn("$Switch", text)

    def test_deep_is_a_switch_that_only_check_uses(self):
        text = script_text()
        self.assertIn("[switch]$Deep", text)
        self.assertIn("Show-ImageCheck $parent ([bool]$Deep)", text)
        self.assertIn("function Show-ImageCheck([string]$Parent, [bool]$Deep)", text)

    def test_the_signature_is_checked_before_the_image_is_downloaded(self):
        text = script_text()
        self.assertLess(text.index("Assert-SumsSignature $sums $sig $gpg.Source"),
                        text.index('& curl.exe -fsSL -o $tar "$ImageRoot/$ReleaseDate/$ImageName"'))

    def test_a_missing_gpg_skips_the_check_with_a_note_and_does_not_fail(self):
        build = function_body(script_text(), "Build-SandboxImage")
        self.assertIn('Get-Command gpg.exe -ErrorAction SilentlyContinue', build)
        self.assertIn("} elseif (-not $gpg) {", build)
        self.assertIn("gpg.exe isn't installed, so SHA256SUMS.gpg was NOT checked.", build)
        self.assertIn("SHA256SUMS.gpg", build)
        self.assertIn("if (-not $VerifySignature) {", build)

    def test_the_signature_check_uses_only_the_pinned_key_and_never_a_keyserver(self):
        text = script_text()
        body = function_body(text, "Assert-SumsSignature")
        self.assertIn("--list-keys $CloudKeyFingerprint", body)
        self.assertIn("--status-fd 1 --verify $Signature $Sums", body)
        self.assertIn("VALIDSIG", body)
        self.assertIn("if ($LASTEXITCODE -ne 0 -or $good.Count -ne 1)", body)
        self.assertIn("isn't in gpg's keyring", body, "a missing key is refused, not fetched")
        self.assertNotIn("--keyserver", text, "the script never fetches a key")
        self.assertNotIn("recv-keys", text)

    def test_the_build_saves_the_hash_next_to_the_disk_and_check_compares_it_only_with_deep(self):
        text = script_text()
        build = function_body(text, "Build-SandboxImage")
        self.assertIn("Set-Content -LiteralPath (Get-HashPath $Parent) -Value $hash -Encoding ASCII", build)
        self.assertEqual(function_body(text, "Get-HashPath").count('"$Parent.sha256"'), 1)
        body = function_body(text, "Show-ImageCheck")
        self.assertIn("Get-SavedHash $Parent", body)
        self.assertLess(body.index("if ($Deep) {"), body.index("Get-FileHash"))
        self.assertEqual(body.count("Get-FileHash"), 1, "only the -Deep branch hashes the disk")

    def test_the_saved_hash_must_be_a_sha256_value_or_it_counts_as_missing(self):
        body = function_body(script_text(), "Get-SavedHash")
        self.assertIn("'^[0-9A-Fa-f]{64}$'", body)
        self.assertIn('return ""', body)

    def test_remove_deletes_the_saved_hash_only_as_part_of_the_base_disk(self):
        remove = function_body(script_text(), "Remove-SandboxImage")
        self.assertIn("Remove-Item -LiteralPath (Get-HashPath $Parent) -Force -ErrorAction SilentlyContinue", remove)
        self.assertLess(remove.index("Remove-Item -LiteralPath $Parent -Force"),
                        remove.index("Remove-Item -LiteralPath (Get-HashPath $Parent)"))

    def test_the_nocloud_kernel_argument_is_unchanged(self):
        # Changing it to ds=nocloud is untested, so the script keeps the form that was tested
        text = script_text()
        self.assertIn("ci.ds=NoCloud", text)
        self.assertNotIn("ds=nocloud", text.lower().replace("ci.ds=nocloud", ""))


def cloud_key_matches_id() -> bool:
    """The key ID is the last 16 hex digits of the pinned fingerprint."""
    text = script_text()
    fingerprint = re.search(r'\$CloudKeyFingerprint = "([0-9A-F]{40})"', text).group(1)
    return fingerprint.endswith(re.search(r'\$CloudKeyId = "([0-9A-F]{16})"', text).group(1))


class SignatureDocsTest(unittest.TestCase):
    def plan(self) -> str:
        return (ROOT / "docs" / "CODE_SANDBOX_PLAN.md").read_text(encoding="utf-8")

    def test_the_key_import_is_documented_with_the_pinned_fingerprint_and_the_re_verify_step(self):
        plan = self.plan()
        self.assertIn("### Base image signature check (Build)", plan)
        self.assertIn("D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81", plan)
        self.assertIn("gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys 0xD2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81",
                      plan)
        self.assertIn("https://ubuntu.com/tutorials/how-to-verify-ubuntu", plan)

    def test_the_known_limits_list_names_the_four_limits(self):
        plan = self.plan()
        start = plan.index("## Known limits")
        section = plan[start:plan.index("## 3. Mentor-only model")]
        self.assertIn("uploads", section.lower())
        self.assertIn("github.com", section)
        self.assertIn("pypi.org", section)
        self.assertIn("No WSL fallback", section)
        self.assertIn("alphabetically", section)
        self.assertIn("ci.ds=NoCloud", section)
        self.assertIn("23.2", section)
        self.assertIn("re-test", section.lower())

    def test_the_settings_block_mentions_the_signature_and_deep_options(self):
        block = SETTINGS.read_text(encoding="utf-8")
        block = block[block.index("## Sandbox base image"):]
        for text in ("SHA256SUMS.gpg", "-VerifySignature:$false", "sandbox-base.vhdx.sha256", "-Action Check -Deep"):
            self.assertIn(text, block)


if __name__ == "__main__":
    unittest.main()


class PowerShellParsePitfallTest(unittest.TestCase):
    """`"$name: text"` doesn't parse on PowerShell 5.1 (it reads $name: as a drive reference); use ${name}: instead."""

    def test_no_variable_is_followed_by_a_colon_and_a_space_in_any_script(self):
        import re
        from pathlib import Path
        scripts = Path(__file__).resolve().parent.parent / "packaging" / "windows" / "scripts"
        pattern = re.compile(r"(?<!\{)\$[A-Za-z_]\w*:(?=[\s\"'.)\]]|$)")
        bad = []
        for path in sorted(scripts.glob("*.ps1")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    bad.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(bad, [], "write ${name}: instead of $name: inside strings")


class BaseDiskAccessTest(unittest.TestCase):
    def test_build_limits_who_can_change_the_base_disk(self):
        text = (Path(__file__).resolve().parent.parent / "packaging" / "windows" / "scripts"
                / "sandbox-image.ps1").read_text(encoding="ascii")
        self.assertIn("/inheritance:r /grant:r", text)
        self.assertIn("*S-1-5-18:F", text)
        self.assertIn("*S-1-5-32-544:F", text)
        self.assertIn("${ServiceAccount}:(R)", text)


class BaseDiskPermissionsKeptTest(unittest.TestCase):
    """An install or restore resets the data folder's permissions; that must not strip the base disk's VM-group read."""

    def test_the_data_folder_reset_puts_the_base_disk_permissions_back(self):
        text = (SCRIPTS / "common.ps1").read_text(encoding="utf-8")
        self.assertIn("function Restore-SandboxImageAccess", text)
        reset = text.index("/reset /T /C /Q")
        self.assertLess(reset, text.index("Restore-SandboxImageAccess\n}"), "called after the reset")
        self.assertIn('"*S-1-5-83-0:(R)"', text)
        self.assertIn("(R)", text[text.index("function Restore-SandboxImageAccess"):])

    def test_the_image_check_reports_missing_vm_read_access(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("S-1-5-83-0", text[text.index("function Show-ImageCheck"):])
        self.assertTrue(text.isascii())

# --------------------------------------------------------------------------- the Code tools in the image
from subzero_ai import sandbox_image  # noqa: E402
from subzero_ai.sandbox import ExitResult, FakeSandbox, SandboxHandle  # noqa: E402

PROVISION_SH = ROOT / "subzero_ai" / "sandbox_provision.sh"
# What each TOOLS_VERSION was built from. Changing the Node.js pin, the runner or the Agent SDK without bumping
# sandbox_image.TOOLS_VERSION would leave old images looking current, so this table has to change with it.
PINS = {1: {"node": "22.23.4", "runner": "0.1.0", "sdk": "0.3.316"}}


def load_build():
    spec = importlib.util.spec_from_file_location("build_windows", ROOT / "packaging" / "build_windows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToolsVersionPinsTest(unittest.TestCase):
    def test_the_pins_match_the_tools_version(self):
        self.assertEqual(PINS[sandbox_image.TOOLS_VERSION], {
            "node": sandbox_image.NODE_VERSION, "runner": sandbox_image.expected_runner_version(),
            "sdk": sandbox_image.expected_sdk_version()},
            "something baked into the image changed: bump sandbox_image.TOOLS_VERSION and add a row to PINS")

    def test_node_is_the_version_the_windows_build_pins(self):
        self.assertEqual(sandbox_image.NODE_VERSION, load_build().NODE_VERSION)
        self.assertRegex(sandbox_image.NODE_LINUX_X64_SHA256, r"^[0-9a-f]{64}$")

    def test_expected_versions_come_from_the_runner_package(self):
        data = json.loads((ROOT / "sandbox_runner" / "package.json").read_text())
        self.assertEqual(sandbox_image.expected_runner_version(), data["version"])
        self.assertEqual(sandbox_image.expected_sdk_version(), data["dependencies"]["@anthropic-ai/claude-agent-sdk"])
        self.assertRegex(sandbox_image.expected_sdk_version(), r"^\d+\.\d+\.\d+$", "an exact version")

    def test_expected_versions_work_from_the_windows_install_layout(self):
        tmp = Path(tempfile.mkdtemp(prefix="layout-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        app = tmp / "app"
        shutil.copytree(ROOT / "subzero_ai", app / "subzero_ai", ignore=shutil.ignore_patterns("__pycache__"))
        build = load_build()
        build.add_sandbox_runner(app)
        self.assertEqual(sandbox_image.expected_runner_version(app / "sandbox_runner"),
                         sandbox_image.expected_runner_version())
        # the module finds the runner folder beside the package, in the source tree and in app\
        self.assertEqual(sandbox_image.RUNNER_SOURCE, ROOT / "sandbox_runner")
        self.assertEqual((app / "subzero_ai" / "sandbox_image.py").parent.parent / "sandbox_runner", app / "sandbox_runner")

    def test_messages_are_plain_and_name_neither_the_product_nor_the_team(self):
        for text in (sandbox_image.MISSING_MESSAGE, sandbox_image.OUTDATED_MESSAGE, sandbox_image.CURRENT_MESSAGE):
            self.assertNotIn("Claude Code", text)
            self.assertNotRegex(text, r"5690|SubZero Robotics")
        self.assertEqual(sandbox_image.MISSING_MESSAGE, "The sandbox image doesn't have the Code tools yet. Run "
                         "prepare-sandbox-image.cmd on the server, then try again.")
        self.assertIn("out of date", sandbox_image.OUTDATED_MESSAGE)
        self.assertIn("prepare-sandbox-image.cmd", sandbox_image.OUTDATED_MESSAGE)


class ScriptedBackend:
    """A backend whose `run` answers from a table, to test check_tools on the real (non-fake) path."""
    name = "scripted"

    def __init__(self, answers: dict[str, tuple[int, str]]):
        self.answers = answers
        self.calls: list[list[str]] = []

    def run(self, handle, argv, *, cwd, env, timeout, on_output=None):
        self.calls.append(list(argv))
        for key, (code, text) in self.answers.items():
            if key in " ".join(argv):
                return ExitResult(code, text)
        return ExitResult(127, "command not found")


HANDLE = SandboxHandle("szsandbox-1-s000-aaaaaa", "scripted", "")


def runner_line(runner=None, sdk=None) -> str:
    return json.dumps({"runner_version": runner or sandbox_image.expected_runner_version(),
                       "sdk_version": sdk or sandbox_image.expected_sdk_version()})


class CheckToolsTest(unittest.TestCase):
    def test_a_current_image_is_ok(self):
        backend = ScriptedBackend({"TOOLS_VERSION": (0, f"{sandbox_image.TOOLS_VERSION}\n"),
                                   "runner.mjs --version": (0, runner_line() + "\n")})
        status = sandbox_image.check_tools(backend, HANDLE)
        self.assertEqual((status.state, status.ok), ("ok", True))
        self.assertEqual(backend.calls[0], ["cat", "/opt/subzero-runner/TOOLS_VERSION"])
        self.assertEqual(backend.calls[1], ["node", "/opt/subzero-runner/runner.mjs", "--version"])

    def test_an_image_without_the_tools_is_missing(self):
        for answers in ({}, {"TOOLS_VERSION": (1, "cat: No such file")},
                        {"TOOLS_VERSION": (0, "1"), "runner.mjs --version": (127, "node: command not found")},
                        {"TOOLS_VERSION": (0, "1"), "runner.mjs --version": (0, "garbage")},
                        {"TOOLS_VERSION": (0, "banana")}):
            status = sandbox_image.check_tools(ScriptedBackend(answers), HANDLE)
            self.assertEqual(status.state, "missing", answers)
            self.assertEqual(status.message, sandbox_image.MISSING_MESSAGE)

    def test_an_old_image_is_out_of_date(self):
        for answers in (
                {"TOOLS_VERSION": (0, str(sandbox_image.TOOLS_VERSION - 1)), "runner.mjs --version": (0, runner_line())},
                {"TOOLS_VERSION": (0, str(sandbox_image.TOOLS_VERSION)),
                 "runner.mjs --version": (0, runner_line(runner="0.0.1"))},
                {"TOOLS_VERSION": (0, str(sandbox_image.TOOLS_VERSION)),
                 "runner.mjs --version": (0, runner_line(sdk="0.0.1"))}):
            status = sandbox_image.check_tools(ScriptedBackend(answers), HANDLE)
            self.assertEqual(status.state, "outdated", answers)
            self.assertEqual(status.message, sandbox_image.OUTDATED_MESSAGE)

    def test_the_fake_sandbox_has_the_tools_unless_told_otherwise(self):
        tmp = Path(tempfile.mkdtemp(prefix="fake-tools-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        backend = FakeSandbox(tmp)
        handle = backend.create(1)
        self.assertTrue(sandbox_image.check_tools(backend, handle).ok)
        backend.tools = "missing"
        self.assertEqual(sandbox_image.check_tools(backend, handle).state, "missing")
        backend.tools = "outdated"
        self.assertEqual(sandbox_image.check_tools(backend, handle).state, "outdated")
        self.assertEqual(FakeSandbox(tmp, tools="missing").tools, "missing")


class ProvisionScriptTest(unittest.TestCase):
    def text(self) -> str:
        return PROVISION_SH.read_text(encoding="utf-8")

    def test_it_is_valid_bash_with_lf_line_endings(self):
        self.assertNotIn(b"\r", PROVISION_SH.read_bytes())
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash isn't installed here")
        done = subprocess.run([bash, "-n", str(PROVISION_SH)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_it_is_strict_and_never_traces(self):
        text = self.text()
        self.assertIn("set -euo pipefail", text)
        self.assertNotRegex(text, r"(?m)^\s*set\s+-[a-z]*x")
        self.assertNotIn("bash -x", text)
        self.assertIn("DEBIAN_FRONTEND=noninteractive", text)

    def test_it_contains_no_token_or_key(self):
        text = self.text()
        for pattern in (r"sk-ant-[A-Za-z0-9_-]{10,}", r"xox[bap]-", r"ghp_[A-Za-z0-9]{20,}", r"[A-Za-z0-9_-]{43}=?\b",
                        r"BEGIN [A-Z ]*PRIVATE KEY", r"://[^/\s:$]+:[^@\s$]+@"):
            self.assertIsNone(re.search(pattern, text), pattern)
        # the proxy login is read from the environment, and only ever tested for being there
        self.assertIn("https_proxy", text)
        self.assertNotRegex(text, r"echo[^\n]*\$\{?https?_proxy|echo[^\n]*HTTPS_PROXY")
        self.assertNotRegex(text, r">\s*/etc/[^\n]*\$\{?https?_proxy")

    def test_apt_goes_over_https_only(self):
        text = self.text()
        self.assertIn("s#http://#https://#g", text)
        self.assertLess(text.index("use_https_sources\n  apt-get"), text.index("update"))
        self.assertNotRegex(text, r"https?://[a-z.]*ubuntu\.com")

    def test_it_installs_the_required_packages(self):
        text = self.text()
        for package in ("git", "python3", "python3-pip", "python3-venv", "unzip", "zip", "curl", "ca-certificates",
                        "build-essential", "openjdk-17-jdk-headless"):
            self.assertRegex(text, rf"\b{re.escape(package)}\b", package)

    def test_node_is_checked_against_its_hash_before_it_is_unpacked(self):
        text = self.text()
        self.assertIn("sha256sum -c", text)
        self.assertLess(text.index("sha256sum -c"), text.index("tar -xzf"))
        self.assertIn("https://nodejs.org/dist/v${NODE_VERSION}/", text)
        self.assertNotIn(sandbox_image.NODE_LINUX_X64_SHA256, text, "the pin is defined once, in Python")
        self.assertNotRegex(text, r"\b[0-9a-f]{64}\b")

    def test_the_runner_is_installed_from_the_lock_file_and_locked_down(self):
        text = self.text()
        self.assertIn("npm ci --omit=dev", text)
        self.assertIn("claude-agent-sdk-linux-x64", text)
        self.assertIn("chown -R root:root", text)
        self.assertIn("chmod -R go-w", text)
        self.assertIn("/opt/subzero-runner", text)
        self.assertLess(text.index("npm ci"), text.index('> "$RUNNER_DIR/TOOLS_VERSION"'), "the version is written last")

    def test_managed_settings_turn_off_background_traffic(self):
        text = self.text()
        start = text.index("<<'JSON'") + len("<<'JSON'")
        block = text[start:text.index("\nJSON\n", start)]
        settings = json.loads(block)
        self.assertEqual(settings["permissions"], {"deny": []})
        self.assertEqual(settings["allowManagedHooksOnly"], True)
        self.assertLessEqual(settings["cleanupPeriodDays"], 3)
        for key in ("DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING", "DISABLE_AUTOUPDATER",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
            self.assertEqual(settings["env"][key], "1")
        self.assertIn("/etc/claude-code/managed-settings.json", text)
        self.assertIn("chmod 0644 /etc/claude-code/managed-settings.json", text)

    def test_git_and_the_caches_are_set_up_without_a_baked_proxy(self):
        text = self.text()
        self.assertIn("safe.directory '*'", text)
        self.assertIn("user.name 'Code'", text)
        self.assertIn("user.email 'code@localhost'", text)
        self.assertIn(".gradle", text)
        self.assertIn(".npm", text)
        self.assertNotRegex(text, r"(?i)git config[^\n]*proxy|npm config set[^\n]*proxy|proxy\s*=")

    def test_the_finish_step_makes_the_next_boot_a_fresh_one(self):
        text = self.text()
        finish = text[text.index("do_finish() {"):]
        for needed in ("rm -f /etc/sudoers.d/90-cloud-init-users", "cloud-init clean --logs --seed",
                       "/etc/ssh/ssh_host_*", "truncate -s 0 /etc/machine-id", "/etc/netplan/50-cloud-init.yaml",
                       "apt-get clean", "systemctl poweroff"):
            self.assertIn(needed, finish)
        self.assertNotRegex(text, r"dd if=/dev/zero|zerofree|fstrim", "no long disk wipes")
        self.assertLess(finish.index("sudoers.d/90-cloud-init-users"), finish.index("poweroff"))

    def test_the_script_takes_everything_it_needs_from_the_environment(self):
        for name in ("TOOLS_VERSION", "NODE_VERSION", "NODE_SHA256"):
            self.assertIn(name, self.text())
        env = sandbox_image.script_env("http://x:y@10.77.0.1:3128")
        self.assertTrue({"TOOLS_VERSION", "NODE_VERSION", "NODE_SHA256", "HTTPS_PROXY", "https_proxy"} <= set(env))
        self.assertNotIn("JAVA_TOOL_OPTIONS", env)


class ReleaseZipTest(unittest.TestCase):
    """What goes into SubZeroAI-windows.zip for the image step. The full build (255 MB) isn't run here."""

    def test_the_runner_is_copied_as_source_and_lock_file_only(self):
        build = load_build()
        tmp = Path(tempfile.mkdtemp(prefix="zip-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (ROOT / "sandbox_runner" / "node_modules").mkdir(exist_ok=True)  # as on a dev PC after npm ci
        marker = ROOT / "sandbox_runner" / "node_modules" / ".marker"
        marker.write_text("x")
        self.addCleanup(lambda: (marker.unlink(missing_ok=True), shutil.rmtree(ROOT / "sandbox_runner" / "node_modules", True)))
        build.add_sandbox_runner(tmp)
        files = sorted(p.relative_to(tmp / "sandbox_runner").as_posix() for p in (tmp / "sandbox_runner").rglob("*")
                       if p.is_file())
        for needed in ("package.json", "package-lock.json", "runner.mjs", "lib/run.mjs", "lib/events.mjs",
                       "lib/options.mjs"):
            self.assertIn(needed, files)
        self.assertFalse([f for f in files if "node_modules" in f or f.startswith("test/")])
        self.assertEqual(sandbox_image.runner_archive.__name__, "runner_archive")

    def test_the_new_scripts_ship_and_are_copied_by_the_updater(self):
        scripts = sorted(p.name for p in SCRIPTS.glob("*.ps1"))
        cmds = sorted(p.name for p in (SCRIPTS.parent / "cmd").glob("*.cmd"))
        self.assertIn("prepare-sandbox-image.ps1", scripts)
        self.assertIn("prepare-sandbox-image.cmd", cmds)
        self.assertIn("sandbox_provision.sh", [p.name for p in (ROOT / "subzero_ai").iterdir()])
        common = (SCRIPTS / "common.ps1").read_text(encoding="utf-8")
        self.assertIn('"prepare-sandbox-image.cmd"', common[common.index("$AppFiles"):], "update.cmd copies it too")
        build = (ROOT / "packaging" / "build_windows.py").read_text(encoding="utf-8")
        self.assertIn("add_sandbox_runner(app)", build)
        self.assertIn('glob("*.cmd")', build)
        self.assertIn('glob("*.ps1")', build)
        self.assertIn("copytree(ROOT / \"subzero_ai\"", build, "sandbox_provision.sh travels inside the app folder")

    def test_the_wrapper_stops_the_app_and_always_starts_it_again(self):
        text = (SCRIPTS / "prepare-sandbox-image.ps1").read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        self.assertIn("Assert-Admin", text)
        self.assertIn("Stop-AppService", text)
        self.assertIn('"sandbox-provision"', text)
        self.assertLess(text.index("Stop-AppService"), text.index("Invoke-AppTool"))
        finally_block = text[text.index("} finally {"):]
        self.assertIn("Start-AppService", finally_block)
        cmd = (SCRIPTS.parent / "cmd" / "prepare-sandbox-image.cmd").read_text(encoding="utf-8")
        self.assertIn("scripts\\prepare-sandbox-image.ps1", cmd)

    def test_the_image_script_points_at_the_next_step_and_the_readme_explains_it(self):
        self.assertIn("prepare-sandbox-image.cmd", script_text()[script_text().index("Built the base disk"):])
        readme = (SCRIPTS.parent / "README.txt").read_text(encoding="utf-8")
        self.assertIn("prepare-sandbox-image.cmd", readme)
        self.assertNotIn("Claude Code", readme)

