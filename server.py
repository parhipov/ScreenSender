"""ScreenSender: shows the desktop on a web page (sending only changed tiles) and accepts input back."""

import ctypes
import hmac
import io
import json
import logging
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import flask.cli
import mss
from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from PIL import Image, ImageChops

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
    "tile_size": 64,
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


# ---------------------------------------------------------------- clipboard

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = -3


class Clipboard:
    def __init__(self):
        self._lock = threading.Lock()
        if not IS_WINDOWS:
            return
        # Own DLL handles, so the argtypes set here do not affect ctypes.windll used elsewhere.
        self.user32 = u = ctypes.WinDLL("user32")
        self.kernel32 = k = ctypes.WinDLL("kernel32")
        vp = ctypes.c_void_p
        u.CreateWindowExW.restype = vp
        u.CreateWindowExW.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      vp, vp, vp, vp]
        u.DestroyWindow.argtypes = [vp]
        u.OpenClipboard.argtypes = [vp]
        u.SetClipboardData.restype = vp
        u.SetClipboardData.argtypes = [ctypes.c_uint, vp]
        k.GlobalAlloc.restype = vp
        k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k.GlobalLock.restype = vp
        k.GlobalLock.argtypes = [vp]
        k.GlobalUnlock.argtypes = [vp]
        k.GlobalFree.argtypes = [vp]

    def set_text(self, text):
        data = (text.replace("\r\n", "\n").replace("\n", "\r\n") + "\0").encode("utf-16-le")
        u, k = self.user32, self.kernel32
        with self._lock:
            # EmptyClipboard without an owner window makes SetClipboardData fail, so borrow a
            # hidden message-only window for the duration of the call.
            hwnd = u.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
            try:
                for _ in range(20):  # another program may be holding the clipboard for a moment
                    if u.OpenClipboard(hwnd):
                        break
                    time.sleep(0.05)
                else:
                    raise RuntimeError("clipboard is busy")
                try:
                    u.EmptyClipboard()
                    handle = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
                    if not handle:
                        raise RuntimeError("out of memory")
                    ctypes.memmove(k.GlobalLock(handle), data, len(data))
                    k.GlobalUnlock(handle)
                    if not u.SetClipboardData(CF_UNICODETEXT, handle):
                        k.GlobalFree(handle)
                        raise RuntimeError("SetClipboardData failed")
                finally:
                    u.CloseClipboard()
            finally:
                if hwnd:
                    u.DestroyWindow(hwnd)


# ---------------------------------------------------------------- capture

# Past this share of changed tiles one whole-screen JPEG is smaller and faster than many pieces.
FULL_FRAME_SHARE = 0.5


@dataclass(frozen=True)
class Frame:
    epoch: int         # changes with the picture geometry; viewers then reload it whole
    seq: int
    monitor: dict
    monitor_index: int
    img: Image.Image
    cols: int
    rows: int
    tile_seq: list     # per tile: seq of the frame where it last changed
    cursor: tuple      # (x, y) in picture pixels, or None


def list_monitors():
    with MSS() as sct:
        return [dict(m) for m in sct.monitors]


def tile_rects(mask, cols, rows, ts, width, height):
    """Groups marked tiles into rectangles: runs within a row, extended down while the run repeats."""
    rects, open_rects = [], {}
    for r in range(rows):
        y1 = min((r + 1) * ts, height)
        nxt = {}
        c = 0
        while c < cols:
            if not mask[r * cols + c]:
                c += 1
                continue
            c0 = c
            while c < cols and mask[r * cols + c]:
                c += 1
            rect = open_rects.pop((c0, c), None)
            if rect:
                rect[3] = y1
            else:
                rect = [c0 * ts, r * ts, min(c * ts, width), y1]
            nxt[(c0, c)] = rect
        rects += open_rects.values()
        open_rects = nxt
    return rects + list(open_rects.values())


class Grabber(threading.Thread):
    """Single capture loop shared by all viewers; idles when nobody is watching.

    The picture is split into tiles and every tile remembers the frame it last changed in,
    so each viewer gets only the tiles that changed since the frame it already shows.
    """

    def __init__(self, monitor_index, fps, quality, max_width, draw_cursor, tile_size):
        super().__init__(daemon=True)
        monitors = list_monitors()
        if not 0 <= monitor_index < len(monitors):
            raise SystemExit(f"monitor {monitor_index} not found, available: 0..{len(monitors) - 1}")
        self.monitor_index = monitor_index
        self.monitor = monitors[monitor_index]
        self.interval = 1.0 / max(1, fps)
        self.quality = quality
        self.max_width = max_width
        self.draw_cursor = draw_cursor
        self.tile_size = max(16, tile_size)
        self._waiting = 0
        self._last_request = 0.0
        self._frame = None
        self._cond = threading.Condition()

    def set_monitor(self, index):
        monitors = list_monitors()  # re-read: resolution or layout may have changed since start
        if not 0 <= index < len(monitors):
            raise ValueError(f"monitor {index} not found")
        with self._cond:
            self.monitor_index = index
            self.monitor = monitors[index]

    def run(self):
        # mss handles are thread-bound on Windows, so the instance lives in this thread.
        with MSS() as sct:
            while True:
                if not self._active():
                    time.sleep(0.2)
                    continue
                started = time.monotonic()
                with self._cond:
                    monitor, index = self.monitor, self.monitor_index
                img = self._capture(sct, monitor)
                self._update(img, self._cursor(monitor, img.width / monitor["width"]), monitor, index)
                time.sleep(max(0.0, self.interval - (time.monotonic() - started)))

    def _capture(self, sct, monitor):
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if self.max_width and img.width > self.max_width:
            img = img.resize((self.max_width, round(img.height * self.max_width / img.width)),
                             Image.BILINEAR)
        return img

    def _cursor(self, monitor, scale):
        # Sent as coordinates and drawn by the page, so a moving pointer costs no picture data.
        pos = get_cursor_pos() if self.draw_cursor else None
        if not pos:
            return None
        x, y = pos[0] - monitor["left"], pos[1] - monitor["top"]
        if not (0 <= x < monitor["width"] and 0 <= y < monitor["height"]):
            return None
        return round(x * scale), round(y * scale)

    def _changed_tiles(self, prev, img, cols):
        diff = ImageChops.difference(prev, img)
        bbox = diff.getbbox()
        if not bbox:
            return []
        ts = self.tile_size
        changed = []
        for r in range(bbox[1] // ts, (bbox[3] - 1) // ts + 1):
            for c in range(bbox[0] // ts, (bbox[2] - 1) // ts + 1):
                box = (c * ts, r * ts, min((c + 1) * ts, img.width), min((r + 1) * ts, img.height))
                if diff.crop(box).getbbox():
                    changed.append(r * cols + c)
        return changed

    def _update(self, img, cursor, monitor, index):
        prev = self._frame  # only this thread replaces it
        seq = prev.seq + 1 if prev else 1
        if prev is None or prev.monitor is not monitor or prev.img.size != img.size:
            cols = -(-img.width // self.tile_size)
            rows = -(-img.height // self.tile_size)
            frame = Frame(random.getrandbits(30) | 1, seq, monitor, index, img, cols, rows,
                          [seq] * (cols * rows), cursor)
        else:
            changed = self._changed_tiles(prev.img, img, prev.cols)
            if not changed and cursor == prev.cursor:
                return  # a static screen costs no bandwidth at all
            tile_seq = list(prev.tile_seq)
            for i in changed:
                tile_seq[i] = seq
            frame = Frame(prev.epoch, seq, monitor, index, img, prev.cols, prev.rows, tile_seq, cursor)
        with self._cond:
            self._frame = frame
            self._cond.notify_all()

    def _active(self):
        # Viewers poll frame by frame, so "someone is watching" means a request is waiting now
        # or arrived a moment ago (the gap while the phone downloads and draws a frame).
        return self._waiting > 0 or time.monotonic() - self._last_request < 3.0

    def wait_frame(self, epoch, after, timeout=10.0):
        """Blocks until there is a frame the viewer (holding epoch/after) has not seen; None on timeout."""
        with self._cond:
            self._waiting += 1
            self._last_request = time.monotonic()
            try:
                ready = self._cond.wait_for(
                    lambda: self._frame is not None
                    and (self._frame.epoch != epoch or self._frame.seq != after), timeout=timeout)
                return self._frame if ready else None
            finally:
                self._waiting -= 1
                self._last_request = time.monotonic()

    def _jpeg(self, img):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=self.quality)
        return buf.getvalue()

    def render_update(self, frame, epoch, after):
        """Binary update: 4-byte header length, JSON header, then the JPEG pieces back to back."""
        img = frame.img
        full = frame.epoch != epoch or after > frame.seq
        if not full:
            mask = [s > after for s in frame.tile_seq]
            full = sum(mask) > len(mask) * FULL_FRAME_SHARE
        if full:
            pieces = [(0, 0, self._jpeg(img))]
        else:
            rects = tile_rects(mask, frame.cols, frame.rows, self.tile_size, img.width, img.height)
            pieces = [(r[0], r[1], self._jpeg(img.crop(r))) for r in rects]
        header = json.dumps({
            "epoch": frame.epoch, "seq": frame.seq, "monitor": frame.monitor_index,
            "width": img.width, "height": img.height, "cursor": frame.cursor,
            "rects": [[x, y, len(data)] for x, y, data in pieces],
        }).encode()
        return b"".join([len(header).to_bytes(4, "big"), header, *(data for _, _, data in pieces)])


grabber = Grabber(cfg["monitor"], cfg["fps"], cfg["jpeg_quality"], cfg["max_width"],
                  cfg["draw_cursor"], cfg["tile_size"])
mouse = Mouse()
keyboard = Keyboard()
clipboard = Clipboard()

# ---------------------------------------------------------------- web

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024


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
    monitors = list_monitors()
    # Cycle order for the page: each monitor, then all of them together (0) if there are several.
    order = list(range(1, len(monitors))) + ([0] if len(monitors) > 2 else [])
    return jsonify(input=bool(cfg["allow_input"] and IS_WINDOWS), monitor=grabber.monitor_index,
                   monitors=[{"index": i, "width": monitors[i]["width"], "height": monitors[i]["height"]}
                             for i in order])


@app.post("/monitor")
def set_monitor():
    try:
        grabber.set_monitor(int((request.get_json(silent=True) or {})["index"]))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="bad monitor index"), 400
    return jsonify(ok=True, monitor=grabber.monitor_index)


@app.get("/frame")
def frame():
    # The client asks for the next update only after it has drawn the previous one, so at most
    # one is ever in flight and a slow link never builds up lag (unlike an MJPEG push).
    try:
        epoch = int(request.args.get("epoch", 0))
        after = int(request.args.get("after", -1))
    except ValueError:
        epoch, after = 0, -1
    snap = grabber.wait_frame(epoch, after)
    if snap is None:
        return Response(status=204)  # screen unchanged; the client simply asks again
    return Response(grabber.render_update(snap, epoch, after), mimetype="application/octet-stream",
                    headers={"Cache-Control": "no-store"})


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
MAX_PASTE = 1_000_000


@app.post("/key")
def key():
    """Body: {"actions": [{"text": "..."} | {"paste": "..."} | {"key": "enter", "mods": ["ctrl"], "repeat": 1}, ...]}"""
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
        if "paste" in a:
            if not isinstance(a["paste"], str) or len(a["paste"]) > MAX_PASTE:
                return jsonify(error="bad paste text"), 400
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
        elif "paste" in a:
            # Through the clipboard: instant for long texts, and editors do not auto-indent
            # every line the way they do when the same text is typed.
            try:
                clipboard.set_text(a["paste"])
            except RuntimeError as e:
                return jsonify(error=str(e)), 503
            keyboard.press("v", ["ctrl"])
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
