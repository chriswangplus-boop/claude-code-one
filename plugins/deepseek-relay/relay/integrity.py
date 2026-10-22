"""Ownership, sentinel validation, git snapshots/commits (never push), and secret scanning."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from .errors import IntegrityError, SecretDetected
from .util import sha256_file

# --- ownership ------------------------------------------------------------

HARNESS_OWNED = {
    "STATE.json", "BATON.json", "PENDING_DECISION.md", "DECISIONS.md", "HEARTBEAT.json",
    "config.json", "GOAL.md", "PAUSE", "STOP", "HALT", ".lock", ".run.pid",
    "RULES_CLAUDE.md", "RULES_CODEX.md", "RULES_CODER.md",
}
# Which role may write which agent file. Source files belong to the role that holds the
# baton: the coder normally, the fixer only during a fixer turn (Part B).
ROLE_FILES: dict[str, set[str]] = {
    "planner": {"PLAN.md", "HANDOFF.md", "CHECKPOINT_planner.md", "CLAUDE_DONE.json", "CODEX_DONE.json"},
    "reviewer": {"REVIEW.md", "FIX_PLAN.md", "CHECKPOINT_reviewer.md", "CLAUDE_DONE.json", "CODEX_DONE.json"},
    "strategist": {
        "PLAN.md", "HANDOFF.md", "STRATEGY.md", "CHECKPOINT_strategist.md", "CLAUDE_DONE.json",
        "CODEX_DONE.json",
    },
    "fixer": {"FIX_REPORT.md", "HANDOFF.md", "CHECKPOINT_fixer.md", "CLAUDE_DONE.json", "CODEX_DONE.json"},
    "coder": {"CODER_REPORT.md", "CODER_DONE.json", "CHECKPOINT_coder.md"},
}
CLAUDE_OWNED = {"PLAN.md", "HANDOFF.md", "REVIEW.md", "FIX_PLAN.md", "STRATEGY.md", "FIX_REPORT.md",
                "CLAUDE_DONE.json", "CODEX_DONE.json", "CHECKPOINT_planner.md", "CHECKPOINT_reviewer.md",
                "CHECKPOINT_strategist.md", "CHECKPOINT_fixer.md"}
CODER_OWNED = {"CODER_REPORT.md", "CODER_DONE.json", "CHECKPOINT_coder.md"}

IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "env", ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", ".DS_Store"}
IGNORED_PREFIXES = (".relay/journal/", ".relay/archive/")
IGNORED_FILES = {
    ".relay/.lock", ".relay/.run.pid", ".relay/.run.lock", ".relay/HEARTBEAT.json",
    ".relay/relay.log", ".relay/monitor/HEARTBEAT.json", ".relay/monitor/.monitor.lock",
}
IGNORED_SUFFIXES = (".tmp", ".swp", ".pyc", ".log")


def relpath(path: str | Path, root: str | Path) -> str:
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def owner_of(rel: str, active_role: str | None = None) -> str:
    """Who may write this path.

    Source files belong to the baton role (`coder`, or `fixer` during a fixer turn).
    Agent files list every role allowed to write them, e.g. `planner/strategist`.
    """
    if not rel.startswith(".relay/"):
        return active_role if active_role in ("coder", "fixer") else "coder"
    name = rel[len(".relay/"):]
    if name in HARNESS_OWNED or name.startswith("journal/") or name.startswith("answers/"):
        return "harness"
    roles = sorted(role for role, names in ROLE_FILES.items() if name in names)
    if roles:
        return "/".join(roles)
    if name == "MONITOR_NOTES.md" or name.startswith("monitor/"):
        return "monitor"
    return "harness"


def _is_ignored(rel: str) -> bool:
    if rel in IGNORED_FILES or rel.endswith(IGNORED_SUFFIXES):
        return True
    return any(rel.startswith(prefix) for prefix in IGNORED_PREFIXES)


def iter_watch_files(root: Path) -> Iterable[tuple[str, Path]]:
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            try:
                rel = path.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            if _is_ignored(rel):
                continue
            try:
                if path.stat().st_size > 10 * 1024 * 1024:
                    continue
            except OSError:
                continue
            yield rel, path


def snapshot(root: str | Path) -> dict[str, str]:
    """sha256 of every watched file (source + agent-authored .relay files)."""
    out: dict[str, str] = {}
    for rel, path in iter_watch_files(Path(root)):
        try:
            out[rel] = sha256_file(path)
        except OSError:
            continue
    return out


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(key for key in keys if before.get(key) != after.get(key))


CONTROL_EVENTS = {
    ".relay/PAUSE": {"human.pause"},
    ".relay/STOP": {"human.stop"},
    ".relay/HALT": {"monitor.halt"},
    ".relay/DECISIONS.md": {"decision.answered", "decision.asked"},
    ".relay/PENDING_DECISION.md": {"decision.asked", "decision.answered"},
    ".relay/MONITOR_NOTES.md": {"monitor.note"},
    ".relay/monitor/CORRECTION.json": {"monitor.correct"},
    ".relay/monitor/layer2.json": {"monitor.layer2"},
}
STATE_EVENTS = {"decision.answered", "decision.asked", "human.halt_cleared"}


def _events_after(events: Iterable[dict], turn_start_id: str | None) -> list[dict]:
    if not events:
        return []
    ordered = list(events)
    if turn_start_id is None:
        return ordered
    for index, event in enumerate(ordered):
        if event.get("id") == turn_start_id:
            return ordered[index + 1:]
    return ordered


def _control_allowed(rel: str, events: list[dict]) -> bool:
    allowed = CONTROL_EVENTS.get(rel)
    if allowed is None:
        if rel in (".relay/STATE.json", ".relay/BATON.json"):
            allowed = STATE_EVENTS
        elif rel.startswith(".relay/answers/"):
            allowed = {"decision.answered"}
        else:
            return False
    for event in events:
        if event.get("type") in allowed and event.get("actor") in ("human", "monitor"):
            return True
    return False


def _actor_may_write(actor: str, owner: str) -> bool:
    """`owner` is a role, a `role/role` set, or a legacy broad label."""
    if "/" in owner:
        allowed = owner.split("/")
        if actor == "claude":  # legacy alias used by older callers/tests
            return all(role != "coder" for role in allowed)
        return actor in allowed
    if actor == "claude":
        return owner not in ("coder", "monitor", "harness")
    return actor == owner


def ownership_violations(
    before: dict[str, str],
    after: dict[str, str],
    actor: str,
    *,
    events: Iterable[dict] | None = None,
    turn_start_id: str | None = None,
    active_role: str | None = None,
) -> list[dict[str, Any]]:
    """Which changed files were written by someone other than `actor`.

    Agent files follow the role table. Source files belong to `active_role` (the baton
    holder: coder or fixer). Control files need proof from a journal event written by the
    harness/monitor/human during this turn; otherwise they are a violation.
    """
    if actor == "human":
        return []
    history = _events_after(events or [], turn_start_id)
    violations = []
    for rel in changed_paths(before, after):
        owner = owner_of(rel, active_role)
        if owner in ("monitor", "harness"):
            if _control_allowed(rel, history):
                continue
            violations.append({"path": rel, "writer": actor, "owner": owner, "reason": "control file changed without a matching event"})
            continue
        if not _actor_may_write(actor, owner):
            violations.append({"path": rel, "writer": actor, "owner": owner})
    return violations


# --- sentinels ------------------------------------------------------------

ALLOWED_STATUS = {"done", "blocked", "needs_decision"}
ISSUE_ID = re.compile(r"^[a-z0-9-]{3,40}$")
ALLOWED_VERDICTS = {"APPROVED", "CHANGES_REQUESTED", "MILESTONE_DONE", "PROJECT_DONE"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def read_sentinel(path: Path) -> dict:
    import json

    if not path.exists():
        raise IntegrityError(f"sentinel not found: {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IntegrityError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IntegrityError(f"{path.name} must contain a JSON object")
    return data


def validate_sentinel(
    sentinel: dict,
    *,
    root: str | Path,
    expected_round: int,
    expected_role: str,
    expected_file: str,
    round_started_wall: float,
    mtime_tolerance: float = 2.0,
    expected_backend: str = "claude",
) -> list[str]:
    """Return a list of problems; an empty list means the sentinel may be trusted."""
    issues: list[str] = []
    root = Path(root).resolve()

    try:
        round_no = int(sentinel.get("round"))
    except (TypeError, ValueError):
        round_no = None
    if round_no != expected_round:
        issues.append(f"round is {sentinel.get('round')!r}, expected {expected_round}")

    role = str(sentinel.get("role", ""))
    if role != expected_role:
        issues.append(f"role is {role!r}, expected {expected_role}")

    status = sentinel.get("status")
    if status not in ALLOWED_STATUS:
        issues.append(f"status is {status!r}")

    verdict = sentinel.get("verdict")
    if verdict is not None and verdict not in ALLOWED_VERDICTS:
        issues.append(f"verdict is {verdict!r}")
    is_checkpoint = os.path.normpath(str(sentinel.get("file") or "")).replace("\\", "/").startswith(".relay/CHECKPOINT_")
    if expected_role == "reviewer" and status == "done" and verdict not in ALLOWED_VERDICTS and not is_checkpoint:
        issues.append("reviewer sentinel needs a verdict")

    ctx = sentinel.get("context_estimate_pct")
    if ctx is not None:
        try:
            ctx_float = float(ctx)
            if not 0 <= ctx_float <= 100:
                issues.append(f"context_estimate_pct out of range: {ctx}")
        except (TypeError, ValueError):
            issues.append(f"context_estimate_pct is not a number: {ctx!r}")

    summary = sentinel.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        issues.append("summary is missing")

    if status == "needs_decision" and not str(sentinel.get("question") or "").strip():
        issues.append("needs_decision without a question")

    issue_ids = sentinel.get("blocking_issue_ids")
    if issue_ids is not None:
        if not isinstance(issue_ids, list):
            issues.append("blocking_issue_ids must be a list")
        elif len(issue_ids) > 10:
            issues.append("blocking_issue_ids has more than 10 entries")
        else:
            for item in issue_ids:
                if not isinstance(item, str) or not ISSUE_ID.match(item):
                    issues.append(f"blocking_issue_ids has an invalid slug: {item!r}")

    backend = sentinel.get("backend")
    if backend is not None and str(backend) != str(expected_backend):
        issues.append(
            f"sentinel is for the {backend} supervisor, expected {expected_backend}"
        )

    announced = str(sentinel.get("file") or "")
    if not announced:
        issues.append("file is missing")
    else:
        if Path(announced).is_absolute():
            issues.append("file must be project-relative")
        normalized = os.path.normpath(announced).replace("\\", "/")
        if normalized.startswith("../") or "/../" in normalized:
            issues.append("file escapes the project folder")
        if normalized != os.path.normpath(expected_file).replace("\\", "/"):
            issues.append(f"announces {announced!r}, expected {expected_file!r}")
        target = root / normalized
        if not target.exists():
            issues.append(f"announced file does not exist: {normalized}")
        else:
            digest = str(sentinel.get("sha256") or "")
            if not SHA256.match(digest):
                issues.append("sha256 is missing or malformed")
            elif sha256_file(target) != digest:
                issues.append("sha256 does not match the announced file")
            if target.stat().st_mtime + mtime_tolerance < round_started_wall:
                issues.append("announced file predates this round")

    from .backends import sentinel_name

    sentinel_path = root / ".relay" / sentinel_name(expected_role, expected_backend)
    if sentinel_path.exists() and sentinel_path.stat().st_mtime + mtime_tolerance < round_started_wall:
        issues.append("sentinel predates this round (stale)")

    return issues


# --- git ------------------------------------------------------------------

# An agent can write .git/config and .git/hooks; `.git` is never snapshotted as a turn file.
# Every git command Relay runs therefore ignores hooks, fsmonitor, pager and external diff, and
# never prompts for credentials or reads system config (review #2, N2).
GIT_SAFE_ARGS = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.pager=cat",
    "-c", "commit.gpgsign=false",
    "-c", "tag.gpgsign=false",
]
GIT_SAFE_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_EXTERNAL_DIFF": "",
    "GIT_PAGER": "cat",
}

GIT_CONTROL_PATHS = (".git/config", ".git/info/attributes", ".gitmodules")

# R3-5: neutralising hooks and fsmonitor is not enough. `filter.*.clean/smudge/process`,
# `diff.*.textconv/command`, `merge.*.driver`, a credential helper, `core.sshCommand` and an
# `include.path` can all run an agent's command from `.git/config` or `.gitattributes`. Every
# git call therefore refuses to run while one of those is configured, unless the driver is on
# this explicit allowlist (git-lfs is the only common safe case).
GIT_DRIVER_ALLOWLIST = {"lfs"}
GIT_CREDENTIAL_ALLOWLIST = {"", "osxkeychain", "store", "cache", "libsecret", "manager"}
# R4-2: the driver *name* is not enough. An agent can define filter.lfs.clean as any command.
# The lfs filter is allowed only with the exact standard git-lfs values.
GIT_LFS_VALUES = {
    "clean": "git-lfs clean -- %f",
    "smudge": "git-lfs smudge -- %f",
    "process": "git-lfs filter-process",
    "required": "true",
}
GIT_LFS_SAFE_DIFF = {"git-lfs textconv"}
GIT_LFS_SAFE_MERGE = {"git-lfs merge"}

# One trust store per project root: a process can open several projects (tests do).
_TRUST_BY_ROOT: dict[str, object] = {}


def bind_trust(trust) -> None:
    """Give the git guard the external trust store for this project (from open_project)."""
    _TRUST_BY_ROOT[str(Path(trust.project_dir).resolve())] = trust


def _trust_for(root: str | Path):
    return _TRUST_BY_ROOT.get(str(Path(root).resolve()))
GIT_SECTION = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*$')


def _config_pairs(text: str):
    """Yield ((section, subsection), key, value) for a git config file, leniently."""
    section: tuple[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        match = GIT_SECTION.match(line)
        if match:
            section = (match.group(1).lower(), (match.group(2) or "").lower())
            continue
        if section is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        yield section, key.strip().lower(), value.strip().strip('"')


def _config_files(root: Path) -> list[Path]:
    """The local config plus every `include.path` it pulls in (bounded depth)."""
    root = Path(root)
    seen: set[Path] = set()
    out: list[Path] = []

    def add(path: Path, depth: int = 0) -> None:
        path = path.expanduser()
        try:
            path = path.resolve()
        except OSError:
            return
        if depth > 5 or path in seen or not path.is_file():
            return
        seen.add(path)
        out.append(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for (name, sub), key, value in _config_pairs(text):
            if key == "path" and (name == "include" or name == "includeif"):
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = path.parent / candidate
                add(candidate, depth + 1)

    add(root / ".git" / "config")
    return out


def git_config_risks(root: str | Path) -> list[str]:
    """Reasons `.git/config` (or an included file) could make git run an agent's command."""
    risks: list[str] = []
    for path in _config_files(Path(root)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        label = path.name if path.name == "config" else str(path)
        for (name, sub), key, value in _config_pairs(text):
            lowered = value.strip().lower()
            if name == "include" or name == "includeif":
                continue
            if name == "filter" and sub and key in ("clean", "smudge", "process", "required"):
                if sub in GIT_DRIVER_ALLOWLIST:
                    expected = GIT_LFS_VALUES.get(key)
                    if key == "required":
                        if lowered not in ("true", "false"):
                            risks.append(f"{label}: filter.{sub}.required")
                    elif expected is None or " ".join(value.split()) != expected:
                        risks.append(f"{label}: filter.{sub}.{key} (not the standard git-lfs command)")
                else:
                    risks.append(f"{label}: filter.{sub}.{key}")
            elif name == "diff" and sub and key in ("textconv", "command"):
                if not (sub in GIT_DRIVER_ALLOWLIST and " ".join(value.split()) in GIT_LFS_SAFE_DIFF):
                    risks.append(f"{label}: diff.{sub}.{key}")
            elif name == "merge" and sub and key == "driver":
                if not (sub in GIT_DRIVER_ALLOWLIST and " ".join(value.split()) in GIT_LFS_SAFE_MERGE):
                    risks.append(f"{label}: merge.{sub}.driver")
            elif name == "core" and key in ("fsmonitor", "sshcommand", "gitproxy", "askpass"):
                if lowered not in ("", "false"):
                    risks.append(f"{label}: core.{key}")
            elif name == "credential" and key == "helper":
                helper = lowered.lstrip("!").split()[0] if lowered.lstrip("!").split() else ""
                if helper not in GIT_CREDENTIAL_ALLOWLIST:
                    risks.append(f"{label}: credential.helper")
    return risks


def _attribute_files(root: Path) -> list[tuple[str, Path]]:
    """Every `.gitattributes` git honours: the working tree (all depths) plus `.git/info`."""
    root = Path(root)
    files: list[tuple[str, Path]] = []
    for rel, path in iter_watch_files(root):
        if path.name == ".gitattributes":
            files.append((rel, path))
    info = root / ".git" / "info" / "attributes"
    if info.is_file():
        files.append((".git/info/attributes", info))
    return files


def git_attribute_risks(root: str | Path) -> list[str]:
    """Reasons any `.gitattributes` selects a non-allowlisted filter/diff/merge driver."""
    root = Path(root)
    risks: list[str] = []
    for rel, path in _attribute_files(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            for token in line.split()[1:]:
                for prefix in ("filter=", "diff=", "merge="):
                    if token.startswith(prefix):
                        driver = token[len(prefix):].strip('"').lower()
                        if driver and driver not in GIT_DRIVER_ALLOWLIST:
                            risks.append(f"{rel}:{lineno}: {prefix}{driver}")
    return risks


def git_control_risks(root: str | Path) -> list[str]:
    return git_config_risks(root) + git_attribute_risks(root)


def assert_git_control_safe(root: str | Path) -> None:
    """Refuse any git call while a config or attributes file can execute an agent's command.

    R4-2: also compare against the signed, last-known-good snapshot, so a change made between
    turns (for example while Relay waits for a human) is caught before git runs it.
    """
    risks = git_control_risks(root)
    if risks:
        raise IntegrityError(
            "unsafe git configuration or attributes (a filter, driver or include could run an "
            "agent's command): " + "; ".join(risks[:5])
        )
    trust = _trust_for(root)
    if trust is not None:
        baseline = trust.git_control()
        if baseline is not None:
            changed = git_control_changes(baseline, git_control_snapshot(root))
            if changed:
                raise IntegrityError(
                    "git control files changed since the last verified turn: "
                    + ", ".join(changed[:5])
                    + ". Restore them, or record a human-accepted baseline with "
                    "`relay resume --accept-git-control --code <code>`."
                )


def _git(root: Path, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    assert_git_control_safe(root)
    env = {**os.environ, **GIT_SAFE_ENV}
    return subprocess.run(
        ["git", "-C", str(root), *GIT_SAFE_ARGS, *args],
        capture_output=True, text=True, timeout=timeout, check=False, env=env,
    )


def is_git_repo(root: str | Path) -> bool:
    try:
        return _git(Path(root), ["rev-parse", "--is-inside-work-tree"]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def git_init(root: str | Path) -> bool:
    root = Path(root)
    if is_git_repo(root):
        return False
    result = _git(root, ["init"])
    if result.returncode != 0:
        raise IntegrityError(f"git init failed: {result.stderr.strip()}")
    return True


def git_control_snapshot(root: str | Path) -> dict[str, str]:
    """Hashes of the files that can make git run an agent's command.

    Compared before and after a turn; any change is an ownership violation. Hooks are also
    neutralised by `GIT_SAFE_ARGS`, so a planted one never runs even before it is noticed.
    """
    root = Path(root)
    out: dict[str, str] = {}
    for rel in GIT_CONTROL_PATHS:
        path = root / rel
        if path.is_file():
            try:
                out[rel] = sha256_file(path)
            except OSError:
                continue
    hooks = root / ".git" / "hooks"
    if hooks.is_dir():
        for path in sorted(hooks.iterdir()):
            if path.is_file():
                out[path.relative_to(root).as_posix()] = sha256_file(path)
    for rel, path in _attribute_files(root):
        if rel == ".git/info/attributes":
            out[rel] = sha256_file(path)
        else:
            out[rel] = sha256_file(path)
    return out


def record_git_control(root: str | Path) -> dict:
    """Store a signed "last known good" snapshot of the git control files (R4-2)."""
    hashes = git_control_snapshot(root)
    trust = _trust_for(root)
    if trust is not None:
        trust.set_git_control(hashes)
    return hashes


def accept_git_control(root: str | Path) -> dict:
    """Human-accepted re-baseline: only when the current files are themselves safe."""
    risks = git_control_risks(root)
    if risks:
        raise IntegrityError(
            "refusing to accept git control files that are unsafe: " + "; ".join(risks[:5])
        )
    return record_git_control(root)


def git_control_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(key for key in keys if before.get(key) != after.get(key))


def git_snapshot(root: str | Path) -> list[str]:
    if not is_git_repo(root):
        return []
    result = _git(Path(root), ["status", "--porcelain"])
    return [line for line in result.stdout.splitlines() if line.strip()]


def _identity_args(root: Path) -> list[str]:
    try:
        email = _git(root, ["config", "user.email"]).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        email = ""
    if email:
        return []
    return ["-c", "user.name=relay", "-c", "user.email=relay@localhost"]


def git_add(root: str | Path, paths: Iterable[str]) -> None:
    root = Path(root)
    items = [p for p in paths if p]
    if not items:
        return
    result = _git(root, ["add", "--", *items])
    if result.returncode != 0:
        raise IntegrityError(f"git add failed: {result.stderr.strip()}")


def git_commit(root: str | Path, message: str, add_all: bool = True) -> str | None:
    """Stage and commit. Returns the commit hash, or None when there was nothing to commit.

    There is deliberately no push helper in Relay.
    """
    root = Path(root)
    if not is_git_repo(root):
        return None
    if add_all:
        added = _git(root, ["add", "-A"])
        if added.returncode != 0:
            raise IntegrityError(f"git add failed: {added.stderr.strip()}")
    staged = _git(root, ["diff", "--cached", "--quiet", "--no-ext-diff"])
    if staged.returncode == 0:
        return None
    staged_files = [
        line
        for line in _git(root, ["diff", "--cached", "--name-only", "--no-ext-diff"]).stdout.splitlines()
        if line.strip()
    ]
    hits = secret_scan(root, staged_files)
    if hits:
        summary = ", ".join(f"{h['path']}:{h['line']} ({h['rule']})" for h in hits[:5])
        raise SecretDetected(f"secret scan blocked the commit: {summary}")
    result = _git(root, [*_identity_args(root), "commit", "--no-verify", "-m", message])
    if result.returncode != 0:
        raise IntegrityError(f"git commit failed: {result.stderr.strip()}")
    head = _git(root, ["rev-parse", "HEAD"]).stdout.strip()
    return head or None


# --- secret scanning ------------------------------------------------------

SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("api_key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("assigned_secret", re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)\b"
        r"\s*[:=]\s*[\"']?([^\s\"'#]{8,})"
    )),
    ("env_secret", re.compile(
        r"(?m)^[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY)[A-Z0-9_]*\s*=\s*(\S+)"
    )),
]

_PLACEHOLDER = re.compile(
    r"(?i)^(?:your(?:[_-]\w+)?|example|placeholder|changeme|redacted|\[secret\]|<[^>]*>|"
    r"\$\{[^}]*\}|\$\([^)]*\)|os\.environ.*|none|null|true|false)$"
)
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".toml", ".yaml", ".yml", ".sh", ".bash", ".zsh", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sql", ".rb", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".env", ".ini", ".cfg", ".conf"}
MAX_SCAN_BYTES = 2 * 1024 * 1024


def _looks_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith(".env"):
        return True
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" not in chunk


def _scan_text(text: str) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for rule, pattern in SECRET_RULES:
        for match in pattern.finditer(text):
            candidate = match.group(match.lastindex or 0).strip("\"'")
            if _PLACEHOLDER.search(candidate):
                continue
            if rule in {"api_key", "assigned_secret", "env_secret"} and len(candidate) < 8:
                continue
            line = text.count("\n", 0, match.start()) + 1
            hits.append((rule, line))
    return hits


def secret_scan(root: str | Path, paths: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Scan project files (or a given subset) for credentials. Never returns the secret itself."""
    root = Path(root)
    if paths is None:
        candidates = list(iter_watch_files(root))
    else:
        candidates = []
        for rel in paths:
            if _is_ignored(rel):
                continue
            path = root / rel
            if path.is_file():
                candidates.append((rel, path))
    hits: list[dict[str, Any]] = []
    for rel, path in candidates:
        try:
            if path.stat().st_size > MAX_SCAN_BYTES or not _looks_text(path):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rule, line in _scan_text(text):
            hits.append({"path": rel, "rule": rule, "line": line})
    return hits


def assert_no_secrets(root: str | Path, paths: Iterable[str] | None = None) -> None:
    hits = secret_scan(root, paths)
    if hits:
        summary = ", ".join(f"{h['path']}:{h['line']} ({h['rule']})" for h in hits[:5])
        raise SecretDetected(f"secret scan blocked the turn: {summary}")


# --- screens retention ----------------------------------------------------

def prune_screens(screens_dir: Path, retention_days: int, now: float | None = None) -> list[Path]:
    if retention_days <= 0 or not screens_dir.exists():
        return []
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    removed = []
    for path in screens_dir.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass
    return removed
