# SSH access

**English** | [Русский](README.ru.md)

ScreenSender shows you the screen. This folder adds a second way into the same Windows PC: an SSH
server, so that Claude Code (or you) on another computer can run commands there, copy files, and fix
ScreenSender itself if it ever stops. It is separate from ScreenSender and can be used without it.

## What is here

- `install-server.ps1`: run on the PC as administrator. Installs the Windows OpenSSH server, starts it
  with Windows, allows key logins only, makes PowerShell the shell, opens the port in the firewall,
  and adds or removes client keys.
- `setup-client.ps1`: run on the computer you connect from. Makes a key, adds short names for the PC
  to `~/.ssh/config`, and tells Claude Code there how to work with the PC.
- `remote.ps1`: runs PowerShell code on the PC through the first route that works, without quote
  escaping and with readable output.

## Routes

You can reach the PC three ways, and set up as many as you like:

| Name | Route | Needs |
|---|---|---|
| `home-ts` | [Tailscale](https://tailscale.com), a private network between your devices | Tailscale on both computers |
| `home` | your public IP or a dynamic DNS name | a port forwarded on the router |
| `home-lan` | the same local network | nothing |

Tailscale needs no open ports and survives a changing IP, but some networks block VPN traffic; the
public route then still works. Each route is independent of the others.

## Set up

On the PC, in PowerShell run as administrator, in the repository folder:

```
powershell -ExecutionPolicy Bypass -File ssh\install-server.ps1
```

On your computer, in the repository folder (no admin needed); give the routes you have:

```
powershell -ExecutionPolicy Bypass -File ssh\setup-client.ps1 -User <your Windows account on the PC> -LanHost 192.168.1.20 -TailscaleHost home-pc -PublicHost my.ddns.net -PublicPort 22022
```

It prints a line with `-PublicKey "ssh-ed25519 ..."`: run that line on the PC as administrator. If you
are not at the PC, paste it through ScreenSender (copy it, then Ctrl+V in the ScreenSender page).
Run `setup-client.ps1` again to see which routes work.

## Use

```
ssh home-lan                                   # an interactive PowerShell on the PC
& ssh\remote.ps1 'Get-Service sshd'            # one command, best route
Get-Content job.ps1 -Raw | & ssh\remote.ps1    # a script
scp report.pdf home-lan:Desktop/               # files
```

Commands run as administrator and without a desktop: programs started over SSH do not show up on the
screen.

## Security

- Only key logins; passwords are switched off.
- Whoever has the key on your computer has administrator access to the PC. If that computer is lost,
  remove its key on the PC: `ssh\install-server.ps1 -RemoveKey claude@<that computer>`.
- To forget the PC on your computer: `ssh\setup-client.ps1 -Name home -Remove`.
- Claude Code asks before every command it runs; keep it that way for commands on the PC.

For Claude: [CLAUDE.md](CLAUDE.md) has the details, including Tailscale setup and troubleshooting.
