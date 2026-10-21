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


def test_runtime_wiring_without_touching_the_gui(tmp_path):
    ctx = open_project(tmp_path)
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="wiring")
    for app in ("claude", "zcode"):
        ctx.paths.calibration(app).write_text(json.dumps({
            "app": app, "process_name": app.title(), "window_title_contains": app.title(),
            "input_point": [10, 10], "submit_key": "return", "new_session_keys": ["cmd", "n"],
        }))
    args = SimpleNamespace(fake=False, no_brain=True, no_monitor=True, allow_sync_dir=False)
    drivers, brain, monitor_required = _runtime(ctx, args)
    assert set(drivers) == {"claude", "coder"}
    assert drivers["claude"].cal.app == "claude"
    assert drivers["coder"].cal.app == "zcode"
    assert brain is None and monitor_required is False
