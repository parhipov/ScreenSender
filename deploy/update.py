"""Updates an unattended install from git without losing remote access.

Normally started through the "ScreenSender update" scheduled task:  schtasks /Run /TN "ScreenSender update"
It can also be run from an elevated shell:  .venv\\Scripts\\python.exe deploy\\update.py

Pulls the upstream branch, restarts the server through the supervisor, and checks the new version:
it must answer, keep answering, and let the check in common.smoke_test log in and use the page's
endpoints. If it fails, the previous commit is restored and started again. The log is
runtime/update.log; its last line is "RESULT <code>: <text>" with code
    0  updated, or already up to date
    1  refused, nothing was changed (local changes, no supervisor, not elevated, ...)
    2  the new version failed and the previous one is running again
    3  the server does not work even after the rollback
"""

import ctypes
import os
import subprocess
import sys
import time

from common import HOLD, ROOT, RUNTIME, VENV_PYTHON, alive, base_url, kill_server, server_pid, smoke_test, status

LOG = RUNTIME / "update.log"

START_TIMEOUT = 60   # for the new server to answer
STAY_UP = 10         # and then to keep answering
CRASH_STARTS = 3     # this many starts without an answer: it exits at start, roll back at once
SYNCHRONIZE = 0x00100000
ERROR_ALREADY_EXISTS = 183

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenMutexW.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


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


def supervisor_running():
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, "Local\\ScreenSenderSupervisor")
    if handle:
        kernel32.CloseHandle(handle)
    return bool(handle)


def answers():
    try:
        return status(base_url() + "/login", timeout=5) == 200
    except Exception:  # noqa: BLE001 - a broken config.json: not answering as far as we can tell
        return False


def healthy(old_pid):
    """A server started after old_pid answers, keeps answering, and passes the smoke test."""
    deadline = time.monotonic() + START_TIMEOUT
    starts = set()
    while time.monotonic() < deadline:
        pid = server_pid()
        if pid and pid != old_pid:
            starts.add(pid)
            if alive(pid) and answers():
                time.sleep(STAY_UP)
                if server_pid() == pid and alive(pid) and answers():
                    problem = smoke_test()
                    if problem is None:
                        return True
                    log(f"the check failed: {problem}")
                    return False
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
        if not kill_server(before):
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
            if any(name in changed for name in ("deploy/supervisor.py", "deploy/common.py")):
                log("note: the supervisor changed; the running one is the old one until the next "
                    "sign-in or until deploy/install.ps1 is run again")
            if "deploy/install.ps1" in changed:
                log("note: deploy/install.ps1 changed; run it again to apply the changes")
            return 0, f"updated to {new[:7]}"
        log("the new version does not work; rolling back")

    try:
        before = switch(old, requirements_changed)
    except Exception as e:  # noqa: BLE001
        return 3, f"the rollback failed ({e}); the server may be down"
    if healthy(before):
        return 2, f"the new version failed; {old[:7]} is running again"
    return 3, "the previous version does not work either; see runtime/server.log"


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
