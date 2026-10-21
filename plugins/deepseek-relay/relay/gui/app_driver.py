"""Calibration-driven app driver: all coordinates come from a calibration file."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..errors import AppMismatch, BrainError, PauseRequested, VisionUnavailable
from ..paths import Paths
from ..state import atomic_write_json
from ..util import ascii_pointer, sha256_file
from .base import AppDriver, Popup
from .screen import Capture, MacOSBackend, Rect, ScreenError

Vision = Callable[[Path, str, str | None], dict]


@dataclass
class Calibration:
    app: str
    process_name: str
    window_title_contains: str
    input_point: tuple[float, float]
    submit_key: str = "return"
    new_session_keys: list[str] = field(default_factory=lambda: ["cmd", "n"])
    model_picker: dict[str, Any] | None = None
    context_region: tuple[float, float, float, float] | None = None
    busy_region: tuple[float, float, float, float] | None = None
    input_region: tuple[float, float, float, float] | None = None
    popup_region: tuple[float, float, float, float] | None = None
    working_dir: str | None = None
    calibrated_at: str | None = None

    @classmethod
    def load(cls, paths: Paths, app: str) -> "Calibration":
        path = paths.calibration(app)
        if not path.exists():
            raise AppMismatch(
                f"{app} is not calibrated. Run `relay calibrate --app {app}` first."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        missing = [key for key in ("process_name", "window_title_contains", "input_point") if key not in data]
        if missing:
            raise AppMismatch(f"calibration for {app} is missing: {', '.join(missing)}")
        return cls(
            app=app,
            process_name=str(data["process_name"]),
            window_title_contains=str(data.get("window_title_contains", "")),
            input_point=(float(data["input_point"][0]), float(data["input_point"][1])),
            submit_key=str(data.get("submit_key", "return")),
            new_session_keys=list(data.get("new_session_keys", ["cmd", "n"])),
            model_picker=data.get("model_picker"),
            context_region=_region(data.get("context_region")),
            busy_region=_region(data.get("busy_region")),
            input_region=_region(data.get("input_region")),
            popup_region=_region(data.get("popup_region")),
            working_dir=data.get("working_dir"),
            calibrated_at=data.get("calibrated_at"),
        )

    def save(self, paths: Paths) -> Path:
        path = paths.calibration(self.app)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "app": self.app,
            "process_name": self.process_name,
            "window_title_contains": self.window_title_contains,
            "input_point": list(self.input_point),
            "submit_key": self.submit_key,
            "new_session_keys": self.new_session_keys,
            "model_picker": self.model_picker,
            "context_region": list(self.context_region) if self.context_region else None,
            "busy_region": list(self.busy_region) if self.busy_region else None,
            "input_region": list(self.input_region) if self.input_region else None,
            "popup_region": list(self.popup_region) if self.popup_region else None,
            "working_dir": self.working_dir,
            "calibrated_at": self.calibrated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        atomic_write_json(path, payload)
        path.chmod(0o600)
        return path


def _region(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return tuple(float(item) for item in value)  # type: ignore[return-value]
    return None


class GuiAppDriver(AppDriver):
    def __init__(
        self,
        cfg,
        journal,
        name: str,
        calibration: Calibration,
        backend: MacOSBackend,
        *,
        vision: Vision | None = None,
        sleep=None,
    ):
        self.cfg = cfg
        self.journal = journal
        self.name = name
        self.cal = calibration
        self.backend = backend
        self.vision = vision
        self.sleep = sleep or time.sleep
        self._ctx_cache: tuple[float, float | None] | None = None

    # --- window -----------------------------------------------------------
    def focus(self) -> None:
        window = self.backend.focus(self.cal.process_name, self.cal.window_title_contains)
        if not window:
            raise AppMismatch(
                f"could not focus {self.cal.process_name!r} with a window matching "
                f"{self.cal.window_title_contains!r}"
            )
        self.journal.append(
            "gui.focus", detail={"app": self.name, "title": window["title"], "rect": list(window["rect"].tuple)}
        )

    def new_session(self) -> None:
        self.focus()
        self.backend.press_keys(self.cal.new_session_keys)
        self.sleep(1.2)
        empty = self._check_empty()
        if empty is False:
            self.backend.press_keys(self.cal.new_session_keys)
            self.sleep(1.5)
            empty = self._check_empty()
        self._ctx_cache = None
        self.journal.append(
            "gui.new_session", detail={"app": self.name, "confirmed_empty": empty},
            result="ok" if empty is not False else "unconfirmed",
        )
        if empty is False:
            raise AppMismatch(f"{self.name} did not open an empty session")

    def _check_empty(self) -> bool | None:
        try:
            capture = self._capture(None, "empty")
            result = self._vision(capture.path, "empty_chat", None)
            return bool(result.get("empty"))
        except (VisionUnavailable, BrainError, ScreenError):
            return None

    def select_model(self, name: str, effort: str) -> None:
        picker = self.cal.model_picker
        if not isinstance(picker, dict) or not picker.get("point"):
            raise AppMismatch(f"{self.name} calibration has no model picker")
        for attempt in range(2):
            self.focus()
            target = self._abs(picker["point"])
            self.backend.click(target.x, target.y)
            self.journal.append("gui.click", detail={"app": self.name, "target": "model-picker"})
            self.sleep(0.5)
            if picker.get("clear", True):
                self.backend.press("a", ["cmd"])
            self.backend.type_ascii(name)
            self.sleep(0.4)
            self.backend.press("return")
            self.sleep(0.8)
            region = _region(picker.get("label_region"))
            if region is None:
                self.journal.append(
                    "gui.model", detail={"app": self.name, "model": name, "effort": effort, "verified": None},
                    result="unverified",
                )
                return
            capture = self._capture(region, "model")
            try:
                seen = self._vision(capture.path, "model_label", f"Expected model {name!r} at effort {effort!r}.")
            except VisionUnavailable:
                return
            got_model = str(seen.get("model") or "").lower()
            got_effort = str(seen.get("effort") or "").lower()
            if name.lower() in got_model and (not got_effort or got_effort == effort.lower()):
                self.journal.append(
                    "gui.model", detail={"app": self.name, "model": name, "effort": effort, "seen": seen},
                )
                return
            self.journal.append(
                "gui.model", detail={"app": self.name, "expected": [name, effort], "seen": seen},
                result="mismatch",
            )
        raise AppMismatch(f"could not verify the model label in {self.name} (expected {name}/{effort})")

    # --- ring -------------------------------------------------------------
    def ring(self, pointer_line: str) -> None:
        pointer = ascii_pointer(pointer_line)
        if not pointer:
            raise ScreenError("refusing to type an empty pointer")
        self.focus()
        point = self._abs(self.cal.input_point)
        self.backend.click(point.x, point.y)
        self.journal.append("gui.click", detail={"app": self.name, "target": "message-input"})
        self.sleep(0.2)
        self.backend.type_ascii(pointer)
        self.sleep(0.2)
        self.backend.press(self.cal.submit_key)
        self.journal.append(
            "gui.type", detail={"app": self.name, "chars": len(pointer), "submit": self.cal.submit_key}
        )

    def verify_sent(self) -> bool | None:
        region = self.cal.input_region
        if region is None:
            return None
        try:
            capture = self._capture(region, "sent")
            result = self._vision(capture.path, "pointer_sent", None)
            self.journal.append(
                "vision.check", detail={"kind": "pointer_sent", "app": self.name, "sent": result.get("sent")}
            )
            return bool(result.get("sent"))
        except (VisionUnavailable, BrainError, ScreenError):
            return None

    # --- observations -----------------------------------------------------
    def read_context_pct(self) -> float | None:
        region = self.cal.context_region
        if region is None:
            return None
        now = time.time()
        if self._ctx_cache and now - self._ctx_cache[0] < 30:
            return self._ctx_cache[1]
        try:
            capture = self._capture(region, "context")
            result = self._vision(capture.path, "context_pct", None)
            value = result.get("context_pct") if float(result.get("confidence", 1.0)) >= 0.5 else None
            self._ctx_cache = (now, value)
            self.journal.append(
                "vision.check", detail={"kind": "context_pct", "app": self.name, "value": value}
            )
            return value
        except (VisionUnavailable, BrainError, ScreenError):
            return None

    def is_busy(self) -> bool | None:
        region = self.cal.busy_region
        try:
            capture = self._capture(region, "busy")
            result = self._vision(capture.path, "busy", None)
            if float(result.get("confidence", 1.0)) < 0.5:
                return None
            return bool(result.get("busy"))
        except (VisionUnavailable, BrainError, ScreenError):
            return None

    def read_popup(self) -> Popup | None:
        try:
            capture = self._capture(self.cal.popup_region, "popup")
            result = self._vision(capture.path, "popup", None)
        except (VisionUnavailable, BrainError):
            return None
        if not result.get("present"):
            return None
        if float(result.get("confidence", 1.0)) < 0.6:
            return Popup(text=str(result.get("text") or ""), buttons=[])
        points: dict[str, tuple[float, float]] = {}
        for label, point in (result.get("button_points") or {}).items():
            try:
                points[str(label)] = self._to_screen(capture, float(point[0]), float(point[1]))
            except (TypeError, ValueError, IndexError):
                continue
        return Popup(text=str(result.get("text") or ""), buttons=list(result.get("buttons") or []), button_points=points)

    def click_popup(self, popup: Popup, button: str) -> None:
        point = popup.button_points.get(button)
        if point is None:
            raise AppMismatch(f"no calibrated click point for popup button {button!r}")
        if self.backend.idle_seconds() < int(self.cfg.get("user_idle_seconds", 30)):
            raise PauseRequested("user became active before the popup click")
        self.backend.click(point[0], point[1])
        self.journal.append("gui.click", detail={"app": self.name, "target": f"popup:{button}"})

    def wait_for_idle(self, seconds: int) -> None:
        deadline = time.time() + 180
        while time.time() < deadline:
            idle = self.backend.idle_seconds()
            if idle >= seconds:
                return
            time.sleep(min(5.0, max(1.0, seconds - idle)))
        raise PauseRequested("user has been active for too long; not taking over the mouse")

    # --- internals --------------------------------------------------------
    def _abs(self, point: Any) -> Rect:
        window = self.backend.window(self.cal.process_name, self.cal.window_title_contains)
        if not window:
            raise AppMismatch(f"{self.name} window disappeared")
        bounds: Rect = window["rect"]
        return Rect(bounds.x + float(point[0]), bounds.y + float(point[1]), 0, 0)

    def _to_screen(self, capture: Capture, px: float, py: float) -> tuple[float, float]:
        scale = capture.scale or 1.0
        return capture.region.x + px / scale, capture.region.y + py / scale

    def _capture(self, region: tuple[float, float, float, float] | None, tag: str) -> Capture:
        now = datetime.now().astimezone()
        stamp = now.strftime("%Y%m%d-%H%M%S")
        unique = time.time_ns() % 1_000_000
        dest = self.cfg.project_dir / ".relay" / "journal" / "screens" / stamp[:8] / f"{self.name}-{tag}-{stamp[9:]}-{unique}.png"
        capture = self.backend.capture(
            self.cal.process_name, self.cal.window_title_contains, dest, region
        )
        try:
            digest = sha256_file(capture.path)
        except OSError:
            digest = None
        self.journal.append(
            "gui.screenshot",
            detail={
                "app": self.name, "tag": tag, "region": list(capture.region.tuple),
                "scale": capture.scale, "sha256": digest,
            },
            screenshot=str(capture.path.relative_to(self.cfg.project_dir)),
        )
        return capture

    def _vision(self, image: Path, kind: str, question: str | None) -> dict:
        if self.vision is None:
            raise VisionUnavailable("no vision reader configured")
        return self.vision(image, kind, question)


def calibration_proof(paths: Paths, app: str) -> dict | None:
    path = paths.calibration_proof(app)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
