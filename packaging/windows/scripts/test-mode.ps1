# Tries SubZero AI on this PC without installing it: sample sign-ins, a pretend Claude, and data in a
# test-data folder next to this one. Close the window to stop it.
. (Join-Path $PSScriptRoot "common.ps1")

$dir = Split-Path -Parent $PSScriptRoot
$env:PYTHONUTF8 = "1"
$env:DATA_DIR = Join-Path $dir "test-data"
Write-Host ""
Write-Host "Starting SubZero AI in test mode at http://localhost:$AppPort" -ForegroundColor Cyan
Write-Host "Sign in as a sample person. Close this window to stop."
Write-Host ""
& (Join-Path $dir "python\python.exe") -m subzero_ai --test-mode --open
