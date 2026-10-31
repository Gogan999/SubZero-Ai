# Updates SubZero AI to the newest release (run update.cmd in C:\Program Files\SubZeroAI).
#   - downloads the newest SubZeroAI-windows.zip from the GitHub releases page (or uses -Zip)
#   - backs up the data first (a backup labelled pre-update)
#   - replaces the app and its Python, keeping config.env, chats, files, and backups
#   - restarts the service; the app applies any database changes as it starts
#   - if the new version doesn't start, puts the old one back
param(
    [string]$Zip = "",
    [switch]$Force
)

. (Join-Path $PSScriptRoot "common.ps1")
$fwd = @()
if ($Zip) { $fwd += @("-Zip", (Resolve-Path $Zip).Path) }
if ($Force) { $fwd += "-Force" }
Assert-Admin $PSCommandPath $fwd

$work = Join-Path $env:TEMP ("SZ-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
$previous = Join-Path $env:TEMP ("SZ-prev-" + (Get-Date -Format "yyyyMMdd-HHmmss"))

try {
    Write-Host "SubZero AI update" -ForegroundColor Cyan
    $current = Get-InstalledVersion
    if (-not $current) { throw "SubZero AI isn't installed in $InstallDir. Run install.cmd from the release zip instead." }
    $release = Get-Content (Join-Path $InstallDir "release.json") -Raw | ConvertFrom-Json
    New-Item -ItemType Directory -Force -Path $work | Out-Null

    # ------------------------------------------------------------------ get the new version
    if (-not $Zip) {
        Write-Step "Looking for a newer version"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $api = "https://api.github.com/repos/$($release.repo)/releases/latest"
        try {
            $latest = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "SubZeroAI-updater" } -TimeoutSec 30
        } catch {
            throw "Couldn't reach GitHub to check for updates ($($_.Exception.Message)). If the repository is private, download SubZeroAI-windows.zip from its releases page and run: update.cmd -Zip path\to\SubZeroAI-windows.zip"
        }
        $newVersion = ($latest.tag_name -replace '^v', '')
        if ((Compare-AppVersion $newVersion $current) -le 0 -and -not $Force) {
            Write-Ok "You have the newest version ($current). Nothing to do."
            return
        }
        $asset = $latest.assets | Where-Object { $_.name -eq $release.asset } | Select-Object -First 1
        if (-not $asset) { throw "Release $($latest.tag_name) doesn't include $($release.asset)." }
        Write-Note "Downloading version $newVersion..."
        $Zip = Join-Path $work $release.asset
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $Zip -UseBasicParsing -TimeoutSec 1800
        # GitHub lists each file's SHA-256; a download that doesn't match is cut short or tampered with.
        if ($asset.digest -and $asset.digest -like "sha256:*") {
            $expected = $asset.digest.Substring(7)
            $actual = (Get-FileHash -Path $Zip -Algorithm SHA256).Hash
            if ($actual -ne $expected) { throw "The download doesn't match the release's checksum. Nothing was changed; try again." }
            Write-Ok "Checksum matches."
        }
    }

    Write-Step "Unpacking"
    $unpacked = Join-Path $work "u"
    # Work folders are named short (SZ-<stamp>\u) because Windows PowerShell 5.1 can't open paths over 260 characters.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $unpacked)
    $newRoot = Get-ChildItem -Path $unpacked -Recurse -Filter "release.json" | Select-Object -First 1 | ForEach-Object { $_.DirectoryName }
    if (-not $newRoot -or -not (Test-Path (Join-Path $newRoot "python\python.exe"))) { throw "That zip isn't a SubZero AI release." }
    $newVersion = Get-InstalledVersion $newRoot
    if ((Compare-AppVersion $newVersion $current) -lt 0 -and -not $Force) {
        throw "That zip has version $newVersion, which is older than the installed $current. Nothing was changed."
    }
    Get-ChildItem -Path $newRoot -Recurse -File | Unblock-File
    Write-Ok "Version $newVersion is ready to install (you have $current)."

    # ------------------------------------------------------------------ back up, then swap
    Write-Step "Backing up the data first"
    if ((Invoke-AppTool @("backup", "pre-update")) -ne 0) { throw "The backup failed, so nothing was changed." }

    Write-Step "Installing version $newVersion"
    Stop-AppService
    Copy-AppFiles $InstallDir $previous
    try {
        Copy-AppFiles $newRoot $InstallDir
        Optimize-AppFiles
        Start-AppService
        $health = Wait-Healthy 120 $newVersion
        if ($null -eq $health) { throw "Version $newVersion didn't start." }
    } catch {
        Write-Problem "$($_.Exception.Message) Putting version $current back..."
        Stop-AppService
        Copy-AppFiles $previous $InstallDir
        Start-AppService
        if ($null -ne (Wait-Healthy 90 $current)) {
            throw "The update didn't work, so version $current is back and running. The log is in $DataDir\logs."
        }
        throw "The update didn't work and version $current didn't restart either. Run restore.cmd to put back the pre-update backup, and check $DataDir\logs."
    }
    Write-Ok "Updated to version $newVersion and running."
    Remove-Item -Recurse -Force $previous -ErrorAction SilentlyContinue
} catch {
    Write-Host ""
    Write-Host "The update stopped: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    Wait-Close
}
