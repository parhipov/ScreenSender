# Gives Claude Code on another computer SSH access to this PC in one step, and writes everything that
# computer needs into one file. Started by give-claude-access.bat in the repository root, or run from
# an elevated PowerShell:
#     powershell -ExecutionPolicy Bypass -File ssh\give-access.ps1 [-Port 22] [-Out <file>]
# It sets up the SSH server (ssh\install-server.ps1), makes a key for Claude here and authorizes it,
# and writes claude-access-<this PC>.json: the private key, the account, the addresses (LAN,
# Tailscale, public), the host keys. On the other computer, ssh\import-access.ps1 reads that file.
# The file gives administrator access to this PC: carry it over privately and delete it afterwards.
# Safe to run again: a new key replaces the previous one, which stops working.

param(
    [int]$Port = 22,
    [string]$Out
)

$ErrorActionPreference = 'Stop'

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw 'Run this from an elevated PowerShell (Run as administrator), or start give-claude-access.bat.' }

$repo = Split-Path $PSScriptRoot
$computer = [Environment]::MachineName
if (-not $Out) { $Out = Join-Path $repo "claude-access-$computer.json" }
$keyName = "claude-access@$computer".ToLower()
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Find-Keygen {
    @((Join-Path $env:SystemRoot 'System32\OpenSSH\ssh-keygen.exe'), (Join-Path $env:ProgramFiles 'OpenSSH\ssh-keygen.exe')) |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}

# ---- the key: made here, given to the other computer in the file

$keygen = Find-Keygen
if (-not $keygen) {
    # The server install brings ssh-keygen when it comes from the MSI; the key is added in a second run.
    & (Join-Path $PSScriptRoot 'install-server.ps1') -Port $Port
    $keygen = Find-Keygen
}
if (-not $keygen) {
    Write-Host 'Installing the OpenSSH client (for ssh-keygen)...'
    $client = Get-WindowsCapability -Online -Name 'OpenSSH.Client*' | Select-Object -First 1
    Add-WindowsCapability -Online -Name $client.Name | Out-Null
    $keygen = Find-Keygen
}
$temp = Join-Path ([IO.Path]::GetTempPath()) "screensender-key-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory $temp | Out-Null
try {
    $keyFile = Join-Path $temp 'key'
    # One argument string, so the empty passphrase survives every PowerShell version.
    $run = Start-Process $keygen -ArgumentList ('-q -t ed25519 -N "" -C "{0}" -f "{1}"' -f $keyName, $keyFile) `
        -Wait -NoNewWindow -PassThru
    if ($run.ExitCode -ne 0) { throw 'ssh-keygen failed.' }
    $privateKey = ([IO.File]::ReadAllText($keyFile) -replace "`r`n", "`n").TrimEnd() + "`n"
    $publicKey = [IO.File]::ReadAllText("$keyFile.pub").Trim()
} finally {
    Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue
}

# ---- the SSH server, with that key (it replaces an earlier key of the same name)

& (Join-Path $PSScriptRoot 'install-server.ps1') -Port $Port -PublicKey $publicKey
Write-Host ''

# ---- what the other computer needs to know

$sshDir = Join-Path $env:ProgramData 'ssh'
$hostKeys = @(foreach ($type in 'ed25519', 'ecdsa', 'rsa') {
    $path = Join-Path $sshDir "ssh_host_${type}_key.pub"
    try {
        $parts = ([IO.File]::ReadAllText($path).Trim() -split '\s+')
        "$($parts[0]) $($parts[1])"
    } catch { }
})

# Every address of every interface: Claude picks the routes from them later.
$defaultRoutes = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0', '::/0' -ErrorAction SilentlyContinue)
$interfaces = @(Get-NetIPAddress -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceAlias -notlike 'Loopback*' -and $_.IPAddress -ne '::1' -and $_.IPAddress -notlike '127.*' } |
    Group-Object InterfaceIndex | ForEach-Object {
        $index = [int]$_.Name
        $adapter = Get-NetAdapter -InterfaceIndex $index -IncludeHidden -ErrorAction SilentlyContinue
        $ips = $_.Group
        [ordered]@{
            name = $ips[0].InterfaceAlias
            description = $adapter.InterfaceDescription
            mac = $adapter.MacAddress
            status = "$($adapter.Status)"
            ipv4 = @($ips | Where-Object AddressFamily -eq 'IPv4' | ForEach-Object { "$($_.IPAddress)/$($_.PrefixLength)" })
            ipv6 = @($ips | Where-Object AddressFamily -eq 'IPv6' | ForEach-Object { "$($_.IPAddress -replace '%\d+$', '')/$($_.PrefixLength)" })
            gateways = @($defaultRoutes | Where-Object InterfaceIndex -eq $index | ForEach-Object NextHop |
                Where-Object { $_ -notin '0.0.0.0', '::' } | Select-Object -Unique)
            dns = @(Get-DnsClientServerAddress -InterfaceIndex $index -ErrorAction SilentlyContinue | ForEach-Object ServerAddresses)
            default_route = [bool]($defaultRoutes | Where-Object InterfaceIndex -eq $index)
        }
    })
# The LAN addresses to reach this PC by: IPv4 on interfaces with a real gateway (a router), not a VPN's.
$lan = @($defaultRoutes | Where-Object { $_.AddressFamily -eq 'IPv4' -and $_.NextHop -ne '0.0.0.0' } |
    Sort-Object RouteMetric | ForEach-Object {
        $route = $_
        Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike '169.254.*' } | ForEach-Object {
                [ordered]@{ address = $_.IPAddress; prefix = $_.PrefixLength; interface = $route.InterfaceAlias; gateway = $route.NextHop }
            }
    })
# A VPN that takes all traffic adds 0.0.0.0/1 and 128.0.0.0/1; the public address seen is then its exit.
$fullVpn = [bool](Get-NetRoute -DestinationPrefix '0.0.0.0/1' -AddressFamily IPv4 -ErrorAction SilentlyContinue)

$tailscale = $null
$tsExe = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if (-not $tsExe -and (Test-Path "$env:ProgramFiles\Tailscale\tailscale.exe")) { $tsExe = "$env:ProgramFiles\Tailscale\tailscale.exe" }
if ($tsExe) {
    try {
        $status = (& $tsExe status --json 2>$null) -join "`n" | ConvertFrom-Json
        $tailscale = [ordered]@{
            state = $status.BackendState
            address = @($status.Self.TailscaleIPs | Where-Object { $_ -match '^\d+\.' })[0]
            addresses = @($status.Self.TailscaleIPs)
            dns_name = "$($status.Self.DNSName)".TrimEnd('.')
        }
        if ($status.BackendState -ne 'Running') { $tailscale.address = $null }
    } catch { }
}

# ---- public addresses

function Get-TraceIp($url, [string[]]$curlArgs = @()) {
    $ip = $null
    try {
        $trace = curl.exe -s --max-time 8 @curlArgs $url
        $ip = (($trace | Where-Object { $_ -like 'ip=*' }) -replace '^ip=', '' | Select-Object -First 1)
    } catch { }
    if ($ip) { return [string]$ip }
    return $null
}

function Get-RouterWanIp($localAddress) {
    # UPnP: the router's own WAN address. A private or 100.64.0.0/10 one means the provider's CGNAT.
    try {
        $udp = New-Object Net.Sockets.UdpClient (New-Object Net.IPEndPoint ([Net.IPAddress]::Parse($localAddress)), 0)
        $udp.Client.ReceiveTimeout = 2500
        $search = "M-SEARCH * HTTP/1.1`r`nHOST: 239.255.255.250:1900`r`nMAN: `"ssdp:discover`"`r`nMX: 2`r`n" +
                  "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1`r`n`r`n"
        $bytes = [Text.Encoding]::ASCII.GetBytes($search)
        [void]$udp.Send($bytes, $bytes.Length, '239.255.255.250', 1900)
        $locations = @()
        $deadline = (Get-Date).AddSeconds(3)
        while ((Get-Date) -lt $deadline) {
            $from = New-Object Net.IPEndPoint ([Net.IPAddress]::Any, 0)
            try { $reply = [Text.Encoding]::ASCII.GetString($udp.Receive([ref]$from)) } catch { break }
            if ($reply -match '(?im)^LOCATION:\s*(\S+)') { $locations += $Matches[1] }
        }
        $udp.Close()
        foreach ($location in ($locations | Select-Object -Unique)) {
            $xml = [xml](curl.exe -s --max-time 5 $location | Out-String)
            foreach ($service in $xml.SelectNodes("//*[local-name()='service']")) {
                $type = $service.serviceType
                if ($type -notmatch 'WAN(IP|PPP)Connection') { continue }
                $body = '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" ' +
                        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>' +
                        "<u:GetExternalIPAddress xmlns:u=`"$type`"/></s:Body></s:Envelope>"
                $answer = Invoke-WebRequest -UseBasicParsing -Method Post -TimeoutSec 5 -Body $body `
                    -Uri (New-Object Uri ([Uri]$location), $service.controlURL) -ContentType 'text/xml; charset="utf-8"' `
                    -Headers @{ SOAPAction = "`"$type#GetExternalIPAddress`"" }
                if ($answer.Content -match '<NewExternalIPAddress>([^<]+)<') { return $Matches[1] }
            }
        }
    } catch { }
}

function Test-PrivateIp($ip) {
    $ip -match '^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)'
}

$publicV4 = Get-TraceIp 'https://1.1.1.1/cdn-cgi/trace'
$publicV6 = Get-TraceIp 'https://[2606:4700:4700::1111]/cdn-cgi/trace' @('-6')
$publicV4WithoutVpn = $null
if ($fullVpn -and $lan) {
    # Ask once more past the VPN, through the router: a host route to 1.0.0.1, removed right after.
    $gateway = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -AddressFamily IPv4 |
        Where-Object NextHop -ne '0.0.0.0' | Sort-Object RouteMetric | Select-Object -First 1
    try {
        New-NetRoute -DestinationPrefix '1.0.0.1/32' -InterfaceIndex $gateway.InterfaceIndex -NextHop $gateway.NextHop `
            -RouteMetric 1 -PolicyStore ActiveStore -ErrorAction Stop | Out-Null
        $publicV4WithoutVpn = Get-TraceIp 'https://1.0.0.1/cdn-cgi/trace'
    } catch {
    } finally {
        Remove-NetRoute -DestinationPrefix '1.0.0.1/32' -InterfaceIndex $gateway.InterfaceIndex -Confirm:$false -ErrorAction SilentlyContinue
    }
}
$routerWan = if ($lan) { Get-RouterWanIp $lan[0].address } else { $null }

# The PC's own internet address, past any VPN.
$publicIp = if ($fullVpn) { $publicV4WithoutVpn } else { $publicV4 }
if (-not $publicIp -and $routerWan -and -not (Test-PrivateIp $routerWan)) { $publicIp = $routerWan }
$ownAddresses = @(Get-NetIPAddress -ErrorAction SilentlyContinue | ForEach-Object { $_.IPAddress -replace '%\d+$', '' })
$publicOnThisPc = [bool]($publicIp -and $ownAddresses -contains $publicIp)
$cgnat = $null
if ($routerWan) { $cgnat = [bool]((Test-PrivateIp $routerWan) -or ($publicIp -and $routerWan -ne $publicIp)) }
$public = [ordered]@{
    ipv4 = $publicV4
    ipv4_without_vpn = $publicV4WithoutVpn
    ipv6 = $publicV6
    ipv6_on_this_pc = [bool]($publicV6 -and $ownAddresses -contains $publicV6)
    router_wan_ipv4 = $routerWan
    behind_cgnat = $cgnat
}

$os = Get-CimInstance Win32_OperatingSystem
$screenSender = [bool](Get-ScheduledTask -TaskName 'ScreenSender' -ErrorAction SilentlyContinue)

$access = [ordered]@{
    format = 'screensender-access/1'
    created = (Get-Date).ToString('o')
    computer = $computer
    user = $env:USERNAME
    account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    os = "$($os.Caption) $($os.Version)"
    port = $Port
    key_name = $keyName
    private_key = $privateKey
    public_key = $publicKey
    host_keys = $hostKeys
    lan = $lan
    tailscale = $tailscale
    public_ip = $publicIp
    public_ip_on_this_pc = $publicOnThisPc
    full_tunnel_vpn = $fullVpn
    public = $public
    interfaces = $interfaces
    repo = $repo
    screensender_installed = $screenSender
}
[IO.File]::WriteAllText($Out, ($access | ConvertTo-Json -Depth 5), $utf8)
# Readable by this user and administrators only: it holds the private key.
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
icacls $Out /inheritance:r /grant "*${me}:F" /grant '*S-1-5-32-544:F' | Out-Null

# ---- summary

Write-Host '==== Claude access is ready ====' -ForegroundColor Green
Write-Host "File:     $Out"
Write-Host "Account:  $env:USERNAME on $computer, SSH port $Port"
foreach ($interface in $interfaces | Where-Object status -eq 'Up') {
    # The file has them all; here only the ones that can carry a connection.
    $addresses = @($interface.ipv4 | Where-Object { $_ -notlike '169.254.*' }) + @($interface.ipv6 | Where-Object { $_ -notlike 'fe80:*' })
    if ($addresses) { Write-Host ("Interface: {0}: {1}" -f $interface.name, ($addresses -join ', ')) }
}
if ($tailscale.address) {
    Write-Host "Tailscale: $($tailscale.addresses -join ', ') $($tailscale.dns_name)"
} else {
    Write-Host 'Tailscale: not running here. For access from anywhere without router setup, install'
    Write-Host '           Tailscale (https://tailscale.com/download), sign in, and run this again.' -ForegroundColor Yellow
}
if ($publicV4) { Write-Host "Public IPv4: $publicV4$(if ($fullVpn) { ' (the VPN''s exit)' })" }
if ($publicV4WithoutVpn) { Write-Host "Public IPv4 past the VPN: $publicV4WithoutVpn" }
if ($publicV6) { Write-Host "Public IPv6: $publicV6$(if ($public.ipv6_on_this_pc) { ' (on this PC)' })" }
if ($routerWan) { Write-Host "Router WAN: $routerWan$(if ($cgnat) { ' (provider CGNAT: port forwarding cannot help)' })" }
if ($publicIp) {
    $how = if ($publicOnThisPc) { 'on this PC itself: reachable directly' }
           elseif ($cgnat) { 'shared by the provider: not reachable from outside' }
           else { 'the router''s: needs a port forwarded to this PC' }
    Write-Host "This PC on the internet: $publicIp ($how)"
}
Write-Host ''
Write-Host 'Give this file to Claude on the other computer, privately (not by e-mail or chat).' -ForegroundColor Yellow
Write-Host 'It gives administrator access to this PC. Delete it here once it has been copied.' -ForegroundColor Yellow
