# Runs SubZero AI on Windows. Use the .cmd files in the app folder rather than calling this directly.
#   run.ps1            start the app with C:\ProgramData\SubZeroAI\config.env
#   run.ps1 -TestMode  sample sign-ins and a pretend Claude, data in the app folder's test-data
#   run.ps1 -Check     check config.env, Slack, and the Claude API key
param(
    [switch]$TestMode,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot
Set-Location $AppDir
$env:PYTHONUTF8 = "1"

function Find-Python {
    $candidates = @(
        @{ Exe = "py"; Args = @("-3.13") },
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "python"; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $version = & $c.Exe @($c.Args + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")) 2>$null
        } catch {
            continue
        }
        if ($LASTEXITCODE -ne 0 -or -not $version) { continue }
        $parts = "$version".Trim().Split(".")
        if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) { return $c }
    }
    return $null
}

$Venv = Join-Path $AppDir ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    $py = Find-Python
    if ($null -eq $py) {
        Write-Host ""
        Write-Host "Python 3.12 or 3.13 isn't installed." -ForegroundColor Red
        Write-Host "Install it from https://www.python.org/downloads/windows/ and tick 'Add python.exe to PATH',"
        Write-Host "then run this again."
        exit 1
    }
    Write-Host "Setting up a private Python environment in $Venv ..."
    & $py.Exe @($py.Args + @("-m", "venv", $Venv))
    if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't create the Python environment." -ForegroundColor Red; exit 1 }
}

# Install or update packages when requirements.txt changes
$Requirements = Join-Path $AppDir "requirements.txt"
$Stamp = Join-Path $Venv "requirements.sha256"
$Hash = (Get-FileHash $Requirements -Algorithm SHA256).Hash
$Installed = ""
if (Test-Path $Stamp) { $Installed = (Get-Content $Stamp -Raw).Trim() }
if ($Installed -ne $Hash) {
    Write-Host "Installing packages (this takes a minute the first time) ..."
    & $VenvPython -m pip install --disable-pip-version-check -q -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Package install failed. Check the internet connection and run this again." -ForegroundColor Red
        exit 1
    }
    Set-Content -Path $Stamp -Value $Hash
}

if ($TestMode) {
    Write-Host ""
    Write-Host "Starting SubZero AI in test mode at http://localhost:8787" -ForegroundColor Cyan
    Write-Host "Sign in as a sample person. Close this window to stop."
    Write-Host ""
    & $VenvPython -m subzero_ai --test-mode --open
    exit $LASTEXITCODE
}

$DataDir = $env:DATA_DIR
if (-not $DataDir) { $DataDir = Join-Path $env:ProgramData "SubZeroAI" }
$Config = Join-Path $DataDir "config.env"
if (-not (Test-Path $Config)) {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    Copy-Item (Join-Path $AppDir "config.example.env") $Config
    Write-Host ""
    Write-Host "Created $Config" -ForegroundColor Cyan
    Write-Host "Fill it in (README.md, 'Set it up for real'), save it, then run this again."
    Start-Process notepad.exe $Config
    exit 0
}

if ($Check) {
    & $VenvPython -m subzero_ai check
    exit $LASTEXITCODE
}

& $VenvPython -m subzero_ai check
Write-Host ""
Write-Host "Starting SubZero AI. Keep this window open; closing it stops the app." -ForegroundColor Cyan
Write-Host ""
# Exit code 3 means the app asked to restart (the setup page does this once it's finished).
do {
    & $VenvPython -m subzero_ai
    $code = $LASTEXITCODE
} while ($code -eq 3)
exit $code
