# Sets up the OpenSSH server on this PC, so Claude Code on another computer can work here over SSH.
# Run from an elevated PowerShell:
#     powershell -ExecutionPolicy Bypass -File ssh\install-server.ps1 [-Port 22] [-PublicKey "ssh-ed25519 AAAA... name"]
#     powershell -ExecutionPolicy Bypass -File ssh\install-server.ps1 -RemoveKey name
# Safe to run again. Installs the Windows OpenSSH server, starts it with Windows (and again if it
# fails), allows key logins only, makes PowerShell the shell, opens the port in the firewall, and adds
# or removes client keys (a key is named by the comment at its end). A key login of this user runs as
# administrator, without a UAC prompt. A new key replaces an earlier one of the same name.
# Over SSH itself run it only while ScreenSender works: it restarts the SSH server.

param(
    [int]$Port = 22,
    [string]$PublicKey,
    [string]$RemoveKey
)

$ErrorActionPreference = 'Stop'

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw 'Run this from an elevated PowerShell (Run as administrator).' }

$sshDir = Join-Path $env:ProgramData 'ssh'
$configPath = Join-Path $sshDir 'sshd_config'
$utf8 = New-Object System.Text.UTF8Encoding($false)  # sshd reads no BOM

# ---- install
# Microsoft's Win32-OpenSSH MSI that ships in ssh\openssh (it installs into Program Files and needs no
# internet). Without it (an ARM PC): the Windows feature, which downloads from Windows Update and can
# be blocked or time out (0x80072EE2), then the MSI from github.com/PowerShell/Win32-OpenSSH.

function Find-OpenSsh {
    foreach ($dir in (Join-Path $env:SystemRoot 'System32\OpenSSH'), (Join-Path $env:ProgramFiles 'OpenSSH')) {
        if (Test-Path (Join-Path $dir 'sshd.exe')) { return $dir }
    }
}

function Install-Msi($msi) {
    $signature = Get-AuthenticodeSignature $msi
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch '^CN=Microsoft Corporation,') {
        throw "$msi is not signed by Microsoft ($($signature.Status)); not installing it."
    }
    Write-Host "Installing $msi ..."
    $run = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn /norestart" -Wait -PassThru
    if ($run.ExitCode -notin 0, 3010) { throw "The OpenSSH installer failed with code $($run.ExitCode)." }
}

function Install-MsiFromGitHub($arch) {
    $url = $null
    try {
        $release = curl.exe -s --max-time 20 https://api.github.com/repos/PowerShell/Win32-OpenSSH/releases/latest | Out-String | ConvertFrom-Json
        $url = @($release.assets | Where-Object { $_.name -like "OpenSSH-$arch-*.msi" })[0].browser_download_url
    } catch { }
    if (-not $url) { $url = "https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.4.0.0p1-Preview/OpenSSH-$arch-v10.4.0.0.msi" }
    $msi = Join-Path ([IO.Path]::GetTempPath()) ($url -split '/')[-1]
    Write-Host "Downloading $url ..."
    curl.exe -fsSL --retry 3 --max-time 600 -o $msi $url
    if ($LASTEXITCODE -ne 0) {
        throw "Could not download OpenSSH (curl exit $LASTEXITCODE). Download $url by hand, put it into $PSScriptRoot\openssh and run this again."
    }
    Install-Msi $msi
}

if (-not (Find-OpenSsh)) {
    Write-Host 'Installing the OpenSSH server...'
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'Arm64' } else { 'Win64' }
    $bundled = Get-ChildItem (Join-Path $PSScriptRoot 'openssh') -Filter "OpenSSH-$arch-*.msi" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty FullName
    if ($bundled) {
        Install-Msi $bundled
    } else {
        try {
            Write-Host 'From Windows Update (this can take a few minutes)...'
            $capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
            Add-WindowsCapability -Online -Name $capability.Name | Out-Null
        } catch {
            Write-Host "Windows Update did not deliver it ($($_.Exception.Message.Trim())); trying GitHub."
            Install-MsiFromGitHub $arch
        }
    }
}
$openssh = Find-OpenSsh
if (-not $openssh -or -not (Get-Service sshd -ErrorAction SilentlyContinue)) { throw 'The OpenSSH server did not install.' }
$sshd = Join-Path $openssh 'sshd.exe'
if (-not (Test-Path $configPath)) {
    # The first start makes the host keys and the default config.
    Start-Service sshd
    for ($i = 0; $i -lt 20 -and -not (Test-Path $configPath); $i++) { Start-Sleep -Milliseconds 500 }
}

# ---- config: a block at the top wins, as sshd uses the first value it finds for each keyword

$begin, $end = '# >>> ssh\install-server.ps1 >>>', '# <<< ssh\install-server.ps1 <<<'
$lines = [IO.File]::ReadAllLines($configPath)
$kept = New-Object System.Collections.Generic.List[string]
$inBlock = $false
foreach ($line in $lines) {
    if ($line -eq $begin) { $inBlock = $true; continue }
    if ($line -eq $end) { $inBlock = $false; continue }
    if (-not $inBlock) { $kept.Add($line) }
}
$block = @(
    $begin,
    "Port $Port",
    'PasswordAuthentication no',
    'ChallengeResponseAuthentication no',
    'PubkeyAuthentication yes',
    $end
)
$backup = "$configPath.bak"
Copy-Item $configPath $backup -Force
[IO.File]::WriteAllLines($configPath, [string[]]($block + $kept), $utf8)
& $sshd -t
if ($LASTEXITCODE -ne 0) {
    Copy-Item $backup $configPath -Force
    throw 'sshd rejected the new config; the old one is back.'
}

$regPath = 'HKLM:\SOFTWARE\OpenSSH'
if (-not (Test-Path $regPath)) { New-Item -Path $regPath | Out-Null }
New-ItemProperty -Path $regPath -Name DefaultShell -PropertyType String -Force `
    -Value (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') | Out-Null

# ---- client keys
# An administrator's keys are read from administrators_authorized_keys only (not ~/.ssh), and sshd
# wants that file readable by Administrators and SYSTEM alone. A key is named by its comment.

$keysFile = Join-Path $sshDir 'administrators_authorized_keys'
if ($PublicKey) {
    $PublicKey = $PublicKey.Trim()
    if ($PublicKey -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+|sk-\S+) [A-Za-z0-9+/=]+( .+)?$') {
        throw 'That is not an SSH public key: give the one line from the .pub file.'
    }
}
if ($PublicKey -or $RemoveKey -or -not (Test-Path $keysFile)) {
    $keys = @()
    if (Test-Path $keysFile) { $keys = @([IO.File]::ReadAllLines($keysFile) | Where-Object { $_.Trim() }) }
    if ($RemoveKey) { $keys = @($keys | Where-Object { ($_.Trim() -split '\s+')[-1] -ne $RemoveKey }) }
    if ($PublicKey) {
        # A new key replaces the one of the same name, as when a client makes its key again.
        $newParts = $PublicKey -split '\s+'
        $keys = @($keys | Where-Object {
            $parts = $_.Trim() -split '\s+'
            $parts[1] -ne $newParts[1] -and -not ($newParts.Count -gt 2 -and $parts[-1] -eq $newParts[-1])
        })
        $keys += $PublicKey
    }
    [IO.File]::WriteAllLines($keysFile, [string[]]$keys, $utf8)
    icacls $keysFile /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not set the permissions of $keysFile" }
}

# ---- service and firewall

Set-Service sshd -StartupType Automatic
sc.exe failure sshd reset= 86400 actions= restart/5000/restart/5000/restart/60000 | Out-Null
Get-NetFirewallRule -DisplayName 'ScreenSender SSH' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'ScreenSender SSH' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
    -Profile Any | Out-Null
Restart-Service sshd

$up = $false
for ($i = 0; $i -lt 20 -and -not $up; $i++) {
    Start-Sleep -Milliseconds 500
    try { (New-Object Net.Sockets.TcpClient('127.0.0.1', $Port)).Close(); $up = $true } catch { }
}
if (-not $up) { throw "The SSH server does not accept connections on port $Port." }

# ---- what a client needs

$names = @()
$adminKeys = Join-Path $sshDir 'administrators_authorized_keys'
if (Test-Path $adminKeys) { $names = @([IO.File]::ReadAllLines($adminKeys) | Where-Object { $_.Trim() } | ForEach-Object { ($_.Trim() -split '\s+')[-1] }) }
# The interfaces with the default route: a VPN that takes all traffic uses 0.0.0.0/1 routes instead.
$lan = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    ForEach-Object { (Get-NetIPAddress -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress } |
    Select-Object -Unique)
if (-not $lan) { $lan = @('<this PC>') }
Write-Host "SSH server is up on port $Port; key logins only."
Write-Host "  User:    $env:USERNAME"
foreach ($address in $lan) { Write-Host "  LAN:     ssh -p $Port $env:USERNAME@$address" }
Write-Host "  Keys:    $(if ($names) { $names -join ', ' } else { 'none yet: add one with -PublicKey' })"
$keygen = @((Join-Path $openssh 'ssh-keygen.exe'), (Join-Path $env:SystemRoot 'System32\OpenSSH\ssh-keygen.exe'),
    (Join-Path $env:ProgramFiles 'OpenSSH\ssh-keygen.exe')) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($keygen) { Write-Host "  Host key: $(& $keygen -lf (Join-Path $sshDir 'ssh_host_ed25519_key.pub'))" }

# sshd starts every command in a PowerShell that loads the user's profile; one the execution policy
# blocks puts an error in front of every command's output.
$policy = Get-ExecutionPolicy -Scope CurrentUser
if ($policy -eq 'Undefined') { $policy = Get-ExecutionPolicy -Scope LocalMachine }
if ($policy -eq 'Undefined') { $policy = 'Restricted' }
if ($policy -in 'Restricted', 'AllSigned' -and ($PROFILE.CurrentUserAllHosts, $PROFILE.CurrentUserCurrentHost | Where-Object { Test-Path $_ })) {
    Write-Warning "The PowerShell profile of $env:USERNAME cannot run (execution policy $policy), so every SSH command starts with an error about it. ssh\remote.ps1 output is clean anyway; to fix it for plain ssh too: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
}
