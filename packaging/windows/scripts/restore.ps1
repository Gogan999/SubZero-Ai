# Puts a SubZero AI backup back (run restore.cmd in C:\Program Files\SubZeroAI).
#   restore.cmd                     lists the backups and asks which one
#   restore.cmd -Backup latest      restores the newest backup
# The service is stopped while the backup goes back, and the current data is saved first
# (as a backup labelled before-restore), so a restore can itself be undone.
param(
    [string]$Backup = ""
)

. (Join-Path $PSScriptRoot "common.ps1")
$fwd = @()
if ($Backup) { $fwd += @("-Backup", $Backup) }
Assert-Admin $PSCommandPath $fwd

try {
    Write-Host "Restore a SubZero AI backup" -ForegroundColor Cyan
    if (-not (Get-InstalledVersion)) { throw "SubZero AI isn't installed in $InstallDir." }
    if (-not $Backup) {
        Write-Step "Backups"
        Invoke-AppTool @("backups") | Out-Null
        Write-Host ""
        $Backup = Read-Host "   Type the name of the backup to restore (or press Enter for the newest)"
        if (-not $Backup) { $Backup = "latest" }
    }
    Write-Host ""
    Write-Problem "This replaces every chat, message, and attached file with the ones in the backup."
    Write-Problem "Anything newer than the backup is set aside in a before-restore backup."
    $answer = Read-Host "   Type YES to continue"
    if ($answer -ne "YES") {
        Write-Note "Nothing was changed."
        return
    }

    Write-Step "Restoring"
    Stop-AppService
    $code = Invoke-AppTool @("restore", $Backup)
    Set-DataFolderAccess
    if (Get-AppService) {
        Start-AppService
        if ($null -eq (Wait-Healthy 90)) { throw "SubZero AI didn't start after the restore. The log is in $DataDir\logs." }
    }
    if ($code -ne 0) { throw "The restore didn't finish, so the data wasn't changed. SubZero AI is running again." }
    Write-Ok "Restored, and SubZero AI is running again."
} catch {
    Write-Host ""
    Write-Host "The restore stopped: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Wait-Close
}
