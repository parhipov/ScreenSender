# Sets ScreenSender up to run unattended on this PC. Run from an elevated PowerShell:
#     powershell -ExecutionPolicy Bypass -File deploy\install.ps1 [-AdminShell]
# Safe to run again: everything is replaced, and a running server is restarted.
#
# Creates scheduled tasks for the current user, all running as administrator (so UAC prompts
# can be clicked and an elevated server can be restarted):
#   ScreenSender             at sign-in and every 5 minutes (if it is not running already):
#                            deploy\supervisor.py, which keeps server.py running
#   ScreenSender update      on demand: deploy\update.py (schtasks /Run /TN "ScreenSender update")
#   ScreenSender admin shell only with -AdminShell; on demand: an elevated PowerShell in this
#                            folder without a UAC prompt, also as a desktop shortcut. Any program
#                            running as this user can start it, so it is a deliberate UAC bypass.
# plus firewall rules for the port.

param([switch]$AdminShell)

$ErrorActionPreference = 'Stop'

$root = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $root '.venv\Scripts'
$user = "$env:USERDOMAIN\$env:USERNAME"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw 'Run this from an elevated PowerShell (Run as administrator).' }
if (-not (Test-Path (Join-Path $venv 'pythonw.exe'))) {
    throw "No venv in $root\.venv. Create it first: python -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt"
}
$configPath = Join-Path $root 'config.json'
if (-not (Test-Path $configPath)) { throw 'No config.json. Copy config.example.json to config.json and set the login first.' }
$config = Get-Content $configPath -Raw | ConvertFrom-Json
$port = if ($config.port) { [int]$config.port } else { 8005 }
if ($config.password -eq 'change-me' -or -not $config.password) { throw 'Set a real password in config.json first.' }

# ---- scheduled tasks

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest

function Register-Task($name, $action, $triggers, $settings) {
    $params = @{ TaskName = $name; Action = $action; Principal = $principal; Settings = $settings; Force = $true }
    if ($triggers) { $params.Trigger = $triggers }
    Register-ScheduledTask @params | Out-Null
    Write-Host "Task: $name"
}

# Priority 4 is normal; the default (7) would starve the server whenever the PC is busy.
# No time limit (the default stops a task after three days). The repeating trigger restarts the
# supervisor if it ever dies; while it runs, MultipleInstances IgnoreNew makes that a no-op.
Register-Task 'ScreenSender' `
    (New-ScheduledTaskAction -Execute (Join-Path $venv 'pythonw.exe') -Argument "`"$root\deploy\supervisor.py`"" -WorkingDirectory $root) `
    @((New-ScheduledTaskTrigger -AtLogOn -User $user),
      (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5))) `
    (New-ScheduledTaskSettingsSet -Priority 4 -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable)

Register-Task 'ScreenSender update' `
    (New-ScheduledTaskAction -Execute (Join-Path $venv 'pythonw.exe') -Argument "`"$root\deploy\update.py`"" -WorkingDirectory $root) `
    $null `
    (New-ScheduledTaskSettingsSet -Priority 4 -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew)

$shortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ScreenSender admin shell.lnk'
if ($AdminShell) {
    Register-Task 'ScreenSender admin shell' `
        (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoExit' -WorkingDirectory $root) `
        $null `
        (New-ScheduledTaskSettingsSet -Priority 4 -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -MultipleInstances Parallel)
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\schtasks.exe'
    $shortcut.Arguments = '/Run /TN "ScreenSender admin shell"'
    $shortcut.IconLocation = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') + ',0'
    $shortcut.WindowStyle = 7  # minimized: only the elevated shell should show up
    $shortcut.Save()
    Write-Host "Shortcut: $shortcutPath"
} elseif (Get-ScheduledTask -TaskName 'ScreenSender admin shell' -ErrorAction SilentlyContinue) {
    Write-Host "Kept the existing 'ScreenSender admin shell' task. To remove it:"
    Write-Host "    Unregister-ScheduledTask 'ScreenSender admin shell'; Remove-Item '$shortcutPath'"
}

# ---- firewall
# By port, so it keeps working when the venv is made again with another Python; and for the
# interpreter that actually listens (the venv's python.exe starts the base one), so Windows does
# not ask about it: a "Cancel" there leaves block rules behind.

$basePython = & (Join-Path $venv 'python.exe') -c 'import sys; print(sys._base_executable)'
Get-NetFirewallRule -DisplayName 'ScreenSender', 'ScreenSender (Python)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'ScreenSender' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port `
    -Profile Any | Out-Null
New-NetFirewallRule -DisplayName 'ScreenSender (Python)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port `
    -Program $basePython -Profile Any | Out-Null
Write-Host "Firewall: TCP $port allowed (and for $basePython)"

# A block rule beats any allow rule.
$blocks = Get-NetFirewallApplicationFilter | Where-Object { $_.Program -eq $basePython } | Get-NetFirewallRule |
    Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' -and $_.Enabled -eq 'True' }
foreach ($rule in $blocks) {
    Write-Warning "Firewall rule '$($rule.DisplayName)' blocks Python and will block ScreenSender. Remove it: Remove-NetFirewallRule -Name '$($rule.Name)'"
}

# ---- (re)start the supervisor
# From here on nothing may stop the script between stopping the server and starting it again.

$ErrorActionPreference = 'Continue'
function Get-TaskState { (Get-ScheduledTask -TaskName 'ScreenSender').State }
$pidFile = Join-Path $root 'runtime\server.pid'
Stop-ScheduledTask -TaskName 'ScreenSender'
for ($i = 0; $i -lt 20 -and (Get-TaskState) -eq 'Running'; $i++) { Start-Sleep -Milliseconds 500 }
if (Test-Path $pidFile) {
    # The server is the supervisor's child; make sure it does not outlive it. Only if the pid
    # still is the venv's python: pids get reused by other programs.
    $server = Get-Process -Id ([int](Get-Content $pidFile -Raw).Trim()) -ErrorAction SilentlyContinue
    if ($server -and $server.Path -eq (Join-Path $venv 'python.exe')) {
        cmd /c "taskkill /PID $($server.Id) /T /F >nul 2>&1"
    }
}
# A start while the task is still stopping is dropped without an error (MultipleInstances
# IgnoreNew), so check that it runs and try again.
for ($i = 0; $i -lt 10 -and (Get-TaskState) -ne 'Running'; $i++) {
    Start-ScheduledTask -TaskName 'ScreenSender'
    Start-Sleep -Seconds 2
}

$scheme = if ($config.https) { 'https' } else { 'http' }
$url = "${scheme}://127.0.0.1:$port/health"
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    $code = & curl.exe -sk -o NUL -w '%{http_code}' $url
    if ($code -eq '200') { Write-Host "The server is up: $url"; exit 0 }
}
Write-Warning "The server does not answer at $url yet; see $root\runtime\server.log"
exit 1
