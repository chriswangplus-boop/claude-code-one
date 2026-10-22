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

# R3-6: per-command allowlist of options. Anything not listed goes to the human, and the
# value of an option (`--opt=value` or `--opt value`) must pass the same path rules.
PYTEST_OPTIONS = {
    "-q", "-v", "-x", "-s", "-k", "-m", "-r", "-W",
    "--maxfail", "--tb", "--color", "--no-header", "--strict-markers",
    "--disable-warnings", "--durations", "--collect-only", "--co", "--lf", "--ff",
    "--ignore",
}
GIT_OPTIONS = {
    "-s", "-b", "-M", "-U", "--short", "--porcelain", "--name-only", "--name-status",
    "--stat", "--cached", "--staged", "--quiet", "--no-ext-diff", "--no-color",
    "--color", "--relative", "--untracked-files", "--ignore-submodules", "--ignored",
    "--column", "--ahead-behind", "--branch", "--long", "--show-stash", "--renames",
    "--find-renames", "--diff-filter", "--ignore-space-at-eol", "--ignore-all-space",
    "--ignore-blank-lines", "--unified", "--word-diff", "--exit-code", "--check",
    "--binary", "--patch", "--full-index", "--abbrev", "--src-prefix", "--dst-prefix",
}
# Options whose value is a path: its value is always checked even without a slash.
PYTEST_PATH_OPTIONS = {"--ignore"}
PYTEST_OPEN_VALUES = {"-k", "-m", "-r", "-W", "--maxfail", "--tb", "--color", "--durations"}
GIT_OPEN_VALUES = {
    "-U", "-M", "--unified", "--diff-filter", "--find-renames", "--stat", "--color",
    "--porcelain", "--untracked-files", "--ignore-submodules", "--word-diff", "--abbrev",
    "--src-prefix", "--dst-prefix",
}


def _path_value_ok(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if text.startswith(("/", "~", "..")) or text == ".git" or text.startswith(".git/"):
        return False
    if "/../" in text or "\\" in text:
        return False
    return True


def _strip_interpreter(tokens: list[str]) -> list[str]:
    if len(tokens) >= 3 and tokens[1] == "-m" and tokens[0].split("/")[-1].startswith("python"):
        return tokens[2:]
    return tokens


def command_arguments_are_safe(command: str) -> bool:
    """Reject flags that write outside the project or load agent code, and bad option values."""
    try:
        tokens = _strip_interpreter(shlex.split(command))
    except ValueError:
        return False
    if not tokens:
        return False
    program = tokens[0].lower()
    if program == "git":
        allowed, open_values, path_options = GIT_OPTIONS, GIT_OPEN_VALUES, set()
    elif program == "pytest":
        allowed, open_values, path_options = PYTEST_OPTIONS, PYTEST_OPEN_VALUES, PYTEST_PATH_OPTIONS
    else:
        return False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return all(_path_value_ok(rest) for rest in tokens[index + 1:])
        if token.startswith("-") and token != "-":
            flag, sep, value = token.partition("=")
            if flag in FORBIDDEN_FLAGS:
                return False
            if flag.startswith("-") and not flag.startswith("--") and len(flag) > 2:
                # Combined short flags: every letter must be allowlisted (`git status -sb`).
                if not all(f"-{letter}" in allowed for letter in flag[1:]):
                    return False
                index += 1
                continue
            if flag not in allowed:
                return False
            if sep:
                if not _path_value_ok(value):
                    return False
            elif flag in path_options:
                index += 1
                if index >= len(tokens) or not _path_value_ok(tokens[index]):
                    return False
            elif flag in open_values and index + 1 < len(tokens):
                if not _path_value_ok(tokens[index + 1]):
                    return False
            index += 1
            continue
        if not _path_value_ok(token):
            return False
        index += 1
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
