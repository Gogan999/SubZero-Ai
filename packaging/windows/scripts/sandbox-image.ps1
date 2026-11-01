#Requires -RunAsAdministrator
# Builds, checks, or removes the base disk that every Code sandbox is made from (see docs/SETTINGS.md,
# "Sandbox base image"). Build is a one-time administrator step on the mini PC. Nothing runs it automatically.
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-image.ps1 -Action Build -ReleaseDate 20261015
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-image.ps1 -Action Check
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-image.ps1 -Action Remove
# Build: downloads the Ubuntu 24.04 (noble) Azure cloud image for one dated release, checks its SHA-256 against that
# release's SHA256SUMS, converts it to a dynamic VHDX, makes it read-only, and sets its boot menu so cloud-init reads
# the sandbox's seed (ci.ds=NoCloud). It needs 45 GB free: the raw disk image is 30 GB while it is converted. Build
# refuses to overwrite an existing base disk, and removes a partial one if anything fails before it is read-only.
# Check: changes nothing. Remove: deletes only the base disk, and only when no VM disk uses it as its parent.
# Only the file named by -ParentName, inside -Folder, is ever written or deleted. The folder must be inside
# the SubZero AI data folder's sandbox folder, which Build creates.
# The release date must be a dated folder (such as 20261015), never "current", so the image never changes under the app.

param(
    [ValidateSet("Build", "Check", "Remove")]
    [string]$Action = "Check",
    [string]$ReleaseDate = "",
    [string]$Folder = "C:\ProgramData\SubZeroAI\sandbox\images",
    [string]$ParentName = "sandbox-base.vhdx"
)

. (Join-Path $PSScriptRoot "common.ps1")

$ReleasePattern = '^[0-9]{8}(\.[0-9]+)?$'  # a dated release folder, such as 20261015 or 20261015.1
$ImageRoot = "https://cloud-images.ubuntu.com/noble"
$ImageName = "noble-server-cloudimg-amd64-azure.vhd.tar.gz"
$ImageVhdName = "livecd.ubuntu-cpc.azure.vhd"  # the disk inside the tar
$EspType = "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}"  # the GPT type of the EFI system partition
$NeedFreeGB = 45
$SandboxRoot = Join-Path $DataDir "sandbox"

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

# Reads the four values the new boot menu needs from the image's EFI boot menu (the search line, the kernel,
# the initrd and the root partition's PARTUUID). Stops with a plain message if any is missing; nothing is changed then.
function Read-GrubValues([string]$Text) {
    $search = ""
    $kernel = ""
    $initrd = ""
    $partuuid = ""
    foreach ($raw in ($Text -split "`r?`n")) {
        $line = $raw.Trim()
        if ((-not $search) -and ($line -match '^search\.fs_uuid\s+\S+\s+root(\s|$)')) { $search = $line }
        if ((-not $kernel) -and ($line -match '^linux\s+/?(vmlinuz-\S*azure)(\s|$)')) {
            $kernel = $Matches[1]
            if ($line -match 'root=PARTUUID=([0-9a-fA-F-]{36})(\s|$)') { $partuuid = $Matches[1] }
        }
        if ((-not $initrd) -and ($line -match '^initrd\s+/?(initrd\.img-\S*azure)(\s|$)')) { $initrd = $Matches[1] }
    }
    $missing = @()
    if (-not $search) { $missing += "the search.fs_uuid line" }
    if (-not $kernel) { $missing += "a linux line with a vmlinuz-...-azure kernel" }
    if (-not $partuuid) { $missing += "root=PARTUUID= on the linux line" }
    if (-not $initrd) { $missing += "an initrd line with an initrd.img-...-azure file" }
    if ($missing.Count -gt 0) {
        throw ("The image's EFI boot menu doesn't have " + ($missing -join ", ") + ". The boot menu was left as it was. " +
            "Some Ubuntu images keep these lines on the root disk instead, which this script can't read.")
    }
    return [pscustomobject]@{ Search = $search; Kernel = $kernel; Initrd = $initrd; PartUuid = $partuuid }
}

# The new boot menu: no pause, the sandbox entry only, cloud-init told to use the NoCloud seed.
function New-BootMenu($Values) {
    $lines = @(
        $Values.Search,
        "set timeout=0",
        "set default=0",
        "menuentry 'sandbox' {",
        "  linux /$($Values.Kernel) root=PARTUUID=$($Values.PartUuid) ro console=tty1 console=ttyS0 ci.ds=NoCloud",
        "  initrd /$($Values.Initrd)",
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
        $values = Read-GrubValues ([System.IO.File]::ReadAllText($grubPath))
        if (-not (Test-Path -LiteralPath $backupPath)) { Copy-Item -LiteralPath $grubPath -Destination $backupPath }
        # UTF-8 without a byte-order mark, LF line endings
        [System.IO.File]::WriteAllText($grubPath, (New-BootMenu $values), (New-Object System.Text.UTF8Encoding($false)))
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
    $vhd = Join-Path $work $ImageVhdName
    try {
        Write-Step "Downloading the Ubuntu $ReleaseDate cloud image (this takes a while)"
        & curl.exe -fsSL -o $sums "$ImageRoot/$ReleaseDate/SHA256SUMS"
        if ($LASTEXITCODE -ne 0) {
            throw "Couldn't download the checksum list for release $ReleaseDate. Check the date: it must be a dated folder under $ImageRoot/."
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
        } catch {
            Remove-PartialParent $Parent
            throw
        }
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Step "Computing the SHA-256 of the base disk (about a minute)"
    $hash = (Get-FileHash -LiteralPath $Parent -Algorithm SHA256).Hash
    Write-Ok "Built the base disk: $Parent"
    Write-Note "SHA-256: $hash"
    Write-Note "Set SANDBOX_BASE_IMAGE=$Parent in config.env, or set Base disk for sandboxes in Settings to that path."
}

function Show-ImageCheck([string]$Parent) {
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
        throw "Kept $Parent: $($users.Count) VM disk(s) still use it as their base. Delete those sandboxes first, then run Remove again."
    }
    Remove-Item -LiteralPath $Parent -Force
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
    elseif ($Action -eq "Check") { if ((Show-ImageCheck $parent) -gt 0) { $code = 1 } }
    elseif ($Action -eq "Remove") { Remove-SandboxImage $parent }
    else { throw "Unknown action $Action." }
} catch {
    Write-Host "Stopped: $($_.Exception.Message)" -ForegroundColor Red
    $code = 1
}
Wait-Close
exit $code
