# Removes SubZero AI from this PC (run uninstall.cmd in C:\Program Files\SubZeroAI).
# The data folder (C:\ProgramData\SubZeroAI: config.env, chats, backups) is kept unless you ask to delete it.
param(
    [switch]$RemoveData
)

. (Join-Path $PSScriptRoot "common.ps1")
$fwd = @()
if ($RemoveData) { $fwd += "-RemoveData" }
Assert-Admin $PSCommandPath $fwd

try {
    Write-Host "Uninstall SubZero AI" -ForegroundColor Cyan
    $answer = Read-Host "   This removes the SubZero AI service and program files. Type YES to continue"
    if ($answer -ne "YES") { Write-Note "Nothing was changed."; return }

    if (Get-AppService) {
        Write-Step "Removing the SubZero AI service"
        Stop-AppService
        & (Join-Path $InstallDir "service\SubZeroAI.exe") uninstall | Out-Null
        Write-Ok "Removed."
    }

    if ((Get-Service -Name $TunnelServiceName -ErrorAction SilentlyContinue) -and (Test-Path $CloudflaredExe)) {
        $tunnel = Read-Host "   Also remove the Cloudflare Tunnel connector service? Type YES to remove it"
        if ($tunnel -eq "YES") {
            & $CloudflaredExe service uninstall
            Write-Ok "Removed the Cloudflare connector. Its program is still in $(Split-Path $CloudflaredExe)."
        } else {
            Write-Note "Kept the Cloudflare connector."
        }
    }

    Write-Step "Removing $InstallDir"
    Set-Location $env:TEMP
    Remove-Item -Recurse -Force $InstallDir
    Write-Ok "Removed."

    if ($RemoveData) {
        $sure = Read-Host "   Delete $DataDir too, with config.env, every chat, and every backup? Type DELETE"
        if ($sure -eq "DELETE") {
            Remove-Item -Recurse -Force $DataDir
            Write-Ok "Deleted the data folder."
        }
    } else {
        Write-Note "Kept $DataDir (config.env, chats, and backups)."
    }
} catch {
    Write-Host ""
    Write-Host "Uninstall stopped: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Wait-Close
}
