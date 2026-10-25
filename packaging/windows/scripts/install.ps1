# Installs SubZero AI on this PC (run install.cmd in the unzipped folder):
#   - copies the app, with its own Python, to C:\Program Files\SubZeroAI
#   - creates C:\ProgramData\SubZeroAI for settings, chats, backups, and logs, readable only by administrators
#     and the app's service account
#   - installs the app as the "SubZero AI" Windows service, starting automatically with nobody signed in
#   - installs the Cloudflare Tunnel connector as a Windows service, using the tunnel token you paste
#   - opens the setup page
# Running it again repairs an install and keeps all data. To move to a newer version, use update.cmd.
param(
    [string]$TunnelToken = "",
    [switch]$SkipTunnel,
    [switch]$NoBrowser
)

. (Join-Path $PSScriptRoot "common.ps1")
$fwd = @()
if ($TunnelToken) { $fwd += @("-TunnelToken", $TunnelToken) }
if ($SkipTunnel) { $fwd += "-SkipTunnel" }
if ($NoBrowser) { $fwd += "-NoBrowser" }
Assert-Admin $PSCommandPath $fwd

$Source = Split-Path -Parent $PSScriptRoot

try {
    Write-Host "SubZero AI installer" -ForegroundColor Cyan
    if (-not [Environment]::Is64BitOperatingSystem) { throw "SubZero AI needs 64-bit Windows." }
    if (-not (Test-Path (Join-Path $Source "python\python.exe"))) {
        throw "This folder is missing the bundled Python. Unzip the whole SubZeroAI-windows.zip and run install.cmd from it."
    }
    $version = Get-InstalledVersion $Source
    $existing = Get-InstalledVersion
    if ($existing -and (Compare-AppVersion $existing $version) -gt 0) {
        throw "Version $existing is already installed, which is newer than this one ($version). Nothing was changed."
    }

    # ------------------------------------------------------------------ files
    Write-Step "Copying SubZero AI $version to $InstallDir"
    Stop-AppService
    Copy-AppFiles $Source $InstallDir
    # Files from a downloaded zip carry a "came from the internet" mark that can trigger warnings.
    Get-ChildItem -Path $InstallDir -Recurse -File | Unblock-File
    Optimize-AppFiles
    Write-Ok "Done."

    # ------------------------------------------------------------------ data folder
    Write-Step "Creating the data folder $DataDir"
    foreach ($sub in @("data", "files", "backups", "logs", "prompts")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $DataDir $sub) | Out-Null
    }
    $config = Join-Path $DataDir "config.env"
    if (-not (Test-Path $config)) {
        Copy-Item (Join-Path $InstallDir "app\config.example.env") $config
        Write-Ok "Created config.env. The setup page fills it in."
    } else {
        Write-Ok "Kept the existing config.env, chats, and backups."
    }

    # ------------------------------------------------------------------ app service
    Write-Step "Installing the SubZero AI Windows service"
    $wrapper = Join-Path $InstallDir "service\SubZeroAI.exe"
    if (-not (Get-AppService)) {
        & $wrapper install | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Couldn't install the service (exit code $LASTEXITCODE)." }
    }
    # Run as its own low-privilege virtual account instead of SYSTEM.
    & sc.exe config $ServiceName obj= $ServiceAccount start= delayed-auto | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Couldn't set the service's account (sc.exe exit code $LASTEXITCODE)." }
    # Restart it when the app exits with an error code too, not only after a crash. The setup page uses this.
    & sc.exe failureflag $ServiceName 1 | Out-Null
    & icacls.exe $InstallDir /grant "${ServiceAccount}:(OI)(CI)RX" /Q | Out-Null
    Set-DataFolderAccess
    Write-Ok "Installed. It starts on its own after a restart, with nobody signed in."

    Write-Step "Starting SubZero AI"
    Start-AppService
    $health = Wait-Healthy 90
    if ($null -eq $health) {
        throw "SubZero AI didn't start. The log is in $DataDir\logs (subzero.log and SubZeroAI.err.log)."
    }
    Write-Ok "Running version $($health.version) at http://localhost:$AppPort"

    # ------------------------------------------------------------------ Cloudflare Tunnel
    Write-Step "Cloudflare Tunnel"
    $tunnelSvc = Get-Service -Name $TunnelServiceName -ErrorAction SilentlyContinue
    if ($tunnelSvc) {
        Write-Ok "The Cloudflare connector is already installed as a service ($($tunnelSvc.Status)). Leaving it as it is."
    } elseif ($SkipTunnel) {
        Write-Note "Skipped. Run install.cmd again later to add it."
    } else {
        if (-not $TunnelToken) {
            Write-Note "In the Cloudflare Zero Trust dashboard, open Networks, Tunnels, your subzero-ai tunnel,"
            Write-Note "and copy the token from the Windows install command (the long text after 'service install')."
            Write-Note "Its public hostname should point to HTTP localhost:$AppPort."
            $secure = Read-Host "   Paste the tunnel token (or press Enter to skip for now)" -AsSecureString
            $TunnelToken = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()
        }
        if ($TunnelToken -match '^(cloudflared(\.exe)?\s+)?service\s+install\s+(\S+)$') { $TunnelToken = $Matches[3] }
        if (-not $TunnelToken) {
            Write-Note "Skipped. Run install.cmd again later to add it."
        } else {
            New-Item -ItemType Directory -Force -Path (Split-Path $CloudflaredExe) | Out-Null
            Copy-Item (Join-Path $Source "cloudflared\cloudflared.exe") $CloudflaredExe -Force
            Unblock-File $CloudflaredExe
            & $CloudflaredExe service install $TunnelToken
            if ($LASTEXITCODE -ne 0) { throw "The Cloudflare connector didn't install (exit code $LASTEXITCODE). Check the token and run install.cmd again." }
            Write-Ok "Installed the Cloudflare connector as a Windows service."
        }
    }

    # ------------------------------------------------------------------ finish
    Write-Step "Almost done"
    if ($health.setup_complete) {
        Write-Ok "SubZero AI is already set up. Nothing else to do."
    } else {
        Write-Note "Finish on the setup page: http://localhost:$AppPort/setup (it only opens on this PC)."
        if (-not $NoBrowser) { Start-Process "http://localhost:$AppPort/setup" }
    }
    Write-Note "Tools in $InstallDir : update.cmd, restore.cmd, back-up-now.cmd, check-settings.cmd."
} catch {
    Write-Host ""
    Write-Host "The install stopped: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Wait-Close
}
