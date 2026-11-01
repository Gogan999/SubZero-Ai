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
# all UDP, ICMPv4, ICMPv6, and the IP protocols in $BlockProtocols (IGMP, IPv6 in IPv4, GRE, ESP, AH). Windows Firewall
# lets a block rule beat an allow rule, so the blocks skip the proxy port, and the one allow rule (szsandbox-allow-proxy)
# opens only TCP to the proxy port from 10.77.0.0/24. Together they leave nothing open but the proxy. Other allow rules
# on the host need no change, because these interface-scoped blocks take precedence. IPv6 is also unbound from the
# host's adapter, so the host has no IPv6 address there. Check also reads the Public profile's default inbound action,
# which must be Block.
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
$BlockProtocols = @(2, 41, 47, 50, 51)  # IP protocols with no ports, blocked whole: generated from sandbox.BLOCK_PROTOCOLS
$ProxyRule = "szsandbox-allow-proxy"

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
    'default-inbound-allow' = "Windows Firewall's default for incoming traffic on the Public profile is Allow, and it must be Block"
}

function Get-SandboxSwitch { return Get-VMSwitch -Name $Switch -ErrorAction SilentlyContinue }

# Only the szsandbox-* rules are asked for, by name, so the lookup stays fast on a PC with many rules
function Get-SandboxRules {
    return @(Get-NetFirewallRule -DisplayName "$Prefix*" -ErrorAction SilentlyContinue)
}

# The block rules the sandbox adapter needs, for a proxy port. A rule with no port range is left out (port 1 or 65535).
function Get-BlockRuleSpecs([int]$Port) {
    $specs = @()
    if ($Port -gt 1) { $specs += @{ Name = $BlockTcpBelow; Protocol = "TCP"; LocalPort = "1-$($Port - 1)" } }
    if ($Port -lt 65535) { $specs += @{ Name = $BlockTcpAbove; Protocol = "TCP"; LocalPort = "$($Port + 1)-65535" } }
    $specs += @{ Name = $BlockUdp; Protocol = "UDP"; LocalPort = $null }
    $specs += @{ Name = $BlockIcmp4; Protocol = "ICMPv4"; LocalPort = $null }
    $specs += @{ Name = $BlockIcmp6; Protocol = "ICMPv6"; LocalPort = $null }
    foreach ($number in $BlockProtocols) {
        $specs += @{ Name = "szsandbox-block-proto-$number"; Protocol = "$number"; LocalPort = $null }
    }
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
        return "The firewall rule $detail is missing, turned off or set up wrong. The sandbox adapter needs its block rules for every port except the proxy's, and for the other IP protocols"
    }
    if ($Code.StartsWith("check-failed:")) { return "The check couldn't finish ($($detail.Trim()))" }
    if ($Messages.ContainsKey($Code)) { return $Messages[$Code] }
    return "The check found a problem it doesn't know how to describe"
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

        $publicProfile = Get-NetFirewallProfile -Profile Public -ErrorAction Stop
        if ($publicProfile.DefaultInboundAction -eq "Allow") { $problems.Add('default-inbound-allow') }

        # Each enabled sandbox rule's filters are fetched once, into $infos, and every check reads from that list
        $rules = @(Get-SandboxRules | Where-Object { $_.Enabled -eq "True" })
        $infos = @()
        foreach ($rule in $rules) {
            $protocols = Get-RuleValues $rule "Get-NetFirewallPortFilter" "Protocol"
            $localPorts = Get-RuleValues $rule "Get-NetFirewallPortFilter" "LocalPort"
            $ifaces = Get-RuleValues $rule "Get-NetFirewallInterfaceFilter" "InterfaceAlias"
            $remotes = Get-RemoteAddresses $rule
            $infos += [pscustomobject]@{ Name = $rule.DisplayName; Direction = $rule.Direction; Action = $rule.Action;
                Protocols = $protocols; LocalPorts = $localPorts; Interfaces = $ifaces; Remotes = $remotes }
        }
        foreach ($spec in (Get-BlockRuleSpecs $ProxyPort)) {
            $ok = $false
            foreach ($info in @($infos | Where-Object { $_.Name -eq $spec.Name -and $_.Direction -eq "Inbound" -and $_.Action -eq "Block" })) {
                $portOk = ($null -eq $spec.LocalPort) -or ($info.LocalPorts -contains $spec.LocalPort)
                if (($info.Protocols -contains $spec.Protocol) -and $portOk -and ($info.Interfaces -contains $Alias) -and ($info.Remotes -contains "Any")) { $ok = $true }
            }
            if (-not $ok) { $problems.Add('block-rule-missing:' + $spec.Name) }
        }

        $proxyRules = @($infos | Where-Object { $_.Name -eq $ProxyRule })
        $proxyBad = $proxyRules.Count -eq 0
        foreach ($info in $proxyRules) {
            $ok = ($info.Direction -eq "Inbound") -and ($info.Action -eq "Allow") -and ($info.Protocols -contains "TCP") `
                -and ($info.LocalPorts -contains "$ProxyPort") -and ($info.LocalPorts.Count -eq 1) `
                -and ($info.Interfaces -contains $Alias) -and ($info.Remotes -contains $Subnet)
            if (-not $ok) { $proxyBad = $true }
        }
        if ($proxyBad) { $problems.Add('proxy-rule-wrong') }
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
