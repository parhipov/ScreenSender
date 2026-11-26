# Sets this computer up to reach a ScreenSender PC over SSH, for you and for Claude Code here.
# No admin rights needed. Run in PowerShell:
#     powershell -ExecutionPolicy Bypass -File ssh\setup-client.ps1 -User Pavel `
#         [-Name home] [-Port 22] [-LanHost 192.168.1.20] [-TailscaleHost home-pc] `
#         [-PublicHost my.ddns.net] [-PublicPort 22]
# Safe to run again. It
#   - makes an SSH key for that PC: ~/.ssh/screensender_<Name>, without a passphrase, so Claude can use
#     it unattended (add one with ssh-keygen -p and an ssh-agent if you prefer);
#   - writes the aliases to ~/.ssh/config: <Name> (public address, through the router), <Name>-ts
#     (Tailscale), <Name>-lan (same network), for the hosts given;
#   - tells Claude Code on this computer how to work there, in ~/.claude/CLAUDE.md;
#   - prints the line that adds the key on the PC, and tries every alias.
# -User is the account name on that PC; -Port is its SSH port, -PublicPort the one the router
# forwards to it. -Shell bash for a Linux PC (a Raspberry Pi); remote.ps1 reads it back from the
# ssh config. To forget the PC here (aliases, Claude's notes, the key):
#     powershell -ExecutionPolicy Bypass -File ssh\setup-client.ps1 -Name home -Remove

param(
    [string]$User,
    [string]$Name = 'home',
    [int]$Port = 22,
    [string]$LanHost,
    [string]$TailscaleHost,
    [string]$PublicHost,
    [int]$PublicPort = 0,
    [string]$RemoteDir,
    [ValidateSet('powershell', 'bash')][string]$Shell = 'powershell',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$linux = $Shell -eq 'bash'
if (-not $RemoteDir) { $RemoteDir = if ($linux) { '~/ScreenSender' } else { 'C:\ScreenSender' } }

$profileDir = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
$sshDir = Join-Path $profileDir '.ssh'
$key = Join-Path $sshDir "screensender_$Name"
$sshConfig = Join-Path $sshDir 'config'
$claudeMd = Join-Path $profileDir '.claude\CLAUDE.md'
$sshMarks = "# >>> ScreenSender $Name >>>", "# <<< ScreenSender $Name <<<"
$claudeMarks = "<!-- >>> ScreenSender $Name >>> -->", "<!-- <<< ScreenSender $Name <<< -->"
$utf8 = New-Object System.Text.UTF8Encoding($false)  # ssh reads no BOM

function Set-Block($path, $begin, $end, [string[]]$block) {
    # Replaces the lines between the markers (or adds them at the top); no block removes them.
    $kept = New-Object System.Collections.Generic.List[string]
    if (Test-Path $path) {
        $inBlock = $false
        foreach ($line in [IO.File]::ReadAllLines($path)) {
            if ($line -eq $begin) { $inBlock = $true; continue }
            if ($line -eq $end) { $inBlock = $false; continue }
            if (-not $inBlock) { $kept.Add($line) }
        }
        while ($kept.Count -and -not $kept[0].Trim()) { $kept.RemoveAt(0) }
    } elseif (-not $block) {
        return
    }
    $all = @()
    if ($block) { $all = @($begin) + $block + @($end) }
    if ($kept.Count) { $all += @(if ($block) { '' }) + $kept }
    if (-not $all) { Remove-Item $path; return }  # it held only our block
    [IO.File]::WriteAllLines($path, [string[]]$all, $utf8)
}

if ($Remove) {
    Set-Block $sshConfig $sshMarks[0] $sshMarks[1] @()
    Set-Block $claudeMd $claudeMarks[0] $claudeMarks[1] @()
    Remove-Item $key, "$key.pub" -ErrorAction SilentlyContinue
    Write-Host "Forgot '$Name' here. On the PC, remove its key with: ssh\install-server.ps1 -RemoveKey <the key's name>"
    exit 0
}

foreach ($tool in 'ssh', 'ssh-keygen') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "No $tool here. On Windows: Settings -> Apps -> Optional features -> OpenSSH Client."
    }
}
if (-not $User) { throw 'Give -User: the account name on that PC.' }
if (-not ($LanHost -or $TailscaleHost -or $PublicHost)) { throw 'Give at least one of -LanHost, -TailscaleHost, -PublicHost.' }
if (-not $PublicPort) { $PublicPort = $Port }
New-Item -ItemType Directory -Force $sshDir | Out-Null

# ---- the key

if (-not (Test-Path $key)) {
    $comment = "claude@$([Environment]::MachineName)".ToLower()
    # One argument string, so the empty passphrase survives every PowerShell version.
    $keygen = Start-Process ssh-keygen -ArgumentList ('-q -t ed25519 -N "" -C "{0}" -f "{1}"' -f $comment, $key) `
        -Wait -NoNewWindow -PassThru
    if ($keygen.ExitCode -ne 0) { throw 'ssh-keygen failed.' }
}
if ($env:OS -eq 'Windows_NT') {
    # ssh ignores a private key that others can read, and a new file may inherit that from its folder.
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    icacls $key /inheritance:r /grant "*${me}:F" | Out-Null
}
$publicKey = ([IO.File]::ReadAllText("$key.pub")).Trim()

# ---- ~/.ssh/config: a managed block at the top, as ssh uses the first value it finds

$aliases = [ordered]@{}
if ($TailscaleHost) { $aliases["$Name-ts"] = @($TailscaleHost, $Port, 'Tailscale') }
if ($PublicHost) { $aliases[$Name] = @($PublicHost, $PublicPort, 'public address') }
if ($LanHost) { $aliases["$Name-lan"] = @($LanHost, $Port, 'same network') }
$hostLines = foreach ($alias in $aliases.Keys) {
    "Host $alias"
    "    HostName $($aliases[$alias][0])"
    "    Port $($aliases[$alias][1])"
    "    User $User"
    "    IdentityFile `"$($key -replace '\\', '/')`""
    '    IdentitiesOnly yes'
    '    StrictHostKeyChecking accept-new'
    '    ConnectTimeout 10'
    '    ServerAliveInterval 30'
    '    ServerAliveCountMax 3'
}
# remote.ps1 reads the remote shell from this comment; ssh ignores it.
$hostLines = @("# shell: $Shell") + @($hostLines)
Set-Block $sshConfig $sshMarks[0] $sshMarks[1] $hostLines

# ---- ~/.claude/CLAUDE.md: what every Claude Code session here should know about that PC

$routes = ($aliases.Keys | ForEach-Object { "``$_`` ($($aliases[$_][2]))" }) -join ', '
$remoteScript = Join-Path $PSScriptRoot 'remote.ps1'
$guide = if ($linux) { @(
    "## Remote PC ``$Name`` (ScreenSender, Linux)",
    '',
    "``$Name`` is a Linux PC (a Raspberry Pi) far away. The user may be controlling it right now through",
    'ScreenSender, with no other way in: if ScreenSender stops there, they lose it until someone is there.',
    '',
    "- Run shell code there with ``& '$remoteScript' -Name $Name '<code>'``",
    "  (from Bash: ``powershell -NoProfile -File '$remoteScript' -Name $Name '<code>'``). It finds a working",
    "  route, needs no escaping of quotes and returns the output. The code runs in bash as $User; use sudo",
    '  for system changes.',
    "- Plain ssh works too, with the aliases $routes;",
    '  the shell there is bash. Copy files with `scp`.',
    "- Its runbook is ``$RemoteDir/CLAUDE.md`` on that PC; read it (``cat $RemoteDir/CLAUDE.md`` there)",
    '  before you touch ScreenSender, the network, power, sign-in or the SSH server there.',
    '- Never kill python processes there or stop the screensender user service. Update it only with',
    "  ``systemctl --user start screensender-update`` and read the RESULT line in ``$RemoteDir/runtime/update.log``.",
    '- Ask the user before rebooting, shutting down or logging out there, and before changing its',
    '  firewall, network or SSH settings.',
    '- Programs started over SSH have no desktop unless given one: GUI apps do not show up on the screen.',
    '- Keep each ssh call short; start long jobs with systemd-run --user or nohup and poll them.'
) } else { @(
    "## Remote PC ``$Name`` (ScreenSender)",
    '',
    "``$Name`` is a Windows PC far away. The user may be controlling it right now through ScreenSender,",
    'with no other way in: if ScreenSender stops there, they lose the PC until someone is physically there.',
    '',
    "- Run PowerShell code there with ``& '$remoteScript' -Name $Name '<code>'``",
    "  (from Bash: ``powershell -NoProfile -File '$remoteScript' -Name $Name '<code>'``). It finds a working",
    '  route, needs no escaping of quotes and returns readable UTF-8 output. The code runs as administrator.',
    "- Plain ssh works too, with the aliases $routes;",
    '  the shell there is PowerShell. Copy files with `scp`.',
    "- Its runbook is ``$RemoteDir\CLAUDE.md`` on that PC; read it (``Get-Content $RemoteDir\CLAUDE.md`` there)",
    '  before you touch ScreenSender, the network, power, sign-in or the SSH server there.',
    '- Never kill python processes there or end the ScreenSender task. Update it only with',
    "  ``schtasks /Run /TN `"ScreenSender update`"`` and wait for the RESULT line in ``$RemoteDir\runtime\update.log``.",
    '- Ask the user before rebooting, shutting down, locking or signing out there, and before changing its',
    '  firewall, network or SSH settings.',
    '- Programs started over SSH have no desktop: GUI apps do not show up on the screen.',
    '- Keep each ssh call short; start long jobs with Start-Process or a scheduled task and poll them.'
) }
New-Item -ItemType Directory -Force (Split-Path $claudeMd) | Out-Null
Set-Block $claudeMd $claudeMarks[0] $claudeMarks[1] $guide

# ---- what is left to do, and what works already

Write-Host "Key: $key"
Write-Host ''
$working = 0
foreach ($alias in $aliases.Keys) {
    cmd /c "ssh -o BatchMode=yes -o ConnectTimeout=8 $alias exit >nul 2>&1"
    $state = if ($LASTEXITCODE -eq 0) { 'works'; $working++ } else { 'no connection yet' }
    Write-Host ("ssh {0,-12} {1}" -f $alias, $state)
}
if (-not $working -and $linux) {
    Write-Host ''
    Write-Host "If the key is not on the PC yet, add it there in a terminal as $User (paste through ScreenSender: Ctrl+V in the browser):"
    Write-Host "    mkdir -p ~/.ssh && echo '$publicKey' >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
} elseif (-not $working) {
    Write-Host ''
    Write-Host 'If the key is not on the PC yet, add it there in its admin shell (paste through ScreenSender: Ctrl+V in the browser):'
    Write-Host "    powershell -ExecutionPolicy Bypass -File ssh\install-server.ps1 -Port $Port -PublicKey `"$publicKey`""
}
