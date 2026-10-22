import json
from pathlib import Path

import pytest

from relay.errors import AppMismatch, HumanRequired, RecoveryFailed
from relay.gui import FakeAppDriver, Popup, pointer_role, pointer_round
from relay.gui.app_driver import Calibration, GuiAppDriver
from relay.gui.recovery import Recovery
from relay.gui.screen import Capture, Rect
from relay.policy import choose_button, decide_popup


def test_popup_allow_list_rules():
    allow = [
        r"^allow .* to read (?:a )?file(?: .*)?\??$",
        r"^allow .* to edit (?:a )?file in (?:the )?project(?: .*)?\??$",
        r"^run (?:the )?tests?(?: .*)?\??$",
        r"^allow git (status|diff)(?: .*)?\??$",
    ]
    assert decide_popup(Popup("Allow Claude to read file src/app.py?", ["Allow", "Deny"]), allow) == "approve"
    assert decide_popup(Popup("Run tests in the project?", ["Run", "Cancel"]), allow) == "approve"
    assert decide_popup(Popup("Allow network access to install packages?", ["Allow", "Deny"]), allow) == "human"
    assert decide_popup(Popup("Allow access to all files?", ["Always allow", "Deny"]), allow) == "human"
    assert decide_popup(Popup("Something new?", ["OK", "Cancel"]), allow) == "human"
    assert decide_popup(Popup("", ["Allow"]), allow) == "human"
    assert decide_popup(Popup("read file", []), allow) == "human"
    assert choose_button(Popup("x", ["Always allow", "Deny"])) is None
    assert choose_button(Popup("x", ["Allow once", "Deny"])) == "Allow once"


def test_chained_shell_commands_are_never_approved():
    allow = [r"^run (?:the )?tests?(?: .*)?\??$"]
    exploits = [
        'Allow Bash: git diff && bash -c "$(echo cm0gLXIgfg== | base64 -d)"?',
        "Run tests? pytest; rm -r ~/Documents",
        "Allow Bash: git commit -m wip && python3 exfil.py",
        "Allow Bash: git diff > /tmp/leak",
        "Allow Bash: git diff\nrm -rf ~",
    ]
    for text in exploits:
        assert decide_popup(Popup(text, ["Allow", "Deny"]), allow) == "human", text
    assert decide_popup(Popup("Allow Bash: git diff", ["Allow", "Deny"]), allow) == "approve"
    assert decide_popup(Popup("Run pytest -q", ["Run", "Cancel"]), allow) == "approve"


class FakeBackend:
    def __init__(self):
        self.clicks: list[tuple[float, float]] = []
        self.button_clicks: list[str] = []
        self.typed: list[str] = []
        self.pressed: list[tuple[str, list[str]]] = []
        self.rect = Rect(100, 50, 800, 600)
        self.capture_same = False
        self.capture_seq = 0
        self.dialog: tuple[str, list[str]] | None = None

    def window(self, process, title):
        return {"title": "Claude", "rect": self.rect}

    def read_dialog(self, process, title=""):
        return self.dialog

    def focus(self, process, title):
        return {"title": "Claude", "rect": self.rect}

    def capture(self, process, title, dest, region=None):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.capture_same:
            dest.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        else:
            self.capture_seq += 1
            dest.write_bytes(b"\x89PNG\r\n\x1a\nfake" + bytes([self.capture_seq % 256]))
        actual = Rect(self.rect.x + (region[0] if region else 0), self.rect.y + (region[1] if region else 0),
                      region[2] if region else self.rect.w, region[3] if region else self.rect.h)
        return Capture(path=dest, window=self.rect, region=actual, scale=2.0)

    def click(self, x, y):
        self.clicks.append((x, y))

    def click_button(self, process, title, label):
        self.button_clicks.append(label)
        return True

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
    with pytest.raises(HumanRequired, match="model label"):
        driver2.select_model("Opus 5.5", "medium")


def test_model_picker_that_does_not_open_never_types(ctx):
    driver, backend = make_driver(ctx, vision=lambda image, kind, question: {
        "model": "Opus 5.5", "effort": "medium", "matches": True, "confidence": 0.9,
    })
    backend.capture_same = True
    with pytest.raises(HumanRequired, match="did not visibly open"):
        driver.select_model("Opus 5.5", "medium")
    assert backend.typed == []
    assert backend.pressed == []


def test_popup_clicks_by_label_and_ignores_vision_points(ctx):
    driver, backend = make_driver(ctx, vision=lambda image, kind, question: {
        "present": True, "text": "Allow Claude to read file src/app.py?", "buttons": ["Allow", "Deny"],
        "button_points": {"Allow": [999, 999]}, "confidence": 0.9,
    })
    backend.dialog = ("Allow Claude to read file src/app.py?", ["Allow", "Deny"])
    popup = driver.read_popup()
    assert popup is not None and popup.buttons == ["Allow", "Deny"]
    assert popup.button_points == {}
    driver.click_popup(popup, "Allow")
    assert backend.button_clicks == ["Allow"]
    assert backend.clicks == []


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


def test_window_capture_uses_window_id_not_screen_region(tmp_path, monkeypatch):
    import sys
    if sys.platform != "darwin":
        pytest.skip("macOS-only capture command")
    from relay.gui.screen import MacOSBackend

    backend = MacOSBackend.__new__(MacOSBackend)
    calls = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(args, timeout=20):
        calls.append(args)
        Path(args[-1]).write_bytes(b"png")
        return Result()

    backend._run = fake_run
    dest = tmp_path / "window.png"
    backend.capture_window(4242, dest)
    assert calls
    assert "-l" in calls[0]
    assert "4242" in calls[0]
    assert "-R" not in calls[0]


# --- N6: dialog text from Accessibility, and argument-level approval checks ----

def test_popup_approval_rejects_risky_arguments():
    risky = [
        "Allow Bash: git diff --output=/Users/me/.zshrc",
        "Allow Bash: git add .relay/STATE.json",
        "Allow Bash: git commit --no-verify -m x",
        "Allow Bash: python3 -m pytest -q -p evil_plugin",
        "Allow Bash: pytest --rootdir=/tmp/evil",
        "Allow Bash: git diff /etc/passwd",
        "Allow Bash: git diff --no-index /etc/passwd",
    ]
    for text in risky:
        assert decide_popup(Popup(text, ["Allow", "Deny"]), []) == "human", text
    assert decide_popup(Popup("Allow Bash: git diff", ["Allow", "Deny"]), []) == "approve"
    assert decide_popup(Popup("Allow Bash: git status -sb", ["Allow", "Deny"]), []) == "approve"
    assert decide_popup(Popup("Run pytest -q", ["Run", "Cancel"]), []) == "approve"


class DialogBackend:
    def __init__(self, dialog):
        self.dialog = dialog
        self.captures = 0

    def read_dialog(self, process, title=""):
        if isinstance(self.dialog, Exception):
            raise self.dialog
        return self.dialog

    def capture(self, *args, **kwargs):
        self.captures += 1
        raise AssertionError("popup text must not be read from a screenshot")


def _dialog_driver(ctx, backend):
    calibration = Calibration(
        app="claude", process_name="Claude", window_title_contains="Claude", input_point=(10.0, 10.0)
    )

    def vision(*args, **kwargs):
        raise AssertionError("popup text must not come from vision")

    return GuiAppDriver(ctx.cfg, ctx.journal, "claude", calibration, backend, vision=vision)


def test_popup_text_and_buttons_come_from_accessibility(ctx):
    backend = DialogBackend(("Allow Bash: git status", ["Allow", "Deny"]))
    driver = _dialog_driver(ctx, backend)

    popup = driver.read_popup()

    assert popup is not None
    assert popup.text == "Allow Bash: git status"
    assert popup.buttons == ["Allow", "Deny"]
    assert backend.captures == 0


def test_unreadable_dialog_escalates_instead_of_approving(ctx):
    from relay.errors import HumanRequired
    from relay.gui.screen import ScreenError
    from relay.orchestrator import Orchestrator
    from relay.notify import FakeNotifier
    from relay.setup import init_project

    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="popup")
    apps = FakeAppDriver("claude", ctx.cfg.project_dir)
    apps.popup = Popup("x", ["Allow"])
    orch = Orchestrator(ctx, {"claude": apps, "coder": FakeAppDriver("coder", ctx.cfg.project_dir)},
                        notifier=FakeNotifier())

    def explode():
        raise ScreenError("automation denied")

    apps.read_popup = explode
    with pytest.raises(HumanRequired, match="Accessibility"):
        orch._screen_check(apps, "planner")
    assert ctx.journal.of_type("popup.approved") == []


# --- N11: the Automation permission is probed, not assumed -------------------

@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS backend")
def test_system_events_permission_is_probed(ctx, monkeypatch):
    from relay.gui.screen import MacOSBackend

    backend = MacOSBackend(ctx.cfg)

    class Result:
        def __init__(self, returncode: int, stderr: str = ""):
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = ""

    monkeypatch.setattr(
        backend, "_run",
        lambda *args, **kwargs: Result(
            1, "execution error: Not authorized to send Apple events to System Events. (-1743)"
        ),
    )
    assert backend._system_events_ok() is False

    monkeypatch.setattr(backend, "_run", lambda *args, **kwargs: Result(0))
    assert backend._system_events_ok() is True


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS backend")
def test_permissions_reports_automation_failure_separately(ctx, monkeypatch):
    from relay.gui.screen import MacOSBackend

    backend = MacOSBackend(ctx.cfg)
    monkeypatch.setattr(backend, "_accessibility_ok", lambda: True)
    monkeypatch.setattr(backend, "_screen_recording_ok", lambda: True)
    monkeypatch.setattr(backend, "_system_events_ok", lambda: False)

    permissions = backend.permissions()
    assert permissions["accessibility"] is True
    assert permissions["system_events"] is False


# --- R3-6: option values may not traverse out of the project -------------------------

def test_option_values_cannot_traverse_out_of_the_project():
    from relay.policy import command_arguments_are_safe

    for unsafe in (
        "pytest --confcutdir=../..",
        "pytest --confcutdir ../../etc",
        "pytest -k ../permissions",
        "pytest --ignore=../secrets",
        "git diff --no-index=../../etc/passwd",
    ):
        assert not command_arguments_are_safe(unsafe), unsafe
    for safe in (
        "pytest -q tests/test_x.py",
        "pytest -q -k 'not slow'",
        "git status -sb",
        "git diff --stat",
        "git diff --name-only",
    ):
        assert command_arguments_are_safe(safe), safe
    assert decide_popup(Popup("Allow Bash: pytest --confcutdir=../..", ["Allow", "Deny"]), []) == "human"
