# Installs, updates or repairs SubZero Hub (run setup.cmd). One script for every case:
#   - not installed yet: installs from this folder (the unzipped release)
#   - run from a newer release's folder, or with -Zip: backs up the data, then updates, keeping all data
#   - run from the same version's folder: repairs the install, keeping all data
#   - run from C:\Program Files\SubZeroAI: downloads the newest release from GitHub and updates to it
# An update that doesn't start puts the previous version back.
param(
    [string]$Zip = "",
    [string]$TunnelToken = "",
    [switch]$SkipTunnel,
    [switch]$NoBrowser,
    [switch]$Force
)

. (Join-Path $PSScriptRoot "common.ps1")
$fwd = @()
if ($Zip) { $fwd += @("-Zip", (Resolve-Path $Zip).Path) }
if ($TunnelToken) { $fwd += @("-TunnelToken", $TunnelToken) }
if ($SkipTunnel) { $fwd += "-SkipTunnel" }
if ($NoBrowser) { $fwd += "-NoBrowser" }
if ($Force) { $fwd += "-Force" }
Assert-Admin $PSCommandPath $fwd

$Source = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$work = Join-Path $env:TEMP ("SZ-" + $stamp)
$previous = Join-Path $env:TEMP ("SZ-prev-" + $stamp)
$LegacyFiles = @("install.cmd", "update.cmd")
$script:Health = $null

function Get-FullPath([string]$Path) {
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

# The newest release's zip from GitHub, or $null when the installed version is already the newest.
function Get-LatestZip([string]$Current) {
    $release = Get-Content (Join-Path $InstallDir "release.json") -Raw | ConvertFrom-Json
    Write-Step "Looking for a newer version"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $api = "https://api.github.com/repos/$($release.repo)/releases/latest"
    try {
        $latest = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "SubZeroHub-setup" } -TimeoutSec 30
    } catch {
        throw "Couldn't reach GitHub to check for updates ($($_.Exception.Message)). If the repository is private, download $($release.asset) from its releases page and run: setup.cmd -Zip path\to\$($release.asset)"
    }
    $newVersion = ($latest.tag_name -replace '^v', '')
    if ((Compare-AppVersion $newVersion $Current) -le 0 -and -not $Force) { return $null }
    $asset = $latest.assets | Where-Object { $_.name -eq $release.asset } | Select-Object -First 1
    if (-not $asset) { throw "Release $($latest.tag_name) doesn't include $($release.asset)." }
    Write-Note "Downloading version $newVersion..."
    $path = Join-Path $work $release.asset
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $path -UseBasicParsing -TimeoutSec 1800
    # Checks the download against the SHA-256 GitHub lists.
    if ($asset.digest -and $asset.digest -like "sha256:*") {
        $expected = $asset.digest.Substring(7)
        $actual = (Get-FileHash -Path $path -Algorithm SHA256).Hash
        if ($actual -ne $expected) { throw "The download doesn't match the release's checksum. Nothing was changed; try again." }
        Write-Ok "Checksum matches."
    }
    return $path
}

# Unpacks a release zip into the work folder and returns the folder that holds release.json.
function Expand-Release([string]$Path) {
    Write-Step "Unpacking"
    $unpacked = Join-Path $work "u"
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Path, $unpacked)
    $root = Get-ChildItem -Path $unpacked -Recurse -Filter "release.json" | Select-Object -First 1 | ForEach-Object { $_.DirectoryName }
    if (-not $root -or -not (Test-Path (Join-Path $root "python\python.exe"))) { throw "That zip isn't a SubZero Hub release." }
    Get-ChildItem -Path $root -Recurse -File | Unblock-File
    return $root
}

function Install-App([string]$From) {
    $version = Get-InstalledVersion $From
    Write-Step "Copying SubZero Hub $version to $InstallDir"
    Stop-AppService
    Copy-AppFiles $From $InstallDir
    # Removes the "came from the internet" mark from the copied files.
    Get-ChildItem -Path $InstallDir -Recurse -File | Unblock-File
    Optimize-AppFiles
    Write-Ok "Done."

    Write-Step "Creating the data folder $DataDir"
    foreach ($sub in @("data", "files", "backups", "logs", "prompts")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $DataDir $sub) | Out-Null
    }
    $config = Join-Path $DataDir "config.env"
    if (-not (Test-Path $config)) {
        Copy-Item (Join-Path $InstallDir "app\config.example.env") $config
        Write-Ok "Created config.env. The setup page fills it in."
    } else {
        Write-Ok "Kept the existing config.env, chats, time entries and backups."
    }

    Write-Step "Installing the SubZero Hub Windows service"
    $wrapper = Join-Path $InstallDir "service\SubZeroAI.exe"
    if (-not (Get-AppService)) {
        & $wrapper install | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Couldn't install the service (exit code $LASTEXITCODE)." }
    }
    # Runs as its own low-privilege virtual account instead of SYSTEM.
    & sc.exe config $ServiceName obj= $ServiceAccount start= delayed-auto | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Couldn't set the service's account (sc.exe exit code $LASTEXITCODE)." }
    # Restarts it after a crash, and when the app exits with an error code (the setup page exits with 3 to
    # load its new settings).
    & sc.exe failure $ServiceName reset= 3600 actions= restart/3000/restart/20000/restart/60000 | Out-Null
    & sc.exe failureflag $ServiceName 1 | Out-Null
    & icacls.exe $InstallDir /grant "${ServiceAccount}:(OI)(CI)RX" /Q | Out-Null
    Set-DataFolderAccess
    Write-Ok "Installed. It starts on its own after a restart, with nobody signed in."

    Write-Step "Starting SubZero Hub"
    Start-AppService
    $script:Health = Wait-Healthy 90
    if ($null -eq $script:Health) {
        throw "SubZero Hub didn't start. The log is in $DataDir\logs (subzero.log and SubZeroAI.err.log)."
    }
    Write-Ok "Running version $($script:Health.version) at http://localhost:$AppPort"

    # Checks that Windows restarts the app when it stops on its own.
    Write-Note "Checking that Windows restarts SubZero Hub if it stops..."
    $appPython = Join-Path $InstallDir "python\python.exe"
    $child = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.ExecutablePath -eq $appPython } | Select-Object -First 1
    if ($child) {
        Stop-Process -Id $child.ProcessId -Force
        Start-Sleep -Seconds 5
        if ($null -ne (Wait-Healthy 60)) {
            Write-Ok "It came back by itself."
        } else {
            Write-Problem "Windows didn't restart it. Starting it by hand. After finishing setup, if the site doesn't"
            Write-Problem "come back, restart the SubZero Hub service in services.msc, and report this."
            Start-AppService
            $null = Wait-Healthy 60
        }
    }
    Install-Tunnel $From
}

function Install-Tunnel([string]$From) {
    Write-Step "Cloudflare Tunnel"
    $tunnelSvc = Get-Service -Name $TunnelServiceName -ErrorAction SilentlyContinue
    if ($tunnelSvc) {
        Write-Ok "The Cloudflare connector is already installed as a service ($($tunnelSvc.Status)). Leaving it as it is."
        return
    }
    if ($SkipTunnel) {
        Write-Note "Skipped. Run setup.cmd again later to add it."
        return
    }
    $token = $TunnelToken
    if (-not $token) {
        Write-Note "In the Cloudflare Zero Trust dashboard, open Networks, Tunnels, your tunnel,"
        Write-Note "and copy the token from the Windows install command (the long text after 'service install')."
        Write-Note "Its public hostname should point to HTTP 127.0.0.1:$AppPort."
        $secure = Read-Host "   Paste the tunnel token (or press Enter to skip for now)" -AsSecureString
        $token = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()
    }
    if ($token -match '^(cloudflared(\.exe)?\s+)?service\s+install\s+(\S+)$') { $token = $Matches[3] }
    if (-not $token) {
        Write-Note "Skipped. Run setup.cmd again later to add it."
        return
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $CloudflaredExe) | Out-Null
    Copy-Item (Join-Path $From "cloudflared\cloudflared.exe") $CloudflaredExe -Force
    Unblock-File $CloudflaredExe
    & $CloudflaredExe service install $token | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "The Cloudflare connector didn't install (exit code $LASTEXITCODE). Check the token and run setup.cmd again." }
    Write-Ok "Installed the Cloudflare connector as a Windows service."
}

function Update-App([string]$From, [string]$Current) {
    $newVersion = Get-InstalledVersion $From
    Write-Ok "Version $newVersion is ready to install (you have $Current)."
    Write-Step "Backing up the data first"
    if ((Invoke-AppTool @("backup", "pre-update")) -ne 0) { throw "The backup failed, so nothing was changed." }

    Write-Step "Installing version $newVersion"
    Stop-AppService
    Copy-AppFiles $InstallDir $previous
    try {
        Copy-AppFiles $From $InstallDir
        Optimize-AppFiles
        Start-AppService
        $script:Health = Wait-Healthy 120 $newVersion
        if ($null -eq $script:Health) { throw "Version $newVersion didn't start." }
    } catch {
        Write-Problem "$($_.Exception.Message) Putting version $Current back..."
        Stop-AppService
        Copy-AppFiles $previous $InstallDir
        Start-AppService
        if ($null -ne (Wait-Healthy 90 $Current)) {
            throw "The update didn't work, so version $Current is back and running. The log is in $DataDir\logs."
        }
        throw "The update didn't work and version $Current didn't restart either. Run restore.cmd to put back the pre-update backup, and check $DataDir\logs."
    }
    Remove-Item -Recurse -Force $previous -ErrorAction SilentlyContinue
    Write-Ok "Updated to version $newVersion and running."
}

try {
    Write-Host "SubZero Hub setup" -ForegroundColor Cyan
    if (-not [Environment]::Is64BitOperatingSystem) { throw "SubZero Hub needs 64-bit Windows." }
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $current = Get-InstalledVersion
    $fromInstall = $current -and ((Get-FullPath $Source) -eq (Get-FullPath $InstallDir))

    if ($Zip) {
        $newRoot = Expand-Release $Zip
    } elseif ($fromInstall) {
        $download = Get-LatestZip $current
        if (-not $download) {
            Write-Ok "You have the newest version ($current). Nothing to do."
            return
        }
        $newRoot = Expand-Release $download
    } else {
        if (-not (Test-Path (Join-Path $Source "python\python.exe"))) {
            throw "This folder is missing the bundled Python. Unzip the whole SubZeroAI-windows.zip and run setup.cmd from it."
        }
        $newRoot = $Source
    }

    $newVersion = Get-InstalledVersion $newRoot
    if (-not $current) {
        Install-App $newRoot
    } else {
        $order = Compare-AppVersion $newVersion $current
        if ($order -lt 0 -and -not $Force) {
            throw "Version $current is installed, which is newer than this one ($newVersion). Nothing was changed."
        }
        if ($order -eq 0 -and -not $fromInstall) {
            Write-Note "Version $current is already installed. Repairing it; all data is kept."
            Install-App $newRoot
        } else {
            Update-App $newRoot $current
        }
    }
    foreach ($file in $LegacyFiles) { Remove-Item (Join-Path $InstallDir $file) -Force -ErrorAction SilentlyContinue }

    Write-Step "Almost done"
    if ($script:Health.setup_complete) {
        Write-Ok "SubZero Hub is set up and running. Open it at your site's address."
    } else {
        Write-Note "Finish on the setup page: http://localhost:$AppPort/setup (it only opens on this PC)."
        if (-not $NoBrowser) { Start-Process "http://localhost:$AppPort/setup" }
    }
    Write-Note "To update later, run setup.cmd in $InstallDir. Other tools there: restore.cmd, back-up-now.cmd, check-settings.cmd."
} catch {
    Write-Host ""
    Write-Host "Setup stopped: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    Wait-Close
}
