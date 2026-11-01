#Requires -RunAsAdministrator
# Sets up, checks, or removes the host side of the Code sandbox network. Run it once, as an administrator, on the
# mini PC (see docs/SETTINGS.md, "Code sandboxes"). Nothing runs it automatically.
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-network.ps1 -Action Check
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-network.ps1 -Action Install
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-network.ps1 -Action Remove
# Install: creates the Internal switch if it's missing, gives the host's adapter for it 10.77.0.1/24 with no gateway,
# turns IP forwarding off on it, sets its network profile to Public, and adds two firewall rules.
# Remove: deletes only the szsandbox-* firewall rules and, when no VM is attached to it, the switch.
# Check: changes nothing and lists what is missing. Only objects named szsandbox-* are ever changed; other adapters,
# switches, VMs and rules are never touched.
# The checks here must match host_network_check_script in subzero_ai/sandbox.py. Keep both in step.
# Windows Firewall gives a block rule priority over an allow rule, so the proxy rule does nothing while the block rule
# covers every port. Decide how the proxy port is opened before the proxy is built (docs/CODE_SANDBOX_PLAN.md).

param(
    [ValidateSet("Install", "Remove", "Check")]
    [string]$Action = "Check",
    [string]$Switch = "szsandbox-internal",
    [ValidateRange(1, 65535)]
    [int]$ProxyPort = 3128
)

. (Join-Path $PSScriptRoot "common.ps1")

$Prefix = "szsandbox-"
$HostAddress = "10.77.0.1"
$Subnet = "10.77.0.0/24"
$BlockRule = "szsandbox-block-inbound"
$ProxyRule = "szsandbox-allow-proxy"
$RiskyPorts = @(135, 139, 445, 3389, 5985)  # SMB, RPC, RDP and WinRM: never open to the sandboxes

if (-not $Switch.StartsWith($Prefix)) {
    throw "The switch name must start with $Prefix, so this script only ever changes sandbox objects."
}
$Alias = "vEthernet ($Switch)"  # the host's adapter for an Internal switch

function Get-SandboxSwitch { return Get-VMSwitch -Name $Switch -ErrorAction SilentlyContinue }

function Get-SandboxRules {
    return @(Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "$Prefix*" })
}

# The values one filter kind holds for a rule, e.g. the addresses of a rule's address filter
function Get-RuleValues($Rule, [string]$Cmdlet, [string]$Field) {
    return @($Rule | & $Cmdlet -ErrorAction SilentlyContinue | ForEach-Object { $_.$Field })
}

# Enabled inbound allow rules, not ours, that reach this adapter from anywhere and open a risky port. Best effort.
function Get-OpenAllowNames {
    $names = @()
    $allows = @(Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True -ErrorAction SilentlyContinue)
    foreach ($rule in $allows) {
        if ($rule.DisplayName -like "$Prefix*") { continue }
        $profileText = [string]$rule.Profile
        if (($profileText -notmatch "Public") -and ($profileText -notmatch "Any")) { continue }
        $ports = @(Get-RuleValues $rule "Get-NetFirewallPortFilter" "Protocol")
        if (@($ports | Where-Object { $_ -match "^ICMP" }).Count -gt 0) { continue }
        $ifaces = Get-RuleValues $rule "Get-NetFirewallInterfaceFilter" "InterfaceAlias"
        $ifaceOk = ($ifaces.Count -eq 0) -or ($ifaces -contains "Any") -or ($ifaces -contains $Alias)
        if (-not $ifaceOk) { continue }
        $remotes = Get-RuleValues $rule "Get-NetFirewallAddressFilter" "RemoteAddress"
        $remoteOk = ($remotes.Count -eq 0) -or ($remotes -contains "Any") -or ($remotes -contains "LocalSubnet")
        if (-not $remoteOk) { continue }
        $wide = $false
        foreach ($spec in (Get-RuleValues $rule "Get-NetFirewallPortFilter" "LocalPort")) {
            if (($spec -eq "Any") -or ($spec -eq "")) { $wide = $true }
            elseif ($spec -match '^(\d+)(?:-(\d+))?$') {
                $lo = [int]$Matches[1]
                $hi = $lo
                if ($Matches[2]) { $hi = [int]$Matches[2] }
                foreach ($p in $RiskyPorts) { if (($p -ge $lo) -and ($p -le $hi)) { $wide = $true } }
            }
        }
        if ($wide) { $names += $rule.DisplayName }
    }
    return $names
}

function Get-SandboxProblems {
    $problems = New-Object System.Collections.Generic.List[string]
    $vswitch = Get-SandboxSwitch
    if (-not $vswitch) {
        $problems.Add("The sandbox switch $Switch doesn't exist yet.")
    } elseif ($vswitch.SwitchType -ne "Internal") {
        $problems.Add("$Switch isn't an Internal switch.")
    }

    $adapter = Get-NetAdapter -Name $Alias -ErrorAction SilentlyContinue
    if (-not $adapter) {
        $problems.Add("The host has no network adapter for $Switch.")
    } else {
        $ips = @(Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue)
        if (-not ($ips | Where-Object { $_.IPAddress -eq $HostAddress -and $_.PrefixLength -eq 24 })) {
            $problems.Add("The host's adapter for the sandboxes doesn't have the address $HostAddress/24.")
        }
        $routes = @(Get-NetRoute -InterfaceAlias $Alias -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue)
        if ($routes.Count -gt 0) {
            $problems.Add("The host's adapter for the sandboxes has a default gateway. It must have none.")
        }
        $ipif = Get-NetIPInterface -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue
        if ($ipif -and ($ipif.Forwarding -ne "Disabled")) {
            $problems.Add("IP forwarding is on for the sandbox adapter. It must be off.")
        }
        $netProfile = Get-NetConnectionProfile -InterfaceAlias $Alias -ErrorAction SilentlyContinue
        $isPublic = $netProfile -and ($netProfile.NetworkCategory -eq "Public")
        if (-not $isPublic) { $problems.Add("The sandbox adapter's network profile isn't Public.") }
    }

    $rules = @(Get-SandboxRules | Where-Object { $_.Enabled -eq "True" })
    $blockOk = $false
    $blockRules = @($rules | Where-Object {
        $_.DisplayName -eq $BlockRule -and $_.Direction -eq "Inbound" -and $_.Action -eq "Block" })
    foreach ($rule in $blockRules) {
        $ifaces = Get-RuleValues $rule "Get-NetFirewallInterfaceFilter" "InterfaceAlias"
        $remotes = Get-RuleValues $rule "Get-NetFirewallAddressFilter" "RemoteAddress"
        if (($ifaces -contains $Alias) -and ($remotes -contains $Subnet)) { $blockOk = $true }
    }
    if (-not $blockOk) {
        $problems.Add("The firewall rule $BlockRule (blocks inbound from $Subnet on the sandbox adapter) is missing or turned off.")
    }

    foreach ($rule in @($rules | Where-Object { $_.DisplayName -eq $ProxyRule })) {
        $protocols = Get-RuleValues $rule "Get-NetFirewallPortFilter" "Protocol"
        $localPorts = Get-RuleValues $rule "Get-NetFirewallPortFilter" "LocalPort"
        $ifaces = Get-RuleValues $rule "Get-NetFirewallInterfaceFilter" "InterfaceAlias"
        $remotes = Get-RuleValues $rule "Get-NetFirewallAddressFilter" "RemoteAddress"
        $ok = ($rule.Direction -eq "Inbound") -and ($rule.Action -eq "Allow") -and ($protocols -contains "TCP") `
            -and ($localPorts -contains "$ProxyPort") -and ($localPorts.Count -eq 1) `
            -and ($ifaces -contains $Alias) -and ($remotes -contains $Subnet)
        if (-not $ok) {
            $problems.Add("The firewall rule $ProxyRule must allow only TCP to port $ProxyPort from $Subnet on the sandbox adapter.")
        }
    }

    foreach ($name in (Get-OpenAllowNames)) {
        $problems.Add("The firewall rule '$name' lets other computers reach this PC over the sandbox network. Turn it off, or limit it so it doesn't apply to the sandbox adapter.")
    }
    return $problems.ToArray()
}

# Prints the checks and returns how many problems there are
function Show-SandboxCheck {
    Write-Step "Checking the sandbox network"
    $problems = @(Get-SandboxProblems)
    foreach ($problem in $problems) { Write-Problem $problem }
    if ($problems.Count -eq 0) {
        Write-Ok "Everything is in place. Sandboxes can reach only this PC, on $Switch."
    } else {
        Write-Note "$($problems.Count) thing(s) missing. An administrator can run this script with -Action Install."
    }
    return $problems.Count
}

function Install-SandboxNetwork {
    Write-Step "Setting up the switch $Switch"
    $vswitch = Get-SandboxSwitch
    if (-not $vswitch) {
        New-VMSwitch -Name $Switch -SwitchType Internal | Out-Null
        Write-Ok "Created the Internal switch $Switch."
    } elseif ($vswitch.SwitchType -ne "Internal") {
        throw "$Switch exists but isn't an Internal switch, so it was left alone. Remove it by hand, then run this again."
    } else {
        Write-Note "The switch $Switch already exists."
    }

    # The host's adapter for the switch appears a moment after the switch is made
    $adapter = $null
    for ($i = 0; ($i -lt 30) -and (-not $adapter); $i++) {
        $adapter = Get-NetAdapter -Name $Alias -ErrorAction SilentlyContinue
        if (-not $adapter) { Start-Sleep -Seconds 1 }
    }
    if (-not $adapter) { throw "Windows didn't create the adapter $Alias. Wait a minute and run this again." }

    Write-Step "Giving the host's adapter $HostAddress/24, with no gateway"
    # This adapter belongs to the sandbox switch, so its old addresses and gateways can be replaced
    Get-NetRoute -InterfaceAlias $Alias -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false
    Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { ($_.IPAddress -notlike "169.254.*") -and (($_.IPAddress -ne $HostAddress) -or ($_.PrefixLength -ne 24)) } |
        Remove-NetIPAddress -Confirm:$false
    $have = Get-NetIPAddress -InterfaceAlias $Alias -IPAddress $HostAddress -PrefixLength 24 -ErrorAction SilentlyContinue
    if (-not $have) {
        New-NetIPAddress -InterfaceAlias $Alias -IPAddress $HostAddress -PrefixLength 24 | Out-Null
    }
    Set-NetIPInterface -InterfaceAlias $Alias -AddressFamily IPv4 -Forwarding Disabled
    Set-NetConnectionProfile -InterfaceAlias $Alias -NetworkCategory Public
    Write-Ok "Set $HostAddress/24, no gateway, forwarding off and the profile to Public."

    Write-Step "Adding the firewall rules"
    Get-SandboxRules | Remove-NetFirewallRule
    New-NetFirewallRule -Name $BlockRule -DisplayName $BlockRule -Direction Inbound -Action Block `
        -RemoteAddress $Subnet -InterfaceAlias $Alias -Profile Any -Enabled True | Out-Null
    New-NetFirewallRule -Name $ProxyRule -DisplayName $ProxyRule -Direction Inbound -Action Allow -Protocol TCP `
        -LocalPort $ProxyPort -RemoteAddress $Subnet -InterfaceAlias $Alias -Profile Any -Enabled True | Out-Null
    Write-Ok "Added $BlockRule and $ProxyRule."
    [void](Show-SandboxCheck)
}

function Remove-SandboxNetwork {
    Write-Step "Removing the sandbox firewall rules"
    $rules = @(Get-SandboxRules)
    $rules | Remove-NetFirewallRule
    Write-Ok "Removed $($rules.Count) rule(s)."

    Write-Step "Removing the switch $Switch"
    $vswitch = Get-SandboxSwitch
    if (-not $vswitch) {
        Write-Note "The switch isn't there."
        return
    }
    if ($vswitch.SwitchType -ne "Internal") {
        Write-Problem "$Switch isn't an Internal switch, so it was left alone."
        return
    }
    $users = @(Get-VM -ErrorAction SilentlyContinue | Get-VMNetworkAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.SwitchName -eq $Switch })
    if ($users.Count -gt 0) {
        Write-Problem "Kept the switch: $($users.Count) VM network adapter(s) still use it. Delete the sandboxes first, then run Remove again."
        return
    }
    Remove-VMSwitch -Name $Switch -Force
    Write-Ok "Removed the switch $Switch and its host adapter."
}

Write-Host "Sandbox network: $Action" -ForegroundColor Cyan
$code = 0
try {
    if (-not (Get-Command Get-VMSwitch -ErrorAction SilentlyContinue)) {
        throw "Hyper-V's PowerShell tools aren't installed, or Hyper-V isn't turned on."
    }
    switch ($Action) {
        "Install" { Install-SandboxNetwork }
        "Remove" { Remove-SandboxNetwork }
        "Check" { if ((Show-SandboxCheck) -gt 0) { $code = 1 } }
    }
} catch {
    Write-Host "Stopped: $($_.Exception.Message)" -ForegroundColor Red
    $code = 1
}
Wait-Close
exit $code
