"""Keeps server.py running on an unattended PC: starts it again whenever it exits or stops answering.

Started at sign-in (and every few minutes, in case it died) by the "ScreenSender" scheduled task,
see deploy/install.ps1, with pythonw.exe from the project venv, so no window appears. The server's
output goes to runtime/server.log. While runtime/hold exists the server is not started;
deploy/update.py uses it to swap the code.
"""

import ctypes
import subprocess
import sys
import time
import traceback
from pathlib import Path

from common import HOLD, PID_FILE, ROOT, RUNTIME, base_url, kill_server, server_pid, status

LOG = RUNTIME / "server.log"

LOG_MAX = 5 * 1024 * 1024
RESTART_DELAY = 3
# A hold left behind by a crashed update must not keep the server down for good.
HOLD_MAX_AGE = 15 * 60
HEALTH_GRACE = 60     # seconds after a start before the first health check
HEALTH_EVERY = 30
HEALTH_FAILURES = 3   # in a row; then the server is restarted
ERROR_ALREADY_EXISTS = 183


def first_instance():
    # A second supervisor would start a second server on the same port.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    first_instance.mutex = kernel32.CreateMutexW(None, False, "Local\\ScreenSenderSupervisor")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def note(text):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} supervisor: {text}\n")
    except OSError:  # a full disk must not stop the supervisor
        pass


def held():
    try:
        age = time.time() - HOLD.stat().st_mtime
    except OSError:
        return False
    if 0 <= age < HOLD_MAX_AGE:  # negative: the clock went back, so its age says nothing
        return True
    note("removing a stale hold")
    HOLD.unlink(missing_ok=True)
    return False


def rotate_log():
    try:
        if LOG.stat().st_size > LOG_MAX:
            LOG.replace(LOG.with_suffix(".log.1"))
    except OSError:  # no log yet, or a reader holds it open: rotate next time
        pass


def run_server(python):
    """Runs the server until it exits, restarting it when it stops answering. Returns its exit code."""
    try:
        health_url = base_url() + "/health"
    except Exception as e:  # noqa: BLE001 - a broken config.json: the server will say what is wrong
        note(f"no health checks: {e}")
        health_url = None
    note("starting the server")
    with open(LOG, "ab") as log:
        proc = subprocess.Popen([str(python), "-u", "server.py"], cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    PID_FILE.write_text(str(proc.pid))
    failures, next_check = 0, time.monotonic() + HEALTH_GRACE
    while True:
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if health_url is None or time.monotonic() < next_check:
            continue
        next_check = time.monotonic() + HEALTH_EVERY
        if status(health_url) == 200:
            failures = 0
            continue
        failures += 1
        note(f"health check failed ({failures} of {HEALTH_FAILURES})")
        if failures >= HEALTH_FAILURES:
            note("the server does not respond; restarting it")
            kill_server(proc.pid)


def main():
    if not first_instance():
        return
    RUNTIME.mkdir(exist_ok=True)
    # No update runs before the supervisor does: a hold now was left by one that a reboot cut off.
    HOLD.unlink(missing_ok=True)
    # A server left over from a supervisor that was killed would keep the port from the new one.
    kill_server(server_pid())
    # pythonw.exe has no stdout; the server runs under python.exe, without a console window.
    python = Path(sys.executable).with_name("python.exe")
    while True:
        try:
            if held():
                time.sleep(1)
                continue
            rotate_log()
            note(f"the server exited with code {run_server(python)}")
        except Exception:  # noqa: BLE001 - the supervisor must outlive anything
            note("error:\n" + traceback.format_exc())
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
