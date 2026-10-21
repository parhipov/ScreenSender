# SSH access: notes for Claude

The `ssh/` folder gives Claude Code on another computer a shell on a ScreenSender PC. It stands apart
from ScreenSender itself: the screen is for the user, SSH is for Claude, and each is a way in when the
other one is down. The rules in the root [CLAUDE.md](../CLAUDE.md) hold on that PC over SSH as well:
never kill ScreenSender, update it only through its task, ask before reboots and network changes.

## The pieces

| Script | Runs on | What it does |
|---|---|---|
| `install-server.ps1` | the PC, elevated | OpenSSH server started with Windows, key logins only, PowerShell as the shell, firewall rule; adds (`-PublicKey`) and removes (`-RemoveKey`) client keys |
| `setup-client.ps1` | the client, no admin | an SSH key, aliases in `~/.ssh/config`, notes for Claude in `~/.claude/CLAUDE.md`; `-Remove` forgets the PC |
| `remote.ps1` | the client | runs PowerShell code on the PC through the first route that works; no quote escaping, UTF-8 output, the remote exit code |

## Routes

Several independent routes, so one blocked network or one broken router does not cut the PC off:

| Alias | Route | Needs |
|---|---|---|
| `<name>-ts` | Tailscale | Tailscale on the PC and the client, signed in to the same account |
| `<name>` | public address, through the router | a forwarded port and a static IP or dynamic DNS |
| `<name>-lan` | the same network | nothing |

Tailscale is a WireGuard VPN, and some networks block VPN traffic; it then falls back to relays over
HTTPS, but its servers can be blocked too. The public route works without any third party. Set up
both when the PC is to be left alone.

## Set up the PC

Do this while the user is at the PC, in an elevated PowerShell in the install folder.

1. **Tailscale** (recommended). Install it with `winget install Tailscale.Tailscale`, or with the
   installer from https://pkgs.tailscale.com/stable/ (`tailscale-setup-latest.exe`) where winget is
   missing. The user signs in. Then `tailscale up --unattended`, so it keeps running while nobody is
   signed in to Windows. In the admin console (https://login.tailscale.com/admin/machines) the user
   turns on "Disable key expiry" for this PC, or it drops out of the network after about six months.
   Install Tailscale before running `deploy\install.ps1`, so ScreenSender's certificate includes the
   Tailscale address (the server prints it at start).
2. **SSH server:** `powershell -ExecutionPolicy Bypass -File ssh\install-server.ps1 [-Port 22]`. It
   prints the user name to log in with, a LAN example and the host key. Act on its warnings.
3. **Router** (for the public route): first find the public address and check that it takes
   incoming connections at all, as in "The public address" in the root [CLAUDE.md](../CLAUDE.md);
   behind a provider's CGNAT no forwarding helps. Then forward an external port to the PC's SSH
   port. A port other than 22 outside keeps most scanners away; logins need a key anyway.

## Set up a client

1. In a clone of this repository on the client:
   `powershell -ExecutionPolicy Bypass -File ssh\setup-client.ps1 -User <account on the PC> [-Name home] [-LanHost <ip>] [-TailscaleHost <name or 100.x ip>] [-PublicHost <ip or DNS name>] [-PublicPort <port>]`
   with only the routes that exist.
2. It prints an `install-server.ps1 -PublicKey "..."` line. Run that on the PC in an elevated shell.
   From afar the way there is ScreenSender: copy the line on the client, open the PC's admin shell in
   the ScreenSender page, and Ctrl+V in the page pastes it into the PC.
3. Run `setup-client.ps1` again with the same arguments: it lists which aliases work.

On macOS or Linux do the same by hand: `ssh-keygen -t ed25519 -f ~/.ssh/screensender_home`, `Host`
blocks in `~/.ssh/config` like the ones the script writes, and plain `ssh home '<PowerShell code>'`.

## Working there

- `& ssh\remote.ps1 -Name home '<code>'`, or pipe a longer script into it. Plain `ssh home '<code>'`
  works too; start the code with `[Console]::OutputEncoding = [Text.Encoding]::UTF8;` to keep
  non-English text readable. Copy files with `scp`.
- Commands run as administrator, without a desktop: GUI programs started over SSH do not appear on
  the screen. To show one there, start it in the signed-in user's session through a one-off task:

  ```powershell
  $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name  # not $env:USERDOMAIN: over SSH it is WORKGROUP
  $task = "Claude open $(Get-Random)"
  Register-ScheduledTask $task -Action (New-ScheduledTaskAction -Execute 'notepad.exe') `
      -Principal (New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive) | Out-Null
  Start-ScheduledTask $task; Start-Sleep 3; Unregister-ScheduledTask $task -Confirm:$false
  ```

  On Windows 11 Calculator, Paint and other inbox apps come from the Store and may have been removed
  (`Get-AppxPackage -AllUsers -Name Microsoft.Paint`); `notepad.exe` is always there.
- Keep each call short; start long jobs with `Start-Process` or a scheduled task and poll them.
- `install-server.ps1` restarts the SSH server. Over SSH, run it only while ScreenSender works.

## Keys

- A key is named by the comment at its end: `claude@<client computer>`.
- The client's key has no passphrase, so Claude can use it unattended; whoever can read the client
  user's files can use it. If a client is lost, remove its key on the PC (through ScreenSender if SSH
  is the lost part): `ssh\install-server.ps1 -RemoveKey claude@<client computer>`.

## Troubleshooting

- **Permission denied (publickey):** the key is not on the PC yet (run the `-PublicKey` line), `-User`
  is wrong, or the client's private key is readable by others. `setup-client.ps1` fixes the key's
  permissions when run again.
- **Connection timed out or refused:** that route is down. Check the firewall rule "ScreenSender SSH"
  and `Get-Service sshd` on the PC, the router's forwarding, or `tailscale status`.
- **kex_exchange_identification: Connection closed by remote host:** something on the way accepts the
  connection and drops it (a firewall or proxy in that network), or nothing listens there.
- **Every plain ssh command starts with an error about a profile:** the PC user's PowerShell profile
  cannot run under the execution policy. `remote.ps1` hides it; `Set-ExecutionPolicy -Scope
  CurrentUser RemoteSigned` on the PC fixes it, which is the user's call.
