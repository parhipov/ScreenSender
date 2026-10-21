# ScreenSender: notes for Claude

What the program does and how to use it: [README.md](README.md). This file covers running it
unattended on a PC that the user controls only through ScreenSender: the rules, install and update.

## The rule that matters most

The user may be controlling this PC right now through ScreenSender, from another city, with no other
way in. If the server stops and does not come back, they lose the PC until someone is physically there.

- Stop or restart the installed server only with `deploy\update.py` (through its scheduled task) or
  `deploy\install.ps1`. Both start it again and check that it works. Never kill python processes,
  never end the `ScreenSender` task by hand.
- Never start another copy on the installed port. To try code, copy `server.py` and `static\` to a temp
  folder with its own `config.json` on another port (e.g. 8765), and stop that copy when done.
- Ask before you lock, sign out, sleep, shut down or reboot the PC, or change network adapters,
  the firewall, power settings, or `username`, `password`, `port`, `https` in `config.json`.
- A change to `config.json` takes effect only when the server restarts. Apply it at once with
  `deploy\install.ps1` and have the user check from the phone; otherwise a mistake surfaces at the
  next unattended reboot, when nobody can fix it.
- Before an update, tell the user the picture will freeze for a few seconds and come back by itself.

## How an install is laid out

- Its own clone used only for running, e.g. `C:\ScreenSender`, not a working copy where code is
  developed: `update.py` refuses to run over local changes.
- `.venv\`: the Python environment. `config.json`: settings. `cert.pem`, `key.pem`: the HTTPS
  certificate (keep `key.pem` private). `secret.key`: mixed into login cookies; deleting it and
  restarting logs out every device. None of them is in git, so updates keep them.
- `runtime\`: `server.log` (server output and supervisor events, rotated at 5 MB), `update.log`,
  `server.pid`, and `hold` while an update swaps the code.
- Scheduled tasks for the signed-in user, all running as administrator:
  - `ScreenSender`: at sign-in, and every 5 minutes in case it died, runs `deploy\supervisor.py`.
    The supervisor starts `server.py`, starts it again 3 s after any exit, and restarts it when
    `/health` fails three times in a row (a hung server, a dead capture loop).
  - `ScreenSender update`: on demand, runs `deploy\update.py`.
  - `ScreenSender admin shell` (only if installed with `-AdminShell`): on demand, opens an elevated
    PowerShell in the install folder without a UAC prompt; the desktop shortcut "ScreenSender admin
    shell" starts it. A UAC prompt appears on the secure desktop, which cannot be seen or clicked
    remotely, so this is the user's way to get an admin shell from afar. Any program running as this
    user can start it too: a deliberate UAC bypass for a single-user PC.

## Install

Do this while the user is at the PC, in an elevated PowerShell.

1. Check `python --version` (3.10 or newer) and `git --version`. If one is missing:
   `winget install Python.Python.3.12` or `winget install Git.Git`, then open a new shell.
2. Clone over HTTPS, so updates need no SSH key:
   `git clone https://github.com/parhipov/ScreenSender.git C:\ScreenSender`, then `cd C:\ScreenSender`.
3. `python -m venv .venv`, then `.venv\Scripts\python -m pip install -r requirements.txt`.
4. `copy config.example.json config.json`. Let the user type the login and password into it
   themselves. For access from the internet set `"https": true`. `install.ps1` refuses the default password.
5. If Tailscale is to be one of the ways in, install it now, before the next step, so ScreenSender's
   certificate covers its address: see [ssh/CLAUDE.md](ssh/CLAUDE.md).
6. `powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -AdminShell` (leave out `-AdminShell`
   if the user does not want the admin shell). It must end with "The server is up". Act on every
   WARNING it prints (firewall rules that block Python).
7. Set up SSH for Claude, the way in that does not depend on ScreenSender: [ssh/CLAUDE.md](ssh/CLAUDE.md).
8. Show the user the addresses from `runtime\server.log` and let them log in from the phone.

### Before the user leaves

Go through every item with the user. Each one, if missed, can cut them off. Do what you can with
their OK; the physical items are theirs.

1. **Automatic sign-in.** The tasks start at sign-in, so after any reboot (power cut, Windows Update)
   Windows must sign in by itself. Use Sysinternals Autologon
   (https://learn.microsoft.com/sysinternals/downloads/autologon); the user types their Windows
   password into it, never into the chat. If Settings → Accounts → Sign-in options has "only allow
   Windows Hello sign-in" turned on, turn it off first, or automatic sign-in will not work.
2. **A password that stays valid.** `net user <name>` must say "Password expires Never" for a local
   account. With a Microsoft account, changing its password anywhere breaks automatic sign-in at the
   next reboot: the user must not change it during the trip.
3. **No sleep.** `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0`.
   A sleeping PC is unreachable. Turning the display off is fine.
4. **No automatic lock.** Treat the lock screen as unreachable through ScreenSender. Settings →
   Accounts → Sign-in options: "If you've been away, when should Windows require you to sign in
   again?" → Never, and Dynamic lock off. Screen saver: "On resume, display logon screen" off.
5. **UAC prompts** cannot be clicked remotely either. With the admin shell most of them can be
   avoided. If the user wants none at all for the trip, set `ConsentPromptBehaviorAdmin` to 0 under
   `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System` (elevate without asking), and
   back to 5 afterwards. It weakens protection; it is their call.
6. **BitLocker.** `manage-bde -status C:`. If it is on, an update can stop the boot at the
   recovery-key screen. Either suspend it for the trip (`Suspend-BitLocker C: -RebootCount 0`, and
   `Resume-BitLocker C:` afterwards) or accept the risk knowingly.
7. **Display.** Keep a monitor connected: capture needs an active display. Test with the monitor
   switched off at its button; if the picture breaks, use an HDMI or DisplayPort dummy plug.
8. **Power.** In the BIOS/UEFI set "Restore on AC power loss" to "Power On". Then a smart plug
   (set to come back "on" after a power cut) turns a frozen PC or an accidental shutdown into a
   two-minute fix from the phone: off, on, and the PC boots and signs in by itself.
9. **The way in from outside.** The router forwards the TCP port to this PC, and the PC keeps its
   LAN IP (a DHCP reservation in the router). If the public IP is not static, set up dynamic DNS
   (most routers have it), or the user loses the address when it changes. Some providers (CGNAT)
   give no public IP at all; the test below shows that. Tailscale on the PC and the phone avoids
   all of this: the phone then opens ScreenSender at the PC's Tailscale address, with no port
   forwarding.
10. **A second way in.** SSH from `ssh/` lets Claude on the user's laptop repair ScreenSender while
    the PC is up. For the screen itself, recommend a remote access tool that does not depend on
    ScreenSender, such as Chrome Remote Desktop: it runs as a service and works even on the lock screen. Not
    Remote Desktop (RDP): it takes over the console session, and after it disconnects ScreenSender
    sees no screen until someone signs in at the PC. If RDP was used anyway, give the session back
    from an elevated prompt with `tscon <session id> /dest:console` before disconnecting.
11. **Windows Update.** Restarts are fine once automatic sign-in works. Pausing updates for the trip
    avoids the setup screens some big updates show after the restart. On Windows 10, keep it from
    upgrading to Windows 11 while away: under `HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate`
    set `TargetReleaseVersion` = 1, `ProductVersion` = "Windows 10", `TargetReleaseVersionInfo` = "22H2".
12. **Know before it matters.** A free external monitor (UptimeRobot, healthchecks.io and similar)
    that opens the internet address every few minutes tests the whole chain (power, router, IP,
    port forwarding, server) and alerts the phone when it breaks.
13. **The password** must also be in the phone's password manager, not only in `config.json`.
14. **Final test, with the user watching:**
    - With the phone on mobile data (Wi-Fi off), open the internet address, log in from a browser
      that has never logged in before, click, type, drag.
    - Reboot (`shutdown /r /t 0`) and touch nothing. Within about two minutes the phone must reach
      the login page again and control the desktop.
    - Better still: pull the power cord (or switch the smart plug), plug it back in, and repeat.
    - From the client, `ssh\remote.ps1 hostname` through every route (`ssh <alias> hostname` for
      each alias), also after the reboot.
15. Tell the user: never lock (Win+L), sign out, sleep or shut down from the phone; each of these
    cuts the PC off. A restart is fine once the reboot test has passed.

## Update

1. See what is new: `git fetch`, then `git log --oneline HEAD..@{u}`. If nothing, stop here.
   Tell the user what changes and that the picture freezes for a few seconds (up to half a minute
   if the new version fails and is rolled back, longer while new requirements install).
2. Start it: `schtasks /Run /TN "ScreenSender update"`. The task runs `deploy\update.py` as
   administrator and independently of your shell, so it finishes even if your session is cut off.
   Run `update.py` directly only if the task is missing, and then from an elevated shell.
3. Poll `runtime\update.log` (`Get-Content runtime\update.log -Tail 5`) every few seconds until a
   `RESULT` line written after you started appears. Usually it takes under a minute; installing
   requirements can take several.
4. Act on the result:
   - `RESULT 0`: done; tell the user.
   - `RESULT 1`: refused, nothing was changed. The text says why. Local changes: show
     `git status` and `git diff` and ask the user. "not elevated" or "supervisor is not running":
     see Troubleshooting.
   - `RESULT 2`: the new version did not start or failed the check (the log line before the result
     says which), and the previous one runs again. Find the error at the end of `runtime\server.log`,
     tell the user, and do not retry the same version.
   - `RESULT 3`: the server does not work and the user cannot see you. Work on your own: read
     `runtime\server.log`, fix the cause (a broken `.venv` can be recreated as in Install), and make
     `/health` answer again. As a last resort, if the reboot test has passed: `shutdown /r /t 0`.
5. If `update.log` notes that the supervisor or `deploy\install.ps1` changed, tell the user and run
   `powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -AdminShell` from an elevated shell
   (without `-AdminShell` if it was installed without). It restarts the supervisor and the server
   and checks that the server is up.

## Troubleshooting

- **Is it up?** `Get-ScheduledTask ScreenSender` shows State Running;
  `curl.exe -sk -o NUL -w "%{http_code}" http://127.0.0.1:8005/health` prints 200 (use `https` and
  the port from `config.json`); `Get-Content runtime\server.log -Tail 30`.
- **The server keeps exiting or restarting:** the supervisor logs every start, exit and failed health
  check in `runtime\server.log`, with the server's own lines (timestamped) in between.
- **The supervisor is not running** (the task is Ready): the task starts it again within 5 minutes.
  To do it now: `Start-ScheduledTask ScreenSender`, then check that its State becomes Running.
- **"not elevated":** open the admin shell (the desktop shortcut, or
  `schtasks /Run /TN "ScreenSender admin shell"`), or use the update task.
- **`runtime\hold` exists while no update runs:** the supervisor does not start the server while it
  exists, and removes it by itself after 15 minutes or when it starts. Delete it.
- **The phone cannot connect, but `/health` answers locally:** the firewall (run `install.ps1` again and
  read its warnings), the router's port forwarding, or a changed public IP.
- **Log out every device** (a lost phone): delete `secret.key`, then run `deploy\install.ps1`.

## Development

- Code comments are in English. The pages show Russian when the browser prefers it and English
  otherwise; every interface string lives, in both languages, in the `TEXT` tables of
  `static/index.html` and `static/login.html`.
- Test server changes on a copy on another port, as described above; never restart the installed
  server to test. Real mouse and keyboard input lands on this PC's desktop, so test input code with
  the input functions stubbed out.
- `deploy\update.py`, `deploy\supervisor.py` and `deploy\install.ps1` are what keep the user
  connected. Test changes to them in a sandbox (own bare repo and clone, renamed tasks and firewall
  rules), never on the live install.
