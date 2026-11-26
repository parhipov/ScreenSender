#!/usr/bin/env bash
# Sets ScreenSender up to run unattended on this Linux PC (Raspberry Pi OS and other Debian-like
# systems). Run it as the user whose screen is to be shown, not as root; it uses sudo itself:
#     bash deploy/install.sh
# Safe to run again: everything is replaced, and a running server is restarted.
#
#   packages  python3-venv; grim, wtype, wl-clipboard, wlr-randr (Wayland); xdotool, xclip (X11)
#   .venv     made if missing, requirements installed into it
#   uinput    loaded at boot, /dev/uinput owned by this user: the virtual mouse and keyboard.
#             Any program of this user can then fake input, as on a desktop it can anyway.
#   video     this user in the group video, for /dev/fb0 (the picture without a desktop)
#   systemd   user services, with "linger" so they run from boot, with or without a sign-in:
#               screensender.service         deploy/supervisor.py, which keeps server.py running;
#                                            systemd starts it again if it ever exits
#               screensender-update.service  on demand: deploy/update.py
#                                            (systemctl --user start screensender-update)
#   firewall  the port allowed in ufw, if ufw is active
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
user="$(id -un)"
uid="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$uid}"

if [ "$uid" = 0 ]; then
    echo "Run this as the user whose screen is to be shown, not as root (it uses sudo itself)." >&2
    exit 1
fi
if [ ! -f config.json ]; then
    echo "No config.json. Copy config.example.json to config.json and set the login first." >&2
    exit 1
fi
read -r port https password_ok < <(python3 -c '
import json
c = json.load(open("config.json", encoding="utf-8-sig"))
print(int(c.get("port", 8005)), "https" if c.get("https") else "http", int(c.get("password") not in (None, "", "change-me")))')
if [ "$password_ok" != 1 ]; then
    echo "Set a real password in config.json first." >&2
    exit 1
fi

echo "== packages"
sudo apt-get update -qq || echo "WARNING: apt-get update failed; using the package lists there are"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip git curl >/dev/null
for pkg in grim wtype wl-clipboard wlr-randr xdotool xclip; do
    # One by one: an older release may lack one of them, and the rest is still worth having.
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$pkg" >/dev/null \
        || echo "WARNING: could not install $pkg"
done

echo "== .venv"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

echo "== uinput and the framebuffer"
echo uinput | sudo tee /etc/modules-load.d/screensender-uinput.conf >/dev/null
# The owner, not a group: it applies at once, while a new group needs a new sign-in.
echo "KERNEL==\"uinput\", SUBSYSTEM==\"misc\", OWNER=\"$user\", MODE=\"0600\", OPTIONS+=\"static_node=uinput\"" \
    | sudo tee /etc/udev/rules.d/60-screensender-uinput.rules >/dev/null
sudo modprobe uinput || echo "WARNING: could not load the uinput module: no mouse and keyboard input"
sudo udevadm control --reload-rules || true
if [ -e /dev/uinput ]; then
    sudo chown "$user" /dev/uinput && sudo chmod 0600 /dev/uinput
fi
if ! id -nG "$user" | tr ' ' '\n' | grep -qx video; then
    sudo usermod -aG video "$user"
    echo "NOTE: $user was added to the group video; the framebuffer picture works after the next reboot"
fi

echo "== systemd user services"
sudo loginctl enable-linger "$user"
for _ in $(seq 20); do [ -S "$XDG_RUNTIME_DIR/systemd/private" ] && break; sleep 0.5; done
units="$HOME/.config/systemd/user"
mkdir -p "$units"
cat > "$units/screensender.service" <<EOF
[Unit]
Description=ScreenSender: deploy/supervisor.py keeps server.py running

[Service]
WorkingDirectory=$root
ExecStart=$root/.venv/bin/python $root/deploy/supervisor.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
cat > "$units/screensender-update.service" <<EOF
[Unit]
Description=ScreenSender update: deploy/update.py, see runtime/update.log

[Service]
Type=oneshot
WorkingDirectory=$root
ExecStart=$root/.venv/bin/python $root/deploy/update.py
TimeoutStartSec=30min
EOF
systemctl --user daemon-reload
systemctl --user enable screensender.service >/dev/null
# Restarting takes the server down with the supervisor (one cgroup) and starts both again.
systemctl --user restart screensender.service
echo "Services: screensender, screensender-update"

if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
    sudo ufw allow "$port/tcp" >/dev/null
    echo "Firewall (ufw): TCP $port allowed"
fi

url="$https://127.0.0.1:$port/health"
for _ in $(seq 30); do
    sleep 1
    if [ "$(curl -sk -o /dev/null -w '%{http_code}' "$url")" = 200 ]; then
        echo "The server is up: $url"
        exit 0
    fi
done
echo "WARNING: the server does not answer at $url yet; see $root/runtime/server.log" >&2
exit 1
