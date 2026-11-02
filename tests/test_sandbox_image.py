"""The Windows base-image script (packaging/windows/scripts/sandbox-image.ps1) and its docs block. Text-level only:
no PowerShell, no Hyper-V and no network are used. The tests check the pitfalls, the safety rules and the steps that
must stay in the script, in the order they must run."""

from __future__ import annotations

import re
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
            self.assertNotIn("sandbox-image", other.read_text(encoding="utf-8", errors="replace"), other.name)
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
