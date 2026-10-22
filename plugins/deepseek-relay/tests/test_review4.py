"""Review #4 regressions: human proof for CLI actions, git-lfs, attributes, decision recovery."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from relay.decisions import hash_code
from relay.context import open_project

ROOT = Path(__file__).resolve().parents[1]


def _project(tmp_path: Path):
    project = tmp_path / "proj"
    from relay.cli import main as cli_main

    assert cli_main(["init", "--project", str(project), "--no-ntfy"]) == 0
    return project, open_project(project)


def _store_code(ctx, action: str, code: str) -> None:
    ctx.trust.store_action_code(action, hash_code(code), ttl=600, now=ctx.clock.epoch())


# --- R4-1: clearing a halt and adopting a baseline need a human proof -----------------

def test_clear_halt_without_a_code_does_not_clear_it(tmp_path, capsys, monkeypatch):
    from relay.cli import main as cli_main

    project, ctx = _project(tmp_path)
    ctx.store.set_flag("halt", {"reason": "test"})
    ctx.journal.append("monitor.halt", round_no=1, detail={"reason": "test"})
    monkeypatch.setattr("relay.cli._run_loop", lambda *args, **kwargs: 0)

    assert cli_main(["resume", "--project", str(project), "--clear-halt"]) == 2

    assert ctx.paths.halt_flag.exists(), "a HALT must not be cleared without human proof"
    assert ctx.journal.of_type("human.halt_cleared") == []
    assert ctx.journal.of_type("action.code_undelivered") or ctx.journal.of_type(
        "action.code_issued"
    )
    capsys.readouterr()


def test_clear_halt_with_the_wrong_code_does_not_clear_it(tmp_path, capsys, monkeypatch):
    from relay.cli import main as cli_main

    project, ctx = _project(tmp_path)
    ctx.store.set_flag("halt", {"reason": "test"})
    _store_code(ctx, "clear-halt", "right")
    monkeypatch.setattr("relay.cli._run_loop", lambda *args, **kwargs: 0)

    assert cli_main(["resume", "--project", str(project), "--clear-halt", "--code", "wrong"]) == 2
    assert ctx.paths.halt_flag.exists()
    assert ctx.journal.of_type("human.halt_cleared") == []
    capsys.readouterr()


def test_clear_halt_with_the_code_clears_it_once(tmp_path, capsys, monkeypatch):
    from relay.cli import main as cli_main

    project, ctx = _project(tmp_path)
    ctx.store.set_flag("halt", {"reason": "test"})
    _store_code(ctx, "clear-halt", "right")
    monkeypatch.setattr("relay.cli._run_loop", lambda *args, **kwargs: 0)

    assert cli_main(["resume", "--project", str(project), "--clear-halt", "--code", "right"]) == 0

    assert not ctx.paths.halt_flag.exists()
    assert len(ctx.journal.of_type("human.halt_cleared")) == 1
    assert ctx.journal.of_type("action.code_used")
    assert ctx.trust.action_code("clear-halt") is None
    # One use only: a second attempt with the same code asks for a new one.
    ctx.store.set_flag("halt", {"reason": "again"})
    capsys.readouterr()
    assert cli_main(["resume", "--project", str(project), "--clear-halt", "--code", "right"]) == 2
    capsys.readouterr()


def test_adopt_baseline_without_a_code_does_nothing(tmp_path, capsys, monkeypatch):
    from relay.cli import main as cli_main

    project, ctx = _project(tmp_path)
    state = ctx.store.load()
    state["phase"] = "PAUSED"
    state["return_to"] = "PLAN"
    state["pending"] = {
        "phase": "PLAN", "role": "planner", "round": 1, "pointer": "p",
        "expected_file": ".relay/HANDOFF.md", "started_wall": 1.0,
    }
    ctx.store.save(state)
    monkeypatch.setattr("relay.cli._run_loop", lambda *args, **kwargs: 0)

    assert cli_main(["resume", "--project", str(project), "--adopt-baseline"]) == 2

    assert ctx.journal.of_type("baseline.adopted") == []
    assert ctx.store.load()["pending"].get("attempt_id") is None
    capsys.readouterr()


def test_adopt_baseline_with_the_code_records_it(tmp_path, capsys, monkeypatch):
    from relay.cli import main as cli_main

    project, ctx = _project(tmp_path)
    state = ctx.store.load()
    state["phase"] = "PAUSED"
    state["return_to"] = "PLAN"
    state["pending"] = {
        "phase": "PLAN", "role": "planner", "round": 1, "pointer": "p",
        "expected_file": ".relay/HANDOFF.md", "started_wall": 1.0,
    }
    ctx.store.save(state)
    _store_code(ctx, "adopt-baseline", "right")
    monkeypatch.setattr("relay.cli._run_loop", lambda *args, **kwargs: 0)

    assert cli_main(["resume", "--project", str(project), "--adopt-baseline", "--code", "right"]) == 0

    adopted = ctx.store.load()["pending"]["attempt_id"]
    assert adopted and ctx.trust.attempt(adopted) is not None
    assert ctx.journal.of_type("baseline.adopted")
    capsys.readouterr()


def test_the_two_resume_actions_cannot_share_one_code(tmp_path, capsys):
    from relay.cli import main as cli_main

    project, ctx = _project(tmp_path)
    assert cli_main([
        "resume", "--project", str(project), "--clear-halt", "--adopt-baseline", "--code", "x",
    ]) == 2
    assert "separately" in capsys.readouterr().err
    capsys.readouterr()


def test_every_human_actor_journal_write_is_gated():
    """Static audit: a new `actor="human"` write must not slip in without proof."""
    expected = {
        ("relay/cli.py", "cmd_resume"),        # one-time code
        ("relay/cli.py", "cmd_pause"),         # exempt: only makes Relay safer
        ("relay/cli.py", "cmd_stop"),          # exempt: only makes Relay safer
        ("relay/gui/calibrate.py", "calibrate_app"),  # interactive, human-run command
    }

    def enclosing(lines: list[str], index: int) -> str:
        for line in reversed(lines[: index + 1]):
            match = re.match(r"^def (\w+)\(", line)
            if match:
                return match.group(1)
        return "?"

    found = set()
    for path in (ROOT / "relay").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if 'actor="human"' in line:
                found.add((str(path.relative_to(ROOT)), enclosing(lines, index)))
    assert found == expected, f"ungated human writes: {sorted(found - expected)}"


# --- R4-2: git-lfs values, and a signed last-known-good git control baseline ----------

def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_a_fake_lfs_filter_command_is_refused(tmp_path):
    from relay.errors import IntegrityError
    from relay.integrity import git_commit, git_init

    git_init(tmp_path)
    _write(tmp_path, "a.txt", "one")
    assert git_commit(tmp_path, "base")
    marker = tmp_path / "lfs-ran"
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "filter.lfs.clean",
         f"sh -c 'touch {marker}; cat'"],
        check=True, capture_output=True,
    )
    _write(tmp_path, ".gitattributes", "*.txt filter=lfs\n")
    _write(tmp_path, "a.txt", "two")

    with pytest.raises(IntegrityError, match="not the standard git-lfs command"):
        git_commit(tmp_path, "two")
    assert not marker.exists(), "the fake lfs filter ran"


def test_the_standard_lfs_values_are_allowed(tmp_path):
    from relay import integrity
    from relay.integrity import git_init

    git_init(tmp_path)
    for key, value in (
        ("clean", "git-lfs clean -- %f"),
        ("smudge", "git-lfs smudge -- %f"),
        ("process", "git-lfs filter-process"),
        ("required", "true"),
    ):
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", f"filter.lfs.{key}", value],
            check=True, capture_output=True,
        )
    assert integrity.git_config_risks(tmp_path) == []


def test_a_git_control_edit_between_turns_pauses_before_any_git_command(tmp_path):
    from conftest import ManualClock
    from relay import integrity
    from relay.errors import IntegrityError
    from relay.integrity import git_snapshot
    from relay.setup import init_project

    ctx = open_project(tmp_path, env={}, clock=ManualClock())
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="baseline")
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "core.quotepath", "false"],
        check=True, capture_output=True,
    )

    with pytest.raises(IntegrityError, match="changed since the last verified turn"):
        git_snapshot(tmp_path)

    integrity.accept_git_control(tmp_path)  # explicit human re-baseline
    git_snapshot(tmp_path)


def test_a_nested_gitattributes_is_scanned(tmp_path):
    from relay import integrity
    from relay.errors import IntegrityError
    from relay.integrity import git_init

    git_init(tmp_path)
    _write(tmp_path, "sub/.gitattributes", "*.bin filter=evil\n")

    risks = integrity.git_attribute_risks(tmp_path)
    assert any("sub/.gitattributes" in risk for risk in risks)
    with pytest.raises(IntegrityError, match="filter=evil"):
        integrity.assert_git_control_safe(tmp_path)


def test_accept_git_control_needs_a_code_and_rebaselines(tmp_path, capsys, monkeypatch):
    from relay.cli import main as cli_main

    project, ctx = _project(tmp_path)
    monkeypatch.setattr("relay.cli._run_loop", lambda *args, **kwargs: 0)

    assert cli_main(["resume", "--project", str(project), "--accept-git-control"]) == 2
    assert ctx.journal.of_type("git.control_accepted") == []

    _store_code(ctx, "accept-git-control", "right")
    assert cli_main([
        "resume", "--project", str(project), "--accept-git-control", "--code", "right",
    ]) == 0
    assert ctx.journal.of_type("git.control_accepted")
    capsys.readouterr()


# --- R4-4: a tampered decision is re-asked, not dead-ended ---------------------------

def test_a_tampered_decision_is_reasked_and_the_new_code_works(ctx):
    from relay.decisions import accept_decision
    from relay.errors import IntegrityError
    from relay.notify import FakeNotifier
    from relay.orchestrator import Orchestrator

    options = [
        {"label": "JSON", "consequence": "simple"},
        {"label": "SQLite", "consequence": "robust"},
    ]
    ctx.store.ensure()
    orch = Orchestrator(ctx, {}, notifier=FakeNotifier())
    first = orch.escalate_human("Which storage?", options, return_to="PLAN", kind="round_cap", data={})
    old_code = orch._decision_codes[first]

    ctx.store.mutate(lambda state: state["pending_decision"].__setitem__("return_to", "CODE"))
    with pytest.raises(IntegrityError, match="relay resume"):
        accept_decision(ctx, "JSON", old_code)

    orch2 = Orchestrator(ctx, {}, notifier=FakeNotifier())
    orch2.resume(max_steps=1, wait_human=False)

    pending = ctx.store.load()["pending_decision"]
    assert pending is not None and pending["id"] != first, "the question must be re-asked"
    assert ctx.journal.of_type("decision.tamper"), "the tamper evidence is kept for the monitor"
    new_code = orch2._decision_codes[pending["id"]]

    result = accept_decision(ctx, "SQLite", new_code)
    assert result["phase"] == "PLAN"
    assert ctx.store.load()["last_decision"]["data"] == {}
