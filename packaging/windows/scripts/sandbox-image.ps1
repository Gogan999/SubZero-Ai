#Requires -RunAsAdministrator
# Builds, checks, or removes the base disk that every Code sandbox is made from (see docs/SETTINGS.md,
# "Sandbox base image"). Build is a one-time administrator step on the mini PC. Nothing runs it automatically.
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-image.ps1 -Action Build -ReleaseDate 20261015
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-image.ps1 -Action Check
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-image.ps1 -Action Remove
# Build: downloads the Ubuntu 24.04 (noble) Azure cloud image for one dated release, checks the release's SHA256SUMS
# against the Ubuntu cloud image key (SHA256SUMS.gpg, when gpg.exe is installed), checks the image's SHA-256 against
# SHA256SUMS, converts it to a dynamic VHDX, makes it read-only, and sets its boot menu so cloud-init reads
# the sandbox's seed (ci.ds=NoCloud). It needs 45 GB free: the raw disk image is 30 GB while it is converted. Build
# refuses to overwrite an existing base disk, and removes a partial one if anything fails before it is read-only.
# Build saves the finished disk's SHA-256 in sandbox-base.vhdx.sha256, next to it.
# Check: changes nothing. With -Deep it also hashes the base disk (about a minute) and compares the hash with that
# saved file. Remove: deletes only the base disk and its saved SHA-256 file, and only when no VM disk uses it as its
# parent.
# Only the file named by -ParentName, inside -Folder, and its .sha256 file are ever written or deleted. The folder must
# be inside the SubZero AI data folder's sandbox folder, which Build creates.
# The release date must be a dated folder (such as 20261015), never "current", so the image never changes under the app.

param(
    [ValidateSet("Build", "Check", "Remove")]
    [string]$Action = "Check",
    [string]$ReleaseDate = "",
    [string]$Folder = "C:\ProgramData\SubZeroAI\sandbox\images",
    [string]$ParentName = "sandbox-base.vhdx",
    # On by default. A PowerShell switch can't default to true, so this is a bool: -VerifySignature:$false turns it off.
    [bool]$VerifySignature = $true,
    # With -Action Check: also hash the base disk (about a minute) and compare it with the SHA-256 that Build saved.
    [switch]$Deep
)

. (Join-Path $PSScriptRoot "common.ps1")

$ReleasePattern = '^[0-9]{8}(\.[0-9]+)?$'  # a dated release folder, such as 20261015 or 20261015.1
$ImageRoot = "https://cloud-images.ubuntu.com/noble"
$ImageName = "noble-server-cloudimg-amd64-azure.vhd.tar.gz"
$ImageVhdName = "livecd.ubuntu-cpc.azure.vhd"  # the disk inside the tar
$EspType = "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}"  # the GPT type of the EFI system partition
$LinuxFsType = "{0fc63daf-8483-4772-8e79-3d69d8477de4}"  # the GPT type of a Linux filesystem partition (the root)
$NeedFreeGB = 45
$SandboxRoot = Join-Path $DataDir "sandbox"
# The Ubuntu Cloud Image Builder key that signs each release's SHA256SUMS (as SHA256SUMS.gpg). The key ID and the full
# fingerprint are both pinned, so any other key is refused. THIS MUST BE RE-VERIFIED against
# https://ubuntu.com/tutorials/how-to-verify-ubuntu before first use. Do that before trusting the signature check.
$CloudKeyId = "1A5D6C4C7DB87C81"
$CloudKeyFingerprint = "D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81"

# Refuses '..' and any folder outside the sandbox folder, so only the sandbox's own files are ever written.
function Get-SafeFolder([string]$Path) {
    if ($Path -match '\.\.') { throw "The folder can't contain two dots in a row. Give the full path, such as $SandboxRoot\images." }
    if (-not [System.IO.Path]::IsPathRooted($Path)) { throw "Give the folder as a full path, such as $SandboxRoot\images." }
    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $root = [System.IO.Path]::GetFullPath($SandboxRoot).TrimEnd('\')
    if (-not $full.StartsWith($root + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The folder must be inside $root, so this script only writes where the sandboxes keep their files."
    }
    return $full
}

function Get-ParentPath([string]$Dir) {
    if ($ParentName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*\.vhdx$') {
        throw "The file name must end in .vhdx and have no folders in it, such as sandbox-base.vhdx."
    }
    return Join-Path $Dir $ParentName
}

function Assert-HyperVTools {
    foreach ($cmd in @("Get-VM", "Get-VHD", "Convert-VHD", "Mount-VHD")) {
        if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
            throw "Hyper-V's PowerShell tools aren't installed, or Hyper-V isn't turned on."
        }
    }
}

function Get-FreeGB([string]$Dir) {
    $drive = New-Object -TypeName System.IO.DriveInfo -ArgumentList ([System.IO.Path]::GetPathRoot($Dir))
    return [Math]::Floor($drive.AvailableFreeSpace / 1GB)
}

# The SHA-256 for one file, from a SHA256SUMS list ("hash *name" or "hash  name" per line)
function Get-ExpectedHash([string]$SumsFile, [string]$FileName) {
    foreach ($line in @(Get-Content -LiteralPath $SumsFile)) {
        if (($line -match '^\s*([0-9a-fA-F]{64})\s+\*?(\S+)\s*$') -and ($Matches[2] -eq $FileName)) {
            return $Matches[1].ToUpper()
        }
    }
    throw "The checksum list has no line for $FileName, so the download can't be checked."
}

# The file Build saves the base disk's SHA-256 in, next to the disk. Check -Deep compares against it.
function Get-HashPath([string]$Parent) {
    return "$Parent.sha256"
}

# The SHA-256 that Build saved for the base disk, in upper case, or "" when there is no usable saved value.
function Get-SavedHash([string]$Parent) {
    $path = Get-HashPath $Parent
    if (-not (Test-Path -LiteralPath $path)) { return "" }
    $text = [System.IO.File]::ReadAllText($path).Trim()
    if ($text -notmatch '^[0-9A-Fa-f]{64}$') { return "" }
    return $text.ToUpper()
}

# Checks SHA256SUMS against its detached signature, using only the pinned key. The key must already be in gpg's
# keyring: an administrator imports it once (docs/CODE_SANDBOX_PLAN.md says how). Nothing here calls a keyserver.
function Assert-SumsSignature([string]$Sums, [string]$Signature, [string]$Gpg) {
    $listing = @(& $Gpg --batch --with-colons --list-keys $CloudKeyFingerprint 2>$null)
    if (@($listing | Where-Object { $_ -like "fpr:*:${CloudKeyFingerprint}:*" }).Count -eq 0) {
        throw ("The Ubuntu cloud image key ($CloudKeyId) isn't in gpg's keyring, so the checksum list can't be " +
            "checked. Import the key once, as docs/CODE_SANDBOX_PLAN.md describes. The download was not used.")
    }
    $status = @(& $Gpg --batch --status-fd 1 --verify $Signature $Sums 2>$null)
    $good = @($status | Where-Object { $_ -match "^\[GNUPG:\] VALIDSIG .*${CloudKeyFingerprint}" })
    if ($LASTEXITCODE -ne 0 -or $good.Count -ne 1) {
        throw ("The checksum list's signature isn't a good signature from the Ubuntu cloud image key, so the " +
            "download was not used. Run Build again.")
    }
    Write-Ok "The checksum list is signed by the Ubuntu cloud image key ($CloudKeyId)."
}

# The search line from the image's EFI grub.cfg. It points GRUB at the boot partition (ext4, which Windows can't read),
# and it is kept exactly as the image has it. Stops with a plain message if it is missing; nothing is changed then.
function Read-SearchLine([string]$Text) {
    foreach ($raw in ($Text -split "`r?`n")) {
        $line = $raw.Trim()
        if ($line -match '^search\.fs_uuid\s+\S+\s+root(\s|$)') { return $line }
    }
    throw ("The image's EFI boot menu has no search.fs_uuid line, so GRUB can't find its boot partition. " +
        "The boot menu was left as it was.")
}

# The PARTUUID of the root filesystem: the largest Linux filesystem partition on the disk. For a GPT disk the PARTUUID
# is the partition's own unique GUID (Get-Partition's Guid), so it is written without braces and in lower case.
# Stops with a plain message if there is no such partition, or two equally large ones, so nothing is guessed.
function Get-RootPartUuid([int]$DiskNumber) {
    $linux = @(Get-Partition -DiskNumber $DiskNumber -ErrorAction Stop | Where-Object { $_.GptType -eq $LinuxFsType })
    if ($linux.Count -eq 0) {
        throw ("The image has no Linux filesystem partition, so the root partition can't be found. " +
            "The boot menu was left as it was.")
    }
    $biggest = ($linux | Measure-Object -Property Size -Maximum).Maximum
    $top = @($linux | Where-Object { $_.Size -eq $biggest })
    if ($top.Count -ne 1) {
        throw ("The image has $($top.Count) Linux partitions of the same largest size, so the root partition " +
            "can't be told apart. The boot menu was left as it was.")
    }
    $guid = ($top[0].Guid -replace '[{}]', '').ToLowerInvariant()
    if ($guid -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
        throw "Windows gave the root partition an unreadable GUID, so the boot menu was left as it was."
    }
    return $guid
}

# The new boot menu: no pause, the sandbox entry only, cloud-init told to use the NoCloud seed. The kernel and the
# initrd are not named here: GRUB finds the files at boot. The loops set each variable to the last match of the glob,
# which is the last name in alphabetical order (the highest version while the version numbers have the same length).
# The lines use single quotes so that GRUB's $variables are not expanded by PowerShell; backticks escape the rest.
function New-BootMenu([string]$SearchLine, [string]$PartUuid) {
    $lines = @(
        $SearchLine,
        "set timeout=0",
        "set default=0",
        "set sandbox_kernel=",
        "set sandbox_initrd=",
        'for k in /vmlinuz-*; do set sandbox_kernel=$k; done',
        'for i in /initrd.img-*; do set sandbox_initrd=$i; done',
        "menuentry 'sandbox' {",
        "  linux `$sandbox_kernel root=PARTUUID=$PartUuid ro console=tty1 console=ttyS0 ci.ds=NoCloud",
        '  initrd $sandbox_initrd',
        "}"
    )
    return (($lines -join "`n") + "`n")
}

# Mounts the base disk, rewrites the boot menu on its EFI partition (keeping grub.cfg.orig), and unmounts it.
# The Azure image ignores a NoCloud seed unless the kernel command line has ci.ds=NoCloud, so this runs once.
function Set-EspBootMenu([string]$Parent) {
    Write-Step "Changing the boot menu so the sandbox reads its seed (NoCloud)"
    Mount-VHD -Path $Parent -ErrorAction Stop | Out-Null
    $diskNumber = (Get-VHD -Path $Parent -ErrorAction Stop).DiskNumber
    $esp = @(Get-Partition -DiskNumber $diskNumber -ErrorAction Stop | Where-Object { $_.GptType -eq $EspType })
    if ($esp.Count -ne 1) { throw "The image should have one EFI system partition and has $($esp.Count), so it was left alone." }
    $partNumber = $esp[0].PartitionNumber
    $letter = $null
    try {
        Add-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $partNumber -AssignDriveLetter
        $letter = (Get-Partition -DiskNumber $diskNumber -PartitionNumber $partNumber).DriveLetter
        if (-not $letter) { throw "Windows didn't give the EFI partition a drive letter." }
        $grubPath = "$($letter):\EFI\ubuntu\grub.cfg"
        $backupPath = "$($letter):\EFI\ubuntu\grub.cfg.orig"
        if (-not (Test-Path -LiteralPath $grubPath)) { throw "There is no EFI\ubuntu\grub.cfg in the image." }
        # Everything that can fail is read before anything is written
        $searchLine = Read-SearchLine ([System.IO.File]::ReadAllText($grubPath))
        $partUuid = Get-RootPartUuid $diskNumber
        if (-not (Test-Path -LiteralPath $backupPath)) { Copy-Item -LiteralPath $grubPath -Destination $backupPath }
        # UTF-8 without a byte-order mark, LF line endings
        $menu = New-BootMenu $searchLine $partUuid
        [System.IO.File]::WriteAllText($grubPath, $menu, (New-Object System.Text.UTF8Encoding($false)))
        Write-Ok "Set the boot menu to the sandbox entry with ci.ds=NoCloud (the old one is grub.cfg.orig)."
    } finally {
        if ($letter) {
            Remove-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $partNumber -AccessPath "$($letter):\" -ErrorAction SilentlyContinue
        }
        Dismount-VHD -Path $Parent -ErrorAction SilentlyContinue
    }
    if ((Get-VHD -Path $Parent -ErrorAction Stop).Attached) { throw "The base disk is still attached, so it can't be made read-only yet." }
}

# Rollback for a failed Build: unmount, then delete the partial base disk. Only ever called for the file Build made.
function Remove-PartialParent([string]$Parent) {
    Write-Problem "Build didn't finish, so the partial base disk is being removed."
    try { Dismount-VHD -Path $Parent -ErrorAction SilentlyContinue } catch { }
    if (Test-Path -LiteralPath $Parent) {
        try {
            Remove-Item -LiteralPath $Parent -Force
            Write-Note "Removed the partial base disk."
        } catch {
            Write-Problem "Couldn't remove the partial base disk $Parent. Delete it by hand before running Build again."
        }
    }
}

function Build-SandboxImage([string]$Dir, [string]$Parent) {
    Write-Step "Checking the disk space"
    if (Test-Path -LiteralPath $Parent) {
        throw "$Parent already exists, so it was left alone. Run -Action Remove first (only when no sandbox uses it), then Build again."
    }
    $free = Get-FreeGB $Dir
    if ($free -lt $NeedFreeGB) {
        throw "Build needs $NeedFreeGB GB free on the drive that holds $Dir, and there is $free GB. The Ubuntu disk image is 30 GB while it is converted, and the rest is room for the download and the finished disk."
    }
    Write-Ok "$free GB free."

    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    # A new work folder for this run only. It is deleted at the end, whatever happens.
    $work = Join-Path $Dir ("build-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $tar = Join-Path $work $ImageName
    $sums = Join-Path $work "SHA256SUMS"
    $sig = Join-Path $work "SHA256SUMS.gpg"
    $vhd = Join-Path $work $ImageVhdName
    try {
        Write-Step "Downloading the Ubuntu $ReleaseDate cloud image (this takes a while)"
        & curl.exe -fsSL -o $sums "$ImageRoot/$ReleaseDate/SHA256SUMS"
        if ($LASTEXITCODE -ne 0) {
            throw "Couldn't download the checksum list for release $ReleaseDate. Check the date: it must be a dated folder under $ImageRoot/."
        }
        # The signature is checked before the big download. Without gpg.exe the check is skipped, with a note.
        Write-Step "Checking the checksum list's signature"
        $gpg = Get-Command gpg.exe -ErrorAction SilentlyContinue
        if (-not $VerifySignature) {
            Write-Problem "The signature check is turned off by -VerifySignature, so SHA256SUMS was not checked."
        } elseif (-not $gpg) {
            Write-Problem ("gpg.exe isn't installed, so SHA256SUMS.gpg was NOT checked. " +
                "Install Gpg4win to check the signature.")
        } else {
            & curl.exe -fsSL -o $sig "$ImageRoot/$ReleaseDate/SHA256SUMS.gpg"
            if ($LASTEXITCODE -ne 0) {
                throw "Couldn't download the checksum list's signature for release $ReleaseDate."
            }
            Assert-SumsSignature $sums $sig $gpg.Source
        }
        & curl.exe -fsSL -o $tar "$ImageRoot/$ReleaseDate/$ImageName"
        if ($LASTEXITCODE -ne 0) { throw "Couldn't download the image (curl exit code $LASTEXITCODE)." }

        Write-Step "Checking the download's SHA-256 against the release's checksums"
        $expected = Get-ExpectedHash $sums $ImageName
        $actual = (Get-FileHash -LiteralPath $tar -Algorithm SHA256).Hash
        if ($actual -ne $expected) { throw "The download doesn't match the release's SHA-256 checksum, so it isn't used. Run Build again." }
        Write-Ok "The checksum matches."

        Write-Step "Unpacking the disk image"
        & tar.exe -xzf $tar -C $work
        if ($LASTEXITCODE -ne 0) { throw "Couldn't unpack the image (tar exit code $LASTEXITCODE)." }
        if (-not (Test-Path -LiteralPath $vhd)) { throw "The image doesn't contain $ImageVhdName, so it can't be used." }

        # Convert-VHD fails on a sparse file (0xC03A001A), so the flag is cleared first
        Write-Step "Clearing the sparse flag on the disk image"
        & fsutil.exe sparse setflag $vhd 0 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "fsutil couldn't clear the sparse flag (exit code $LASTEXITCODE)." }

        try {
            Write-Step "Converting it to a dynamic disk (about a minute)"
            Convert-VHD -Path $vhd -DestinationPath $Parent -VHDType Dynamic -BlockSizeBytes 1MB
            Remove-Item -LiteralPath $vhd -Force
            Set-EspBootMenu $Parent
            Write-Step "Making the base disk read-only"
            Set-ItemProperty -LiteralPath $Parent -Name IsReadOnly -Value $true
            Write-Step "Limiting who can change the base disk"
            # Only SYSTEM and Administrators may change it. Hyper-V adds each VM's own read permission when it uses it
            icacls $Parent /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "icacls couldn't limit access to the base disk (exit code $LASTEXITCODE)." }
            # The app's service only reads the disk (Get-VHD); a missing service account is not an error here
            icacls $Parent /grant "${ServiceAccount}:(R)" 2>&1 | Out-Null
        } catch {
            Remove-PartialParent $Parent
            throw
        }
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Step "Computing the SHA-256 of the base disk (about a minute)"
    $hash = (Get-FileHash -LiteralPath $Parent -Algorithm SHA256).Hash
    Set-Content -LiteralPath (Get-HashPath $Parent) -Value $hash -Encoding ASCII
    Write-Ok "Built the base disk: $Parent"
    Write-Note "SHA-256: $hash (saved in $(Get-HashPath $Parent) for Check -Deep)"
    Write-Note "Set SANDBOX_BASE_IMAGE=$Parent in config.env, or set Base disk for sandboxes in Settings to that path."
}

function Show-ImageCheck([string]$Parent, [bool]$Deep) {
    Write-Step "Checking the base disk"
    if (-not (Test-Path -LiteralPath $Parent)) {
        Write-Problem "There is no base disk at $Parent. An administrator can run this script with -Action Build."
        return 1
    }
    Write-Ok "Found $Parent"
    $problems = 0
    if (-not (Get-Item -LiteralPath $Parent).IsReadOnly) {
        Write-Problem "The base disk isn't read-only. Every sandbox would write into it. Run Build again, or set IsReadOnly on the file."
        $problems++
    }
    try {
        $vhd = Get-VHD -Path $Parent -ErrorAction Stop
        if ($vhd.VhdFormat -ne "VHDX") { Write-Problem "The base disk isn't a VHDX file."; $problems++ }
        if ($vhd.VhdType -ne "Dynamic") { Write-Problem "The base disk isn't a dynamic disk."; $problems++ }
    } catch {
        Write-Problem "Couldn't read the base disk ($($_.Exception.Message -replace '[\r\n]+', ' '))."
        $problems++
    }
    if ($problems -eq 0) { Write-Ok "The base disk is read-only and dynamic." }
    if ($Deep) {
        Write-Step "Computing the base disk's SHA-256 and comparing it with the one Build saved (about a minute)"
        $saved = Get-SavedHash $Parent
        if (-not $saved) {
            Write-Problem ("There is no saved SHA-256 for the base disk ($(Get-HashPath $Parent)). " +
                "Run Build again to save one.")
            $problems++
        } elseif ((Get-FileHash -LiteralPath $Parent -Algorithm SHA256).Hash -ne $saved) {
            Write-Problem ("The base disk's SHA-256 doesn't match the value Build saved, so the disk may have " +
                "changed. Run Build again.")
            $problems++
        } else {
            Write-Ok "The base disk's SHA-256 matches the value Build saved."
        }
    }
    return $problems
}

# The VM disks whose parent is the base disk. A VM disk that can't be read stops the removal (fails closed).
function Get-ChildDisks([string]$Parent) {
    $found = @()
    $full = [System.IO.Path]::GetFullPath($Parent)
    foreach ($drive in @(Get-VM -ErrorAction Stop | Get-VMHardDiskDrive -ErrorAction Stop)) {
        $disk = Get-VHD -Path $drive.Path -ErrorAction Stop
        if ($disk.ParentPath -and ([System.IO.Path]::GetFullPath($disk.ParentPath) -eq $full)) { $found += $drive.Path }
    }
    return $found
}

function Remove-SandboxImage([string]$Parent) {
    Write-Step "Removing the base disk"
    if (-not (Test-Path -LiteralPath $Parent)) {
        Write-Note "There is no base disk at $Parent. Nothing to remove."
        return
    }
    try {
        $users = @(Get-ChildDisks $Parent)
    } catch {
        throw "Couldn't check the VM disks, so the base disk was kept ($($_.Exception.Message -replace '[\r\n]+', ' '))."
    }
    if ($users.Count -gt 0) {
        throw "Kept ${Parent}: $($users.Count) VM disk(s) still use it as their base. Delete those sandboxes first, then run Remove again."
    }
    Remove-Item -LiteralPath $Parent -Force
    # The saved SHA-256 goes with the disk
    Remove-Item -LiteralPath (Get-HashPath $Parent) -Force -ErrorAction SilentlyContinue
    Write-Ok "Removed $Parent."
}

Write-Host "Sandbox base image: $Action" -ForegroundColor Cyan
$code = 0
try {
    $dir = Get-SafeFolder $Folder
    $parent = Get-ParentPath $dir
    Assert-HyperVTools
    if ($Action -eq "Build") {
        if (-not (Test-Admin)) { throw "Build needs administrator rights. Run it again as an administrator." }
        if ($ReleaseDate -notmatch $ReleasePattern) {
            throw "-ReleaseDate must be the dated release folder, eight digits such as 20261015. Words such as 'current' aren't accepted."
        }
        Build-SandboxImage $dir $parent
    }
    elseif ($Action -eq "Check") { if ((Show-ImageCheck $parent ([bool]$Deep)) -gt 0) { $code = 1 } }
    elseif ($Action -eq "Remove") { Remove-SandboxImage $parent }
    else { throw "Unknown action $Action." }
} catch {
    Write-Host "Stopped: $($_.Exception.Message)" -ForegroundColor Red
    $code = 1
}
Wait-Close
exit $code
