# Runs one SubZero AI command as an administrator, with the bundled Python and the data in ProgramData.
# Used by back-up-now.cmd, check-settings.cmd, and recovery-code.cmd.
param(
    [Parameter(Mandatory = $true)][string]$Command
)

. (Join-Path $PSScriptRoot "common.ps1")
Assert-Admin $PSCommandPath @("-Command", $Command)

try {
    $dir = Split-Path -Parent $PSScriptRoot
    switch ($Command) {
        "backup" { $code = Invoke-AppTool @("backup", "manual") $dir }
        "check" { $code = Invoke-AppTool @("check") $dir }
        "recovery-code" { $code = Invoke-AppTool @("recovery-code") $dir }
        default { throw "Unknown command $Command" }
    }
    if ($code -ne 0) { Write-Problem "Finished with problems (exit code $code)." }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
} finally {
    Wait-Close
}
