"""Keeps server.py running on an unattended PC: starts it again whenever it exits.

Started at sign-in by the "ScreenSender" scheduled task (see deploy/install.ps1) with pythonw.exe
from the project venv, so no window appears. The server's output goes to runtime/server.log.
While runtime/hold exists the server is not started; deploy/update.py uses it to swap the code.
"""

import ctypes
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
LOG = RUNTIME / "server.log"
PID_FILE = RUNTIME / "server.pid"
HOLD = RUNTIME / "hold"

LOG_MAX = 5 * 1024 * 1024
RESTART_DELAY = 3
# A hold left behind by a crashed update must not keep the server down for good.
HOLD_MAX_AGE = 15 * 60
ERROR_ALREADY_EXISTS = 183


def first_instance():
    # A second supervisor would start a second server on the same port.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    first_instance.mutex = kernel32.CreateMutexW(None, False, "Local\\ScreenSenderSupervisor")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def note(text):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} supervisor: {text}\n")


def held():
    try:
        age = time.time() - HOLD.stat().st_mtime
    except FileNotFoundError:
        return False
    if age < HOLD_MAX_AGE:
        return True
    note("removing a stale hold")
    HOLD.unlink(missing_ok=True)
    return False


def main():
    if not first_instance():
        return
    RUNTIME.mkdir(exist_ok=True)
    # pythonw.exe has no stdout; the server runs under python.exe, without a console window.
    python = Path(sys.executable).with_name("python.exe")
    while True:
        if held():
            time.sleep(1)
            continue
        if LOG.exists() and LOG.stat().st_size > LOG_MAX:
            LOG.replace(LOG.with_suffix(".log.1"))
        note("starting the server")
        with open(LOG, "ab") as log:
            proc = subprocess.Popen([str(python), "-u", "server.py"], cwd=ROOT, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            PID_FILE.write_text(str(proc.pid))
            code = proc.wait()
        note(f"the server exited with code {code}")
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
