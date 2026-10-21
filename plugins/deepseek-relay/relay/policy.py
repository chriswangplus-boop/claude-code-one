"""Deterministic popup policy. The LLM may read a popup; only this code may approve one."""

from __future__ import annotations

import re
import shlex

from .gui.base import Popup

DANGEROUS = (
    "push", "force push", "delete", "remove", "uninstall", "install", "download", "curl", "wget",
    "network", "outside", "sudo", "chmod", "chown", "credential", "credentials", "secret",
    "api key", "private key", "keychain", "ssh", "deploy", "publish", "always allow", "all files",
    "system settings", "rm -rf", "disable safety",
)
ALLOW_WORDS = ("allow once", "allow", "yes", "approve", "run", "ok", "continue", "accept", "submit")
DENY_WORDS = ("always", "all", "don't", "dont", "deny", "cancel", "no", "reject", "block")
SHELL_META = re.compile(r"[;&|`$<>\n\\]")

# Anchored commands. A substring match is never enough.
SAFE_COMMANDS = [
    # `git add`/`git commit` are deliberately absent: only Relay commits.
    re.compile(r"^git (?:status|diff)(?: [^;&|`$<>\n\\]*)?$", re.IGNORECASE),
    re.compile(r"^(?:pytest|python3? -m pytest)(?: [^;&|`$<>\n\\]*)?$", re.IGNORECASE),
]

COMMAND_EXTRACTORS = [
    re.compile(r"(?:allow|approve|permit)\s+(?:bash|shell|terminal|command|to run)\s*:?\s*(.+)$", re.IGNORECASE),
    re.compile(r"(?:run|execute)\s+(?:the\s+)?(?:command\s+)?:?\s*(.+)$", re.IGNORECASE),
]

# N6: a whole-command match is not enough; flags can write files or load code.
FORBIDDEN_FLAGS = {
    "--output", "-o", "--ext-diff", "-c", "--config", "--config-file", "--no-verify",
    "-p", "--rootdir", "--upload-pack", "--receive-pack", "--exec-path", "--git-dir",
    "--work-tree", "--no-index", "--basetemp", "--override-ini", "--plugin",
}


def command_arguments_are_safe(command: str) -> bool:
    """Reject flags that write outside the project or load agent code, and absolute paths."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    for token in tokens[1:]:
        lowered = token.lower()
        flag = lowered.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS or lowered in FORBIDDEN_FLAGS:
            return False
        if lowered.startswith(("-p", "-c", "-o")) and lowered not in ("-",):
            return False
        if token.startswith(("/", "~", "../")) or "/../" in token or token.startswith(".git/"):
            return False
    return True


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


def extract_command(text: str) -> str | None:
    raw = str(text or "").strip()
    for pattern in COMMAND_EXTRACTORS:
        match = pattern.search(raw)
        if match:
            candidate = match.group(1).strip()
            candidate = candidate.rstrip(" ?.").strip().strip('"').strip("'")
            return candidate or None
    lowered = raw.lower()
    if lowered.startswith(("git ", "pytest", "python3 -m pytest", "python -m pytest")):
        return raw.rstrip(" ?.").strip()
    return None


def decide_popup(
    popup: Popup,
    allow_list: list[str],
    allow_commands: list[str] | None = None,
) -> str:
    """Return 'approve' or 'human'. There is deliberately no automatic deny."""
    text = normalise(popup.text)
    if not text or not popup.buttons:
        return "human"
    if any(_phrase(term, text) for term in DANGEROUS):
        return "human"
    if choose_button(popup) is None:
        return "human"

    command = extract_command(popup.text)
    if command and re.match(r"(?i)^(?:the |a )?tests?\b", command.strip()):
        command = None
    if command:
        if SHELL_META.search(command):
            return "human"
        patterns = [re.compile(str(item), re.IGNORECASE) for item in (allow_commands or [])]
        if not command_arguments_are_safe(command):
            return "human"
        if any(pattern.fullmatch(command) for pattern in patterns + SAFE_COMMANDS):
            return "approve"
        return "human"

    for item in allow_list:
        try:
            if re.fullmatch(str(item), text, re.IGNORECASE):
                return "approve"
        except re.error:
            continue
    return "human"
