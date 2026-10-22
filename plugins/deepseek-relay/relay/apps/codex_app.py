"""Calibration-driven profile for the Codex desktop app (second supervisor)."""

from __future__ import annotations

from ..gui.app_driver import Calibration, GuiAppDriver


def make_codex_driver(cfg, paths, journal, backend, vision=None) -> GuiAppDriver:
    calibration = Calibration.load(paths, "codex")
    return GuiAppDriver(cfg, journal, "codex", calibration, backend, vision=vision)
