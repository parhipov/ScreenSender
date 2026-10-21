# Runs PowerShell code on the PC that ssh\setup-client.ps1 set up, and prints what it prints.
#     ssh\remote.ps1 'Get-Service sshd'                   # the code as an argument
#     Get-Content job.ps1 -Raw | ssh\remote.ps1           # or on stdin
#     ssh\remote.ps1 -Name home 'Get-Date'
# It tries the aliases from setup-client.ps1 (<Name>-ts, <Name>, <Name>-lan), starting with the one
# that worked last time. The code travels base64-encoded, so quotes need no escaping, and runs in a
# PowerShell without a profile; the output comes back as UTF-8, so non-English text stays readable.
# The exit code is the remote one; 255 means no route got through.

param(
    [Parameter(Position = 0)][string]$Code,
    [string]$Name = 'home',
    # Lines piped in (powershell -File passes stdin this way); read through $input below.
    [Parameter(ValueFromPipeline = $true)][string]$Line
)

$ErrorActionPreference = 'Stop'

if (-not $Code) { $Code = if ($MyInvocation.ExpectingInput) { @($input) -join "`n" } else { [Console]::In.ReadToEnd() } }
if (-not $Code.Trim()) { throw 'Give the code to run, as an argument or on stdin.' }

$wrapped = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; `$ProgressPreference = 'SilentlyContinue'`n$Code"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($wrapped))
if ($encoded.Length -gt 24000) { throw 'The code is too long for one command: copy it as a script with scp and run that.' }
# sshd runs this in a PowerShell of its own, which does not pass the inner exit code on by itself.
$remote = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " +
          "powershell -NoProfile -NonInteractive -EncodedCommand $encoded; exit `$LASTEXITCODE"
# That outer PowerShell loads the user's profile; one the execution policy blocks prints this error first.
$profileNoise = '(?ms)^\. : .*?WindowsPowerShell.*?FullyQualifiedErrorId : UnauthorizedAccess\s*'

$profileDir = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
$config = Join-Path $profileDir '.ssh\config'
$known = if (Test-Path $config) { [IO.File]::ReadAllLines($config) } else { @() }
$aliases = @("$Name-ts", $Name, "$Name-lan") | Where-Object { $known -contains "Host $_" }
if (-not $aliases) { throw "No ssh aliases for '$Name' in $config; run ssh\setup-client.ps1 first." }
$cache = Join-Path ([IO.Path]::GetTempPath()) "screensender-$Name.alias"
$last = if (Test-Path $cache) { (Get-Content $cache -Raw).Trim() } else { '' }
if ($aliases -contains $last) { $aliases = @($last) + @($aliases | Where-Object { $_ -ne $last }) }

foreach ($alias in $aliases) {
    $psi = New-Object Diagnostics.ProcessStartInfo 'ssh', "-o BatchMode=yes -o ConnectTimeout=6 $alias `"$remote`""
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
    $ssh = [Diagnostics.Process]::Start($psi)
    $errors = $ssh.StandardError.ReadToEndAsync()
    $output = $ssh.StandardOutput.ReadToEnd()
    $ssh.WaitForExit()
    # 255 is ssh's own failure. Only one before the command started makes the next route worth
    # trying; after a connection lost midway the command may have run, and must not run twice.
    $failures = 'connect to host|timed out|Could not resolve|No route|unreachable|refused|Permission denied|' +
                'kex_exchange_identification|Host key verification failed'
    if ($ssh.ExitCode -eq 255 -and $errors.Result -match $failures) {
        [Console]::Error.WriteLine("ssh ${alias}: $($errors.Result.Trim())")
        continue
    }
    Set-Content $cache $alias
    $previous = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [Text.Encoding]::UTF8
        [Console]::Out.Write($output)
        $errorText = $errors.Result -replace $profileNoise, ''
        if ($errorText) { [Console]::Error.Write($errorText) }
    } finally {
        [Console]::OutputEncoding = $previous
    }
    exit $ssh.ExitCode
}
exit 255
