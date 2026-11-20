# Sets this computer up to reach a PC over SSH from the file that give-claude-access.bat wrote there.
# No admin rights needed. Run in PowerShell:
#     powershell -ExecutionPolicy Bypass -File ssh\import-access.ps1 <claude-access-PC.json> `
#         [-Name <alias>] [-PublicHost <ip or DNS name>] [-PublicPort <port>]
# It saves the key as ~/.ssh/screensender_<Name>, trusts the PC's host keys at its addresses, and runs
# setup-client.ps1 with the routes from the file: Tailscale and LAN, and the public address when the
# PC has it itself. Behind a router, give -PublicPort (the port forwarded to the PC's SSH port) to add
# the public route too; -PublicHost replaces the address from the file (a dynamic DNS name).
# -Name defaults to the PC's name in lower case. Delete the file afterwards: it holds the key.

param(
    [Parameter(Mandatory = $true, Position = 0)][string]$File,
    [string]$Name,
    [string]$PublicHost,
    [int]$PublicPort = 0
)

$ErrorActionPreference = 'Stop'

$access = Get-Content $File -Raw -Encoding UTF8 | ConvertFrom-Json
if ($access.format -ne 'screensender-access/1') { throw "$File is not a file from give-claude-access.bat." }
if (-not $Name) { $Name = $access.computer.ToLower() }
if ($Name -notmatch '^[A-Za-z0-9_.-]+$') { throw "Give -Name: '$Name' does not suit an ssh alias." }
$port = [int]$access.port

$profileDir = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
$sshDir = Join-Path $profileDir '.ssh'
$key = Join-Path $sshDir "screensender_$Name"
$utf8 = New-Object System.Text.UTF8Encoding($false)
New-Item -ItemType Directory -Force $sshDir | Out-Null

# ---- the key (setup-client.ps1 keeps a key that exists and fixes its permissions)

Remove-Item $key, "$key.pub" -Force -ErrorAction SilentlyContinue
[IO.File]::WriteAllText($key, $access.private_key, $utf8)
[IO.File]::WriteAllText("$key.pub", $access.public_key + "`n", $utf8)
# Everything else from the file, without the key: every address of the PC, for later.
$info = $access | Select-Object * -ExcludeProperty private_key
[IO.File]::WriteAllText("$key.json", ($info | ConvertTo-Json -Depth 6), $utf8)

# ---- routes

$lanHost = @($access.lan | ForEach-Object address)[0]
$tsHost = if ($access.tailscale) { $access.tailscale.address } else { $null }
if (-not $PublicHost -and ($PublicPort -or $access.public_ip_on_this_pc)) { $PublicHost = $access.public_ip }
if ($PublicHost -and -not $PublicPort) { $PublicPort = $port }
if (-not ($lanHost -or $tsHost -or $PublicHost)) { throw 'The file has no address of the PC.' }

# ---- host keys: known up front, so the first connection needs no trust on first use

$knownHosts = Join-Path $sshDir 'known_hosts'
$entries = @()
foreach ($pair in @(@($lanHost, $port), @($tsHost, $port), @($PublicHost, $PublicPort))) {
    if (-not $pair[0]) { continue }
    $hostName = if ($pair[1] -eq 22) { $pair[0] } else { "[$($pair[0])]:$($pair[1])" }
    if (Test-Path $knownHosts) { cmd /c "ssh-keygen -R `"$hostName`" -f `"$knownHosts`" >nul 2>&1" }
    foreach ($hostKey in $access.host_keys) {
        if ($hostKey -match '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+) [A-Za-z0-9+/=]+$') { $entries += "$hostName $hostKey" }
    }
}
$old = if (Test-Path $knownHosts) { [IO.File]::ReadAllText($knownHosts) } else { '' }
if ($old -and -not $old.EndsWith("`n")) { $old += "`n" }
[IO.File]::WriteAllText($knownHosts, $old + (($entries -join "`n") + "`n"), $utf8)

# ---- aliases and notes for Claude

$arguments = @{ User = $access.user; Name = $Name; Port = $port; RemoteDir = $access.repo }
if ($lanHost) { $arguments.LanHost = $lanHost }
if ($tsHost) { $arguments.TailscaleHost = $tsHost }
if ($PublicHost) { $arguments.PublicHost = $PublicHost; $arguments.PublicPort = $PublicPort }
& (Join-Path $PSScriptRoot 'setup-client.ps1') @arguments

Write-Host ''
Write-Host "PC '$($access.computer)' ($($access.os)), account $($access.account), as '$Name'."
Write-Host "All its addresses (every interface, public IPv4/IPv6, router WAN): $key.json"
if (-not $tsHost) { Write-Host 'No Tailscale route: the PC had no Tailscale running when the file was made.' }
if ($access.public_ip -and -not $PublicHost) {
    Write-Host "Public address $($access.public_ip) is the router's: forward a port to the PC's port $port, then run this again with -PublicPort <that port>."
}
Write-Host "Delete $File now: it holds the key."
