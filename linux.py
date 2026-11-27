"""The Linux side of ScreenSender (Raspberry Pi OS and other Linux PCs): the screen and the input.

server.py imports it only on Linux. The screen is whichever of these is there, looked for again
on every layout check, so a desktop that comes up after the server is still found:

  wayland  a wlroots compositor (labwc, wayfire, sway): grim takes the picture, wlr-randr lists outputs
  x11      an X server: mss, as on Windows
  fb       no desktop, a program drawing straight into /dev/fb0 (Qt's linuxfb, the text console)

Mouse and keys go through /dev/uinput: a virtual mouse and keyboard that all three read.
Text in any language goes through wtype (Wayland) or xdotool (X11); without them only ASCII is
typed, as US-layout keys. On X11 without uinput access, xdotool does all of the input.
"""

import ctypes
import ctypes.util
import fcntl
import glob
import os
import re
import shutil
import stat
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import mss
import numpy as np

MSS = getattr(mss, "MSS", None) or mss.mss  # mss 10 renamed the class

FB_DEVICE = "/dev/fb0"
UINPUT = "/dev/uinput"
TOOL_TIMEOUT = 10
# Shown when there is no screen at all, so the server still starts and answers /health.
NO_SCREEN = {"left": 0, "top": 0, "width": 640, "height": 360}


def log(text):
    """server.py replaces it with its own timestamped log."""
    print(text, flush=True)


# ---------------------------------------------------------------- which screen

_session = None
_layout = dict(NO_SCREEN)   # the whole desktop, as the last monitors() call saw it
_lock = threading.Lock()


def _is_socket(path):
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _runtime_dir():
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def _wayland_display():
    rd = _runtime_dir()
    name = os.environ.get("WAYLAND_DISPLAY")
    if name and _is_socket(name if name.startswith("/") else os.path.join(rd, name)):
        return name
    for path in sorted(glob.glob(os.path.join(rd, "wayland-*"))):
        if not path.endswith(".lock") and _is_socket(path):
            return os.path.basename(path)
    return None


def _x_display():
    name = os.environ.get("DISPLAY", "")
    m = re.fullmatch(r":(\d+)(\.\d+)?", name)
    if name and not m:
        return name  # a remote display: taken on trust
    if m and _is_socket(f"/tmp/.X11-unix/X{m.group(1)}"):
        return name
    for path in sorted(glob.glob("/tmp/.X11-unix/X*")):
        if _is_socket(path):
            return ":" + path.rsplit("X", 1)[1]
    return None


def session():
    """'wayland', 'x11', 'fb' or None. Sets the variables grim, wtype and xdotool look for.

    Wayland first: Xwayland makes an X socket too, but X clients see only its own windows there.
    """
    global _session
    with _lock:
        found = None
        wl = _wayland_display()
        if wl:
            os.environ["WAYLAND_DISPLAY"] = wl
            os.environ.setdefault("XDG_RUNTIME_DIR", _runtime_dir())
            found = "wayland"
        else:
            x = _x_display()
            if x:
                os.environ["DISPLAY"] = x
                auth = Path.home() / ".Xauthority"
                if "XAUTHORITY" not in os.environ and auth.exists():
                    os.environ["XAUTHORITY"] = str(auth)
                found = "x11"
            elif os.access(FB_DEVICE, os.R_OK):
                found = "fb"
        if found != _session:
            what = {"wayland": f"Wayland ({os.environ.get('WAYLAND_DISPLAY')})",
                    "x11": f"X11 ({os.environ.get('DISPLAY')})",
                    "fb": f"the framebuffer ({FB_DEVICE})"}.get(found, "no screen (no Wayland, X11 or framebuffer)")
            log(f"screen: {what}")
            _session = found
        return found


def current_session():
    """The session as the last check found it; cheap, for every frame."""
    return _session


def _tool(name):
    return shutil.which(name)


def _run(cmd, data=None, timeout=TOOL_TIMEOUT):
    r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"{cmd[0]}: {(r.stderr or r.stdout).decode(errors='replace').strip() or r.returncode}")
    return r.stdout


# ---------------------------------------------------------------- monitors

def monitors():
    """Like mss: [the whole desktop, monitor 1, monitor 2, ...] as dicts of left, top, width, height."""
    global _layout
    s = session()
    try:
        if s == "x11":
            with MSS() as sct:
                mons = [dict(m) for m in sct.monitors]
        elif s == "wayland":
            mons = _wayland_monitors()
        elif s == "fb":
            info = fb_info()
            mons = [{"left": 0, "top": 0, "width": info.width, "height": info.height}]
            mons.insert(0, dict(mons[0]))
        else:
            mons = [dict(NO_SCREEN), dict(NO_SCREEN)]
    except Exception as e:  # noqa: BLE001 - the screen went away between the check and the call
        log(f"cannot list monitors ({s}): {e}")
        mons = [dict(_layout), dict(_layout)]
    _layout = dict(mons[0])
    return mons


def _union(outs):
    left, top = min(m["left"] for m in outs), min(m["top"] for m in outs)
    right = max(m["left"] + m["width"] for m in outs)
    bottom = max(m["top"] + m["height"] for m in outs)
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


ROTATED = {"90", "270", "flipped-90", "flipped-270"}
_grim_size = (0.0, None)
_randr_problem = None


def parse_wlr_randr(text):
    """Enabled outputs from wlr-randr's text output (its --json is missing from older versions):

    HDMI-A-1 "Some Monitor"
      Enabled: yes
      Modes:
        1920x1080 px, 60.000000 Hz (preferred, current)
      Position: 0,0
      Transform: normal
      Scale: 1.000000
    """
    outs, o = [], None
    for line in text.splitlines() + [""]:
        if line and not line[0].isspace() or not line:  # an output starts (or the text ends)
            if o and o.get("enabled", True) and "mode" in o:
                w, h = o["mode"]
                if o.get("transform", "normal") in ROTATED:
                    w, h = h, w
                scale = o.get("scale", 1.0) or 1.0
                outs.append({"left": o.get("x", 0), "top": o.get("y", 0),
                             "width": round(w / scale), "height": round(h / scale)})
            o = {} if line else None
            continue
        if o is None:
            continue
        key, _, value = line.strip().partition(":")
        value = value.strip()
        if key == "Enabled":
            o["enabled"] = value == "yes"
        elif key == "Position":
            o["x"], o["y"] = (int(v) for v in value.split(","))
        elif key == "Transform":
            o["transform"] = value
        elif key == "Scale":
            o["scale"] = float(value)
        else:
            m = re.match(r"\s*(\d+)x(\d+) px.*\bcurrent\b", line)
            if m:
                o["mode"] = int(m.group(1)), int(m.group(2))
    return outs


def _wayland_monitors():
    global _grim_size, _randr_problem
    outs = []
    if _tool("wlr-randr"):
        try:
            outs = parse_wlr_randr(_run(["wlr-randr"]).decode(errors="replace"))
        except Exception as e:  # noqa: BLE001 - then one picture of everything
            if str(e) != _randr_problem:
                log(f"wlr-randr: {e}")
                _randr_problem = str(e)
    if not outs:
        # Without the output list the whole desktop is one monitor, as big as grim's picture.
        t, size = _grim_size
        if size is None or time.monotonic() - t > 30:
            w, h, _ = _ppm_header(_run(["grim", "-t", "ppm", "-"]))
            _grim_size = (time.monotonic(), (w, h))
        w, h = _grim_size[1]
        outs = [{"left": 0, "top": 0, "width": w, "height": h}]
    outs.sort(key=lambda m: (m["left"], m["top"]))
    return [_union(outs)] + outs


# ---------------------------------------------------------------- capture

class _Capture:
    session = None

    def stale(self):
        """The screen changed kind (the console gave way to a desktop): open the capture anew."""
        return current_session() != self.session

    def close(self):
        pass


class X11Capture(_Capture):
    name = "X11"
    session = "x11"

    def __init__(self, monitor):
        self.monitor = monitor
        self._sct = MSS()

    def grab(self):
        shot = self._sct.grab(self.monitor)
        return np.frombuffer(shot.bgra, np.uint8).reshape(shot.height, shot.width, 4)

    def close(self):
        # Once only: when the X server has gone, mss frees the connection and then raises, so a
        # second close would free it again and abort the whole process.
        sct, self._sct = self._sct, None
        if sct is not None:
            try:
                sct.close()
            except Exception:  # noqa: BLE001 - the connection is gone either way
                pass


def _ppm_header(data):
    m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s", data)
    if not m or m.group(3) != b"255":
        raise RuntimeError("grim returned no PPM picture")
    return int(m.group(1)), int(m.group(2)), m.end()


def ppm_to_array(data):
    w, h, start = _ppm_header(data)
    return np.frombuffer(data, np.uint8, count=w * h * 3, offset=start).reshape(h, w, 3)


class WaylandCapture(_Capture):
    """grim, once per frame: a wlroots compositor (labwc, wayfire, sway) hands it the picture."""

    name = "Wayland"
    session = "wayland"

    def __init__(self, monitor, cursor=True):
        if not _tool("grim"):
            raise RuntimeError("grim is not installed (sudo apt install grim)")
        self.monitor = monitor
        self._cursor = ["-c"] if cursor else []
        self._last = None

    def grab(self):
        m = self.monitor
        data = _run(["grim", *self._cursor, "-t", "ppm",
                     "-g", f"{m['left']},{m['top']} {m['width']}x{m['height']}", "-"])
        if data == self._last:
            return None  # the same picture: nothing to scale and compare
        self._last = data
        return ppm_to_array(data)


FBIOGET_VSCREENINFO = 0x4600


@dataclass(frozen=True)
class FbInfo:
    width: int
    height: int
    yoffset: int
    bpp: int
    red: int      # bit offsets of the colors in a pixel
    blue: int
    stride: int   # bytes per line


def fb_info(path=FB_DEVICE):
    with open(path, "rb") as f:
        buf = bytearray(160)  # struct fb_var_screeninfo
        fcntl.ioctl(f.fileno(), FBIOGET_VSCREENINFO, buf)
    (xres, yres, _xv, _yv, _xoff, yoff, bpp, _gray, red, _rl, _rm, _green, _gl, _gm,
     blue, _bl, _bm) = struct.unpack_from("17I", buf)
    name = os.path.basename(os.path.realpath(path))
    try:
        stride = int(Path(f"/sys/class/graphics/{name}/stride").read_text())
    except (OSError, ValueError):
        stride = xres * bpp // 8
    return FbInfo(xres, yres, yoff, bpp, red, blue, stride)


def fb_to_array(raw, info):
    """The framebuffer's bytes as (h, w, 4) BGRX or (h, w, 3) RGB, as the grabber takes them."""
    h, w = info.height, info.width
    if info.bpp == 32:
        px = np.frombuffer(raw, np.uint8).reshape(h, info.stride)[:, :w * 4].reshape(h, w, 4)
        return px if info.blue == 0 else np.ascontiguousarray(px[..., :3])  # bytes R, G, B, X: as RGB
    if info.bpp == 24:
        px = np.frombuffer(raw, np.uint8).reshape(h, info.stride)[:, :w * 3].reshape(h, w, 3)
        return np.ascontiguousarray(px[..., ::-1]) if info.blue == 0 else px
    if info.bpp == 16:  # RGB565: the Pi's console and Qt's linuxfb
        px = np.frombuffer(raw, "<u2").reshape(h, info.stride // 2)[:, :w]
        if info.red == 0:  # BGR565
            px = ((px & 0x1F) << 11) | (px & 0x7E0) | (px >> 11)
        out = np.empty((h, w, 3), np.uint8)
        r, g, b = (px >> 11).astype(np.uint8), ((px >> 5) & 0x3F).astype(np.uint8), (px & 0x1F).astype(np.uint8)
        out[..., 0] = (r << 3) | (r >> 2)
        out[..., 1] = (g << 2) | (g >> 4)
        out[..., 2] = (b << 3) | (b >> 2)
        return out
    raise RuntimeError(f"{info.bpp} bits per pixel in the framebuffer is not supported")


class FbCapture(_Capture):
    """/dev/fb0: what a program without a desktop draws (Qt's linuxfb), or the text console.

    Programs drawing through DRM/KMS directly (Qt's eglfs, a Wayland compositor) do not show here.
    """

    name = "framebuffer"
    session = "fb"

    def __init__(self, monitor, path=FB_DEVICE):
        self.monitor = monitor
        self.info = fb_info(path)
        self._fd = os.open(path, os.O_RDONLY)
        self._last = None

    def grab(self):
        i = self.info
        size, offset = i.stride * i.height, i.stride * i.yoffset
        chunks, got = [], 0
        while got < size:
            chunk = os.pread(self._fd, size - got, offset + got)
            if not chunk:
                raise RuntimeError("the framebuffer is smaller than it says")
            chunks.append(chunk)
            got += len(chunk)
        raw = b"".join(chunks) if len(chunks) > 1 else chunks[0]
        if raw == self._last:
            return None
        self._last = raw
        return fb_to_array(raw, i)

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def open_capture(monitor, cursor=True):
    """cursor: on Wayland the compositor draws the pointer into the picture (see cursor_pos)."""
    s = current_session() or session()
    if s == "x11":
        return X11Capture(monitor)
    if s == "wayland":
        return WaylandCapture(monitor, cursor)
    if s == "fb":
        return FbCapture(monitor)
    raise RuntimeError("no screen: no Wayland, X11 or framebuffer")


# ---------------------------------------------------------------- the pointer's position

class _XPointer:
    """XQueryPointer through Xlib, on the capture thread only."""

    def __init__(self):
        self._x = None
        self._dpy = None
        self._display = None

    def pos(self):
        display = os.environ.get("DISPLAY")
        if display != self._display:
            self._open(display)
        if not self._dpy:
            return None
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        rx, ry, wx, wy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()
        x = self._x
        if not x.XQueryPointer(self._dpy, x.XDefaultRootWindow(self._dpy), ctypes.byref(root), ctypes.byref(child),
                               ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy),
                               ctypes.byref(mask)):
            return None
        return rx.value, ry.value

    def _open(self, display):
        if self._x is None:
            lib = ctypes.util.find_library("X11")
            if not lib:
                self._display = display
                return
            x = self._x = ctypes.CDLL(lib)
            x.XOpenDisplay.restype = ctypes.c_void_p
            x.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x.XDefaultRootWindow.restype = ctypes.c_ulong
            x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            x.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_void_p] * 7
        if self._dpy:
            self._x.XCloseDisplay(self._dpy)
        self._dpy = self._x.XOpenDisplay(display.encode()) if display else None
        self._display = display


_xpointer = _XPointer()


def cursor_pos():
    """The pointer in desktop pixels: asked from X; on the framebuffer, where uinput last put it.

    None on Wayland: a client cannot ask where the pointer is, and where uinput last put it is
    wrong once a real mouse moves it (and unknown until the first tap). grim -c draws the real
    pointer into the picture instead."""
    s = current_session()
    if s == "x11":
        try:
            return _xpointer.pos()
        except Exception:  # noqa: BLE001 - no Xlib: no pointer drawn
            return None
    if s == "wayland":
        return None
    return _pointer_at


_pointer_at = None


# ---------------------------------------------------------------- uinput

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
SYN_REPORT = 0
REL_HWHEEL, REL_WHEEL = 0x06, 0x08
ABS_X, ABS_Y = 0x00, 0x01
BTN = {"left": 0x110, "right": 0x111, "middle": 0x112}
BUS_VIRTUAL = 0x06
ABS_MAX = 32767


def _iow(nr, size):
    return (1 << 30) | (size << 16) | (ord("U") << 8) | nr


UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = _iow(3, 92)    # struct uinput_setup
UI_ABS_SETUP = _iow(4, 28)    # struct uinput_abs_setup
UI_SET_EVBIT = _iow(100, 4)
UI_SET_KEYBIT = _iow(101, 4)
UI_SET_RELBIT = _iow(102, 4)
UI_SET_ABSBIT = _iow(103, 4)
EVENT = struct.Struct("@llHHi")  # struct input_event: the time (the kernel fills it), type, code, value

# Linux key codes for the key names server.py knows (the same names as its Windows VK table).
KEY = {
    "esc": 1, "backspace": 14, "tab": 15, "enter": 28, "ctrl": 29, "shift": 42, "alt": 56, "space": 57,
    "home": 102, "up": 103, "pageup": 104, "left": 105, "right": 106, "end": 107, "down": 108,
    "pagedown": 109, "insert": 110, "delete": 111, "win": 125, "apps": 127,
    **{f"f{i}": 58 + i for i in range(1, 11)}, "f11": 87, "f12": 88,
    **dict(zip("1234567890", range(2, 12))),
    **dict(zip("qwertyuiop", range(16, 26))), **dict(zip("asdfghjkl", range(30, 39))),
    **dict(zip("zxcvbnm", range(44, 51))),
}
# ASCII as keys of the US layout: (key, shift).
_PUNCT = {"-": 12, "=": 13, "[": 26, "]": 27, ";": 39, "'": 40, "`": 41, "\\": 43, ",": 51, ".": 52, "/": 53}
_SHIFTED = dict(zip('!@#$%^&*()_+{}:"~|<>?', [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 26, 27, 39, 40, 41, 43, 51, 52, 53]))
ASCII_KEYS = {
    " ": (57, False), "\n": (28, False), "\t": (15, False),
    **{c: (KEY[c], False) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
    **{c.upper(): (KEY[c], True) for c in "abcdefghijklmnopqrstuvwxyz"},
    **{c: (k, False) for c, k in _PUNCT.items()},
    **{c: (k, True) for c, k in _SHIFTED.items()},
}


class _UinputDevice:
    def __init__(self, name, product, keys=(), rels=(), absolute=()):
        self.fd = os.open(UINPUT, os.O_WRONLY | os.O_NONBLOCK)
        try:
            for bit, setbit, codes in ((EV_KEY, UI_SET_KEYBIT, keys), (EV_REL, UI_SET_RELBIT, rels),
                                       (EV_ABS, UI_SET_ABSBIT, absolute)):
                if codes:
                    fcntl.ioctl(self.fd, UI_SET_EVBIT, bit)
                    for code in codes:
                        fcntl.ioctl(self.fd, setbit, code)
            for code in absolute:
                fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("=H2x6i", code, 0, 0, ABS_MAX, 0, 0, 0))
            fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack("=4H80sI", BUS_VIRTUAL, 0x5353, product, 1,
                                                           name.encode(), 0))
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except Exception:
            os.close(self.fd)
            raise

    def send(self, events):
        """events: (type, code, value); each one is followed by a report, as a real device does."""
        data = b"".join(EVENT.pack(0, 0, t, c, v) + EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0)
                        for t, c, v in events)
        os.write(self.fd, data)


class Uinput:
    """A virtual absolute mouse (like a VM's tablet) and a keyboard."""

    def __init__(self):
        # Two devices: one with both keys and axes may be taken for a joystick.
        self.mouse = _UinputDevice("ScreenSender pointer", 1, keys=list(BTN.values()),
                                   rels=[REL_WHEEL, REL_HWHEEL], absolute=[ABS_X, ABS_Y])
        self.keyboard = _UinputDevice("ScreenSender keyboard", 2, keys=range(1, 249))
        self._abs = (None, None)
        time.sleep(0.5)  # the desktop needs a moment to pick the new devices up

    def move(self, x, y):
        global _pointer_at
        lay = _layout
        ax = round((x - lay["left"]) * ABS_MAX / max(1, lay["width"] - 1))
        ay = round((y - lay["top"]) * ABS_MAX / max(1, lay["height"] - 1))
        ax, ay = min(max(ax, 0), ABS_MAX), min(max(ay, 0), ABS_MAX)
        events = []
        # The kernel drops a value equal to the last one, so after the real mouse moved the
        # pointer would stay where it is: a step aside first makes the move arrive.
        if ax == self._abs[0]:
            events.append((EV_ABS, ABS_X, ax - 1 if ax else 1))
        if ay == self._abs[1]:
            events.append((EV_ABS, ABS_Y, ay - 1 if ay else 1))
        events += [(EV_ABS, ABS_X, ax), (EV_ABS, ABS_Y, ay)]
        self.mouse.send(events)
        self._abs = (ax, ay)
        _pointer_at = (int(x), int(y))

    def button(self, name, down):
        self.mouse.send([(EV_KEY, BTN[name], int(down))])

    def wheel(self, dy, dx):
        self.mouse.send(([(EV_REL, REL_WHEEL, dy)] if dy else []) + ([(EV_REL, REL_HWHEEL, dx)] if dx else []))

    def keys(self, steps):
        """steps: (key name, down)."""
        self.keyboard.send([(EV_KEY, KEY[name], int(down)) for name, down in steps])

    def type_ascii(self, text):
        events, skipped = [], 0
        for ch in text.replace("\r\n", "\n"):
            if ch not in ASCII_KEYS:
                skipped += 1
                continue
            code, shift = ASCII_KEYS[ch]
            if shift:
                events.append((EV_KEY, KEY["shift"], 1))
            events += [(EV_KEY, code, 1), (EV_KEY, code, 0)]
            if shift:
                events.append((EV_KEY, KEY["shift"], 0))
        if events:
            self.keyboard.send(events)
        if skipped:
            raise RuntimeError(f"{skipped} characters not typed: only ASCII without wtype or xdotool")


# ---------------------------------------------------------------- xdotool (X11 without uinput)

XKEYSYM = {
    "ctrl": "Control_L", "alt": "Alt_L", "shift": "Shift_L", "win": "Super_L", "backspace": "BackSpace",
    "tab": "Tab", "enter": "Return", "esc": "Escape", "space": "space", "pageup": "Prior", "pagedown": "Next",
    "end": "End", "home": "Home", "left": "Left", "up": "Up", "right": "Right", "down": "Down",
    "insert": "Insert", "delete": "Delete", "apps": "Menu",
    **{f"f{i}": f"F{i}" for i in range(1, 13)}, **{c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
}


class Xdotool:
    XBUTTON = {"left": "1", "middle": "2", "right": "3"}

    def move(self, x, y):
        _run(["xdotool", "mousemove", "--", str(int(x)), str(int(y))])

    def button(self, name, down):
        _run(["xdotool", "mousedown" if down else "mouseup", self.XBUTTON[name]])

    def wheel(self, dy, dx):
        for n, (pos, neg) in ((dy, ("4", "5")), (dx, ("7", "6"))):
            if n:
                _run(["xdotool", "click", "--repeat", str(abs(n)), "--delay", "10", pos if n > 0 else neg])

    def keys(self, steps):
        cmd = ["xdotool"]
        for name, down in steps:
            cmd += ["keydown" if down else "keyup", XKEYSYM[name]]
        _run(cmd)


class Wtype:
    """Keys on Wayland without uinput. A modifier is let go when wtype exits, so it holds only
    within one call: Ctrl+V works, Shift+click does not."""

    MODS = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "win": "logo"}

    def keys(self, steps):
        cmd = ["wtype"]
        for name, down in steps:
            if name in self.MODS:
                cmd += ["-M" if down else "-m", self.MODS[name]]
            else:
                cmd += ["-P" if down else "-p", XKEYSYM[name]]
        _run(cmd)


# ---------------------------------------------------------------- what server.py uses

class Input:
    """Picks uinput when it can be opened, else xdotool on X11 (all input) or wtype on Wayland (keys)."""

    def __init__(self):
        self.uinput = None
        self.problem = None
        try:
            self.uinput = Uinput()
        except OSError as e:
            self.problem = (f"no access to {UINPUT} ({e.strerror}): run deploy/install.sh"
                            if e.errno in (13, 2) else f"{UINPUT}: {e}")
        except Exception as e:  # noqa: BLE001
            self.problem = f"{UINPUT}: {e}"

    def ready(self):
        """Some input works; without uinput on Wayland only keys do, and clicks get an error."""
        s = current_session()
        return bool(self.uinput or (s == "x11" and _tool("xdotool")) or (s == "wayland" and _tool("wtype")))

    def backend(self):
        if self.uinput:
            return self.uinput
        if current_session() == "x11" and _tool("xdotool"):
            return Xdotool()
        raise RuntimeError(self.problem or "no input on this screen")

    def key_backend(self):
        if not self.uinput and current_session() == "wayland" and _tool("wtype"):
            return Wtype()
        return self.backend()


class Pointer:
    """move, button, wheel for server.Mouse."""

    def __init__(self, inp):
        self.inp = inp

    def move(self, x, y):
        self.inp.backend().move(x, y)

    def button(self, name, down):
        self.inp.backend().button(name, down)

    def wheel(self, dy, dx):
        self.inp.backend().wheel(dy, dx)


class Keyboard:
    """The same calls as server.Keyboard on Windows."""

    def __init__(self, inp):
        self.inp = inp
        self._lock = threading.Lock()

    def type_text(self, text):
        text = text.replace("\r\n", "\n")
        s = current_session()
        with self._lock:
            if s == "wayland" and _tool("wtype"):
                # Line by line: wtype types a line break as a character, not as Enter.
                for i, line in enumerate(text.split("\n")):
                    if i:
                        self.inp.key_backend().keys([("enter", True), ("enter", False)])
                    if line:
                        _run(["wtype", "-"], line.encode(), timeout=60)
            elif s == "x11" and _tool("xdotool"):
                # A character missing from the layout goes through a key xdotool remaps for it;
                # with less than its default 12 ms between keys, such characters get lost.
                _run(["xdotool", "type", "--delay", "12", "--file", "-"], text.encode(), timeout=120)
            elif self.inp.uinput:
                self.inp.uinput.type_ascii(text)
            else:
                raise RuntimeError(self.inp.problem or "no keyboard on this screen")

    def hold(self, mods, up=False):
        names = [m for m in ("ctrl", "alt", "shift", "win") if m in mods]
        if names:
            with self._lock:
                self.inp.key_backend().keys([(m, not up) for m in (reversed(names) if up else names)])

    def press(self, key, mods=(), repeat=1):
        names = [m for m in ("ctrl", "alt", "shift", "win") if m in mods]
        steps = [(m, True) for m in names]
        for _ in range(repeat):
            steps += [(key, True), (key, False)]
        steps += [(m, False) for m in reversed(names)]
        with self._lock:
            self.inp.key_backend().keys(steps)


class Clipboard:
    def set_text(self, text):
        """wl-copy or xclip; RuntimeError without either (the framebuffer has no clipboard)."""
        s = current_session()
        if s == "wayland" and _tool("wl-copy"):
            cmd = ["wl-copy"]
        elif s == "x11" and _tool("xclip"):
            cmd = ["xclip", "-selection", "clipboard", "-in"]
        else:
            raise RuntimeError("no clipboard here (wl-copy or xclip)")
        # Both stay in the background to serve the clipboard: no pipes to wait for, only stdin.
        r = subprocess.run(cmd, input=text.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=TOOL_TIMEOUT)
        if r.returncode:
            raise RuntimeError(f"{cmd[0]} failed with code {r.returncode}")
