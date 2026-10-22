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
class WindowInfo:
    id: int
    owner: str
    title: str | None
    rect: Rect
    layer: int


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
        self._cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
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
            # N11: a real probe, not "same as Accessibility". Automation can be revoked alone.
            "system_events": self._system_events_ok(),
        }

    def _accessibility_ok(self) -> bool:
        # AXIsProcessTrusted is a silent check: it never shows the TCC prompt mid-run.
        try:
            function = self._lib.AXIsProcessTrusted
            function.restype = ctypes.c_bool
            function.argtypes = []
            return bool(function())
        except Exception:
            return self._system_events_ok()

    def _system_events_ok(self) -> bool:
        script = 'tell application "System Events" to get name of first process'
        try:
            result = self._run([OSASCRIPT, "-e", script], timeout=15)
        except ScreenError:
            return False
        if result.returncode == 0:
            return True
        low = (result.stderr + result.stdout).lower()
        denied = (
            "assistive", "not allowed", "-1719", "-1743", "not authorized",
            "not permitted", "operation not permitted",
        )
        return not any(token in low for token in denied)

    def _screen_recording_ok(self) -> bool:
        probe = Path("/tmp") / f"relay-screen-probe-{int(time.time())}.png"
        try:
            self._run([SCREENCAPTURE, "-x", str(probe)], timeout=15)
            if not probe.exists() or probe.stat().st_size == 0:
                return False
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
    def _cfstring(self, text: str) -> int:
        return int(self._cf.CFStringCreateWithCString(None, text.encode("utf-8"), 0x08000100))

    def _cg_windows(self) -> list[WindowInfo]:
        arr = self._cg.CGWindowListCopyWindowInfo(1, 0)
        if not arr:
            return []
        keys = {
            name: self._cfstring(name)
            for name in ("kCGWindowNumber", "kCGWindowOwnerName", "kCGWindowName", "kCGWindowLayer", "kCGWindowBounds")
        }
        bkeys = {name: self._cfstring(name) for name in ("X", "Y", "Width", "Height")}
        windows: list[WindowInfo] = []

        def value(dct, key):
            return self._cf.CFDictionaryGetValue(dct, key) if dct else None

        def text_of(dct, key):
            ref = value(dct, key)
            if not ref or self._cf.CFGetTypeID(ref) != self._cf.CFStringGetTypeID():
                return None
            buf = ctypes.create_string_buffer(2048)
            if self._cf.CFStringGetCString(ref, buf, 2048, 0x08000100):
                return buf.value.decode("utf-8", "replace")
            return None

        def number_of(dct, key):
            ref = value(dct, key)
            if not ref or self._cf.CFGetTypeID(ref) != self._cf.CFNumberGetTypeID():
                return None
            out = ctypes.c_longlong()
            if self._cf.CFNumberGetValue(ref, 4, ctypes.byref(out)):
                return int(out.value)
            return None

        try:
            for index in range(self._cf.CFArrayGetCount(arr)):
                dct = self._cf.CFArrayGetValueAtIndex(arr, index)
                number = number_of(dct, keys["kCGWindowNumber"])
                bounds_ref = value(dct, keys["kCGWindowBounds"])
                if number is None or not bounds_ref or self._cf.CFGetTypeID(bounds_ref) != self._cf.CFDictionaryGetTypeID():
                    continue
                rect_values = {name: number_of(bounds_ref, key) for name, key in bkeys.items()}
                if any(item is None for item in rect_values.values()):
                    continue
                windows.append(WindowInfo(
                    id=number,
                    owner=text_of(dct, keys["kCGWindowOwnerName"]) or "",
                    title=text_of(dct, keys["kCGWindowName"]),
                    rect=Rect(float(rect_values["X"]), float(rect_values["Y"]),
                              float(rect_values["Width"]), float(rect_values["Height"])),
                    layer=number_of(dct, keys["kCGWindowLayer"]) or 0,
                ))
        finally:
            for key in list(keys.values()) + list(bkeys.values()):
                self._cf.CFRelease(key)
            self._cf.CFRelease(arr)
        return windows

    def inspect_window(self, process_name: str, title_contains: str = "") -> dict[str, Any] | None:
        """Read-only window lookup. Never focuses or raises anything."""
        windows = [
            item for item in self._cg_windows()
            if item.owner.casefold() == str(process_name).casefold() and item.layer == 0
        ]
        if not windows:
            return None
        if title_contains:
            titled = [item for item in windows if item.title and title_contains in item.title]
            if titled:
                windows = titled
            elif any(item.title for item in windows):
                return None
        chosen = max(windows, key=lambda item: item.rect.w * item.rect.h)
        return {"id": chosen.id, "title": chosen.title or "", "rect": chosen.rect, "layer": chosen.layer}

    def capture_window(self, window_id: int, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.parent.chmod(0o700)
        except OSError:
            pass
        result = self._run(
            [SCREENCAPTURE, "-x", "-o", "-l", str(int(window_id)), str(dest)], timeout=30
        )
        if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            raise ScreenError(
                "screencapture -l failed; grant Screen Recording to this app "
                f"({result.stderr.strip()[:160]})"
            )
        dest.chmod(0o600)
        return dest

    def capture(
        self,
        process_name: str,
        title_contains: str,
        dest: Path,
        region: tuple[float, float, float, float] | None = None,
    ) -> Capture:
        window = self.inspect_window(process_name, title_contains)
        if window is None:
            raise ScreenError(f"window not found (read-only): {process_name!r}")
        bounds: Rect = window["rect"]
        self.capture_window(window["id"], dest)
        scale = 1.0
        try:
            from PIL import Image  # type: ignore

            with Image.open(dest) as image:
                image.load()
                if bounds.w > 0:
                    scale = image.width / bounds.w
                if region:
                    rx, ry, rw, rh = region
                    box = (
                        max(0, int(round(rx * scale))), max(0, int(round(ry * scale))),
                        min(image.width, int(round((rx + rw) * scale))),
                        min(image.height, int(round((ry + rh) * scale))),
                    )
                    image.crop(box).save(dest)
        except ImportError as exc:
            if region:
                raise ScreenError("Pillow is required for cropped window captures") from exc
        except Exception:
            if region:
                raise
        if region:
            rx, ry, rw, rh = region
            absolute = Rect(bounds.x + rx, bounds.y + ry, rw, rh)
        else:
            absolute = bounds
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
        cg = self._cg
        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        cf = self._cf
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFNumberGetTypeID.restype = ctypes.c_ulong
        cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong

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

    def read_dialog(self, process_name: str, title_contains: str = "") -> tuple[str, list[str]] | None:
        """Read an approval sheet/dialog through Accessibility. No language model is involved.

        Returns (text, button labels), or None when no sheet/dialog is open. Raises ScreenError
        when Accessibility cannot be read: Relay must never approve what it cannot read.
        """
        script = f'''
tell application "System Events"
    if not (exists process "{_as_escape(process_name)}") then return "NO_PROCESS"
    tell process "{_as_escape(process_name)}"
        repeat with w in windows
            try
                if "{_as_escape(title_contains)}" is "" or ((name of w as string) contains "{_as_escape(title_contains)}") then
                    set dlg to missing value
                    try
                        if exists sheet 1 of w then set dlg to sheet 1 of w
                    end try
                    if dlg is missing value then
                        try
                            if exists dialog 1 of w then set dlg to dialog 1 of w
                        end try
                    end if
                    if dlg is missing value then return "NONE"
                    set out to ""
                    repeat with e in (entire contents of dlg)
                        try
                            if class of e is static text then
                                set out to out & (value of e as text) & linefeed
                            end if
                        end try
                    end repeat
                    set out to out & "---BUTTONS---" & linefeed
                    repeat with e in (entire contents of dlg)
                        try
                            if class of e is button then
                                set out to out & (name of e as text) & linefeed
                            end if
                        end try
                    end repeat
                    return out
                end if
            end try
        end repeat
    end tell
end tell
return "NONE"'''
        result = self._run([OSASCRIPT, "-e", script], timeout=25)
        if result.returncode != 0:
            raise ScreenError(
                "could not read the approval dialog through Accessibility: "
                + (result.stderr.strip() or result.stdout.strip() or "osascript failed")
            )
        output = result.stdout.strip()
        if output in ("", "NONE", "NO_PROCESS"):
            return None
        if "---BUTTONS---" not in output:
            return (output, [])
        text, buttons = output.split("---BUTTONS---", 1)
        labels = [line.strip() for line in buttons.splitlines() if line.strip()]
        return (text.strip(), labels)

    def click_button(self, process_name: str, title_contains: str, label: str) -> bool:
        """Click a button by Accessibility label. Never focuses or raises the window."""
        script = f'''
tell application "System Events"
    if not (exists process "{_as_escape(process_name)}") then return "NO_PROCESS"
    tell process "{_as_escape(process_name)}"
        repeat with w in windows
            try
                if "{_as_escape(title_contains)}" is "" or ((name of w as string) contains "{_as_escape(title_contains)}") then
                    repeat with e in (entire contents of w)
                        try
                            if class of e is button then
                                set nm to ""
                                try
                                    set nm to name of e as string
                                end try
                                if nm is "{_as_escape(label)}" then
                                    perform action "AXPress" of e
                                    return "CLICKED"
                                end if
                            end if
                        end try
                    end repeat
                end if
            end try
        end repeat
    end tell
end tell
return "NOT_FOUND"'''
        result = self._run([OSASCRIPT, "-e", script], timeout=20)
        return result.returncode == 0 and result.stdout.strip() == "CLICKED"

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
