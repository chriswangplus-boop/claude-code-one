"""Bounded GUI recovery: at most six concrete actions, then a human."""

from __future__ import annotations

import time

from ..errors import AppMismatch, PauseRequested, RecoveryFailed
from ..policy import choose_button, decide_popup
from .base import AppDriver


class Recovery:
    def __init__(self, driver: AppDriver, journal, cfg, *, max_actions: int | None = None, sleep=None):
        self.driver = driver
        self.journal = journal
        self.cfg = cfg
        self.max_actions = int(max_actions or cfg.get("gui.max_recovery_actions", 6))
        self.sleep = sleep or time.sleep

    def ensure_ready(self) -> bool:
        self.journal.append("recovery.start", detail={"app": self.driver.name, "max_actions": self.max_actions})
        for action in range(1, self.max_actions + 1):
            try:
                self.driver.focus()
            except (AppMismatch, PauseRequested):
                self.journal.append("recovery.action", detail={"n": action, "action": "refocus"}, result="failed")
                self.sleep(1.0)
                continue
            popup = self.driver.read_popup()
            if popup is not None and (popup.text or popup.buttons):
                if decide_popup(popup, list(self.cfg.get("popup_allow_list", []))) == "approve":
                    button = choose_button(popup)
                    if button:
                        self.driver.click_popup(popup, button)
                        self.journal.append(
                            "recovery.action", detail={"n": action, "action": f"popup:{button}"}, result="ok"
                        )
                        continue
                raise RecoveryFailed(f"recovery met a non-allowlisted popup: {popup.text[:160]}")
            self.journal.append("recovery.end", detail={"actions": action, "app": self.driver.name})
            return True
        raise RecoveryFailed(f"could not make {self.driver.name} ready in {self.max_actions} actions")
