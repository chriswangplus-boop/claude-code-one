import importlib
import json
import pkgutil
from types import SimpleNamespace

import relay
from relay.cli import _runtime
from relay.context import open_project
from relay.setup import init_project


def test_every_module_imports():
    modules = [
        info.name for info in pkgutil.walk_packages(relay.__path__, "relay.")
        if not info.name.endswith("__main__")
    ]
    assert len(modules) >= 30
    for name in modules:
        importlib.import_module(name)


def test_runtime_wiring_without_touching_the_gui(tmp_path, monkeypatch):
    class DummyBackend:
        def permissions(self):
            return {"accessibility": True, "screen_recording": True, "system_events": True}

    monkeypatch.setattr("relay.gui.screen.backend_for", lambda cfg, journal=None: DummyBackend())
    ctx = open_project(tmp_path)
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="wiring")
    for app in ("claude", "zcode"):
        ctx.paths.calibration(app).write_text(json.dumps({
            "app": app, "process_name": app.title(), "window_title_contains": app.title(),
            "input_point": [10, 10], "submit_key": "return", "new_session_keys": ["cmd", "n"],
        }))
        ctx.paths.calibration_proof(app).write_text(json.dumps({"working_dir": str(tmp_path.resolve())}))
    args = SimpleNamespace(fake=False, no_brain=True, no_monitor=True, allow_sync_dir=False)
    drivers, brain, monitor_required = _runtime(ctx, args)
    assert set(drivers) == {"claude", "coder"}
    assert drivers["claude"].cal.app == "claude"
    assert drivers["coder"].cal.app == "zcode"
    assert brain is None and monitor_required is False


def test_runtime_refuses_missing_macos_permissions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from relay.config import ConfigError

    class DummyBackend:
        def permissions(self):
            return {"accessibility": True, "screen_recording": False, "system_events": True}

    monkeypatch.setattr("relay.gui.screen.backend_for", lambda cfg, journal=None: DummyBackend())
    ctx = open_project(tmp_path)
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="wiring")
    for app in ("claude", "zcode"):
        ctx.paths.calibration(app).write_text(json.dumps({
            "app": app, "process_name": app.title(), "window_title_contains": app.title(),
            "input_point": [10, 10], "submit_key": "return", "new_session_keys": ["cmd", "n"],
        }))
        ctx.paths.calibration_proof(app).write_text(json.dumps({"working_dir": str(tmp_path.resolve())}))
    args = SimpleNamespace(fake=False, no_brain=True, no_monitor=True, allow_sync_dir=False)
    with pytest.raises(ConfigError, match="QUIT and RELAUNCH"):
        _runtime(ctx, args)
