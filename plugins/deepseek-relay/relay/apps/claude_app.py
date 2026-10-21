"""Calibration-driven profiles for the two desktop apps."""

from __future__ import annotations

from ..gui.app_driver import Calibration, GuiAppDriver


def make_claude_driver(cfg, paths, journal, backend, vision=None) -> GuiAppDriver:
    calibration = Calibration.load(paths, "claude")
    return GuiAppDriver(cfg, journal, "claude", calibration, backend, vision=vision)


def make_zcode_driver(cfg, paths, journal, backend, vision=None) -> GuiAppDriver:
    calibration = Calibration.load(paths, "zcode")
    return GuiAppDriver(cfg, journal, "zcode", calibration, backend, vision=vision)
