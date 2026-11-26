#!/usr/bin/env python3
"""Gives Claude Code on another computer SSH access to this Linux PC (Raspberry Pi OS and other
Debian-like systems) in one step, and writes everything that computer needs into one file.
Started by give-claude-access.sh in the repository root, as the user Claude is to log in as:
    bash give-claude-access.sh [--port 22] [--out <file>]
The Linux counterpart of give-access.ps1. It makes sure the OpenSSH server runs and starts at boot,
makes a key for Claude and adds it to this user's ~/.ssh/authorized_keys (replacing an earlier key of
the same name), and writes claude-access-<this PC>.json in the same format, with "shell": "bash".
On the other computer, ssh\\import-access.ps1 reads that file. The file gives this user's access,
sudo included: carry it over privately and delete it afterwards. Safe to run again.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run(*cmd, check=True, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise SystemExit(f"{' '.join(cmd)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def ip_json(*args):
    try:
        return json.loads(run("ip", "-j", *args) or "[]")
    except (SystemExit, ValueError, OSError):
        return []


# ---------------------------------------------------------------- the SSH server

def setup_server(port):
    if not shutil.which("sshd") and not Path("/usr/sbin/sshd").exists():
        print("Installing the OpenSSH server...")
        run("sudo", "apt-get", "install", "-y", "-qq", "openssh-server")
    if port != 22:
        # Port 22 stays as well: a typo in the new port must not cut anyone off.
        conf = f"# Written by ScreenSender's ssh/give-access.py\nPort 22\nPort {port}\n"
        subprocess.run(["sudo", "tee", "/etc/ssh/sshd_config.d/screensender.conf"], input=conf, text=True,
                       capture_output=True, check=True)
        run("sudo", "sshd", "-t")
    unit = "ssh" if run("systemctl", "list-unit-files", "ssh.service", check=False).count("ssh.service") else "sshd"
    run("sudo", "systemctl", "enable", unit)
    run("sudo", "systemctl", "restart" if port != 22 else "start", unit)
    if shutil.which("ufw") and "Status: active" in run("sudo", "ufw", "status", check=False):
        run("sudo", "ufw", "allow", f"{port}/tcp")
        print(f"Firewall (ufw): TCP {port} allowed")
    effective = run("sudo", "sshd", "-T", check=False)
    return re.search(r"^passwordauthentication yes$", effective, re.M) is not None


def make_key(name):
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / "key"
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key))
        return key.read_text().strip() + "\n", Path(f"{key}.pub").read_text().strip()


def authorize(public_key, name):
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    ssh_dir.chmod(0o700)
    auth = ssh_dir / "authorized_keys"
    lines = auth.read_text().splitlines() if auth.exists() else []
    kept = [line for line in lines if not line.rstrip().endswith(" " + name)]
    auth.write_text("\n".join(kept + [public_key]) + "\n")
    auth.chmod(0o600)


def host_keys():
    keys = []
    for kind in ("ed25519", "ecdsa", "rsa"):
        try:
            parts = Path(f"/etc/ssh/ssh_host_{kind}_key.pub").read_text().split()
            keys.append(f"{parts[0]} {parts[1]}")
        except (OSError, IndexError):
            pass
    return keys


# ---------------------------------------------------------------- addresses

def interfaces_and_lan():
    routes = ip_json("route", "show", "default") + ip_json("-6", "route", "show", "default")
    interfaces = []
    for link in ip_json("addr"):
        name = link.get("ifname")
        if name == "lo":
            continue
        addrs = link.get("addr_info") or []
        interfaces.append({
            "name": name,
            "mac": link.get("address"),
            "status": "Up" if link.get("operstate") in ("UP", "UNKNOWN") and "UP" in (link.get("flags") or []) else "Down",
            "ipv4": [f"{a['local']}/{a['prefixlen']}" for a in addrs if a.get("family") == "inet"],
            "ipv6": [f"{a['local']}/{a['prefixlen']}" for a in addrs if a.get("family") == "inet6"],
            "gateways": sorted({r["gateway"] for r in routes if r.get("dev") == name and r.get("gateway")}),
            "default_route": any(r.get("dev") == name for r in routes),
        })
    lan = []
    v4routes = sorted((r for r in ip_json("route", "show", "default") if r.get("gateway")),
                      key=lambda r: r.get("metric", 0))
    for r in v4routes:
        for link in ip_json("-4", "addr", "show", "dev", r["dev"]):
            for a in link.get("addr_info") or []:
                if a.get("family") == "inet" and not a["local"].startswith("169.254."):
                    lan.append({"address": a["local"], "prefix": a["prefixlen"], "interface": r["dev"],
                                "gateway": r["gateway"]})
    # A VPN that takes all traffic adds 0.0.0.0/1 and 128.0.0.0/1; the public address seen is then its exit.
    full_vpn = bool(ip_json("route", "show", "0.0.0.0/1"))
    return interfaces, lan, full_vpn


def tailscale():
    if not shutil.which("tailscale"):
        return None
    try:
        status = json.loads(run("tailscale", "status", "--json", timeout=10))
    except (SystemExit, ValueError, OSError, subprocess.TimeoutExpired):
        return None
    me = status.get("Self") or {}
    ips = me.get("TailscaleIPs") or []
    running = status.get("BackendState") == "Running"
    return {"state": status.get("BackendState"),
            "address": next((ip for ip in ips if ":" not in ip), None) if running else None,
            "addresses": ips, "dns_name": (me.get("DNSName") or "").rstrip(".")}


def trace_ip(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            for line in r.read().decode().splitlines():
                if line.startswith("ip="):
                    return line[3:]
    except Exception:  # noqa: BLE001 - no internet, or no IPv6
        return None
    return None


def router_wan_ip(local_address):
    """UPnP GetExternalIPAddress: the router's own WAN address (private or 100.64/10: the provider's CGNAT)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((local_address, 0))
            s.settimeout(2.5)
            s.sendto(b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\n'
                     b"ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n", ("239.255.255.250", 1900))
            locations = set()
            while True:
                try:
                    reply = s.recv(4096).decode(errors="replace")
                except OSError:
                    break
                m = re.search(r"(?im)^LOCATION:\s*(\S+)", reply)
                if m:
                    locations.add(m.group(1))
        for location in locations:
            with urllib.request.urlopen(location, timeout=5) as r:
                xml = r.read().decode(errors="replace")
            for service in re.findall(r"<service>(.*?)</service>", xml, re.S):
                kind = re.search(r"<serviceType>([^<]*WAN(?:IP|PPP)Connection[^<]*)</serviceType>", service)
                control = re.search(r"<controlURL>([^<]+)</controlURL>", service)
                if not (kind and control):
                    continue
                body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                        f'<u:GetExternalIPAddress xmlns:u="{kind.group(1)}"/></s:Body></s:Envelope>')
                req = urllib.request.Request(urllib.parse.urljoin(location, control.group(1)), body.encode(), {
                    "Content-Type": 'text/xml; charset="utf-8"',
                    "SOAPAction": f'"{kind.group(1)}#GetExternalIPAddress"'})
                with urllib.request.urlopen(req, timeout=5) as r:
                    m = re.search(r"<NewExternalIPAddress>([^<]+)<", r.read().decode(errors="replace"))
                    if m:
                        return m.group(1)
    except Exception:  # noqa: BLE001 - no UPnP on this router
        return None
    return None


def private_ip(ip):
    return bool(re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)", ip or ""))


def os_name():
    name = "Linux"
    try:
        name = re.search(r'^PRETTY_NAME="?([^"\n]*)', Path("/etc/os-release").read_text(), re.M).group(1)
    except (OSError, AttributeError):
        pass
    try:
        name += ", " + Path("/proc/device-tree/model").read_text().rstrip("\0")
    except OSError:
        pass
    return f"{name}, kernel {os.uname().release}"


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--out")
    a = p.parse_args()
    if os.geteuid() == 0:
        raise SystemExit("Run this as the user Claude is to log in as, not as root (it uses sudo itself).")
    computer = socket.gethostname().split(".")[0]
    user = getpass.getuser()
    out = Path(a.out) if a.out else REPO / f"claude-access-{computer}.json"
    key_name = f"claude-access@{computer}".lower()

    print("Checking sudo (it may ask for this user's password)...")
    subprocess.run(["sudo", "-v"], check=True)
    passwords = setup_server(a.port)
    private_key, public_key = make_key(key_name)
    authorize(public_key, key_name)

    interfaces, lan, full_vpn = interfaces_and_lan()
    ts = tailscale()
    public_v4 = trace_ip("https://1.1.1.1/cdn-cgi/trace")
    public_v6 = trace_ip("https://[2606:4700:4700::1111]/cdn-cgi/trace")
    router_wan = router_wan_ip(lan[0]["address"]) if lan else None
    public_ip = None if full_vpn else public_v4
    if not public_ip and router_wan and not private_ip(router_wan):
        public_ip = router_wan
    own = {i.split("/")[0] for f in interfaces for i in f["ipv4"] + f["ipv6"]}
    cgnat = None if not router_wan else bool(private_ip(router_wan) or (public_ip and router_wan != public_ip))
    unit = Path.home() / ".config/systemd/user/screensender.service"

    access = {
        "format": "screensender-access/1",
        "created": datetime.now(timezone.utc).astimezone().isoformat(),
        "computer": computer,
        "user": user,
        "account": user,
        "os": os_name(),
        "shell": "bash",
        "port": a.port,
        "key_name": key_name,
        "private_key": private_key,
        "public_key": public_key,
        "host_keys": host_keys(),
        "lan": lan,
        "tailscale": ts,
        "public_ip": public_ip,
        "public_ip_on_this_pc": bool(public_ip and public_ip in own),
        "full_tunnel_vpn": full_vpn,
        "public": {"ipv4": public_v4, "ipv4_without_vpn": None, "ipv6": public_v6,
                   "ipv6_on_this_pc": bool(public_v6 and public_v6 in own),
                   "router_wan_ipv4": router_wan, "behind_cgnat": cgnat},
        "interfaces": interfaces,
        "repo": str(REPO),
        "screensender_installed": unit.exists(),
    }
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # it holds the private key
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(access, f, indent=2)
    os.chmod(out, 0o600)

    print("\n==== Claude access is ready ====")
    print(f"File:     {out}")
    print(f"Account:  {user} on {computer}, SSH port {a.port}")
    for f in interfaces:
        addrs = [x for x in f["ipv4"] + f["ipv6"] if not x.startswith(("169.254.", "fe80:"))]
        if f["status"] == "Up" and addrs:
            print(f"Interface: {f['name']}: {', '.join(addrs)}")
    if ts and ts["address"]:
        print(f"Tailscale: {', '.join(ts['addresses'])} {ts['dns_name']}")
    else:
        print("Tailscale: not running here. For access from anywhere without router setup, install it")
        print("           (curl -fsSL https://tailscale.com/install.sh | sh), sudo tailscale up, and run this again.")
    if public_v4:
        print(f"Public IPv4: {public_v4}" + (" (the VPN's exit)" if full_vpn else ""))
    if public_v6:
        print(f"Public IPv6: {public_v6}")
    if router_wan:
        print(f"Router WAN: {router_wan}" + (" (provider CGNAT: port forwarding cannot help)" if cgnat else ""))
    if public_ip:
        how = ("on this PC itself: reachable directly" if access["public_ip_on_this_pc"]
               else "shared by the provider: not reachable from outside" if cgnat
               else "the router's: needs a port forwarded to this PC")
        print(f"This PC on the internet: {public_ip} ({how})")
    if passwords:
        print("NOTE: the SSH server also takes passwords. Before forwarding a port to it from the internet,")
        print("      make sure every account here has a strong password, or turn password logins off.")
    print("\nGive this file to Claude on the other computer, privately (not by e-mail or chat).")
    print("It gives this account's access to this PC, sudo included. Delete it here once it has been copied.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"{' '.join(e.cmd)} failed")
