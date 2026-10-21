import json
from pathlib import Path

import pytest

from relay.monitor import Monitor
from relay.notify import FakeNotifier
from relay.setup import init_project


class StubBrain:
    def __init__(self, findings=None, score=80, verdict="ok"):
        self.findings = findings or []
        self.score = score
        self.verdict = verdict
        self.calls = 0

    def monitor_review(self, **kwargs):
        self.calls += 1
        return {
            "score": self.score, "verdict": self.verdict, "summary": "stub",
            "findings": self.findings, "call_id": "call-aaaaaaaaaaaa",
        }


def base_monitor(ctx, phase="CODE", role="coder", round_no=2):
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="Build a todo CLI")
    ctx.store.heartbeat()
    state = ctx.store.load()
    state["phase"] = phase
    state["round"] = round_no
    state["role"] = role
    state["pending"] = {
        "phase": phase, "role": role, "round": round_no,
        "pointer": f"RELAY round {round_no} role={role}: do it",
        "expected_file": ".relay/CODER_REPORT.md", "started_wall": ctx.clock.epoch(),
    }
    ctx.store.save(state)
    ctx.store.set_baton(role, role, round_no)
    ctx.journal.append("turn.start", state=phase, round_no=round_no, detail={"role": role})
    notifier = FakeNotifier()
    return Monitor(ctx, notifier=notifier), notifier


def add_turn_end(ctx, role, round_no, *, ctx_pct=None, review_sha=None):
    outputs = []
    if review_sha:
        outputs.append({"path": ".relay/REVIEW.md", "sha256": review_sha})
    detail = {"role": role, "verdict": "CHANGES_REQUESTED", "summary": f"{role} round {round_no}"}
    if ctx_pct is not None:
        detail["ctx_pct"] = ctx_pct
    return ctx.journal.append("turn.end", state="REVIEW", round_no=round_no, detail=detail, outputs=outputs)


def test_baseline_is_quiet(ctx):
    monitor, notifier = base_monitor(ctx)
    result = monitor.check_once()
    assert result["halted"] is False
    assert ctx.paths.monitor_heartbeat.exists()
    assert not ctx.paths.halt_flag.exists()
    assert not notifier.sent
    assert ctx.journal.latest("monitor.check")["result"] == "ok"


def test_ownership_violation_halts(ctx):
    monitor, notifier = base_monitor(ctx)
    ctx.journal.append("integrity.violation", detail={"kind": "ownership"}, result="blocked")
    result = monitor.check_once()
    assert result["halted"] is True
    assert ctx.paths.halt_flag.exists()
    assert "ownership" in ctx.store.read_flag("halt")["reason"]
    assert notifier.sent and notifier.sent[-1]["urgent"] is True
    assert ctx.journal.of_type("monitor.halt")
    assert "integrity violation" in ctx.paths.monitor_notes.read_text()


def test_repeated_feedback_requests_replan(ctx):
    monitor, notifier = base_monitor(ctx)
    for round_no in (2, 3, 4):
        add_turn_end(ctx, "reviewer", round_no, review_sha="f" * 64)
    result = monitor.check_once()
    assert result["halted"] is False
    assert ctx.paths.correction.exists()
    correction = json.loads(ctx.paths.correction.read_text())
    assert correction["action"] == "replan"
    assert "repeated_feedback" in correction["codes"]
    assert ctx.journal.of_type("monitor.correct")


def test_ai_written_decision_halts(ctx):
    monitor, _ = base_monitor(ctx)
    event = ctx.journal.append("decision.answered", actor="orchestrator", detail={"id": "D-001"})
    ctx.paths.decisions.write_text(
        "# Decisions\n\n## D-001 - who chose this?\n"
        f"- evidence: decision.answered {event['id']} (actor orchestrator)\n"
    )
    result = monitor.check_once()
    assert result["halted"] is True
    assert "decision_authorship" in str(ctx.store.read_flag("halt")["findings"])


def test_decision_without_evidence_halts(ctx):
    monitor, _ = base_monitor(ctx)
    ctx.paths.decisions.write_text("# Decisions\n\n## D-002 - no evidence\n- answer: yes\n")
    assert monitor.check_once()["halted"] is True


def test_missed_rollover_halts(ctx):
    monitor, _ = base_monitor(ctx, phase="REVIEW", role="reviewer")
    add_turn_end(ctx, "reviewer", 2, ctx_pct=85)
    ctx.journal.append("turn.start", state="REVIEW", round_no=3, detail={"role": "reviewer"})
    state = ctx.store.load()
    state["pending"] = {"phase": "REVIEW", "role": "reviewer", "round": 3}
    ctx.store.save(state)
    ctx.store.set_baton("reviewer", "reviewer", 3)
    result = monitor.check_once()
    assert result["halted"] is True
    assert "missed_rollover" in str(ctx.store.read_flag("halt")["findings"])


def test_dead_orchestrator_heartbeat_halts(ctx):
    monitor, _ = base_monitor(ctx)
    ctx.paths.heartbeat.unlink()
    result = monitor.check_once()
    assert result["halted"] is True
    assert "orchestrator_heartbeat" in str(ctx.store.read_flag("halt")["findings"])


def test_tampered_journal_halts(ctx):
    monitor, _ = base_monitor(ctx)
    files = sorted(ctx.paths.journal_dir.glob("*.jsonl"))
    lines = files[0].read_text().splitlines()
    lines[-1] = lines[-1].replace('"turn.start"', '"turn.restart"')
    files[0].write_text("\n".join(lines) + "\n")
    result = monitor.check_once()
    assert result["halted"] is True
    assert "journal_chain" in str(ctx.store.read_flag("halt")["findings"])


def test_unguarded_deepseek_call_halts(ctx):
    monitor, _ = base_monitor(ctx)
    ctx.journal.append("deepseek.call", deepseek={"call_id": "call-aaaaaaaaaaaa", "ctx_pct": 0.82})
    ctx.journal.append("guard.tripped", detail={"kind": "context_usage"})
    result = monitor.check_once()
    assert result["halted"] is True
    findings = ctx.store.read_flag("halt")["findings"]
    assert any(item["code"] in ("context_guard", "guard_unhandled") for item in findings)


def test_round_cap_halts(ctx):
    monitor, _ = base_monitor(ctx)
    state = ctx.store.load()
    state["counters"]["total_rounds"] = 61
    ctx.store.save(state)
    assert monitor.check_once()["halted"] is True


def test_stuck_baton_halts(ctx):
    monitor, _ = base_monitor(ctx)
    ctx.store.set_baton("claude", None, 2)
    ctx.journal.append("run.start", detail={"pid": 1})
    result = monitor.check_once()
    assert result["halted"] is True
    assert "baton" in str(ctx.store.read_flag("halt")["findings"])


def test_layer2_halt_and_correct(ctx):
    monitor, _ = base_monitor(ctx)
    add_turn_end(ctx, "reviewer", 2)
    monitor.brain = StubBrain(findings=[
        {"severity": "halt", "kind": "scope", "note": "scope changed without a human", "action": "none"}
    ])
    assert monitor.check_once()["halted"] is True
    assert ctx.paths.halt_flag.exists()

    ctx.store.clear_flag("halt")
    monitor2, _ = base_monitor(ctx, phase="ROUTE", role="reviewer", round_no=3)
    add_turn_end(ctx, "reviewer", 3)
    monitor2.brain = StubBrain(findings=[
        {"severity": "correct", "kind": "loop", "note": "re-plan this", "action": "replan"}
    ], verdict="loop")
    assert monitor2.check_once()["halted"] is False
    assert json.loads(ctx.paths.correction.read_text())["action"] == "replan"


def test_monitor_cannot_drive_the_gui():
    import relay.monitor.watchdog as watchdog

    source = Path(watchdog.__file__).read_text()
    assert "relay.gui" not in source
    assert "AppDriver" not in source
    assert "pyautogui" not in source


def test_wrong_app_keystrokes_halt(ctx):
    monitor, _ = base_monitor(ctx, phase="CODE", role="coder")
    ctx.journal.append("gui.ring", round_no=2, detail={"role": "coder", "app": "claude"})
    result = monitor.check_once()
    assert result["halted"] is True
    assert "wrong_app" in str(ctx.store.read_flag("halt")["findings"])


def test_paused_pending_turn_is_not_a_baton_violation(ctx):
    monitor, _ = base_monitor(ctx, phase="PAUSED", role="coder")
    ctx.store.clear_baton()
    result = monitor.check_once()
    assert result["halted"] is False
    assert not ctx.paths.halt_flag.exists()


def test_fresh_chat_counts_as_rollover(ctx):
    monitor, _ = base_monitor(ctx, phase="REVIEW", role="reviewer")
    add_turn_end(ctx, "reviewer", 2, ctx_pct=85)
    ctx.journal.append("session.open", state="REVIEW", round_no=3, detail={"role": "reviewer", "app": "claude"})
    ctx.journal.append("turn.start", state="REVIEW", round_no=3, detail={"role": "reviewer"})
    state = ctx.store.load()
    state["pending"] = {"phase": "REVIEW", "role": "reviewer", "round": 3}
    ctx.store.save(state)
    ctx.store.set_baton("reviewer", "reviewer", 3)
    result = monitor.check_once()
    assert result["halted"] is False
    assert not ctx.paths.halt_flag.exists()


def test_host_instruction_is_valid_human_evidence(ctx):
    from relay.decisions import record_instruction

    monitor, _ = base_monitor(ctx)
    record_instruction(ctx, "Prefer JSON and keep dependencies minimal", confirmed=True)
    result = monitor.check_once()
    assert result["halted"] is False
    assert not ctx.paths.halt_flag.exists()


def test_halt_clear_watermark_prevents_immediate_rehalt(ctx):
    monitor, _ = base_monitor(ctx)
    ctx.journal.append("integrity.violation", detail={"kind": "ownership"}, result="blocked")
    assert monitor.check_once()["halted"] is True
    ctx.store.clear_flag("halt")
    ctx.journal.append("human.halt_cleared", actor="human", detail={"previous_reason": "ownership"})
    fresh = Monitor(ctx, notifier=FakeNotifier())
    assert fresh.check_once()["halted"] is False
    assert not ctx.paths.halt_flag.exists()

    ctx.journal.append("integrity.violation", detail={"kind": "secret"}, result="blocked")
    assert fresh.check_once()["halted"] is True


def test_turn_starting_between_pending_and_baton_does_not_halt(ctx):
    monitor, _ = base_monitor(ctx, phase="CODE", role="coder")
    state = ctx.store.load()
    state["pending"] = {"phase": "CODE", "role": "coder", "round": 3}
    ctx.store.save(state)
    ctx.store.clear_baton()
    assert monitor.check_once()["halted"] is False
