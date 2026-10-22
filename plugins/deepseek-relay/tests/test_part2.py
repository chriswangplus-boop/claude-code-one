"""REVISION_PLAN_3 Part 2: Codex desktop as a second supervisor."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay.backends import sentinel_name, rules_name, supervisor_backend
from relay.cli import _runtime
from relay.codex_policy import POLICY_DIR, policy_status, require_codex_policy
from relay.errors import ConfigError
from relay.gui import write_sentinel
from relay.integrity import validate_sentinel
from relay.setup import init_project

ROOT = Path(__file__).resolve().parents[1]


def test_supervisor_backend_defaults_to_claude_and_validates(ctx):
    assert supervisor_backend(ctx.cfg) == "claude"
    ctx.cfg.set("supervisor.backend", "codex")
    assert supervisor_backend(ctx.cfg) == "codex"
    ctx.cfg.set("supervisor.backend", "gemini")
    with pytest.raises(ConfigError, match="supervisor.backend"):
        supervisor_backend(ctx.cfg)


def test_sentinel_and_rules_names_follow_the_backend():
    assert sentinel_name("planner", "claude") == "CLAUDE_DONE.json"
    assert sentinel_name("planner", "codex") == "CODEX_DONE.json"
    assert sentinel_name("coder", "codex") == "CODER_DONE.json"
    assert rules_name("reviewer", "claude") == "RULES_CLAUDE.md"
    assert rules_name("reviewer", "codex") == "RULES_CODEX.md"
    assert rules_name("coder", "codex") == "RULES_CODER.md"


def test_init_writes_the_codex_rules(ctx):
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="codex rules")
    assert (ctx.cfg.project_dir / ".relay/RULES_CODEX.md").exists()
    assert "CODEX_DONE.json" in (ctx.cfg.project_dir / ".relay/RULES_CODEX.md").read_text()


def test_the_codex_policy_bundle_verifies():
    assert (POLICY_DIR / "MANIFEST.json").exists()
    status = policy_status()
    assert status["ok"], status["errors"]
    assert status["version"] == "3.0.0"
    assert status["model"] == "gpt-6.1-sol"
    require_codex_policy()


def test_a_missing_or_modified_policy_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="missing or modified"):
        require_codex_policy(tmp_path)

    copy = tmp_path / "policy"
    shutil.copytree(POLICY_DIR, copy)
    policy = copy / "routing-policy.json"
    policy.write_text(policy.read_text() + "\n")
    status = policy_status(copy)
    assert not status["ok"]
    with pytest.raises(ConfigError, match="manifest hash"):
        require_codex_policy(copy)


def test_a_sentinel_from_another_backend_is_rejected(ctx):
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="sentinel backend")
    write_sentinel(
        ctx.cfg.project_dir, "planner", 1, ".relay/HANDOFF.md", backend="claude",
    )
    sentinel = json.loads((ctx.cfg.project_dir / ".relay/CLAUDE_DONE.json").read_text())
    issues = validate_sentinel(
        sentinel, root=ctx.cfg.project_dir, expected_round=1, expected_role="planner",
        expected_file=".relay/HANDOFF.md", round_started_wall=0.0, expected_backend="codex",
    )
    assert any("expected codex" in issue for issue in issues)


def test_start_refuses_codex_without_its_calibration(ctx):
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="calibration gate")
    ctx.cfg.set("supervisor.backend", "codex")
    args = SimpleNamespace(fake=False, no_brain=True, no_monitor=True)
    with pytest.raises(ConfigError, match="codex is not calibrated"):
        _runtime(ctx, args)


def test_codex_wins_the_supervisor_turns(ctx):
    from test_orchestrator import build

    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"], supervisor="codex")
    state = orch.run(max_steps=30)

    assert state["phase"] == "DONE"
    assert orch.backend == "codex"
    supervisor_turns = [
        event for event in ctx.journal.of_type("turn.start")
        if event["detail"].get("role") != "coder"
    ]
    assert supervisor_turns
    assert all(event["detail"]["app"] == "codex" for event in supervisor_turns)
    assert all(event["detail"]["backend"] == "codex" for event in supervisor_turns)
    assert any(".relay/RULES_CODEX.md" in event["detail"]["pointer"] for event in supervisor_turns)
    opened = [
        event for event in ctx.journal.of_type("session.open")
        if event["detail"].get("role") != "coder"
    ]
    assert opened and all(event["detail"]["app"] == "codex" for event in opened)
    assert not (ctx.cfg.project_dir / ".relay/CLAUDE_DONE.json").exists()


def test_a_turn_started_on_claude_cannot_resume_on_codex(ctx):
    from test_orchestrator import build

    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    apps.crash_once = True
    with pytest.raises(RuntimeError, match="simulated crash"):
        orch.run(max_steps=10)

    ctx.cfg.set("supervisor.backend", "codex")
    from relay.orchestrator import Orchestrator

    orch2 = Orchestrator(ctx, apps.drivers(), notifier=notifier)
    state = orch2.resume(max_steps=2, wait_human=False)

    assert state["phase"] == "PAUSED"
    assert "cannot switch" in str(state.get("paused_reason"))
