"""ScreenSender: shows the desktop on a web page (sending only changed tiles) and accepts input back."""

import ctypes
import hmac
import io
import ipaddress
import json
import logging
import random
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import flask.cli
import mss
import numpy as np
from flask import Flask, Response, jsonify, redirect, request, send_file, send_from_directory
from PIL import Image
from werkzeug.serving import BaseWSGIServer, WSGIRequestHandler, make_server

MSS = getattr(mss, "MSS", None) or mss.mss  # mss 10 renamed the class

try:
    import dxcam  # DXGI Desktop Duplication, Windows only
except Exception:  # noqa: BLE001 - not installed, or no DirectX on this machine
    dxcam = None

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
CERT_PATH = BASE_DIR / "cert.pem"
KEY_PATH = BASE_DIR / "key.pem"
SECRET_PATH = BASE_DIR / "secret.key"

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8005,
    "https": False,
    "public_ip": "",
    "username": "admin",
    "password": "change-me",
    "monitor": 1,
    "capture": "auto",
    "fps": 10,
    "max_width": 1600,
    "tile_size": 64,
    "draw_cursor": True,
    "allow_input": True,
}


def say(text):
    try:
        print(text, flush=True)
    except OSError:  # a full disk must not take the server down
        pass


def log(text):
    # Timestamped, so these lines can be matched with the supervisor's and the update's logs.
    say(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        # utf-8-sig: Notepad and PowerShell 5.1 may save the file with a BOM.
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
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
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
WHEEL_DELTA = 120


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

    # A held button is let go when the page goes quiet, e.g. the connection dropped mid-drag.
    DRAG_TIMEOUT = 10.0

    def __init__(self):
        self._last = (0.0, 0, 0)
        self._lock = threading.Lock()
        self._held = False
        self._release_timer = None

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

    def wheel(self, x, y, dy=0, dx=0):
        """Scrolls by whole notches at a point; Windows scrolls the window under the pointer."""
        with self._lock:
            self._last = (0.0, 0, 0)
            user32 = ctypes.windll.user32
            user32.SetCursorPos(int(x), int(y))
            if dy:
                user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, dy * WHEEL_DELTA, 0)
            if dx:
                user32.mouse_event(MOUSEEVENTF_HWHEEL, 0, 0, dx * WHEEL_DELTA, 0)

    def drag(self, action, x, y):
        """Left-button drag: "down" where it starts, "move" while it goes on, "up" where it ends."""
        with self._lock:
            self._last = (0.0, 0, 0)
            user32 = ctypes.windll.user32
            user32.SetCursorPos(int(x), int(y))
            if action == "down" and not self._held:
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                self._held = True
            elif action == "up" and self._held:
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                self._held = False
            if self._release_timer:
                self._release_timer.cancel()
                self._release_timer = None
            if self._held:
                self._release_timer = threading.Timer(self.DRAG_TIMEOUT, self._release)
                self._release_timer.daemon = True
                self._release_timer.start()

    def _release(self):
        with self._lock:
            # A move that arrived meanwhile has started a newer timer; then this one is stale.
            if self._held and threading.current_thread() is self._release_timer:
                ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                self._held = False


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
    "win": 0x5B, "apps": 0x5D,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
}
# Without the extended flag these arrive as numpad keys and break when NumLock is on.
EXTENDED_VK = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D}
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

    def hold(self, mods, up=False):
        """Presses (or lets go of) modifiers around a mouse click: Shift+click, Ctrl+click."""
        vks = [VK[m] for m in MODIFIERS if m in mods]
        with self._lock:
            self._send([self._vk_event(vk, up) for vk in (reversed(vks) if up else vks)])

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


# ---------------------------------------------------------------- capture backends

def list_monitors():
    with MSS() as sct:
        return [dict(m) for m in sct.monitors]


class GdiCapture:
    """BitBlt through mss: works everywhere, but copies the whole screen on every call."""

    name = "GDI"

    def __init__(self, monitor):
        self.monitor = monitor
        self._sct = MSS()

    def grab(self):
        """Returns the screen as a BGRA array (height, width, 4)."""
        shot = self._sct.grab(self.monitor)
        return np.frombuffer(shot.bgra, np.uint8).reshape(shot.height, shot.width, 4)

    def close(self):
        self._sct.close()


def _no_recovery():
    raise RuntimeError("desktop duplication access lost")


class DxgiCapture:
    """DXGI Desktop Duplication through dxcam: fast, and reports when nothing has changed.

    One camera per physical monitor; the "all monitors" picture is put together from several.
    """

    name = "DXGI"
    FIRST_FRAME_TRIES = 25

    def __init__(self, monitor):
        self.monitor = monitor
        left, top, w, h = monitor["left"], monitor["top"], monitor["width"], monitor["height"]
        self._cams = []
        try:
            for di, outputs in enumerate(getattr(dxcam, "__factory").outputs):
                for oi, out in enumerate(outputs):
                    c = out.desc.DesktopCoordinates
                    if c.left >= left and c.top >= top and c.right <= left + w and c.bottom <= top + h:
                        cam = dxcam.create(device_idx=di, output_idx=oi, output_color="BGRA",
                                           processor_backend="numpy")
                        # dxcam retries forever while the desktop cannot be duplicated (lock screen,
                        # UAC prompt); fail instead, so the grabber can switch to GDI meanwhile.
                        cam._recover_output = _no_recovery
                        self._cams.append((cam, c.left - left, c.top - top, c.right - c.left, c.bottom - c.top))
            if not self._cams:
                raise RuntimeError("no DXGI output matches the monitor")
        except Exception:
            self.close()
            raise
        single = len(self._cams) == 1 and self._cams[0][1:] == (0, 0, w, h)
        self._buf = None if single else np.zeros((h, w, 4), np.uint8)
        self._got_frame = False

    def grab(self):
        """Returns a BGRA array, or None when the screen has not changed since the last call."""
        frame = self._grab()
        if frame is None and not self._got_frame:
            # Right after DuplicateOutput the first frame may take a moment to arrive.
            for _ in range(self.FIRST_FRAME_TRIES):
                time.sleep(0.02)
                frame = self._grab()
                if frame is not None:
                    break
            else:
                raise RuntimeError("no frame from desktop duplication")
        if frame is not None:
            self._got_frame = True
        return frame

    def _grab(self):
        if self._buf is None:
            return self._cams[0][0].grab()
        changed = False
        for cam, x, y, cw, ch in self._cams:
            frame = cam.grab()
            if frame is not None:
                self._buf[y:y + ch, x:x + cw] = frame[:ch, :cw]
                changed = True
        return self._buf if changed else None

    def close(self):
        for cam, *_ in self._cams:
            try:
                cam.release()
            except Exception:  # noqa: BLE001 - nothing useful to do with a failed release
                pass
        self._cams = []


# ---------------------------------------------------------------- grabber

# Past this share of pending tiles one picture of the whole area is smaller and faster than pieces.
FULL_FRAME_SHARE = 0.5
# Small pieces go as WebP (tiny header, better compression); bigger ones as JPEG, which
# Pillow encodes about ten times faster.
WEBP_MAX_AREA = 256 * 256
# Once the screen has been still this long, tiles sent in low quality are sent again crisp.
REFINE_DELAY = 0.7
# After a DXGI failure GDI is used this long before desktop duplication is tried again.
DXGI_RETRY = 30.0
# A capture that nobody has looked at this long is released.
CAPTURE_IDLE = 30.0
# The capture loop passes several times a second; this long without a pass means it is stuck.
HEALTH_STALL = 30.0
LONG_POLL = 5.0

# Quality presets the page can choose from: (normal quality, quality of the crisp refill).
QUALITY = {"low": (30, 55), "normal": (55, 85), "high": (75, 95)}


@dataclass(frozen=True, eq=False)
class Frame:
    epoch: int          # changes with the picture geometry; viewers then start from scratch
    seq: int
    monitor: dict
    monitor_index: int
    img: Image.Image
    arr: np.ndarray     # the same picture as (height, width, 3) for the diff
    cols: int
    rows: int
    tile_seq: np.ndarray  # per tile: seq of the frame where it last changed (or was refined)
    tile_hq: np.ndarray   # per tile: True once it has been published in high quality
    cursor: tuple         # (x, y) in picture pixels, or None


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


def encode(piece, quality, crisp):
    buf = io.BytesIO()
    if crisp or piece.width * piece.height <= WEBP_MAX_AREA:
        # The crisp pass happens on a still screen, so its slower encoding costs nothing
        # noticeable, and WebP is half the size of a JPEG of the same quality.
        piece.save(buf, "WEBP", quality=quality, method=0)
    else:
        piece.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


class Viewer:
    """What one browser tab has already received, so it is sent only the tiles it lacks."""

    TTL = 120.0

    def __init__(self):
        self.lock = threading.Lock()
        self.epoch = 0
        self.sent = None      # per tile: tile_seq value at the time the tile was last sent; -1: send again
        self.pending = None   # (req, tiles, values) of the last response; applied once acknowledged
        self.seen = 0         # seq of the last frame rendered for this viewer
        self.req = 0          # number of the latest request; older waiting requests give up
        self.last_seen = time.monotonic()

    def ack(self, req):
        # The page reports the request whose response it drew last. Any other pending response
        # never arrived: its tiles are sent again, and so is the frame (the cursor) it carried.
        # Requests, not frame seqs: the same frame can go out twice when the view moves.
        if self.pending:
            if self.pending[0] == req:
                _, tiles, values = self.pending
                self.sent[tiles] = values
            else:
                self.seen = 0
        self.pending = None

    def reset(self, epoch, tiles):
        self.epoch = epoch
        self.sent = np.zeros(tiles, np.int64)
        self.pending = None
        self.seen = 0

    def stale(self, frame, view_mask):
        """Tiles inside the view that this viewer has not seen in their current state."""
        if frame.epoch != self.epoch or self.sent is None:
            return view_mask
        return (frame.tile_seq > self.sent) & view_mask


class Viewers:
    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()

    def get(self, viewer_id):
        now = time.monotonic()
        with self._lock:
            viewer = self._items.get(viewer_id)
            if viewer is None:
                for key in [k for k, v in self._items.items() if now - v.last_seen > Viewer.TTL]:
                    del self._items[key]
                viewer = self._items[viewer_id] = Viewer()
            viewer.last_seen = now
            return viewer


class Grabber(threading.Thread):
    """Single capture loop shared by all viewers; idles when nobody is watching.

    The picture is split into tiles and every tile remembers the frame it last changed in,
    so each viewer gets only the tiles it has not seen yet, and only inside the area it shows.
    """

    def __init__(self, monitor_index, fps, max_width, draw_cursor, tile_size, capture):
        super().__init__(daemon=True)
        monitors = list_monitors()
        if not 0 <= monitor_index < len(monitors):
            raise SystemExit(f"monitor {monitor_index} not found, available: 0..{len(monitors) - 1}")
        if capture not in ("auto", "dxgi", "gdi"):
            raise SystemExit(f"capture must be auto, dxgi or gdi, not {capture!r}")
        self.monitor_index = monitor_index
        self.monitor = monitors[monitor_index]
        self.interval = 1.0 / max(1, fps)
        self.max_width = max_width
        self.draw_cursor = draw_cursor
        self.tile_size = max(16, tile_size)
        self.capture_mode = capture
        self.backend = "-"
        self._capture = None
        self._dxgi_retry_at = 0.0
        self._last_change = 0.0
        self._waiting = 0
        self._last_request = 0.0
        self._frame = None
        self._cond = threading.Condition()
        self.heartbeat = time.monotonic()

    def set_monitor(self, index):
        monitors = list_monitors()  # re-read: resolution or layout may have changed since start
        if not 0 <= index < len(monitors):
            raise ValueError(f"monitor {index} not found")
        with self._cond:
            self.monitor_index = index
            self.monitor = monitors[index]

    # -- capture loop (its own thread; mss and DXGI objects are used from here only)

    def run(self):
        while True:
            self.heartbeat = time.monotonic()
            try:
                self._step()
            except Exception as e:  # noqa: BLE001 - keep the loop alive whatever the screen does
                log(f"capture error: {e}")
                try:
                    self._close_capture()
                except Exception:  # noqa: BLE001 - a broken capture object is simply dropped
                    self._capture = None
                time.sleep(1)

    def _step(self):
        if not self._active():
            if self._capture and time.monotonic() - self._last_request > CAPTURE_IDLE:
                self._close_capture()
            time.sleep(0.2)
            return
        started = time.monotonic()
        with self._cond:
            monitor, index = self.monitor, self.monitor_index
        buf = self._grab(monitor)
        if buf is not None:
            img = self._to_image(buf)
            self._update(img, self._cursor(monitor, img.width / monitor["width"]), monitor, index)
        elif self._frame is not None and self._frame.monitor is monitor:
            img = self._frame.img
            self._idle(self._cursor(monitor, img.width / monitor["width"]), monitor, index)
        time.sleep(max(0.0, self.interval - (time.monotonic() - started)))

    def healthy(self):
        """The capture thread runs and is not stuck; otherwise every page shows a frozen picture."""
        return self.is_alive() and time.monotonic() - self.heartbeat < HEALTH_STALL

    def _want_dxgi(self):
        return (dxcam is not None and IS_WINDOWS and self.capture_mode != "gdi"
                and time.monotonic() >= self._dxgi_retry_at)

    def _close_capture(self):
        if self._capture:
            self._capture.close()
            self._capture = None

    def _open_capture(self, monitor):
        if self._want_dxgi():
            try:
                return DxgiCapture(monitor)
            except Exception as e:  # noqa: BLE001 - any failure means "not now"
                self._dxgi_retry_at = time.monotonic() + DXGI_RETRY
                log(f"DXGI capture unavailable ({e}); using GDI")
        elif self.capture_mode == "dxgi" and dxcam is None:
            log("capture=dxgi but dxcam is not installed; using GDI")
        return GdiCapture(monitor)

    def _grab(self, monitor):
        cap = self._capture
        if cap and (cap.monitor is not monitor or (isinstance(cap, GdiCapture) and self._want_dxgi())):
            self._close_capture()
            cap = None
        if cap is None:
            cap = self._capture = self._open_capture(monitor)
            self.backend = cap.name
        try:
            return cap.grab()
        except Exception as e:  # noqa: BLE001 - lock screen, UAC, display change ...
            self._close_capture()
            if not isinstance(cap, DxgiCapture):
                raise
            self._dxgi_retry_at = time.monotonic() + DXGI_RETRY
            log(f"DXGI capture failed ({e}); using GDI for the next {DXGI_RETRY:.0f} s")
            return None

    def _to_image(self, buf):
        h, w = buf.shape[:2]
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        if self.max_width and w > self.max_width:
            # reducing_gap: an integer-factor reduce first, then the exact resize; twice as fast.
            img = img.resize((self.max_width, round(h * self.max_width / w)), Image.BILINEAR,
                             reducing_gap=1.0)
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

    def _changed_tiles(self, prev, arr, cols, rows):
        """Boolean per tile: any pixel differs. Rows are compared as flat byte rows."""
        ts = self.tile_size
        h, w = arr.shape[:2]
        diff = prev.reshape(h, -1) != arr.reshape(h, -1)
        if h != rows * ts or w != cols * ts:
            padded = np.zeros((rows * ts, cols * ts * 3), bool)
            padded[:h, :w * 3] = diff
            diff = padded
        return diff.reshape(rows, ts, cols, ts * 3).any(axis=(1, 3)).ravel()

    def _update(self, img, cursor, monitor, index):
        prev = self._frame  # only this thread replaces it
        seq = prev.seq + 1 if prev else 1
        arr = np.asarray(img)
        if prev is None or prev.monitor is not monitor or prev.img.size != img.size:
            cols = -(-img.width // self.tile_size)
            rows = -(-img.height // self.tile_size)
            n = cols * rows
            frame = Frame(random.getrandbits(30) | 1, seq, monitor, index, img, arr, cols, rows,
                          np.full(n, seq, np.int64), np.zeros(n, bool), cursor)
        else:
            changed = self._changed_tiles(prev.arr, arr, prev.cols, prev.rows)
            if not changed.any():
                self._idle(cursor, monitor, index)
                return
            tile_seq = prev.tile_seq.copy()
            tile_seq[changed] = seq
            frame = Frame(prev.epoch, seq, monitor, index, img, arr, prev.cols, prev.rows,
                          tile_seq, prev.tile_hq & ~changed, cursor)
        self._last_change = time.monotonic()
        self._publish(frame)

    def _idle(self, cursor, monitor, index):
        """Nothing changed on the screen: maybe the pointer moved, maybe it is time to refine."""
        prev = self._frame
        refine = time.monotonic() - self._last_change >= REFINE_DELAY and not prev.tile_hq.all()
        if not refine and cursor == prev.cursor:
            return  # a still screen costs no bandwidth at all
        tile_seq, tile_hq = prev.tile_seq, prev.tile_hq
        if refine:
            tile_seq = tile_seq.copy()
            tile_seq[~tile_hq] = prev.seq + 1
            tile_hq = np.ones_like(tile_hq)
        self._publish(Frame(prev.epoch, prev.seq + 1, monitor, index, prev.img, prev.arr,
                            prev.cols, prev.rows, tile_seq, tile_hq, cursor))

    def _publish(self, frame):
        with self._cond:
            self._frame = frame
            self._cond.notify_all()

    # -- viewer side (request threads)

    def _active(self):
        # Viewers poll frame by frame, so "someone is watching" means a request is waiting now
        # or arrived a moment ago (the gap while the phone downloads and draws a frame).
        return self._waiting > 0 or time.monotonic() - self._last_request < 3.0

    def view_mask(self, frame, view):
        """Tiles inside the area the page shows (plus a one-tile margin); all tiles without a view."""
        n = frame.cols * frame.rows
        if view is None:
            return np.ones(n, bool)
        x, y, w, h = view
        ts = self.tile_size
        mask = np.zeros((frame.rows, frame.cols), bool)
        mask[max(0, y // ts - 1):(y + h) // ts + 2, max(0, x // ts - 1):(x + w) // ts + 2] = True
        return mask.ravel() if mask.any() else np.ones(n, bool)

    def _news(self, viewer, view):
        frame = self._frame
        return frame is not None and (frame.epoch != viewer.epoch or frame.seq != viewer.seen
                                      or viewer.stale(frame, self.view_mask(frame, view)).any())

    def wait_frame(self, viewer, req, view, timeout=LONG_POLL):
        """Blocks until the viewer lacks something (or a newer request replaces this one); None on timeout."""
        with self._cond:
            self._waiting += 1
            self._last_request = time.monotonic()
            try:
                ready = self._cond.wait_for(lambda: viewer.req != req or self._news(viewer, view),
                                            timeout=timeout)
                return self._frame if ready else None
            finally:
                self._waiting -= 1
                self._last_request = time.monotonic()

    def _tiles_bbox(self, mask, frame):
        grid = mask.reshape(frame.rows, frame.cols)
        rows = np.flatnonzero(grid.any(axis=1))
        cols = np.flatnonzero(grid.any(axis=0))
        ts = self.tile_size
        return (int(cols[0]) * ts, int(rows[0]) * ts,
                min((int(cols[-1]) + 1) * ts, frame.img.width), min((int(rows[-1]) + 1) * ts, frame.img.height))

    def render_update(self, frame, viewer, view, preset, req):
        """Binary update: 4-byte header length, JSON header, then the picture pieces back to back."""
        img = frame.img
        if frame.epoch != viewer.epoch:
            viewer.reset(frame.epoch, frame.tile_seq.size)
        mask = self.view_mask(frame, view)
        stale = viewer.stale(frame, mask)
        tiles = np.flatnonzero(stale)
        values = frame.tile_seq[tiles]
        low, high = QUALITY[preset]
        pieces = []
        if tiles.size > np.count_nonzero(mask) * FULL_FRAME_SHARE:
            tiles = np.flatnonzero(mask)  # the whole visible area goes as one picture
            box = self._tiles_bbox(mask, frame)
            piece = img if box == (0, 0, img.width, img.height) else img.crop(box)
            # Sharp when most of what the page lacks is sharp (a new viewer, a big refine);
            # quick and low while most of it is moving.
            crisp = np.count_nonzero(stale & frame.tile_hq) >= np.count_nonzero(stale & ~frame.tile_hq)
            pieces.append((box[0], box[1], encode(piece, high if crisp else low, crisp)))
            values = frame.tile_seq[tiles]
            if not crisp:
                # The picture also covered tiles the page had sharp. They go again, sharp, once the
                # big change is over; until then every such picture would cover them anyway.
                values = np.where(frame.tile_hq[tiles], -1, values)
        elif tiles.size:
            for part, quality, crisp in ((stale & frame.tile_hq, high, True),
                                         (stale & ~frame.tile_hq, low, False)):
                if part.any():
                    for r in tile_rects(part, frame.cols, frame.rows, self.tile_size, img.width, img.height):
                        pieces.append((r[0], r[1], encode(img.crop(r), quality, crisp)))
        viewer.pending = (req, tiles, values)
        viewer.seen = frame.seq
        header = json.dumps({
            "epoch": frame.epoch, "req": req, "monitor": frame.monitor_index,
            "width": img.width, "height": img.height, "cursor": frame.cursor,
            "capture": self.backend,
            "rects": [[x, y, len(data)] for x, y, data in pieces],
        }).encode()
        return b"".join([len(header).to_bytes(4, "big"), header, *(data for _, _, data in pieces)])


grabber = Grabber(cfg["monitor"], cfg["fps"], cfg["max_width"], cfg["draw_cursor"], cfg["tile_size"],
                  cfg["capture"])
viewers = Viewers()
mouse = Mouse()
keyboard = Keyboard()
clipboard = Clipboard()

# ---------------------------------------------------------------- web

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024


AUTH_COOKIE = "screensender_auth"
AUTH_COOKIE_AGE = 30 * 24 * 3600
PUBLIC_PATHS = {"/login", "/cert", "/health"}


def load_secret():
    # Mixed into the login cookie, so a stolen cookie is no password hash to crack offline.
    # Deleting the file and restarting the server logs out every device.
    if not SECRET_PATH.exists() or len(SECRET_PATH.read_bytes()) < 32:
        SECRET_PATH.write_bytes(secrets.token_bytes(32))
    return SECRET_PATH.read_bytes()


SECRET = load_secret()


def auth_token():
    # Changing the password in config logs everyone out as well.
    key = SECRET + f"{cfg['username']}\0{cfg['password']}".encode()
    return hmac.new(key, b"screensender-auth", "sha256").hexdigest()


def set_auth_cookie(resp):
    resp.set_cookie(AUTH_COOKIE, auth_token(), max_age=AUTH_COOKIE_AGE,
                    httponly=True, samesite="Lax", secure=bool(cfg["https"]))
    return resp


def input_enabled():
    return bool(cfg["allow_input"] and IS_WINDOWS)


@app.before_request
def require_auth():
    if request.path in PUBLIC_PATHS:
        return None
    if hmac.compare_digest(request.cookies.get(AUTH_COOKIE, "").encode(), auth_token().encode()):
        return None
    if request.path == "/":
        return redirect("/login")
    return jsonify(error="unauthorized"), 401


@app.get("/login")
def login_page():
    return send_from_directory(BASE_DIR / "static", "login.html")


@app.post("/login")
def login():
    # Compared as bytes: compare_digest rejects str with non-ASCII characters.
    ok = (hmac.compare_digest(request.form.get("username", "").encode(), cfg["username"].encode())
          & hmac.compare_digest(request.form.get("password", "").encode(), cfg["password"].encode()))
    if not ok:
        time.sleep(1)  # slows down password guessing
        return redirect("/login?error=1")
    return set_auth_cookie(redirect("/"))


@app.get("/logout")
def logout():
    resp = redirect("/login")
    resp.delete_cookie(AUTH_COOKIE)
    return resp


@app.get("/cert")
def certificate():
    """The self-signed certificate, to install on the phone so the browser trusts the server."""
    if not (cfg["https"] and CERT_PATH.exists()):
        return jsonify(error="https is not enabled"), 404
    return send_file(CERT_PATH, as_attachment=True, download_name="screensender.crt",
                     mimetype="application/x-x509-ca-cert")


@app.get("/health")
def health():
    """For the supervisor and the update check: 503 when the capture loop has died or hangs."""
    if grabber.healthy():
        return jsonify(ok=True)
    return jsonify(ok=False, error="the capture loop is not running"), 503


@app.get("/")
def index():
    # Every visit starts the 30 days again, so a PC in regular use never asks for the password.
    return set_auth_cookie(send_from_directory(BASE_DIR / "static", "index.html"))


@app.get("/info")
def info():
    monitors = list_monitors()
    # Cycle order for the page: each monitor, then all of them together (0) if there are several.
    order = list(range(1, len(monitors))) + ([0] if len(monitors) > 2 else [])
    return jsonify(input=input_enabled(), monitor=grabber.monitor_index, capture=grabber.backend,
                   https=bool(cfg["https"]),
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
    """Next update for one viewer.

    v: viewer id, r: request number, ack: r of the last update the page drew,
    view: x,y,w,h of the visible part in picture pixels (omitted = everything), q: quality preset.
    The page asks for the next update only after drawing the previous one, so at most one is
    ever in flight and a slow link lowers the frame rate instead of building up lag.
    """
    args = request.args
    viewer_id = args.get("v", "")
    if not 1 <= len(viewer_id) <= 64:
        return jsonify(error="viewer id required"), 400
    try:
        req, ack = int(args.get("r", 0)), int(args.get("ack", 0))
        view = tuple(int(p) for p in args["view"].split(",")) if args.get("view") else None
        if view and (len(view) != 4 or min(view) < 0):
            raise ValueError
    except ValueError:
        return jsonify(error="bad arguments"), 400
    preset = args.get("q") if args.get("q") in QUALITY else "normal"

    viewer = viewers.get(viewer_id)
    with viewer.lock:
        if req < viewer.req:
            return Response(status=204)  # already replaced by a newer request
        viewer.req = req
        viewer.ack(ack)
    snap = grabber.wait_frame(viewer, req, view)
    if snap is None:
        return Response(status=204)  # nothing new; the page simply asks again
    with viewer.lock:
        if viewer.req != req:
            return Response(status=204)
        body = grabber.render_update(snap, viewer, view, preset, req)
    return Response(body, mimetype="application/octet-stream", headers={"Cache-Control": "no-store"})


def point_from_request(data):
    """0..1 picture coordinates from the page -> screen pixels of the shown monitor."""
    nx, ny = float(data["x"]), float(data["y"])
    if not (0 <= nx <= 1 and 0 <= ny <= 1):
        raise ValueError("x and y must be in 0..1")
    mon = grabber.monitor
    return mon["left"] + round(nx * (mon["width"] - 1)), mon["top"] + round(ny * (mon["height"] - 1))


@app.post("/click")
def click():
    if not input_enabled():
        return jsonify(error="input disabled"), 403
    data = request.get_json(silent=True) or {}
    try:
        x, y = point_from_request(data)
    except (KeyError, TypeError, ValueError):
        return jsonify(error="x and y in 0..1 are required"), 400
    button = "right" if data.get("button") == "right" else "left"
    mods = data.get("mods") or []
    if not isinstance(mods, list) or any(m not in MODIFIERS for m in mods):
        return jsonify(error="bad mods"), 400
    keyboard.hold(mods)
    try:
        mouse.click(x, y, button)
    finally:
        keyboard.hold(mods, up=True)
    return jsonify(ok=True, x=x, y=y, button=button)


@app.post("/mouse")
def mouse_drag():
    """Body: {"actions": [{"drag": "down" | "move" | "up", "x", "y" (0..1)}, ...]}: a left-button drag."""
    if not input_enabled():
        return jsonify(error="input disabled"), 403
    actions = (request.get_json(silent=True) or {}).get("actions")
    if not isinstance(actions, list) or len(actions) > 100:
        return jsonify(error="actions list is required"), 400
    try:
        steps = [(a["drag"], *point_from_request(a)) for a in actions]
    except (KeyError, TypeError, ValueError):
        return jsonify(error="every action needs drag and x, y in 0..1"), 400
    if any(action not in ("down", "move", "up") for action, _, _ in steps):
        return jsonify(error="drag must be down, move or up"), 400
    for action, x, y in steps:
        mouse.drag(action, x, y)
    return jsonify(ok=True)


MAX_SCROLL = 50


@app.post("/scroll")
def scroll():
    """Body: {"x", "y" (0..1), "dy", "dx"}: wheel notches; positive dy scrolls towards the top."""
    if not input_enabled():
        return jsonify(error="input disabled"), 403
    data = request.get_json(silent=True) or {}
    try:
        x, y = point_from_request(data)
        dy, dx = int(data.get("dy", 0)), int(data.get("dx", 0))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="x and y in 0..1 are required"), 400
    if max(abs(dy), abs(dx)) > MAX_SCROLL:
        return jsonify(error="too many notches"), 400
    if dy or dx:
        mouse.wheel(x, y, dy, dx)
    return jsonify(ok=True)


MAX_TEXT = 2000
MAX_PASTE = 1_000_000


@app.post("/key")
def key():
    """Body: {"actions": [{"text": "..."} | {"paste": "..."} | {"key": "enter", "mods": ["ctrl"], "repeat": 1}, ...]}"""
    if not input_enabled():
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


# ---------------------------------------------------------------- startup

def lan_ips():
    """This PC's addresses in its local network: those of the interfaces with the default route.

    Asking which address reaches the internet would give a VPN's: one that takes all traffic adds
    0.0.0.0/1 and 128.0.0.0/1 routes, but the real 0.0.0.0/0 stays on the local network card.
    """
    if IS_WINDOWS:
        try:
            out = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True, text=True,
                                 timeout=5, creationflags=subprocess.CREATE_NO_WINDOW).stdout
            # Destination, netmask, gateway, interface, metric; numbers, whatever the language.
            ips = [f[3] for f in (line.split() for line in out.splitlines())
                   if len(f) == 5 and f[0] == f[1] == "0.0.0.0"]
            if ips:
                return list(dict.fromkeys(ips))
        except Exception:  # noqa: BLE001 - fall back to asking the network stack
            pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return [s.getsockname()[0]]
    except OSError:
        return ["127.0.0.1"]


TAILSCALE_EXE = r"C:\Program Files\Tailscale\tailscale.exe"


def tailscale_addresses():
    """This PC's Tailscale IP and name when Tailscale runs here: a way in without port forwarding."""
    exe = shutil.which("tailscale") or (TAILSCALE_EXE if Path(TAILSCALE_EXE).exists() else None)
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        me = json.loads(r.stdout)["Self"]
    except Exception:  # noqa: BLE001 - stopped, logged out, or an output we do not understand
        return []
    ips = [ip for ip in me.get("TailscaleIPs") or [] if ":" not in ip]  # IPv4 is enough
    name = (me.get("DNSName") or "").rstrip(".")
    return ips + ([name] if name else [])


def ensure_certificate(hosts):
    """Self-signed certificate for HTTPS, made once and reused; a new one when it has expired.

    It is also a CA certificate, so it can be installed on the phone as a trusted root and the
    browser then opens the page without warnings.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(timezone.utc)
    if CERT_PATH.exists() and KEY_PATH.exists():
        cert = x509.load_pem_x509_certificate(CERT_PATH.read_bytes())
        if cert.not_valid_after_utc > now + timedelta(days=1):
            return
        log("The certificate has expired, making a new one")
    names = [x509.DNSName("localhost")]
    for host in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ScreenSender")])
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=800))  # iOS refuses server certificates valid longer
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    KEY_PATH.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log(f"Made a new certificate: {CERT_PATH}")


def quiet_logs():
    # Our own startup lines replace the werkzeug banner (it lists only local interfaces),
    # and per-request lines are noise at ten frames a second.
    flask.cli.show_server_banner = lambda *args, **kwargs: None
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("dxcam").setLevel(logging.ERROR)
    logging.getLogger("comtypes").setLevel(logging.ERROR)


class _Handler(WSGIRequestHandler):
    # A client that connects and then says nothing (port scanners do) gives up its
    # thread after this long instead of keeping it forever.
    timeout = 60


def serve(host, port, ssl_files):
    """app.run(), except for HTTPS: werkzeug wraps the listening socket, so the TLS
    handshake runs inside accept(), on the one thread that accepts everyone. A single
    port scanner that connects and sends nothing stalls the whole server for good.
    Here the handshake waits for the connection's first read, in its own thread,
    under _Handler.timeout."""
    if IS_WINDOWS:
        # There SO_REUSEADDR lets a second copy bind the same port without an error, and then
        # either copy may answer. Without it a restart still rebinds at once.
        BaseWSGIServer.allow_reuse_address = False
    server = make_server(host, port, app, threaded=True, request_handler=_Handler)
    if ssl_files:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*ssl_files)
        server.socket = context.wrap_socket(server.socket, server_side=True,
                                            do_handshake_on_connect=False)
        server.ssl_context = context        # werkzeug reads it for wsgi.url_scheme
    server.serve_forever()


if __name__ == "__main__":
    port = cfg["port"]
    scheme = "https" if cfg["https"] else "http"
    lan = lan_ips()
    ip = lan[0]
    tailnet = tailscale_addresses()
    ssl_context = None
    if cfg["https"]:
        ensure_certificate([h for h in (*lan, cfg["public_ip"], "127.0.0.1", *tailnet) if h])
        ssl_context = (str(CERT_PATH), str(KEY_PATH))
    mon = grabber.monitor
    log(f"Capturing monitor {cfg['monitor']}: {mon['width']}x{mon['height']} at ({mon['left']},{mon['top']}), "
        f"capture={cfg['capture']}" + ("" if dxcam or cfg["capture"] == "gdi" else " (dxcam not installed: GDI)"))
    for host in lan:
        say(f"  LAN:      {scheme}://{host}:{port}/")
    for host in tailnet:
        say(f"  Tailscale: {scheme}://{host}:{port}/")
    if cfg["public_ip"]:
        say(f"  Internet: {scheme}://{cfg['public_ip']}:{port}/")
    else:
        say("  Internet: public_ip is not set in config.json")
    if cfg["https"]:
        say(f"  Certificate for the phone: {scheme}://{ip}:{port}/cert")
    if cfg["password"] == DEFAULT_CONFIG["password"]:
        say("  WARNING: default password in config.json - change it before exposing the port!")
    # Not the password: under the supervisor this output goes to runtime/server.log.
    say(f"  Login:    {cfg['username']} (the password is in config.json)")
    say("Press Ctrl+C to stop")
    quiet_logs()
    grabber.start()
    serve(cfg["host"], port, ssl_context)
