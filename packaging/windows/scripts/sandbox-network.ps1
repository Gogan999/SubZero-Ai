#Requires -RunAsAdministrator
# Sets up, checks, or removes the host side of the Code sandbox network. Run it once, as an administrator, on the
# mini PC (see docs/SETTINGS.md, "Code sandboxes"). Nothing runs it automatically.
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-network.ps1 -Action Check
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-network.ps1 -Action Install
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox-network.ps1 -Action Remove
# Install: creates the Internal switch if it's missing, gives the host's adapter for it 10.77.0.1/24 with no gateway,
# turns IP forwarding and IPv6 off on it, sets its network profile to Public, and adds the firewall rules below.
# Remove: deletes only the szsandbox-* firewall rules and, when no VM is attached to it, the switch.
# Check: changes nothing and lists what is missing. Only objects named szsandbox-* are ever changed; other adapters,
# switches, VMs and rules are never touched.
# Firewall rules, all inbound on the sandbox adapter. Block rules from any address: TCP below and above the proxy port,
# all UDP, ICMPv4 and ICMPv6. Windows Firewall lets a block rule beat an allow rule, so the blocks skip the proxy port,
# and the one allow rule (szsandbox-allow-proxy) opens only TCP to the proxy port from 10.77.0.0/24. Together they
# leave nothing open but the proxy. IPv6 is also unbound from the host's adapter, so the host has no IPv6 address there.
# The checks here must match host_network_check_script in subzero_ai/sandbox.py. Keep both in step (a test compares
# the problem codes, the rule names and the helper functions).

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
$BlockTcpBelow = "szsandbox-block-tcp-below"
$BlockTcpAbove = "szsandbox-block-tcp-above"
$BlockUdp = "szsandbox-block-udp"
$BlockIcmp4 = "szsandbox-block-icmp4"
$BlockIcmp6 = "szsandbox-block-icmp6"
$ProxyRule = "szsandbox-allow-proxy"
$RiskyPorts = @(135, 139, 445, 3389, 5985)  # SMB, RPC, RDP and WinRM: never open to the sandboxes

if (-not $Switch.StartsWith($Prefix)) {
    throw "The switch name must start with $Prefix, so this script only ever changes sandbox objects."
}
$Alias = "vEthernet ($Switch)"  # the host's adapter for an Internal switch

# Plain-language text for each problem code. The keys are the codes sandbox.py uses (NETWORK_PROBLEMS).
$Messages = @{
    'switch-missing' = "The sandbox switch $Switch doesn't exist yet"
    'switch-not-internal' = "The sandbox switch $Switch isn't an Internal switch"
    'host-adapter-missing' = "The host has no network adapter for $Switch"
    'host-address' = "The host's adapter for the sandboxes doesn't have the address $HostAddress/24"
    'host-gateway' = "The host's adapter for the sandboxes has a default gateway, and it must have none"
    'forwarding-on' = "IP forwarding is on for the host's adapter for the sandboxes, and it must be off"
    'profile-not-public' = "The network profile of the sandbox adapter isn't Public"
    'ipv6-enabled' = "IPv6 is still on for the host's adapter for the sandboxes, and it must be off (uncheck TCP/IPv6 on that adapter)"
    'proxy-rule-wrong' = "The firewall rule $ProxyRule must exist and allow only TCP to the proxy port from $Subnet on the sandbox adapter"
}

function Get-SandboxSwitch { return Get-VMSwitch -Name $Switch -ErrorAction SilentlyContinue }

function Get-SandboxRules {
    return @(Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "$Prefix*" })
}

# The block rules the sandbox adapter needs, for a proxy port. A rule with no port range is left out (port 1 or 65535).
function Get-BlockRuleSpecs([int]$Port) {
    $specs = @()
    if ($Port -gt 1) { $specs += @{ Name = $BlockTcpBelow; Protocol = "TCP"; LocalPort = "1-$($Port - 1)" } }
    if ($Port -lt 65535) { $specs += @{ Name = $BlockTcpAbove; Protocol = "TCP"; LocalPort = "$($Port + 1)-65535" } }
    $specs += @{ Name = $BlockUdp; Protocol = "UDP"; LocalPort = $null }
    $specs += @{ Name = $BlockIcmp4; Protocol = "ICMPv4"; LocalPort = $null }
    $specs += @{ Name = $BlockIcmp6; Protocol = "ICMPv6"; LocalPort = $null }
    return $specs
}

# Shared with host_network_check_script in sandbox.py (NETWORK_CHECK_HELPERS). Keep the two identical.
function Get-RuleValues($Rule, [string]$Cmdlet, [string]$Field) {
    return @($Rule | & $Cmdlet -ErrorAction SilentlyContinue | ForEach-Object { $_.$Field })
}
function ConvertTo-Cidr([string]$Text) {
    if ($Text -match '^(\d+\.\d+\.\d+\.\d+)/(\d+\.\d+\.\d+\.\d+)$') {
        $address = $Matches[1]
        $bits = 0
        foreach ($octet in [System.Net.IPAddress]::Parse($Matches[2]).GetAddressBytes()) {
            $bits += ([Convert]::ToString([int]$octet, 2) -replace '0', '').Length
        }
        return "$address/$bits"
    }
    return $Text
}
function Get-RemoteAddresses($Rule) {
    return @(Get-RuleValues $Rule "Get-NetFirewallAddressFilter" "RemoteAddress" |
        ForEach-Object { ConvertTo-Cidr ([string]$_) })
}

# The text for one problem code (a code with a name after the colon is built from the name)
function Get-ProblemText([string]$Code) {
    $detail = ""
    if ($Code.IndexOf(':') -ge 0) { $detail = $Code.Substring($Code.IndexOf(':') + 1) }
    if ($Code.StartsWith("block-rule-missing:")) {
        return "The firewall rule $detail is missing, turned off or set up wrong. The sandbox adapter needs its block rules for every port except the proxy's"
    }
    if ($Code.StartsWith("open-allow:")) {
        return "The firewall rule '$detail' lets other computers reach this PC over the sandbox network. Turn it off, or limit it so it doesn't apply to the sandbox adapter"
    }
    if ($Code.StartsWith("check-failed:")) { return "The check couldn't finish ($($detail.Trim()))" }
    if ($Messages.ContainsKey($Code)) { return $Messages[$Code] }
    return "The check found a problem it doesn't know how to describe"
}

# Enabled inbound allow rules, not ours, that reach this adapter from anywhere and open a risky port. Best effort.
function Get-OpenAllowNames {
    $names = @()
    $allows = @(Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True -ErrorAction SilentlyContinue)
    foreach ($rule in $allows) {
        if ($rule.DisplayName -like "$Prefix*") { continue }
        $profileText = [string]$rule.Profile
        if (($profileText -notmatch "Public") -and ($profileText -notmatch "Any")) { continue }
        $protocols = Get-RuleValues $rule "Get-NetFirewallPortFilter" "Protocol"
        if (@($protocols | Where-Object { $_ -match "^ICMP" }).Count -gt 0) { continue }
        $ifaces = Get-RuleValues $rule "Get-NetFirewallInterfaceFilter" "InterfaceAlias"
        $ifaceOk = ($ifaces.Count -eq 0) -or ($ifaces -contains "Any") -or ($ifaces -contains $Alias)
        if (-not $ifaceOk) { continue }
        $remotes = Get-RemoteAddresses $rule
        $remoteOk = ($remotes.Count -eq 0) -or ($remotes -contains "Any") -or ($remotes -contains "LocalSubnet")
        if (-not $remoteOk) { continue }
        $wide = $false
        foreach ($portText in (Get-RuleValues $rule "Get-NetFirewallPortFilter" "LocalPort")) {
            if (($portText -eq "Any") -or ($portText -eq "")) { $wide = $true }
            elseif ($portText -match '^(\d+)(?:-(\d+))?$') {
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

# The problem codes for the host's sandbox network (the same codes as sandbox.py). An empty list means it's ready.
function Get-SandboxProblems {
    $problems = New-Object System.Collections.Generic.List[string]
    try {
        $vswitch = Get-SandboxSwitch
        if (-not $vswitch) {
            $problems.Add('switch-missing')
        } elseif ($vswitch.SwitchType -ne "Internal") {
            $problems.Add('switch-not-internal')
        }

        $adapter = Get-NetAdapter -Name $Alias -ErrorAction SilentlyContinue
        if (-not $adapter) {
            $problems.Add('host-adapter-missing')
        } else {
            $ips = @(Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue)
            if (-not ($ips | Where-Object { $_.IPAddress -eq $HostAddress -and $_.PrefixLength -eq 24 })) {
                $problems.Add('host-address')
            }
            $routes = @(Get-NetRoute -InterfaceAlias $Alias -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue)
            if ($routes.Count -gt 0) { $problems.Add('host-gateway') }
            $ipif = Get-NetIPInterface -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue
            if ($ipif -and ($ipif.Forwarding -ne "Disabled")) { $problems.Add('forwarding-on') }
            $netProfile = Get-NetConnectionProfile -InterfaceAlias $Alias -ErrorAction SilentlyContinue
            $isPublic = $netProfile -and ($netProfile.NetworkCategory -eq "Public")
            if (-not $isPublic) { $problems.Add('profile-not-public') }
            $binding = Get-NetAdapterBinding -Name $Alias -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue
            if (-not $binding -or $binding.Enabled) { $problems.Add('ipv6-enabled') }
        }

        $rules = @(Get-SandboxRules | Where-Object { $_.Enabled -eq "True" })
        foreach ($spec in (Get-BlockRuleSpecs $ProxyPort)) {
            $ok = $false
            foreach ($rule in @($rules | Where-Object { $_.DisplayName -eq $spec.Name -and $_.Direction -eq "Inbound" -and $_.Action -eq "Block" })) {
                $protocols = Get-RuleValues $rule "Get-NetFirewallPortFilter" "Protocol"
                $localPorts = Get-RuleValues $rule "Get-NetFirewallPortFilter" "LocalPort"
                $ifaces = Get-RuleValues $rule "Get-NetFirewallInterfaceFilter" "InterfaceAlias"
                $remotes = Get-RemoteAddresses $rule
                $portOk = ($null -eq $spec.LocalPort) -or ($localPorts -contains $spec.LocalPort)
                if (($protocols -contains $spec.Protocol) -and $portOk -and ($ifaces -contains $Alias) -and ($remotes -contains "Any")) { $ok = $true }
            }
            if (-not $ok) { $problems.Add('block-rule-missing:' + $spec.Name) }
        }

        $proxyRules = @($rules | Where-Object { $_.DisplayName -eq $ProxyRule })
        $proxyBad = $proxyRules.Count -eq 0
        foreach ($rule in $proxyRules) {
            $protocols = Get-RuleValues $rule "Get-NetFirewallPortFilter" "Protocol"
            $localPorts = Get-RuleValues $rule "Get-NetFirewallPortFilter" "LocalPort"
            $ifaces = Get-RuleValues $rule "Get-NetFirewallInterfaceFilter" "InterfaceAlias"
            $remotes = Get-RemoteAddresses $rule
            $ok = ($rule.Direction -eq "Inbound") -and ($rule.Action -eq "Allow") -and ($protocols -contains "TCP") `
                -and ($localPorts -contains "$ProxyPort") -and ($localPorts.Count -eq 1) `
                -and ($ifaces -contains $Alias) -and ($remotes -contains $Subnet)
            if (-not $ok) { $proxyBad = $true }
        }
        if ($proxyBad) { $problems.Add('proxy-rule-wrong') }

        foreach ($name in (Get-OpenAllowNames)) { $problems.Add('open-allow:' + $name) }
    } catch {
        $problems.Add('check-failed:' + ($_.Exception.Message -replace '[\r\n]+', ' '))
    }
    return $problems.ToArray()
}

# Prints the checks and returns how many problems there are
function Show-SandboxCheck {
    Write-Step "Checking the sandbox network"
    $problems = @(Get-SandboxProblems)
    foreach ($code in $problems) { Write-Problem (Get-ProblemText $code) }
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

    Write-Step "Turning IPv6 off on the host's adapter"
    Disable-NetAdapterBinding -Name $Alias -ComponentID ms_tcpip6 -Confirm:$false
    Write-Ok "Turned IPv6 off on $Alias."

    Write-Step "Adding the firewall rules"
    Get-SandboxRules | Remove-NetFirewallRule
    foreach ($spec in (Get-BlockRuleSpecs $ProxyPort)) {
        $params = @{ Name = $spec.Name; DisplayName = $spec.Name; Direction = "Inbound"; Action = "Block";
            Protocol = $spec.Protocol; RemoteAddress = "Any"; InterfaceAlias = $Alias; Profile = "Any"; Enabled = "True" }
        if ($null -ne $spec.LocalPort) { $params["LocalPort"] = $spec.LocalPort }
        New-NetFirewallRule @params | Out-Null
    }
    New-NetFirewallRule -Name $ProxyRule -DisplayName $ProxyRule -Direction Inbound -Action Allow -Protocol TCP `
        -LocalPort $ProxyPort -RemoteAddress $Subnet -InterfaceAlias $Alias -Profile Any -Enabled True | Out-Null
    Write-Ok "Added the block rules and $ProxyRule."
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
    # Not a `switch` statement: the -Switch parameter is the automatic $switch variable, so `switch` would run nothing
    if ($Action -eq "Install") { Install-SandboxNetwork }
    elseif ($Action -eq "Remove") { Remove-SandboxNetwork }
    elseif ($Action -eq "Check") { if ((Show-SandboxCheck) -gt 0) { $code = 1 } }
    else { throw "Unknown action $Action." }
} catch {
    Write-Host "Stopped: $($_.Exception.Message)" -ForegroundColor Red
    $code = 1
}
Wait-Close
exit $code
