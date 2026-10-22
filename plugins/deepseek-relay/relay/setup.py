"""`relay init`: create the .relay contract, rules files, and the initial git baseline."""

from __future__ import annotations

import os
from pathlib import Path
from string import Template

from .config import Config, PACKAGE_ROOT
from .errors import ConfigError
from .integrity import git_add, git_commit, git_init, secret_scan
from .journal import Journal
from .paths import Paths
from .state import StateStore, new_state

GITIGNORE_LINES = [
    "# Relay runtime state (regenerated every turn; never committed)",
    ".relay/STATE.json",
    ".relay/BATON.json",
    ".relay/HEARTBEAT.json",
    ".relay/PAUSE",
    ".relay/STOP",
    ".relay/HALT",
    ".relay/.lock",
    ".relay/.run.pid",
    ".relay/.run.lock",
    ".relay/relay.log",
    ".relay/journal/",
    ".relay/monitor/",
    ".relay/answers/",
    ".relay/archive/",
    ".relay/calibration/",
    ".relay/CALIBRATION_*.json",
    "# Secrets",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
]

DECISIONS_HEADER = (
    "# Decisions\n\n"
    "Only human answers belong here. Every entry links to a `decision.answered` journal event.\n"
    "Agents may read this file; they may never write it.\n"
)

NOTES_HEADER = (
    "# Monitor notes\n\n"
    "Written by the independent monitor. Agents must read this file every turn.\n"
)


def _write_new(path: Path, text: str, *, force: bool = False) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return True


def _ensure_gitignore(root: Path) -> bool:
    path = root / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [line for line in GITIGNORE_LINES if line not in existing]
    if not missing:
        return False
    text = "\n".join(existing + ([""] if existing else []) + missing) + "\n"
    path.write_text(text, encoding="utf-8")
    return True


def _render_rules(name: str, project: str) -> str:
    path = PACKAGE_ROOT / "prompts" / name
    if not path.exists():
        raise ConfigError(f"missing rules template: {path}")
    return Template(path.read_text(encoding="utf-8")).substitute(project=project)


def init_project(
    cfg: Config,
    paths: Paths,
    store: StateStore,
    journal: Journal,
    *,
    goal: str = "",
    force: bool = False,
) -> dict:
    if paths.state.exists() and not force:
        updated = _ensure_gitignore(cfg.project_dir)
        return {
            "already_initialized": True,
            "phase": store.load().get("phase"),
            "gitignore_updated": updated,
        }
    if not cfg.project_dir.exists():
        raise ConfigError(f"project folder does not exist: {cfg.project_dir}")

    for directory in paths.all_working_dirs():
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

    created: list[str] = []
    goal_path = paths.goal
    if goal.strip():
        goal_path.write_text(f"# Goal\n\n{goal.strip()}\n", encoding="utf-8")
        created.append("GOAL.md")
    elif _write_new(goal_path, "# Goal\n\n(Write the project goal here before `relay start`.)\n", force=force):
        created.append("GOAL.md")

    if _write_new(paths.rules_claude, _render_rules("RULES_CLAUDE.md", cfg.project_dir.name), force=force):
        created.append("RULES_CLAUDE.md")
    if _write_new(paths.rules_codex, _render_rules("RULES_CODEX.md", cfg.project_dir.name), force=force):
        created.append("RULES_CODEX.md")
    if _write_new(paths.rules_coder, _render_rules("RULES_CODER.md", cfg.project_dir.name), force=force):
        created.append("RULES_CODER.md")
    if _write_new(paths.decisions, DECISIONS_HEADER, force=False):
        created.append("DECISIONS.md")
    if _write_new(paths.monitor_notes, NOTES_HEADER, force=False):
        created.append("MONITOR_NOTES.md")
    if _ensure_gitignore(cfg.project_dir):
        created.append(".gitignore")

    state = store.load() if paths.state.exists() else new_state(store.clock)
    state["phase"] = "INIT" if not paths.state.exists() else state.get("phase", "INIT")
    if state.get("started_at") is None:
        state["started_at"] = store.clock.now().isoformat()
    store.save(state)
    store.set_baton("none", None, int(state.get("round", 0)))

    commit = None
    if bool(cfg.get("git.enabled", True)):
        git_init(cfg.project_dir)
        targets = [
            ".gitignore", ".relay/GOAL.md", ".relay/RULES_CLAUDE.md", ".relay/RULES_CODEX.md",
        ".relay/RULES_CODER.md",
            ".relay/DECISIONS.md", ".relay/MONITOR_NOTES.md",
        ]
        existing = [target for target in targets if (cfg.project_dir / target).exists()]
        git_add(cfg.project_dir, existing)
        hits = secret_scan(cfg.project_dir, existing)
        if hits:
            raise ConfigError(f"refusing to initialize: secrets found in {hits[0]['path']}")
        commit = git_commit(cfg.project_dir, "relay: initialize", add_all=False)

    journal.append(
        "init",
        actor="harness",
        detail={"created": created, "goal": bool(goal.strip()), "git_commit": commit},
    )
    # R4-2: remember the vetted git control files as the signed last-known-good baseline.
    from .integrity import record_git_control

    record_git_control(cfg.project_dir)
    return {"already_initialized": False, "created": created, "git_commit": commit}
