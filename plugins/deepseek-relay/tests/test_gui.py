import json
from pathlib import Path

import pytest

from relay.errors import AppMismatch, RecoveryFailed
from relay.gui import FakeAppDriver, Popup, pointer_role, pointer_round
from relay.gui.app_driver import Calibration, GuiAppDriver
from relay.gui.recovery import Recovery
from relay.gui.screen import Capture, Rect
from relay.policy import choose_button, decide_popup


def test_popup_allow_list_rules():
    allow = ["read file", "run tests", "git diff", "git commit", "git status", "git add", "edit file in project"]
    assert decide_popup(Popup("Allow Claude to read file src/app.py?", ["Allow", "Deny"]), allow) == "approve"
    assert decide_popup(Popup("Run tests in the project?", ["Run", "Cancel"]), allow) == "approve"
    assert decide_popup(Popup("Allow network access to install packages?", ["Allow", "Deny"]), allow) == "human"
    assert decide_popup(Popup("Allow access to all files?", ["Always allow", "Deny"]), allow) == "human"
    assert decide_popup(Popup("Something new?", ["OK", "Cancel"]), allow) == "human"
    assert decide_popup(Popup("", ["Allow"]), allow) == "human"
    assert decide_popup(Popup("read file", []), allow) == "human"
    assert choose_button(Popup("x", ["Always allow", "Deny"])) is None
    assert choose_button(Popup("x", ["Allow once", "Deny"])) == "Allow once"


class FakeBackend:
    def __init__(self):
        self.clicks: list[tuple[float, float]] = []
        self.typed: list[str] = []
        self.pressed: list[tuple[str, list[str]]] = []
        self.rect = Rect(100, 50, 800, 600)

    def window(self, process, title):
        return {"title": "Claude", "rect": self.rect}

    def focus(self, process, title):
        return {"title": "Claude", "rect": self.rect}

    def capture(self, process, title, dest, region=None):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        actual = Rect(self.rect.x + (region[0] if region else 0), self.rect.y + (region[1] if region else 0),
                      region[2] if region else self.rect.w, region[3] if region else self.rect.h)
        return Capture(path=dest, window=self.rect, region=actual, scale=2.0)

    def click(self, x, y):
        self.clicks.append((x, y))

    def type_ascii(self, text):
        self.typed.append(text)

    def press(self, key, modifiers=None):
        self.pressed.append((key, modifiers or []))

    def press_keys(self, keys):
        self.press(keys[-1], keys[:-1])

    def idle_seconds(self):
        return 999


def make_driver(ctx, vision=None):
    calibration = Calibration(
        app="claude", process_name="Claude", window_title_contains="Claude",
        input_point=(400, 500), input_region=(0, 460, 800, 120),
        model_picker={"point": (700, 40), "label_region": (500, 20, 280, 40)},
        context_region=(700, 60, 100, 20), popup_region=(10, 10, 700, 500),
    )
    backend = FakeBackend()
    driver = GuiAppDriver(ctx.cfg, ctx.journal, "claude", calibration, backend, vision=vision, sleep=lambda _: None)
    return driver, backend


def test_calibration_roundtrip(ctx):
    calibration = Calibration(
        app="claude", process_name="Claude", window_title_contains="Claude", input_point=(1, 2),
        context_region=(1, 2, 3, 4),
    )
    path = calibration.save(ctx.paths)
    loaded = Calibration.load(ctx.paths, "claude")
    assert loaded.input_point == (1.0, 2.0)
    assert loaded.context_region == (1.0, 2.0, 3.0, 4.0)
    assert json.loads(path.read_text())["app"] == "claude"
    with pytest.raises(AppMismatch, match="not calibrated"):
        Calibration.load(ctx.paths, "zcode")


def test_ring_types_ascii_pointer_and_submits(ctx):
    driver, backend = make_driver(ctx)
    driver.ring("RELAY round 2 role=coder: read .relay/HANDOFF.md")
    assert backend.typed == ["RELAY round 2 role=coder: read .relay/HANDOFF.md"]
    assert backend.clicks[0] == (500.0, 550.0)  # window origin + input point
    assert backend.pressed[-1] == ("return", [])
    assert ctx.journal.of_type("gui.type")


def test_model_verification(ctx):
    driver, backend = make_driver(ctx, vision=lambda image, kind, question: {
        "model": "Opus 5.5", "effort": "medium", "matches": True, "confidence": 0.9,
    })
    driver.select_model("Opus 5.5", "medium")
    assert ("a", ["cmd"]) in backend.pressed
    assert backend.typed == ["Opus 5.5"]

    driver2, _ = make_driver(ctx, vision=lambda image, kind, question: {
        "model": "Sonnet 5.5", "effort": "high", "matches": False, "confidence": 0.9,
    })
    with pytest.raises(AppMismatch, match="model label"):
        driver2.select_model("Opus 5.5", "medium")


def test_popup_points_are_converted_to_screen_coordinates(ctx):
    driver, _ = make_driver(ctx, vision=lambda image, kind, question: {
        "present": True, "text": "Allow Claude to read file src/app.py?", "buttons": ["Allow", "Deny"],
        "button_points": {"Allow": [200, 100]}, "confidence": 0.9,
    })
    popup = driver.read_popup()
    assert popup is not None and popup.buttons == ["Allow", "Deny"]
    assert popup.button_points["Allow"] == (210.0, 110.0)
    driver.click_popup(popup, "Allow")
    assert (210.0, 110.0) in driver.backend.clicks


def test_context_and_verification(ctx):
    driver, _ = make_driver(ctx, vision=lambda image, kind, question: {
        "context_pct": 63, "sent": True, "empty": True, "busy": False, "confidence": 0.8,
    })
    assert driver.read_context_pct() == 63
    assert driver.verify_sent() is True
    assert driver.is_busy() is False
    assert driver.new_session() is None


def test_recovery_refocuses_then_succeeds(ctx):
    class FlakyDriver(FakeAppDriver):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.failures = 2

        def focus(self):
            if self.failures:
                self.failures -= 1
                raise AppMismatch("not frontmost")
            super().focus()

    driver = FlakyDriver("claude", ctx.cfg.project_dir)
    recovery = Recovery(driver, ctx.journal, ctx.cfg, sleep=lambda _: None)
    assert recovery.ensure_ready() is True
    assert driver.focus_calls == 1
    assert ctx.journal.of_type("recovery.end")


def test_recovery_gives_up(ctx):
    class BadDriver(FakeAppDriver):
        def focus(self):
            raise AppMismatch("never")

    recovery = Recovery(BadDriver("claude", ctx.cfg.project_dir), ctx.journal, ctx.cfg, sleep=lambda _: None)
    with pytest.raises(RecoveryFailed):
        recovery.ensure_ready()


def test_pointer_helpers():
    assert pointer_role("RELAY round 3 role=reviewer: x") == "reviewer"
    assert pointer_round("RELAY round 3 role=reviewer: x") == 3
    assert pointer_role("RELAY seed role=coder: x") == "coder"
    assert pointer_round("RELAY seed role=coder: x") is None
