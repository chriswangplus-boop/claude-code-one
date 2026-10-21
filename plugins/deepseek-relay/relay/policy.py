"""Deterministic popup policy. The LLM may read a popup; only this code may approve one."""

from __future__ import annotations

import re

from .gui.base import Popup

DANGEROUS = (
    "push", "force push", "delete", "remove", "uninstall", "install", "download", "curl", "wget",
    "network", "outside", "sudo", "chmod", "chown", "credential", "credentials", "secret",
    "api key", "private key", "keychain", "ssh", "deploy", "publish", "always allow", "all files",
    "system settings", "rm -rf", "disable safety",
)
ALLOW_WORDS = ("allow once", "allow", "yes", "approve", "run", "ok", "continue", "accept", "submit")
DENY_WORDS = ("always", "all", "don't", "dont", "deny", "cancel", "no", "reject", "block")


def _phrase(term: str, text: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def choose_button(popup: Popup) -> str | None:
    for wanted in ALLOW_WORDS:
        for button in popup.buttons:
            label = normalise(button)
            if not label:
                continue
            if wanted in label and not any(_phrase(deny, label) for deny in DENY_WORDS):
                return button
    return None


def decide_popup(popup: Popup, allow_list: list[str]) -> str:
    """Return 'approve' or 'human'. There is deliberately no automatic deny."""
    text = normalise(popup.text)
    if not text or not popup.buttons:
        return "human"
    if any(_phrase(term, text) for term in DANGEROUS):
        return "human"
    if not any(normalise(item) in text for item in allow_list if str(item).strip()):
        return "human"
    return "approve" if choose_button(popup) is not None else "human"
