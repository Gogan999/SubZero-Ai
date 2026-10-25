# Shared helpers for the SubZero AI install, update, restore, and uninstall scripts.
# Dot-source it:  . (Join-Path $PSScriptRoot "common.ps1")
# Written for Windows PowerShell 5.1, which every Windows 10 and 11 PC has. Keep this file ASCII-only.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is much faster without the progress bar

$ServiceName = "SubZeroAI"
$ServiceAccount = "NT SERVICE\SubZeroAI"
$TunnelServiceName = "Cloudflared"
$InstallDir = Join-Path $env:ProgramFiles "SubZeroAI"
# The Cloudflare connector gets its own folder, so removing or updating SubZero AI never breaks the tunnel.
$CloudflaredExe = Join-Path $env:ProgramFiles "cloudflared\cloudflared.exe"
$DataDir = Join-Path $env:ProgramData "SubZeroAI"
$AppPort = 8787

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) { Write-Host "   $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "   $Text" }
function Write-Problem([string]$Text) { Write-Host "   $Text" -ForegroundColor Yellow }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Re-run the calling script as an administrator (Windows shows the usual "allow changes" prompt).
function Assert-Admin([string]$ScriptPath, [string[]]$ScriptArgs = @()) {
    if (Test-Admin) { return }
    $quoted = @($ScriptArgs | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' })
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$ScriptPath`"") + $quoted
    try {
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $argList | Out-Null
    } catch {
        Write-Host "This needs administrator rights. Run it again and choose Yes when Windows asks." -ForegroundColor Red
        Wait-Close
    }
    exit
}

function Wait-Close {
    if ($env:SUBZERO_NO_PAUSE) { return }
    Write-Host ""
    Read-Host "Press Enter to close this window" | Out-Null
}

function Get-InstalledVersion([string]$Dir = $InstallDir) {
    $file = Join-Path $Dir "VERSION"
    if (Test-Path $file) { return (Get-Content $file -Raw).Trim() }
    return $null
}

# Compare dotted versions like 0.4.0 and 0.10.1. Returns -1, 0, or 1.
function Compare-AppVersion([string]$A, [string]$B) {
    $pa = @(($A -replace '^v', '') -split '[^0-9]+' | Where-Object { $_ -ne "" } | ForEach-Object { [int]$_ })
    $pb = @(($B -replace '^v', '') -split '[^0-9]+' | Where-Object { $_ -ne "" } | ForEach-Object { [int]$_ })
    for ($i = 0; $i -lt [Math]::Max($pa.Count, $pb.Count); $i++) {
        $x = 0; $y = 0
        if ($i -lt $pa.Count) { $x = $pa[$i] }
        if ($i -lt $pb.Count) { $y = $pb[$i] }
        if ($x -lt $y) { return -1 }
        if ($x -gt $y) { return 1 }
    }
    return 0
}

function Get-AppService { return Get-Service -Name $ServiceName -ErrorAction SilentlyContinue }

function Stop-AppService {
    $svc = Get-AppService
    if ($null -eq $svc -or $svc.Status -eq "Stopped") { return }
    Write-Note "Stopping the SubZero AI service..."
    Stop-Service -Name $ServiceName -Force
    $svc.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(60))
}

function Start-AppService {
    Start-Service -Name $ServiceName
}

# Wait until the app answers on this PC. Returns the /healthz answer, or $null after the timeout.
function Wait-Healthy([int]$Seconds = 90, [string]$WantVersion = "") {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$AppPort/healthz" -UseBasicParsing -TimeoutSec 5
            $health = $resp.Content | ConvertFrom-Json
            if (-not $WantVersion -or $health.version -eq $WantVersion) { return $health }
        } catch {
            # not up yet
        }
        Start-Sleep -Seconds 2
    }
    return $null
}

# Run a SubZero AI command (like "backup") with the bundled Python, against the data in ProgramData.
function Invoke-AppTool([string[]]$ToolArgs, [string]$Dir = $InstallDir) {
    $python = Join-Path $Dir "python\python.exe"
    $env:DATA_DIR = $DataDir
    $env:PYTHONUTF8 = "1"
    & $python -m subzero_ai @ToolArgs | Out-Host  # show the output instead of returning it
    return $LASTEXITCODE
}

# The data folder holds config.env (the API key and Slack tokens) and the team's chats. Only administrators,
# Windows itself, and the app's own service account can open it.
function Set-DataFolderAccess {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    $grants = @(
        "*S-1-5-32-544:(OI)(CI)F",   # Administrators
        "*S-1-5-18:(OI)(CI)F",       # SYSTEM
        "${ServiceAccount}:(OI)(CI)M"
    )
    & icacls.exe $DataDir /inheritance:r /grant:r @grants | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Couldn't lock down $DataDir (icacls exit code $LASTEXITCODE)." }
    # Everything inside just inherits the folder's permissions.
    & icacls.exe (Join-Path $DataDir "*") /reset /T /C /Q | Out-Null
}

# Copy one folder over another, deleting files that are no longer in the source.
function Copy-Mirror([string]$From, [string]$To) {
    & robocopy.exe $From $To /MIR /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $From to $To failed (robocopy exit code $LASTEXITCODE)." }
    $global:LASTEXITCODE = 0
}

# The app's files that an install or update replaces. cloudflared and the data folder are left alone.
$AppParts = @("app", "python", "scripts", "service")
$AppFiles = @("VERSION", "release.json", "README.txt", "update.cmd", "restore.cmd", "back-up-now.cmd",
              "check-settings.cmd", "recovery-code.cmd", "uninstall.cmd")

function Copy-AppFiles([string]$From, [string]$To) {
    New-Item -ItemType Directory -Force -Path $To | Out-Null
    foreach ($part in $AppParts) {
        Copy-Mirror (Join-Path $From $part) (Join-Path $To $part)
    }
    foreach ($file in $AppFiles) {
        $src = Join-Path $From $file
        if (Test-Path $src) { Copy-Item $src (Join-Path $To $file) -Force }
    }
}

# Byte-compile the Python files once, as an administrator, since the service can't write to Program Files.
function Optimize-AppFiles([string]$Dir = $InstallDir) {
    $python = Join-Path $Dir "python\python.exe"
    & $python -m compileall -q -j 0 (Join-Path $Dir "app") (Join-Path $Dir "python\Lib\site-packages") | Out-Null
    $global:LASTEXITCODE = 0
}
