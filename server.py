"""ScreenSender: streams the desktop as MJPEG to a web page and accepts mouse clicks back."""

import ctypes
import hmac
import io
import json
import logging
import socket
import sys
import threading
import time
from pathlib import Path

import flask.cli
import mss
from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from PIL import Image, ImageDraw

MSS = getattr(mss, "MSS", None) or mss.mss  # mss 10 renamed the class

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8005,
    "public_ip": "",
    "username": "admin",
    "password": "change-me",
    "monitor": 1,
    "fps": 5,
    "jpeg_quality": 60,
    "max_width": 1600,
    "draw_cursor": True,
    "allow_input": True,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    else:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


cfg = load_config()
IS_WINDOWS = sys.platform == "win32"

# Without DPI awareness Windows reports scaled coordinates and clicks land in the wrong place.
if IS_WINDOWS:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()


# ---------------------------------------------------------------- mouse

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010


def get_cursor_pos():
    if not IS_WINDOWS:
        return None
    pt = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


class Mouse:
    # Taps are never pixel-exact, so a quick second tap nearby is snapped to the first
    # one; otherwise Windows would not recognise it as a double click.
    DOUBLE_TAP_TIME = 0.45
    DOUBLE_TAP_DIST = 25

    def __init__(self):
        self._last = (0.0, 0, 0)
        self._lock = threading.Lock()

    def click(self, x, y, button="left"):
        if not IS_WINDOWS:
            raise RuntimeError("mouse input is implemented for Windows only")
        with self._lock:
            now = time.monotonic()
            last_t, last_x, last_y = self._last
            if (button == "left" and now - last_t < self.DOUBLE_TAP_TIME
                    and abs(x - last_x) <= self.DOUBLE_TAP_DIST
                    and abs(y - last_y) <= self.DOUBLE_TAP_DIST):
                x, y = last_x, last_y
            self._last = (now, x, y) if button == "left" else (0.0, 0, 0)

            down, up = ((MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if button == "right"
                        else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
            user32 = ctypes.windll.user32
            user32.SetCursorPos(int(x), int(y))
            user32.mouse_event(down, 0, 0, 0, 0)
            user32.mouse_event(up, 0, 0, 0, 0)


# ---------------------------------------------------------------- keyboard

ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]


class _INPUT(ctypes.Structure):
    # MOUSEINPUT is the largest union member; it must be present for sizeof(INPUT) to match.
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "insert": 0x2D, "delete": 0x2E,
    "win": 0x5B,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
}
# Without the extended flag these arrive as numpad keys and break when NumLock is on.
EXTENDED_VK = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}
MODIFIERS = ("ctrl", "alt", "shift", "win")


class Keyboard:
    def __init__(self):
        self._lock = threading.Lock()

    @staticmethod
    def _key_input(vk=0, scan=0, flags=0):
        inp = _INPUT(type=INPUT_KEYBOARD)
        inp.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)
        return inp

    def _vk_event(self, vk, up=False):
        flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VK else 0)
        return self._key_input(vk=vk, flags=flags)

    @staticmethod
    def _send(events):
        if events:
            arr = (_INPUT * len(events))(*events)
            ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))

    def type_text(self, text):
        """Types arbitrary text (any language) as Unicode key events, independent of the PC layout."""
        events = []
        for ch in text.replace("\r\n", "\n"):
            if ch == "\n":
                events += [self._vk_event(VK["enter"]), self._vk_event(VK["enter"], up=True)]
                continue
            data = ch.encode("utf-16-le")
            for i in range(0, len(data), 2):  # astral chars (emoji) go as a surrogate pair
                unit = int.from_bytes(data[i:i + 2], "little")
                events += [self._key_input(scan=unit, flags=KEYEVENTF_UNICODE),
                           self._key_input(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
        with self._lock:
            self._send(events)

    def press(self, key, mods=(), repeat=1):
        vk = VK[key]
        mod_vks = [VK[m] for m in MODIFIERS if m in mods]
        events = [self._vk_event(m) for m in mod_vks]
        for _ in range(repeat):
            events += [self._vk_event(vk), self._vk_event(vk, up=True)]
        events += [self._vk_event(m, up=True) for m in reversed(mod_vks)]
        with self._lock:
            self._send(events)


# ---------------------------------------------------------------- capture

class Grabber(threading.Thread):
    """Single capture loop shared by all viewers; idles when nobody is watching."""

    def __init__(self, monitor_index, fps, quality, max_width, draw_cursor):
        super().__init__(daemon=True)
        with MSS() as sct:
            if not 0 <= monitor_index < len(sct.monitors):
                raise SystemExit(f"monitor {monitor_index} not found, available: 0..{len(sct.monitors) - 1}")
            self.monitor = dict(sct.monitors[monitor_index])
        self.interval = 1.0 / max(1, fps)
        self.quality = quality
        self.max_width = max_width
        self.draw_cursor = draw_cursor
        self._waiting = 0
        self._last_request = 0.0
        self._frame = None
        self._seq = 0
        self._cond = threading.Condition()

    def run(self):
        # mss handles are thread-bound on Windows, so the instance lives in this thread.
        with MSS() as sct:
            while True:
                if not self._active():
                    time.sleep(0.2)
                    continue
                started = time.monotonic()
                frame = self._encode(sct.grab(self.monitor))
                with self._cond:
                    # An unchanged screen produces identical JPEG bytes; skipping it means a
                    # static screen costs no bandwidth at all.
                    if frame != self._frame:
                        self._frame = frame
                        self._seq += 1
                        self._cond.notify_all()
                time.sleep(max(0.0, self.interval - (time.monotonic() - started)))

    def _encode(self, shot):
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        scale = 1.0
        if self.max_width and img.width > self.max_width:
            scale = self.max_width / img.width
            img = img.resize((self.max_width, round(img.height * scale)), Image.BILINEAR)
        if self.draw_cursor:
            pos = get_cursor_pos()
            if pos:
                cx = (pos[0] - self.monitor["left"]) * scale
                cy = (pos[1] - self.monitor["top"]) * scale
                r = 6
                ImageDraw.Draw(img).ellipse((cx - r, cy - r, cx + r, cy + r),
                                            fill=(255, 40, 40), outline=(255, 255, 255), width=2)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=self.quality)
        return buf.getvalue()

    def _active(self):
        # Viewers poll frame by frame, so "someone is watching" means a request is waiting now
        # or arrived a moment ago (the gap while the phone downloads and draws a frame).
        return self._waiting > 0 or time.monotonic() - self._last_request < 3.0

    def wait_frame(self, last_seq, timeout=10.0):
        """Blocks until there is a frame newer than last_seq; returns (frame, seq) or (None, seq) on timeout."""
        with self._cond:
            self._waiting += 1
            self._last_request = time.monotonic()
            try:
                ready = self._cond.wait_for(
                    lambda: self._frame is not None and self._seq != last_seq, timeout=timeout)
                return (self._frame if ready else None), self._seq
            finally:
                self._waiting -= 1
                self._last_request = time.monotonic()


grabber = Grabber(cfg["monitor"], cfg["fps"], cfg["jpeg_quality"], cfg["max_width"], cfg["draw_cursor"])
mouse = Mouse()
keyboard = Keyboard()

# ---------------------------------------------------------------- web

app = Flask(__name__, static_folder=None)


AUTH_COOKIE = "screensender_auth"
AUTH_COOKIE_AGE = 30 * 24 * 3600
PUBLIC_PATHS = {"/login"}


def auth_token():
    # Derived from the credentials, so changing the password in config logs everyone out.
    key = f"{cfg['username']}\0{cfg['password']}".encode()
    return hmac.new(key, b"screensender-auth", "sha256").hexdigest()


@app.before_request
def require_auth():
    if request.path in PUBLIC_PATHS:
        return None
    if hmac.compare_digest(request.cookies.get(AUTH_COOKIE, ""), auth_token()):
        return None
    if request.path == "/":
        return redirect("/login")
    return jsonify(error="unauthorized"), 401


@app.get("/login")
def login_page():
    return send_from_directory(BASE_DIR / "static", "login.html")


@app.post("/login")
def login():
    ok = (hmac.compare_digest(request.form.get("username", ""), cfg["username"])
          & hmac.compare_digest(request.form.get("password", ""), cfg["password"]))
    if not ok:
        time.sleep(1)  # slows down password guessing
        return redirect("/login?error=1")
    resp = redirect("/")
    resp.set_cookie(AUTH_COOKIE, auth_token(), max_age=AUTH_COOKIE_AGE,
                    httponly=True, samesite="Lax")
    return resp


@app.get("/logout")
def logout():
    resp = redirect("/login")
    resp.delete_cookie(AUTH_COOKIE)
    return resp


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.get("/info")
def info():
    return jsonify(width=grabber.monitor["width"], height=grabber.monitor["height"],
                   input=bool(cfg["allow_input"] and IS_WINDOWS))


@app.get("/frame.jpg")
def frame():
    # The client asks for the next frame only after it has shown the previous one, so at most
    # one frame is ever in flight and a slow link never builds up lag (unlike an MJPEG push).
    try:
        after = int(request.args.get("after", -1))
    except ValueError:
        after = -1
    data, seq = grabber.wait_frame(after)
    if data is None:
        return Response(status=204)  # screen unchanged; the client simply asks again
    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store", "X-Frame-Seq": str(seq)})


@app.post("/click")
def click():
    if not (cfg["allow_input"] and IS_WINDOWS):
        return jsonify(error="input disabled"), 403
    data = request.get_json(silent=True) or {}
    try:
        nx, ny = float(data["x"]), float(data["y"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="x and y are required"), 400
    if not (0 <= nx <= 1 and 0 <= ny <= 1):
        return jsonify(error="x and y must be in 0..1"), 400
    button = "right" if data.get("button") == "right" else "left"
    mon = grabber.monitor
    x = mon["left"] + round(nx * (mon["width"] - 1))
    y = mon["top"] + round(ny * (mon["height"] - 1))
    mouse.click(x, y, button)
    return jsonify(ok=True, x=x, y=y, button=button)


MAX_TEXT = 2000


@app.post("/key")
def key():
    """Body: {"actions": [{"text": "..."} | {"key": "enter", "mods": ["ctrl"], "repeat": 1}, ...]}"""
    if not (cfg["allow_input"] and IS_WINDOWS):
        return jsonify(error="input disabled"), 403
    actions = (request.get_json(silent=True) or {}).get("actions")
    if not isinstance(actions, list):
        return jsonify(error="actions list is required"), 400
    for a in actions:
        if not isinstance(a, dict):
            return jsonify(error="bad action"), 400
        if "text" in a:
            if not isinstance(a["text"], str) or len(a["text"]) > MAX_TEXT:
                return jsonify(error="bad text"), 400
            continue
        name = str(a.get("key", "")).lower()
        mods = a.get("mods") or []
        repeat = a.get("repeat", 1)
        if (name not in VK or not isinstance(mods, list) or any(m not in MODIFIERS for m in mods)
                or not isinstance(repeat, int) or not 1 <= repeat <= 100):
            return jsonify(error=f"bad key action: {a}"), 400
    for a in actions:
        if "text" in a:
            keyboard.type_text(a["text"])
        else:
            keyboard.press(str(a["key"]).lower(), a.get("mods") or [], a.get("repeat", 1))
    return jsonify(ok=True)


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def quiet_startup_banner():
    # Flask/werkzeug list only local interfaces, which hides our own URL list; drop their banner
    # but keep per-request logging.
    flask.cli.show_server_banner = lambda *args, **kwargs: None
    noise = ("Running on", "development server", "CTRL+C")
    logging.getLogger("werkzeug").addFilter(
        lambda record: not any(n in record.getMessage() for n in noise))


if __name__ == "__main__":
    port = cfg["port"]
    mon = grabber.monitor
    print(f"Capturing monitor {cfg['monitor']}: {mon['width']}x{mon['height']} at ({mon['left']},{mon['top']})")
    print(f"  LAN:      http://{lan_ip()}:{port}/")
    if cfg["public_ip"]:
        print(f"  Internet: http://{cfg['public_ip']}:{port}/")
    else:
        print("  Internet: public_ip is not set in config.json")
    if cfg["password"] == DEFAULT_CONFIG["password"]:
        print("  WARNING: default password in config.json - change it before exposing the port!")
    print(f"  Login:    {cfg['username']} / {cfg['password']}")
    print("Press Ctrl+C to stop")
    quiet_startup_banner()
    grabber.start()
    app.run(host=cfg["host"], port=port, threaded=True)
