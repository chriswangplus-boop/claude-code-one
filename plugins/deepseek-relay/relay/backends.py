"""Supervisor backend selection (REVISION_PLAN_3 Part 2).

Two apps can act as the supervisor: Claude Code (default) and Codex desktop. The coder is
always Zcode. Filenames and the Claude behaviour stay unchanged; only the selected supervisor's
sentinel and rules filenames differ.
"""

from __future__ import annotations

from .errors import ConfigError

SUPERVISOR_BACKENDS = ("claude", "codex")


def supervisor_backend(cfg) -> str:
    value = str(cfg.get("supervisor.backend", "claude") or "claude").strip().lower()
    if value not in SUPERVISOR_BACKENDS:
        raise ConfigError(
            f"supervisor.backend must be one of {', '.join(SUPERVISOR_BACKENDS)}; got {value!r}"
        )
    return value


def sentinel_name(role: str, backend: str) -> str:
    if role == "coder":
        return "CODER_DONE.json"
    return "CODEX_DONE.json" if backend == "codex" else "CLAUDE_DONE.json"


def rules_name(role: str, backend: str) -> str:
    if role == "coder":
        return "RULES_CODER.md"
    return "RULES_CODEX.md" if backend == "codex" else "RULES_CLAUDE.md"
