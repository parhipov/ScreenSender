"""What deploy/supervisor.py and deploy/update.py share: paths, the server's address, its process."""

import ctypes
import http.cookiejar
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
PID_FILE = RUNTIME / "server.pid"
HOLD = RUNTIME / "hold"
IS_WINDOWS = sys.platform == "win32"
VENV_PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
# For every subprocess: no console window flashing up under pythonw.exe.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

if IS_WINDOWS:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
                                                    ctypes.POINTER(ctypes.c_ulong)]
else:
    import fcntl


def lock(name):
    """Takes runtime/<name>.lock for as long as this process lives; False if another one holds it.

    Windows: a named mutex, Linux: flock. Both go away with the process, however it ends.
    """
    if IS_WINDOWS:
        handle = kernel32.CreateMutexW(None, False, f"Local\\ScreenSender{name.title()}")
        lock.held.append(handle)
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    f = open(RUNTIME / f"{name}.lock", "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    lock.held.append(f)
    return True


lock.held = []


def locked(name):
    """Whether another process holds lock(name) now."""
    if IS_WINDOWS:
        handle = kernel32.OpenMutexW(0x00100000, False, f"Local\\ScreenSender{name.title()}")  # SYNCHRONIZE
        if handle:
            kernel32.CloseHandle(handle)
        return bool(handle)
    try:
        with open(RUNTIME / f"{name}.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    return False


# ---------------------------------------------------------------- the server's address

def config():
    path = ROOT / "config.json"
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def base_url(cfg=None):
    cfg = config() if cfg is None else cfg
    host = cfg.get("host", "0.0.0.0")
    host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    scheme = "https" if cfg.get("https") else "http"
    return f"{scheme}://{host}:{cfg.get('port', 8005)}"


def opener():
    # The certificate is self-signed, and this is the server on this very PC.
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
                                       urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def status(url, timeout=10):
    """HTTP status of a GET, or None when the server does not answer."""
    try:
        with opener().open(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 - refused, timed out, TLS failed: all "no answer"
        return None


def smoke_test():
    """Logs in and calls what the page calls. Returns None, or what failed."""
    cfg = config()
    base = base_url(cfg)
    web = opener()
    try:
        form = urllib.parse.urlencode({"username": cfg.get("username", ""), "password": cfg.get("password", "")})
        with web.open(base + "/login", form.encode(), timeout=10) as r:
            if urllib.parse.urlparse(r.url).path != "/" or b"<canvas" not in r.read():
                return "the login did not lead to the viewer page"
        with web.open(base + "/info", timeout=10) as r:
            if "monitors" not in json.load(r):
                return "/info has no monitors"
        for req in range(1, 4):  # the very first frame may take longer than one long poll
            with web.open(f"{base}/frame?v=update-check&r={req}&ack=0", timeout=15) as r:
                body = r.read()
                if r.status == 200:
                    break
        else:
            return "/frame sent no picture"
        n = int.from_bytes(body[:4], "big")
        if "epoch" not in json.loads(body[4:4 + n]):
            return "/frame has a broken header"
        try:
            key = urllib.request.Request(base + "/key", b'{"actions": []}', {"Content-Type": "application/json"})
            web.open(key, timeout=10).close()
        except urllib.error.HTTPError as e:
            if e.code != 403:  # 403: input is switched off in config.json
                raise
        try:
            web.open(base + "/health", timeout=10).close()
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404: a version from before /health, e.g. after a rollback
                raise
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} from {e.filename}"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return None


# ---------------------------------------------------------------- the server's process

def server_pid():
    try:
        return int(PID_FILE.read_text())
    except (OSError, ValueError):
        return None


def alive(pid):
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:  # a zombie has exited; only its parent has not collected it yet
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def is_server(pid):
    """pid is the venv's python, as the supervisor starts it; pids get reused by other programs."""
    if not IS_WINDOWS:
        # The venv's python is a link to the system one, so: server.py, run in this folder.
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            return False
        return cwd == ROOT.resolve() and b"server.py" in args
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        buf, size = ctypes.create_unicode_buffer(32768), ctypes.c_ulong(32768)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return False
        return Path(buf.value).resolve() == VENV_PYTHON.resolve()
    finally:
        kernel32.CloseHandle(handle)


def kill_server(pid):
    """Kills the server's process tree. True when it is gone (or was not running)."""
    if pid is None or not alive(pid) or not is_server(pid):
        return True
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, **NO_WINDOW)
    else:
        # The supervisor starts it in a group of its own, so grim, wtype and the like go too.
        try:
            os.killpg(pid, signal.SIGKILL)
        except PermissionError:
            return False
        except ProcessLookupError:
            return True
    for _ in range(20):
        if not alive(pid):
            return True
        time.sleep(0.5)
    return False
