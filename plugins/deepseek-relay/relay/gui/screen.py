"""macOS GUI primitives with no third-party dependencies.

Window identity and bounds come from AppleScript (System Events); screenshots from
`screencapture`; clicks and keystrokes from CoreGraphics via ctypes. Everything is
window-relative, and every capture is checked for the Screen Recording permission.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import PauseRequested, RelayError

SCREENCAPTURE = "/usr/sbin/screencapture"
OSASCRIPT = "/usr/bin/osascript"


class ScreenError(RelayError):
    pass


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def tuple(self) -> tuple[float, float, float, float]:
        return self.x, self.y, self.w, self.h

    def contains(self, point: tuple[float, float]) -> bool:
        return self.x <= point[0] <= self.x + self.w and self.y <= point[1] <= self.y + self.h


@dataclass
class Capture:
    path: Path
    window: Rect
    region: Rect
    scale: float


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_CG_POINT = _CGPoint
_MOUSE_DOWN = 1
_MOUSE_UP = 2
_KEY_NAMES = {
    "return": 36, "enter": 76, "tab": 48, "space": 49, "escape": 53, "esc": 53,
    "delete": 51, "backspace": 51, "forwarddelete": 117, "left": 123, "right": 124,
    "down": 125, "up": 126, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4, "i": 34, "j": 38,
    "k": 40, "l": 37, "m": 46, "n": 45, "o": 31, "p": 35, "q": 12, "r": 15, "s": 1,
    "t": 17, "u": 32, "v": 9, "w": 13, "x": 7, "y": 16, "z": 6,
}
_MODIFIERS = {"cmd": 1 << 20, "command": 1 << 20, "shift": 1 << 17, "option": 1 << 19, "alt": 1 << 19, "control": 1 << 18, "ctrl": 1 << 18}


class MacOSBackend:
    def __init__(self, cfg, journal=None):
        if sys.platform != "darwin":
            raise ScreenError("the macOS backend requires macOS")
        self.cfg = cfg
        self.journal = journal
        self._lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        self._cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._setup_signatures()

    def _run(self, args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ScreenError(f"command failed ({args[0]}): {exc}") from exc

    # --- permissions ------------------------------------------------------
    def permissions(self) -> dict[str, Any]:
        return {
            "accessibility": self._accessibility_ok(),
            "screen_recording": self._screen_recording_ok(),
        }

    def _accessibility_ok(self) -> bool:
        script = 'tell application "System Events" to get name of first process'
        try:
            result = self._run([OSASCRIPT, "-e", script], timeout=15)
        except ScreenError:
            return False
        if result.returncode == 0:
            return True
        low = (result.stderr + result.stdout).lower()
        return not ("assistive" in low or "not allowed" in low or "-1719" in low)

    def _screen_recording_ok(self) -> bool:
        probe = Path("/tmp") / f"relay-screen-probe-{int(time.time())}.png"
        size = self.screen_size() or (800.0, 600.0)
        try:
            self.capture_region(Rect(size[0] / 2 - 32, size[1] / 2 - 32, 64, 64), probe)
            try:
                from PIL import Image  # type: ignore

                with Image.open(probe) as image:
                    return image.convert("L").getextrema() != (0, 0)
            except Exception:
                return True
        except ScreenError:
            return False
        finally:
            probe.unlink(missing_ok=True)

    # --- windows ----------------------------------------------------------
    def window(self, process_name: str, title_contains: str = "") -> dict[str, Any] | None:
        needle = _as_escape(title_contains)
        script = f'''
tell application "System Events"
    if not (exists process "{_as_escape(process_name)}") then return "NO_PROCESS"
    tell process "{_as_escape(process_name)}"
        set frontmost to true
        set target to missing value
        repeat with w in windows
            try
                if "{needle}" is "" or ((name of w as string) contains "{needle}") then
                    set target to w
                    exit repeat
                end if
            end try
        end repeat
        if target is missing value then return "NO_WINDOW"
        try
            perform action "AXRaise" of target
        end try
        set p to position of target
        set s to size of target
        return (name of target as string) & tab & (item 1 of p) & tab & (item 2 of p) & tab & (item 1 of s) & tab & (item 2 of s)
    end tell
end tell'''
        result = self._run([OSASCRIPT, "-e", script], timeout=20)
        if result.returncode != 0:
            raise ScreenError(f"cannot read window of {process_name!r}: {result.stderr.strip()[:200]}")
        output = result.stdout.strip()
        if output in ("NO_PROCESS", "NO_WINDOW") or not output:
            return None
        parts = output.split("\t")
        if len(parts) != 5:
            return None
        try:
            x, y, w, h = (float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]))
        except ValueError:
            return None
        if w < 50 or h < 50:
            return None
        return {"title": parts[0], "rect": Rect(x, y, w, h)}

    def focus(self, process_name: str, title_contains: str = "") -> dict[str, Any] | None:
        return self.window(process_name, title_contains)

    # --- screenshots ------------------------------------------------------
    def capture_region(self, rect: Rect, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.parent.chmod(0o700)
        except OSError:
            pass
        x = max(0, int(round(rect.x)))
        y = max(0, int(round(rect.y)))
        w = max(1, int(round(rect.w)))
        h = max(1, int(round(rect.h)))
        result = self._run(
            [SCREENCAPTURE, "-x", "-o", "-R", f"{x},{y},{w},{h}", str(dest)], timeout=30
        )
        if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            raise ScreenError(f"screencapture failed: {result.stderr.strip()[:200]}")
        dest.chmod(0o600)
        return dest

    def capture(
        self,
        process_name: str,
        title_contains: str,
        dest: Path,
        region: tuple[float, float, float, float] | None = None,
    ) -> Capture:
        window = self.window(process_name, title_contains)
        if window is None:
            raise ScreenError(f"window not found: {process_name!r}")
        bounds: Rect = window["rect"]
        if region:
            rx, ry, rw, rh = region
            absolute = Rect(bounds.x + rx, bounds.y + ry, rw, rh)
        else:
            absolute = bounds
        if absolute.x < 0 or absolute.y < 0:
            raise ScreenError("window is partly off-screen; move it fully onto the display")
        self.capture_region(absolute, dest)
        scale = 1.0
        try:
            from PIL import Image  # type: ignore

            with Image.open(dest) as image:
                if absolute.w > 0:
                    scale = image.width / absolute.w
        except Exception:
            pass
        return Capture(path=dest, window=bounds, region=absolute, scale=scale)

    # --- input ------------------------------------------------------------
    def _setup_signatures(self) -> None:
        lib = self._lib
        lib.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        lib.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        lib.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        lib.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        lib.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        lib.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, _CG_POINT, ctypes.c_uint32]
        lib.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        lib.CGEventCreate.argtypes = [ctypes.c_void_p]
        lib.CGEventCreate.restype = ctypes.c_void_p
        lib.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        lib.CGEventGetLocation.restype = _CG_POINT
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]

    def _corner_check(self) -> None:
        if not bool(self.cfg.get("gui.corner_failsafe", True)):
            return
        size = self.screen_size()
        if not size:
            return
        x, y = self.mouse_pos()
        width, height = size
        if (x <= 3 and y <= 3) or (x >= width - 4 and y <= 3) or (x <= 3 and y >= height - 4) or (x >= width - 4 and y >= height - 4):
            raise PauseRequested("mouse is in a screen corner (failsafe)")

    def click(self, x: float, y: float) -> None:
        self._corner_check()
        point = _CG_POINT(float(x), float(y))
        down = self._lib.CGEventCreateMouseEvent(None, _MOUSE_DOWN, point, 0)
        up = self._lib.CGEventCreateMouseEvent(None, _MOUSE_UP, point, 0)
        self._lib.CGEventPost(0, down)
        time.sleep(0.03)
        self._lib.CGEventPost(0, up)
        self._cf.CFRelease(down)
        self._cf.CFRelease(up)
        time.sleep(0.05)

    def type_ascii(self, text: str, delay_ms: int | None = None) -> None:
        self._corner_check()
        try:
            text.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ScreenError("Relay only types ASCII pointer text") from exc
        delay = (int(self.cfg.get("gui.typing_delay_ms", 12)) if delay_ms is None else delay_ms) / 1000.0
        for start in range(0, len(text), 16):
            chunk = text[start:start + 16]
            utf16 = chunk.encode("utf-16-le")
            buf = ctypes.create_string_buffer(utf16, len(utf16))
            down = self._lib.CGEventCreateKeyboardEvent(None, 0, True)
            self._lib.CGEventKeyboardSetUnicodeString(down, len(chunk), ctypes.cast(buf, ctypes.c_void_p))
            self._lib.CGEventPost(0, down)
            up = self._lib.CGEventCreateKeyboardEvent(None, 0, False)
            self._lib.CGEventPost(0, up)
            self._cf.CFRelease(down)
            self._cf.CFRelease(up)
            time.sleep(delay)

    def press(self, key: str, modifiers: list[str] | None = None) -> None:
        self._corner_check()
        name = key.lower()
        if name not in _KEY_NAMES:
            raise ScreenError(f"unknown key: {key}")
        flags = 0
        for modifier in modifiers or []:
            if modifier.lower() not in _MODIFIERS:
                raise ScreenError(f"unknown modifier: {modifier}")
            flags |= _MODIFIERS[modifier.lower()]
        code = _KEY_NAMES[name]
        down = self._lib.CGEventCreateKeyboardEvent(None, code, True)
        up = self._lib.CGEventCreateKeyboardEvent(None, code, False)
        if flags:
            self._lib.CGEventSetFlags(down, flags)
            self._lib.CGEventSetFlags(up, flags)
        self._lib.CGEventPost(0, down)
        time.sleep(0.03)
        self._lib.CGEventPost(0, up)
        self._cf.CFRelease(down)
        self._cf.CFRelease(up)
        time.sleep(0.05)

    def press_keys(self, keys: list[str]) -> None:
        if not keys:
            return
        self.press(keys[-1], keys[:-1])

    def mouse_pos(self) -> tuple[float, float]:
        event = self._lib.CGEventCreate(None)
        point = self._lib.CGEventGetLocation(event)
        self._cf.CFRelease(event)
        return float(point.x), float(point.y)

    def screen_size(self) -> tuple[float, float] | None:
        script = 'tell application "Finder" to get bounds of window of desktop'
        try:
            result = self._run([OSASCRIPT, "-e", script], timeout=10)
        except ScreenError:
            return None
        if result.returncode != 0:
            return None
        numbers = re.findall(r"-?\d+(?:\.\d+)?", result.stdout)
        if len(numbers) < 4:
            return None
        return float(numbers[2]), float(numbers[3])

    def idle_seconds(self) -> float:
        try:
            result = self._run(["ioreg", "-c", "IOHIDSystem"], timeout=10)
        except ScreenError:
            return 0.0
        match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
        if not match:
            return 0.0
        return int(match.group(1)) / 1_000_000_000


def _as_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def backend_for(cfg, journal=None):
    requested = str(cfg.get("gui.backend", "macos"))
    if requested != "macos":
        raise ScreenError(f"unsupported gui.backend {requested!r}; only 'macos' is implemented")
    if sys.platform == "darwin":
        return MacOSBackend(cfg, journal)
    raise ScreenError(
        "only the macOS GUI backend is implemented in this build. "
        "Run Relay on macOS, or supply a custom AppDriver for your platform."
    )
