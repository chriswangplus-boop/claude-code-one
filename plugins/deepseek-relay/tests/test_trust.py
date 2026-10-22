"""Review #2 N1: human-only actions need proof from outside the project."""

from __future__ import annotations

import json

import pytest

from relay import decisions
from relay.context import open_project
from relay.decisions import accept_decision, hash_code, record_instruction
from relay.errors import StateError
from relay.integrity import ownership_violations, snapshot
from relay.monitor import Monitor
from relay.notify import FakeNotifier
from relay.util import canonical_json, sha256_text


def test_trust_material_lives_outside_the_project(ctx):
    assert not str(ctx.trust.root).startswith(str(ctx.cfg.project_dir))
    ctx.trust.store_code("D-001", hash_code("chosen-by-the-human"), {"round": 1})
    stored = (ctx.trust.root / "decision-codes.json").read_text(encoding="utf-8")
    assert "chosen-by-the-human" not in stored
    assert ctx.trust.code_hash("D-001") == hash_code("chosen-by-the-human")
    ctx.trust.clear_code("D-001")
    assert ctx.trust.code_hash("D-001") is None


def test_instruct_confirmation_never_trusts_stdin(monkeypatch):
    """Path B: `echo y | script -q ...` must not confirm an instruction."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    class NoReadStdin:
        def isatty(self) -> bool:
            return True

        def readline(self, *args, **kwargs) -> str:
            raise AssertionError("confirm_human must never read stdin")

        def read(self, *args, **kwargs) -> str:
            raise AssertionError("confirm_human must never read stdin")

    class Cancelled:
        returncode = 1
        stdout = ""
        stderr = "cancel"

    monkeypatch.setattr(decisions.sys, "stdin", NoReadStdin())
    monkeypatch.setattr(decisions.subprocess, "run", lambda *args, **kwargs: Cancelled())
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: pytest.fail("input() used"))

    assert decisions.confirm_human("record this instruction?") is False


def test_a_forged_decision_event_does_not_cover_a_state_edit(ctx, paths):
    """Path C: an unsigned, correctly chained `decision.answered` is not trusted."""
    ctx.store.ensure()
    ctx.journal.append("turn.start", round_no=1, detail={"role": "coder"})
    tail = ctx.journal.all_events()[-1]
    forged = {
        "id": "evt-forged-decision",
        "ts": "2026-10-21T00:00:00.000+00:00",
        "round": 1,
        "state": "CODE",
        "actor": "human",
        "type": "decision.answered",
        "detail": {"id": "D-001", "answer": "agent wrote this"},
        "inputs": [], "outputs": [], "screenshot": None, "deepseek": None,
        "duration_ms": None, "result": "ok", "error": None,
        "prev_hash": tail["hash"],
    }
    forged["hash"] = sha256_text(canonical_json(forged))
    file = sorted(paths.journal_dir.glob("*.jsonl"))[-1]
    with open(file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged, sort_keys=True) + "\n")

    before = snapshot(ctx.cfg.project_dir)
    state = ctx.store.load()
    state["pending_decision"] = {"id": "D-001", "question": "agent-edited"}
    state["decisions"] = int(state.get("decisions", 0)) + 1
    ctx.store.save(state)
    after = snapshot(ctx.cfg.project_dir)

    trusted = ctx.journal.trusted_events()
    assert not any(event["id"] == "evt-forged-decision" for event in trusted)
    violations = ownership_violations(before, after, "coder", events=trusted)
    assert any(item["path"] == ".relay/STATE.json" for item in violations)
    # And the unkeyed journal check fails, so the monitor halts on it too.
    ok, errors = ctx.journal.verify()
    assert not ok


def test_a_forged_halt_cleared_does_not_clear_the_monitor_history(ctx, paths):
    ctx.journal.append(
        "integrity.violation", round_no=1,
        detail={"kind": "ownership", "violations": [{"path": ".relay/PLAN.md"}]}, result="blocked",
    )
    first = Monitor(ctx, brain=None, notifier=FakeNotifier())
    assert first.check_once()["halted"] is True

    tail = ctx.journal.all_events()[-1]
    forged = {
        "id": "evt-forged-halt-cleared",
        "ts": "2026-10-21T00:00:01.000+00:00",
        "round": 1,
        "state": "CODE",
        "actor": "human",
        "type": "human.halt_cleared",
        "detail": {"previous_reason": "ownership"},
        "inputs": [], "outputs": [], "screenshot": None, "deepseek": None,
        "duration_ms": None, "result": "ok", "error": None,
        "prev_hash": tail["hash"],
    }
    forged["hash"] = sha256_text(canonical_json(forged))
    file = sorted(paths.journal_dir.glob("*.jsonl"))[-1]
    with open(file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged, sort_keys=True) + "\n")

    second = Monitor(ctx, brain=None, notifier=FakeNotifier())
    assert second.check_once()["halted"] is True
    assert not any(event["type"] == "human.halt_cleared" for event in ctx.journal.trusted_events())


def test_record_instruction_requires_confirmation(ctx):
    with pytest.raises(StateError, match="confirmation"):
        record_instruction(ctx, "do this without asking", confirmed=False)
    assert ctx.journal.of_type("decision.answered") == []


def test_accept_decision_uses_the_external_code_not_state_json(ctx):
    code = "realcode"
    ctx.store.mutate(lambda state: state.update({
        "phase": "WAIT_HUMAN",
        "return_to": "CODE",
        "round": 1,
        "pending_decision": {
            "id": "D-001", "question": "q", "return_to": "CODE", "round": 1,
            "options": [], "code_hash": hash_code("agentchosen"),
        },
    }))
    ctx.trust.store_decision({
        "id": "D-001", "question_sha256": sha256_text("q"),
        "options": [], "kind": None, "data": {}, "return_to": "CODE", "round": 1,
        "code_hash": hash_code(code), "asked_at": "2026-10-21T00:00:00+00:00",
    })

    with pytest.raises(StateError, match="invalid one-time code"):
        accept_decision(ctx, "use JSON", "agentchosen")
    assert ctx.store.load()["phase"] == "WAIT_HUMAN"
    assert ctx.journal.of_type("decision.answered") == []

    result = accept_decision(ctx, "use JSON", code)
    assert result["phase"] == "CODE"
    assert len(ctx.journal.of_type("decision.answered")) == 1
    assert ctx.trust.code_hash("D-001") is None


# --- R3-1: a genuine signature does not make a rewritten body trustworthy -------------

def test_a_replayed_signed_event_with_a_new_body_is_not_trusted(ctx, paths):
    ctx.store.ensure()
    ctx.journal.append(
        "decision.answered", actor="human", round_no=1,
        detail={"id": "D-000", "answer": "the real human answer"},
    )
    genuine = ctx.journal.all_events()[-1]
    start = ctx.journal.append("turn.start", round_no=1, detail={"role": "coder"})

    # The probe: keep the genuine `hash` and `sig`, change the body, append as a new event.
    forged = json.loads(json.dumps(genuine))
    forged["id"] = "evt-20261020-09999"
    forged["type"] = "decision.answered"
    forged["detail"] = {"id": "D-002", "answer": "agent wrote this"}
    forged["prev_hash"] = start["hash"]
    file = sorted(paths.journal_dir.glob("*.jsonl"))[-1]
    with open(file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged, sort_keys=True) + "\n")

    trusted = ctx.journal.trusted_events()
    assert not any(event["id"] == forged["id"] for event in trusted)
    assert not any(event.get("detail", {}).get("id") == "D-002" for event in trusted)

    before = snapshot(ctx.cfg.project_dir)
    state = ctx.store.load()
    state["pending_decision"] = {"id": "D-002", "question": "agent-edited"}
    ctx.store.save(state)
    after = snapshot(ctx.cfg.project_dir)

    violations = ownership_violations(
        before, after, "coder", events=trusted, turn_start_id=start["id"],
    )
    assert any(item["path"] == ".relay/STATE.json" for item in violations)

    ok, errors = ctx.journal.verify()
    assert not ok
    assert any("hash mismatch" in error for error in errors)


def test_trusted_events_stops_at_the_first_broken_line(ctx, paths):
    ctx.store.ensure()
    ctx.journal.append("turn.start", round_no=1, detail={"role": "coder"})
    ctx.journal.append("turn.end", round_no=1, detail={"role": "coder"})
    ctx.journal.append("integrity.ok", round_no=1, detail={"role": "coder"})
    ctx.journal.append("state.transition", round_no=1, detail={"to": "REVIEW"})
    file = sorted(paths.journal_dir.glob("*.jsonl"))[-1]
    lines = file.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[-2])  # integrity.ok; keep the tip intact
    tampered["detail"]["role"] = "planner"
    lines[-2] = json.dumps(tampered, sort_keys=True)
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    trusted = ctx.journal.trusted_events()
    # turn.start is the anchor; turn.end is the only event before the broken line.
    assert [event["type"] for event in trusted] == ["turn.end"]


# --- R3-2: a human answer is bound to the question that was asked ---------------------

OPTIONS_R3 = [
    {"label": "JSON", "consequence": "simple"},
    {"label": "SQLite", "consequence": "robust"},
]


def _escalate(orch):
    orch.store.ensure()
    decision_id = orch.escalate_human(
        "Which storage?", OPTIONS_R3, reason="test", return_to="PLAN",
        kind="round_cap", data={"cap": "max_total_rounds"},
    )
    return decision_id, orch._decision_codes[decision_id]


def test_editing_the_pending_decision_while_stopped_is_refused(ctx):
    from relay.errors import IntegrityError
    from relay.orchestrator import Orchestrator

    orch = Orchestrator(ctx, {}, notifier=FakeNotifier())
    decision_id, code = _escalate(orch)
    assert ctx.trust.decision(decision_id)["return_to"] == "PLAN"

    # The agent edits the display copy while the loop is stopped.
    ctx.store.mutate(lambda state: state["pending_decision"].__setitem__("return_to", "CODE"))

    with pytest.raises(IntegrityError, match="changed while the decision was pending"):
        accept_decision(ctx, "JSON", code)
    assert ctx.journal.of_type("decision.answered") == []
    assert ctx.store.load()["phase"] == "WAIT_HUMAN"
    assert ctx.trust.decision(decision_id) is not None


def test_a_decision_record_survives_a_restart_and_governs_the_answer(ctx):
    from relay.orchestrator import Orchestrator

    orch = Orchestrator(ctx, {}, notifier=FakeNotifier())
    decision_id, code = _escalate(orch)
    # A fresh context, as after a restart: the signed record is the only source of meaning.
    restarted = open_project(ctx.cfg.project_dir, env={}, clock=ctx.clock)
    result = accept_decision(restarted, "SQLite", code)

    assert result["phase"] == "PLAN"
    state = restarted.store.load()
    assert state["last_decision"]["return_to"] == "PLAN"
    assert state["last_decision"]["kind"] == "round_cap"
    assert state["last_decision"]["data"] == {"cap": "max_total_rounds"}
    assert restarted.trust.decision(decision_id) is None
    assert restarted.trust.wait_fingerprint(decision_id) is None


def test_a_pending_decision_without_a_signed_record_cannot_be_answered(ctx):
    code = "legacycode"
    ctx.store.mutate(lambda state: state.update({
        "phase": "WAIT_HUMAN", "return_to": "CODE", "round": 1,
        "pending_decision": {"id": "D-001", "question": "q", "return_to": "CODE", "round": 1},
    }))
    ctx.trust.store_code("D-001", hash_code(code), {"round": 1})

    with pytest.raises(StateError, match="no signed record"):
        accept_decision(ctx, "answer", code)
    assert ctx.journal.of_type("decision.answered") == []


def test_the_record_never_stores_the_code_in_plaintext(ctx):
    from relay.orchestrator import Orchestrator

    orch = Orchestrator(ctx, {}, notifier=FakeNotifier())
    _, code = _escalate(orch)
    stored = (ctx.trust.root / "decisions.json").read_text(encoding="utf-8")
    assert code not in stored
    assert code not in ctx.paths.state.read_text()
