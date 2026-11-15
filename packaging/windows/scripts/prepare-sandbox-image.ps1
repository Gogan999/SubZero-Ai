# Puts the Code tools (Node.js, Java 17, git, Python, and the Code runner) into a NEW sandbox base disk, then switches
# the sandbox base disk setting to it (run prepare-sandbox-image.cmd in C:\Program Files\SubZeroAI).
#   prepare-sandbox-image.cmd            does nothing, quickly, when the image is already current
#   prepare-sandbox-image.cmd -Force     builds it again
# It takes about 20 minutes. SubZero AI is stopped while it runs (the builder uses the same internet proxy address as the
# app) and started again at the end, whatever happens. The old base disk is never changed and stays in place.
param(
    [switch]$Force
)

. (Join-Path $PSScriptRoot "common.ps1")
$fwd = @()
if ($Force) { $fwd += "-Force" }
Assert-Admin $PSCommandPath $fwd

$wasRunning = $false
try {
    Write-Host "Prepare the sandbox image" -ForegroundColor Cyan
    if (-not (Get-InstalledVersion)) { throw "SubZero AI isn't installed in $InstallDir." }
    Write-Note "This takes about 20 minutes. SubZero AI is stopped while it runs, so do it when no one is using Code."
    $answer = Read-Host "   Type YES to continue"
    if ($answer -ne "YES") {
        Write-Note "Nothing was changed."
        return
    }
    $svc = Get-AppService
    $wasRunning = ($null -ne $svc -and $svc.Status -ne "Stopped")
    Write-Step "Preparing"
    Stop-AppService
    $toolArgs = @("sandbox-provision")
    if ($Force) { $toolArgs += "--force" }
    $code = Invoke-AppTool $toolArgs
    if ($code -ne 0) { Write-Problem "The sandbox image wasn't prepared. The old base disk is still in use (see the log named above)." }
    else { Write-Ok "Done." }
} catch {
    Write-Host ""
    Write-Host "Stopped: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    if ($wasRunning -and (Get-AppService)) {
        Write-Step "Starting SubZero AI again"
        Start-AppService
        if ($null -eq (Wait-Healthy 90)) { Write-Problem "SubZero AI didn't answer yet. The log is in $DataDir\logs." }
        else { Write-Ok "SubZero AI is running again." }
    }
    Wait-Close
}
