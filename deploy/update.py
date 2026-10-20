"""Updates an unattended install from git without losing remote access.

Normally started through the "ScreenSender update" scheduled task:  schtasks /Run /TN "ScreenSender update"
It can also be run from an elevated shell:  .venv\\Scripts\\python.exe deploy\\update.py

Pulls the upstream branch, restarts the server through the supervisor and checks that it answers.
If it does not, the previous commit is restored and started again. The log is runtime/update.log;
its last line is "RESULT <code>: <text>" with code
    0  updated, or already up to date
    1  refused, nothing was changed (local changes, no supervisor, not elevated, ...)
    2  the new version failed and the previous one is running again
    3  the server does not answer even after the rollback
"""

import ctypes
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
LOG = RUNTIME / "update.log"
PID_FILE = RUNTIME / "server.pid"
HOLD = RUNTIME / "hold"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

START_TIMEOUT = 60   # for the new server to answer
STAY_UP = 10         # and then to keep answering
CRASH_STARTS = 3     # this many starts without an answer: it exits at start, roll back at once
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
ERROR_ALREADY_EXISTS = 183

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenMutexW.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]


class Refused(Exception):
    pass


def log(text):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
    print(line, flush=True)  # nowhere under pythonw.exe; the file is the record
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(*cmd, timeout=120):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # a credential prompt would hang with no window
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode:
        raise RuntimeError(f"{' '.join(cmd)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def git(*args):
    return run("git", *args)


def alive(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def server_pid():
    try:
        return int(PID_FILE.read_text())
    except (OSError, ValueError):
        return None


def supervisor_running():
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, "Local\\ScreenSenderSupervisor")
    if handle:
        kernel32.CloseHandle(handle)
    return bool(handle)


def stop_server():
    pid = server_pid()
    if pid is None or not alive(pid):
        return True
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(20):
        if not alive(pid):
            return True
        time.sleep(0.5)
    return False


def login_url():
    path = ROOT / "config.json"
    cfg = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    host = cfg.get("host", "0.0.0.0")
    host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    scheme = "https" if cfg.get("https") else "http"
    return f"{scheme}://{host}:{cfg.get('port', 8005)}/login"


def answers():
    try:
        with urllib.request.urlopen(login_url(), timeout=5, context=ssl._create_unverified_context()) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - any failure means "not answering"
        return False


def healthy(old_pid):
    """A server started after old_pid answers, and still answers a little later."""
    deadline = time.monotonic() + START_TIMEOUT
    starts = set()
    while time.monotonic() < deadline:
        pid = server_pid()
        if pid and pid != old_pid:
            starts.add(pid)
            if alive(pid) and answers():
                time.sleep(STAY_UP)
                if server_pid() == pid and alive(pid) and answers():
                    return True
            elif len(starts) >= CRASH_STARTS:
                return False
        time.sleep(1)
    return False


def switch(commit, requirements_changed):
    """Stops the server, checks out commit, and lets the supervisor start it again.

    Returns the pid of the server that was stopped, so the caller can wait for a new one.
    """
    before = server_pid()
    HOLD.touch()
    try:
        if not stop_server():
            raise Refused("could not stop the server; run this elevated or through the scheduled task")
        git("reset", "--hard", "--quiet", commit)
        if requirements_changed:
            log("installing requirements")
            run(str(VENV_PYTHON), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                "-r", "requirements.txt", timeout=900)
    finally:
        HOLD.unlink(missing_ok=True)
    return before


def update():
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise Refused("not elevated: the server runs as administrator and cannot be stopped from here")
    if not supervisor_running():
        raise Refused("the supervisor is not running, so nothing would start the server again")
    git("fetch", "--quiet")
    old, new = git("rev-parse", "HEAD"), git("rev-parse", "@{u}")
    if old == new:
        return 0, "already up to date"
    if git("status", "--porcelain", "--untracked-files=no"):
        raise Refused("tracked files have local changes; commit or discard them first")
    if subprocess.run(["git", "merge-base", "--is-ancestor", old, new], cwd=ROOT,
                      creationflags=subprocess.CREATE_NO_WINDOW).returncode:
        raise Refused("the local branch has commits that are not upstream")
    changed = git("diff", "--name-only", old, new).splitlines()
    requirements_changed = "requirements.txt" in changed
    log(f"updating {old[:7]} -> {new[:7]}, {len(changed)} files changed")

    try:
        before = switch(new, requirements_changed)
    except Refused:
        raise  # nothing was changed
    except Exception as e:  # noqa: BLE001 - whatever broke, the old version goes back
        log(f"the update failed: {e}")
        before = None
    else:
        if healthy(before):
            if "deploy/supervisor.py" in changed:
                log("note: deploy/supervisor.py changed; the running supervisor is the old one "
                    "until the next sign-in or until deploy/install.ps1 is run again")
            if "deploy/install.ps1" in changed:
                log("note: deploy/install.ps1 changed; run it again to apply the changes")
            return 0, f"updated to {new[:7]}"
        log("the new version does not answer; rolling back")

    try:
        before = switch(old, requirements_changed)
    except Exception as e:  # noqa: BLE001
        return 3, f"the rollback failed ({e}); the server may be down"
    if healthy(before):
        return 2, f"the new version failed; {old[:7]} is running again"
    return 3, "the server does not answer even after the rollback; see runtime/server.log"


def main():
    RUNTIME.mkdir(exist_ok=True)
    kernel32.CreateMutexW(None, False, "Local\\ScreenSenderUpdate")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        log("RESULT 1: another update is running")
        return 1
    log("update started")
    try:
        code, text = update()
    except Refused as e:
        code, text = 1, str(e)
    except Exception as e:  # noqa: BLE001 - reached only before anything was stopped
        code, text = 1, f"{e}"
    log(f"RESULT {code}: {text}")
    return code


if __name__ == "__main__":
    sys.exit(main())
