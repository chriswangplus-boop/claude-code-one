"""Interactive calibration and the shared-folder proof."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from ..errors import ConfigError
from ..paths import Paths
from ..util import ascii_pointer
from .app_driver import Calibration, GuiAppDriver
from .screen import backend_for

DEFAULTS = {
    "claude": {"process_name": "Claude", "title": "Claude"},
    "zcode": {"process_name": "Zcode", "title": "Zcode"},
}


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def _ask_point(label: str) -> tuple[float, float]:
    while True:
        raw = _ask(f"{label} (x,y in window points, e.g. 420,680)")
        try:
            x, y = (float(part.strip()) for part in raw.split(","))
            return x, y
        except ValueError:
            print("  enter two numbers separated by a comma")


def _ask_region(label: str, *, optional: bool = True) -> tuple[float, float, float, float] | None:
    raw = _ask(f"{label} (x,y,w,h){' - blank to skip' if optional else ''}")
    if not raw:
        return None
    try:
        values = tuple(float(part.strip()) for part in raw.split(","))
        if len(values) != 4:
            raise ValueError
        return values  # type: ignore[return-value]
    except ValueError:
        print("  enter four numbers separated by commas")
        return _ask_region(label, optional=optional)


def calibrate_app(
    ctx,
    app: str,
    *,
    timeout_seconds: int = 240,
    opener=None,
    skip_self_test: bool = False,
) -> dict:
    if sys.platform != "darwin":
        raise ConfigError("calibration currently supports macOS only")
    if app not in DEFAULTS:
        raise ConfigError(f"unknown app: {app}")
    backend = backend_for(ctx.cfg, ctx.journal)
    permissions = backend.permissions()
    print(f"Accessibility: {'ok' if permissions['accessibility'] else 'NOT GRANTED'}")
    print(f"Screen Recording: {'ok' if permissions['screen_recording'] else 'NOT GRANTED (captures may be black)'}")
    if not permissions["accessibility"]:
        raise ConfigError(
            "Grant Accessibility to the app running Relay in System Settings > Privacy & Security > "
            "Accessibility, then run calibration again."
        )
    if not permissions["screen_recording"]:
        print("Tip: grant Screen Recording too, or screenshots will be black and vision checks will fail.")

    defaults = DEFAULTS[app]
    process_name = _ask("Process name (as shown in Activity Monitor)", defaults["process_name"])
    title_contains = _ask("Window title contains", defaults["title"])
    window = backend.window(process_name, title_contains)
    if not window:
        raise ConfigError(f"could not find a window for process {process_name!r}")
    bounds = window["rect"]
    print(f"Window: {window['title']!r} at {bounds.x:.0f},{bounds.y:.0f} size {bounds.w:.0f}x{bounds.h:.0f} points")

    shots = ctx.paths.screens_dir / "calibration"
    shots.mkdir(parents=True, exist_ok=True)
    try:
        shots.chmod(0o700)
    except OSError:
        pass
    shot = shots / f"{app}-window.png"
    backend.capture_region(bounds, shot)
    print(f"Screenshot: {shot}")
    try:
        from PIL import Image  # type: ignore

        with Image.open(shot) as image:
            print(f"Image: {image.width}x{image.height} pixels (divide pixel coordinates by {image.width / bounds.w:.1f})")
    except Exception:
        pass
    if opener:
        opener(shot)
    print("All coordinates below are window-relative POINTS, not screenshot pixels.")

    input_point = _ask_point("Click point for the message input")
    if not (0 <= input_point[0] <= bounds.w and 0 <= input_point[1] <= bounds.h):
        raise ConfigError("input point is outside the window")
    submit_key = _ask("Submit key", "return")
    new_session = _ask("New-session shortcut (comma-separated modifiers + key)", "cmd,n")
    keys = [part.strip() for part in new_session.split(",") if part.strip()]

    model_picker = None
    context_region = busy_region = input_region = popup_region = None
    if app == "claude":
        use_picker = _ask("Does this app have a model picker you can click? (y/n)", "y").lower().startswith("y")
        if use_picker:
            point = _ask_point("Model picker button")
            label = _ask_region("Model label region (x,y,w,h)", optional=False)
            model_picker = {"point": list(point), "label_region": list(label) if label else None, "clear": True}
        context_region = _ask_region("Context percentage indicator region")
    else:
        context_region = _ask_region("Context percentage indicator region (blank if none)")
    busy_region = _ask_region("Busy indicator region")
    input_region = _ask_region("Input box region (to verify the pointer was sent)", optional=False)
    popup_region = _ask_region("Approval popup region (blank = whole window)")

    calibration = Calibration(
        app=app,
        process_name=process_name,
        window_title_contains=title_contains,
        input_point=input_point,
        submit_key=submit_key,
        new_session_keys=keys,
        model_picker=model_picker,
        context_region=context_region,
        busy_region=busy_region,
        input_region=input_region,
        popup_region=popup_region,
    )
    calibration.save(ctx.paths)

    result = {"app": app, "calibration": str(ctx.paths.calibration(app)), "self_test": "skipped"}
    if skip_self_test:
        return result

    proof = ctx.paths.calibration_proof(app)
    proof.unlink(missing_ok=True)
    driver = GuiAppDriver(ctx.cfg, ctx.journal, app, calibration, backend, vision=None)
    pointer = ascii_pointer(
        f"RELAY calibration app={app}: create the file .relay/CALIBRATION_{app}.json "
        f'containing exactly {{"working_dir": "<your absolute working directory>"}}. '
        "Use your file tools; do not ask questions."
    )
    print("Self-test: asking the app to write a calibration proof file...")
    driver.ring(pointer)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline and not proof.exists():
        time.sleep(2)
    if not proof.exists():
        result["self_test"] = "failed"
        raise ConfigError(
            f"self-test failed: {proof.name} did not appear within {timeout_seconds}s. "
            "Check the input click point and submit key, rerun calibration, and make sure the app's "
            "working directory is this project."
        )
    try:
        data = json.loads(proof.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"the app wrote invalid JSON to {proof.name}: {exc}") from exc
    working_dir = data.get("working_dir") or data.get("cwd") or data.get("path")
    if not working_dir:
        raise ConfigError(f"{proof.name} does not contain a working directory")
    resolved = Path(str(working_dir)).expanduser().resolve()
    if resolved != ctx.cfg.project_dir:
        raise ConfigError(
            f"the app is working in {resolved}, but Relay expects {ctx.cfg.project_dir}. "
            "Both apps must open this one project folder."
        )
    calibration.working_dir = str(resolved)
    calibration.save(ctx.paths)
    result["self_test"] = "ok"
    result["working_dir"] = str(resolved)
    ctx.journal.append("calibration.ok", actor="human", detail={"app": app, "working_dir": str(resolved)})
    print(f"Self-test ok: both Relay and {app} use {resolved}")
    return result


def shared_folder_status(paths: Paths) -> dict:
    proofs = {}
    for app in DEFAULTS:
        path = paths.calibration_proof(app)
        if not path.exists():
            proofs[app] = None
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            proofs[app] = str(Path(data.get("working_dir", "")).expanduser().resolve())
        except (json.JSONDecodeError, TypeError):
            proofs[app] = None
    values = [value for value in proofs.values() if value]
    return {
        "proofs": proofs,
        "same_folder": len(values) == len(DEFAULTS) and len(set(values)) == 1,
    }
