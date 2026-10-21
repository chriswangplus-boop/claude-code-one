import json
import subprocess
from collections import deque
from pathlib import Path

import pytest

from relay.brain.judgments import RouteDecision
from relay.gui import FakeAppDriver, Popup, pointer_role, pointer_round, write_sentinel
from relay.journal import Journal
from relay.notify import FakeNotifier
from relay.orchestrator import Orchestrator
from relay.setup import init_project


class StubBrain:
    """Deterministic routing for orchestrator tests; the real Brain is tested separately."""

    def __init__(self):
        self.calls = 0

    def route(self, **kwargs):
        self.calls += 1
        verdict = (kwargs.get("sentinel") or {}).get("verdict")
        route = {
            "CHANGES_REQUESTED": "CODE",
            "APPROVED": "PLAN",
            "MILESTONE_DONE": "PLAN",
            "PROJECT_DONE": "DONE",
        }.get(verdict, "NEEDS_HUMAN")
        return RouteDecision(
            route=route, reason="stub", round_summary=f"{route} after {verdict}",
            needs_human=None, drift_flags=[], call_id=None,
        )

    def vision(self, image, kind, question=None):
        return {}


class ScriptedApps:
    def __init__(self, project: Path, verdicts=("PROJECT_DONE",), decision: dict | None = None,
                 reviewer_ctx: int = 45, coder_ctx: int = 45):
        self.project = Path(project)
        self.verdicts = deque(verdicts)
        self.decision = decision
        self.reviewer_ctx = reviewer_ctx
        self.coder_ctx = coder_ctx
        self.silent: set[str] = set()
        self.crash_once = False
        self.violate_once = False
        self.secret_once = False
        self.stale_once = False
        self.control_once = False
        self.crash_review_once = False
        self.claude = FakeAppDriver("claude", project, on_ring=self._claude_ring)
        self.coder = FakeAppDriver("coder", project, on_ring=self._coder_ring)

    def drivers(self):
        return {"claude": self.claude, "coder": self.coder}

    def _write(self, rel: str, text: str) -> Path:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def _claude_ring(self, driver, pointer):
        if pointer.startswith("RELAY seed") or pointer.startswith("RELAY nudge"):
            return
        role = pointer_role(pointer)
        round_no = pointer_round(pointer)
        if role in self.silent:
            return
        if pointer.startswith("RELAY checkpoint"):
            self._write(f".relay/CHECKPOINT_{role}.md", f"checkpoint {role} {round_no}")
            write_sentinel(self.project, role, round_no, f".relay/CHECKPOINT_{role}.md", context_pct=70)
            return
        if role in ("planner", "strategist"):
            if role == "strategist":
                self._write(".relay/STRATEGY.md", f"strategy for round {round_no}")
            self._write(".relay/PLAN.md", f"plan for round {round_no}")
            self._write(".relay/HANDOFF.md", f"handoff content {round_no}")
            write_sentinel(self.project, role, round_no, ".relay/HANDOFF.md", context_pct=30)
        elif role == "fixer":
            self._write(".relay/FIX_REPORT.md", f"fix report {round_no}")
            write_sentinel(self.project, "fixer", round_no, ".relay/FIX_REPORT.md", context_pct=35)
        elif role == "reviewer":
            if self.crash_review_once:
                self.crash_review_once = False
                raise RuntimeError("simulated reviewer crash before sentinel")
            verdict = self.verdicts.popleft() if self.verdicts else "PROJECT_DONE"
            self._write(".relay/REVIEW.md", f"review {round_no}: {verdict}")
            write_sentinel(
                self.project, "reviewer", round_no, ".relay/REVIEW.md",
                verdict=verdict, context_pct=self.reviewer_ctx,
            )

    def _coder_ring(self, driver, pointer):
        if pointer.startswith("RELAY seed"):
            return
        role = "coder"
        round_no = pointer_round(pointer)
        if pointer.startswith("RELAY checkpoint"):
            self._write(".relay/CHECKPOINT_coder.md", f"checkpoint {round_no}")
            write_sentinel(self.project, role, round_no, ".relay/CHECKPOINT_coder.md", context_pct=70)
            return
        if pointer.startswith("RELAY nudge"):
            if self.stale_once:
                self.stale_once = False
                write_sentinel(self.project, role, round_no, ".relay/CODER_REPORT.md", context_pct=self.coder_ctx)
            return
        if "coder" in self.silent:
            return
        if self.stale_once:
            write_sentinel(self.project, role, 1, ".relay/CODER_REPORT.md", context_pct=50)
            return
        if self.secret_once:
            self.secret_once = False
            self._write("src/leak.py", 'API_KEY = "sk-abcdefghijklmnopqrstuvwx"\n')
            self._write(".relay/CODER_REPORT.md", "added leak")
            write_sentinel(self.project, role, round_no, ".relay/CODER_REPORT.md")
            return
        if self.violate_once:
            self.violate_once = False
            self._write(".relay/PLAN.md", "coder overwrote the plan")
        if self.control_once:
            self.control_once = False
            self._write(".relay/monitor/CORRECTION.json", '{"action":"replan","reason":"agent"}')
        if self.decision:
            decision, self.decision = self.decision, None
            self._write(".relay/CODER_REPORT.md", f"round {round_no} needs a decision")
            write_sentinel(
                self.project, role, round_no, ".relay/CODER_REPORT.md",
                status="needs_decision", question=decision["question"],
                options=decision["options"], context_pct=55,
            )
            return
        self._write("src/app.py", f"VERSION = {round_no}\n")
        self._write(".relay/CODER_REPORT.md", f"coder report {round_no}")
        write_sentinel(self.project, role, round_no, ".relay/CODER_REPORT.md", context_pct=self.coder_ctx)
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("simulated crash after writing the sentinel")


def build(ctx, *, verdicts=("PROJECT_DONE",), decision=None, notifier=None,
          reviewer_ctx=45, coder_ctx=45, **cfg):
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="Build a tiny todo CLI")
    for key, value in cfg.items():
        ctx.cfg.set(key, value)
    apps = ScriptedApps(ctx.cfg.project_dir, verdicts, decision, reviewer_ctx=reviewer_ctx, coder_ctx=coder_ctx)
    notifier = notifier or FakeNotifier()
    brain = StubBrain()
    orch = Orchestrator(ctx, apps.drivers(), brain=brain, notifier=notifier)
    return orch, apps, notifier, brain


def git_log(project: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(project), "log", "--oneline"], capture_output=True, text=True, check=True
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_two_milestones_to_done_offline(ctx):
    orch, apps, notifier, brain = build(
        ctx, verdicts=["CHANGES_REQUESTED", "MILESTONE_DONE", "PROJECT_DONE"]
    )
    state = orch.run(max_steps=30)
    assert state["phase"] == "DONE"
    assert state["round"] == 8
    assert state["milestone"] == 2
    assert len(ctx.journal.of_type("turn.end")) == 8
    assert len(ctx.journal.of_type("route.decided")) == 3
    assert len(ctx.journal.of_type("integrity.ok")) == 8
    assert brain.calls == 3
    assert apps.coder.new_session_calls == 1          # one clean coder chat at first use
    assert apps.claude.new_session_calls == 4         # planner/reviewer switch on each re-entry
    assert len(ctx.journal.of_type("session.open")) == 5
    assert len(git_log(ctx.cfg.project_dir)) == 12  # init + 8 turns + 3 role-switch checkpoints
    checkpoints = ctx.journal.of_type("session.checkpoint")
    assert len(checkpoints) == 3
    assert {item["detail"]["role"] for item in checkpoints} == {"planner", "reviewer", "strategist"}
    ok, errors = ctx.journal.verify()
    assert ok, errors
    pointers = apps.claude.rings + apps.coder.rings
    assert any("role=planner" in p for p in pointers)
    assert any("role=reviewer" in p for p in pointers)
    assert any("role=coder" in p for p in pointers)
    # pointer messages never carry file contents
    assert not any("handoff content" in p for p in pointers)
    assert all(len(p) < 500 for p in pointers)
    seed_rings = [p for p in apps.claude.rings if p.startswith("RELAY seed")]
    assert any("RULES_CLAUDE.md" in p and "reviewer" in p for p in seed_rings)
    assert any("RULES_CLAUDE.md" in p and "planner" in p for p in seed_rings)
    assert any("RULES_CODER.md" in p for p in apps.coder.rings)


def test_decision_pauses_notifies_and_resumes(ctx):
    decision = {
        "question": "Store tasks as JSON or SQLite?",
        "options": [
            {"label": "JSON", "consequence": "zero dependencies"},
            {"label": "SQLite", "consequence": "better concurrency"},
        ],
    }
    orch, apps, notifier, _ = build(
        ctx, verdicts=["CHANGES_REQUESTED", "PROJECT_DONE"], decision=decision
    )
    state = orch.run(max_steps=10, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    pending = ctx.paths.pending_decision.read_text()
    assert "D-001" in pending and "ANSWER:" in pending and "SQLite" in pending
    assert notifier.sent and notifier.sent[-1]["decision_id"] == "D-001"
    assert "JSON or SQLite" in notifier.sent[-1]["body"]
    assert "VERSION" not in notifier.sent[-1]["body"]

    import re
    code = re.search(r"One-time answer code: (\S+)", notifier.sent[-1]["desktop_body"]).group(1)
    assert code not in pending
    assert code not in ctx.paths.state.read_text()
    result = orch.answer_decision("JSON, no dependencies", code)
    assert result["phase"] == "CODE"
    decisions = ctx.paths.decisions.read_text()
    assert "JSON, no dependencies" in decisions
    assert "decision.answered" in decisions
    event = ctx.journal.latest("decision.answered")
    assert event["actor"] == "human"
    assert event["id"] in decisions

    state = orch.run(max_steps=10)
    assert state["phase"] == "DONE"
    assert state["round"] == 6


def test_crash_after_sentinel_resumes_without_re_ringing(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    apps.crash_once = True
    with pytest.raises(RuntimeError, match="simulated crash"):
        orch.run(max_steps=10)
    state = ctx.store.load()
    assert state["pending"]["phase"] == "CODE"
    assert ctx.store.baton()["holder"] == "coder"
    coder_rings = len(apps.coder.rings)

    orch2 = Orchestrator(ctx, apps.drivers(), brain=StubBrain(), notifier=notifier)
    state = orch2.resume(max_steps=20)
    assert state["phase"] == "DONE"
    assert len(apps.coder.rings) == coder_rings  # sentinel was already there
    assert ctx.journal.of_type("turn.resume_accepted")


def test_stale_sentinel_is_rejected_then_nudged(ctx):
    orch, apps, notifier, _ = build(
        ctx, verdicts=["PROJECT_DONE"], nudge_after_seconds=1, max_nudges=1, poll_seconds=1
    )
    apps.stale_once = True
    state = orch.run(max_steps=4)
    assert state["phase"] in ("REVIEW", "ROUTE", "DONE")
    assert ctx.journal.of_type("sentinel.rejected")
    assert ctx.journal.of_type("sentinel.seen")
    assert ctx.journal.of_type("turn.nudge")


def test_rollover_for_both_apps(ctx):
    orch, apps, notifier, _ = build(
        ctx,
        verdicts=["CHANGES_REQUESTED", "MILESTONE_DONE", "PROJECT_DONE"],
        reviewer_ctx=80,
        **{
            "claude_app.max_rounds_per_session": 1,
            "zcode_app.max_rounds_per_session": 1,
            "poll_seconds": 1,
        },
    )
    state = orch.run(max_steps=30)
    assert state["phase"] == "DONE"
    assert apps.claude.new_session_calls >= 2
    assert apps.coder.new_session_calls >= 2
    rollovers = ctx.journal.of_type("rollover")
    assert len(rollovers) >= 2
    assert any("CHECKPOINT_reviewer.md" in p for p in apps.claude.rings if "checkpoint" in p)
    assert any("CHECKPOINT_coder.md" in p for p in apps.coder.rings)
    assert any(p.startswith("RELAY seed") for p in apps.claude.rings)
    assert any(p.startswith("RELAY seed") for p in apps.coder.rings)


def test_timeout_escalates_to_human(ctx):
    orch, apps, notifier, _ = build(
        ctx, **{"zcode_app.timeout_minutes": 1, "poll_seconds": 1, "nudge_after_seconds": 100}
    )
    apps.silent.add("coder")
    state = orch.run(max_steps=5, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    assert ctx.journal.of_type("turn.timeout")
    assert "timed out" in ctx.paths.pending_decision.read_text().lower() or "responding" in ctx.paths.pending_decision.read_text().lower()
    assert state["pending"]["phase"] == "CODE"


def test_ownership_violation_pauses(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    apps.violate_once = True
    state = orch.run(max_steps=4)
    assert state["phase"] == "PAUSED"
    violation = ctx.journal.latest("integrity.violation")
    assert violation["detail"]["kind"] == "ownership"
    assert notifier.sent[-1]["title"] == "Relay paused"


def test_secret_blocks_turn_and_commit(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    apps.secret_once = True
    state = orch.run(max_steps=4)
    assert state["phase"] == "PAUSED"
    assert ctx.journal.latest("integrity.violation")["detail"]["kind"] == "secret"
    commits = ctx.journal.of_type("git.commit")
    assert not any("round 2" in str(c["detail"].get("message", "")) for c in commits)


def test_user_edits_pause_instead_of_being_overwritten(ctx):
    orch, apps, notifier, _ = build(ctx)
    orch.run(max_steps=1)
    (ctx.cfg.project_dir / "my-notes.txt").write_text("user was here")
    state = orch.resume(max_steps=1, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    assert "uncommitted" in ctx.paths.pending_decision.read_text().lower()


def test_popup_allow_list_approves_in_place(ctx):
    orch, apps, notifier, _ = build(ctx)
    apps.claude.popup = Popup("Allow Claude Code to read file src/app.py?", ["Allow", "Deny"])
    orch._screen_check(apps.claude, "planner")
    assert ctx.journal.of_type("popup.approved")
    assert apps.claude.clicked[1] == "Allow"


def test_dangerous_popup_escalates(ctx):
    orch, apps, notifier, _ = build(ctx)
    apps.claude.popup = Popup("Allow network access to install packages from the internet?", ["Allow", "Deny"])
    apps.silent.add("strategist")
    state = orch.run(max_steps=2, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    assert ctx.journal.of_type("popup.escalated")
    assert "dialog" in ctx.paths.pending_decision.read_text().lower()


def test_monitor_correction_replans_at_boundary(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    orch.run(max_steps=1)
    ctx.paths.correction.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.correction.write_text(json.dumps({"action": "replan", "reason": "drift"}))
    state = orch.run(max_steps=2)
    assert ctx.journal.of_type("monitor.correct_applied")
    assert state["phase"] in ("CODE", "REVIEW", "ROUTE", "PLAN")


def test_uninitialized_project_fails_clearly(ctx):
    orch = Orchestrator(ctx, {"claude": FakeAppDriver("claude", ctx.cfg.project_dir), "coder": FakeAppDriver("coder", ctx.cfg.project_dir)})
    with pytest.raises(Exception, match="not initialized"):
        orch.run()


def test_milestone_forces_rollover_above_half_context(ctx):
    orch, apps, notifier, _ = build(
        ctx, verdicts=["MILESTONE_DONE", "PROJECT_DONE"], reviewer_ctx=60, coder_ctx=60,
    )
    state = orch.run(max_steps=30)
    assert state["phase"] == "DONE"
    assert apps.coder.new_session_calls >= 1
    assert apps.claude.new_session_calls >= 1
    assert any("CHECKPOINT_coder.md" in p for p in apps.coder.rings)


def test_monitor_heartbeat_loss_pauses(ctx):
    orch, apps, notifier, _ = build(ctx)
    ctx.store.heartbeat(ctx.paths.monitor_heartbeat)
    orch.monitor_required = True
    orch.run(max_steps=1)
    assert orch._state()["phase"] != "PAUSED"

    stale = ctx.clock.epoch() - 10_000
    ctx.paths.monitor_heartbeat.write_text(json.dumps({"ts": "old", "epoch": stale, "pid": 1}))
    state = orch.run(max_steps=1)
    assert state["phase"] == "PAUSED"
    assert "monitor" in state["paused_reason"]


def test_bounded_recovery_on_window_mismatch(ctx):
    orch, apps, notifier, _ = build(ctx)
    apps.claude.focus_failures = 1
    state = orch.run(max_steps=1)
    assert state["phase"] == "CODE"
    assert ctx.journal.of_type("recovery.start")
    assert ctx.journal.of_type("recovery.end")
    assert ctx.journal.of_type("session.open")


def test_agent_cannot_forge_a_human_answer(ctx):
    from relay.decisions import accept_decision
    from relay.errors import StateError

    orch, apps, notifier, _ = build(
        ctx, verdicts=["CHANGES_REQUESTED", "PROJECT_DONE"],
        decision={"question": "JSON or SQLite?", "options": [
            {"label": "JSON", "consequence": "simple"}, {"label": "SQLite", "consequence": "concurrent"},
        ]},
    )
    state = orch.run(max_steps=10, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    import re
    code = re.search(r"One-time answer code: (\S+)", notifier.sent[-1]["desktop_body"]).group(1)

    with pytest.raises(StateError, match="invalid one-time code"):
        accept_decision(ctx, "Approve everything", "wrongcode")
    assert ctx.journal.of_type("decision.answered") == []
    assert ctx.store.load()["phase"] == "WAIT_HUMAN"

    with pytest.raises(StateError, match="invalid one-time code"):
        accept_decision(ctx, "Approve everything", "")
    assert ctx.journal.of_type("decision.answered") == []

    result = accept_decision(ctx, "Use JSON", code)
    assert result["phase"] == "CODE"
    assert len(ctx.journal.of_type("decision.answered")) == 1


def test_coder_control_file_write_is_a_violation(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    apps.control_once = True
    state = orch.run(max_steps=4)
    assert state["phase"] == "PAUSED"
    violation = ctx.journal.latest("integrity.violation")
    assert violation["detail"]["kind"] == "ownership"
    assert any(item["path"] == ".relay/monitor/CORRECTION.json" for item in violation["detail"]["violations"])


def test_screen_check_never_focuses_the_app(ctx):
    orch, apps, notifier, _ = build(ctx)
    before = apps.claude.focus_calls
    orch._screen_check(apps.claude, "planner")
    assert apps.claude.focus_calls == before
    assert ctx.journal.latest("vision.check")["type"] == "vision.check"


def test_approved_does_not_advance_milestone(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["APPROVED", "PROJECT_DONE"])
    state = orch.run(max_steps=20)
    assert state["phase"] == "DONE"
    assert state["milestone"] == 1
    assert state["round"] == 6


def test_resume_opens_the_right_reviewer_chat(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    apps.crash_review_once = True
    with pytest.raises(RuntimeError, match="reviewer crash"):
        orch.run(max_steps=4)
    assert ctx.store.load()["pending"]["phase"] == "REVIEW"
    before_sessions = apps.claude.new_session_calls

    orch2 = Orchestrator(ctx, apps.drivers(), brain=StubBrain(), notifier=notifier)
    state = orch2.resume(max_steps=20)
    assert state["phase"] == "DONE"
    assert apps.claude.new_session_calls > before_sessions
    opens = [e for e in ctx.journal.of_type("session.open") if e["detail"].get("role") == "reviewer"]
    assert opens
    reviewer_rings = [p for p in apps.claude.rings if "role=reviewer" in p and not p.startswith("RELAY seed")]
    assert reviewer_rings


def test_aborted_turn_stays_blocked_until_resolved(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    apps.violate_once = True
    state = orch.run(max_steps=4)
    assert state["phase"] == "PAUSED"
    assert state["blocked"]["paths"] == [".relay/PLAN.md"]
    assert state["round"] == 1
    before_commits = len(git_log(ctx.cfg.project_dir))

    state = orch.resume(max_steps=4, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    assert "blocked" in ctx.paths.pending_decision.read_text().lower() or "unsafe" in ctx.paths.pending_decision.read_text().lower()
    assert len(git_log(ctx.cfg.project_dir)) == before_commits


def test_concurrent_instruction_survives_routing(ctx):
    from relay.brain.judgments import RouteDecision
    from relay.decisions import record_instruction

    class InterleavingBrain(StubBrain):
        def route(self, **kwargs):
            record_instruction(ctx, "Keep JSON storage", confirmed=True)
            return super().route(**kwargs)

    orch, apps, notifier, _ = build(ctx, verdicts=["CHANGES_REQUESTED", "PROJECT_DONE"])
    orch.brain = InterleavingBrain()
    orch.run(max_steps=3)
    assert ctx.store.load()["phase"] == "ROUTE"
    state = orch.run(max_steps=1)
    assert state["phase"] == "CODE"
    assert state["decisions"] == 1
    assert state["last_instruction"]["text"] == "Keep JSON storage"


def test_crash_after_turn_save_keeps_the_next_phase(ctx):
    orch, apps, notifier, _ = build(ctx)
    original_append = ctx.journal.append

    def fail_on_turn_end(event_type, **kwargs):
        if event_type == "turn.end":
            raise RuntimeError("simulated crash after state save")
        return original_append(event_type, **kwargs)

    ctx.journal.append = fail_on_turn_end
    with pytest.raises(RuntimeError, match="after state save"):
        orch.run(max_steps=2)
    state = ctx.store.load()
    assert state["phase"] == "CODE"
    assert state["pending"] is None
    assert state["round"] == 1

    ctx.journal.append = original_append
    orch2 = Orchestrator(ctx, apps.drivers(), brain=StubBrain(), notifier=notifier)
    state = orch2.run(max_steps=1)
    assert state["round"] == 2
    assert state["phase"] == "REVIEW"


# --- P2-10: every state writer reloads under the lock -------------------------

def _stale_outside_lock(ctx, monkeypatch):
    """Simulate a concurrent writer between a writer's load and its save.

    Reads made outside the lock return a snapshot from before the concurrent write;
    reads made by `store.mutate` (inside the lock) see the real file.
    """
    import copy

    stale = copy.deepcopy(ctx.store.load())
    real_load = ctx.store.load

    def patched():
        if ctx.store.lock._depth == 0:
            return copy.deepcopy(stale)
        return real_load()

    monkeypatch.setattr(ctx.store, "load", patched)
    return real_load


def _concurrent_context(ctx):
    from relay.context import open_project

    return open_project(ctx.cfg.project_dir, env={}, clock=ctx.clock)


def _concurrent_instruction(ctx, text="keep this instruction"):
    from relay.decisions import record_instruction

    result = record_instruction(_concurrent_context(ctx), text, confirmed=True)
    return result


def test_ensure_session_keeps_a_concurrent_instruction(ctx, monkeypatch):
    orch, apps, notifier, _ = build(ctx)
    real_load = _stale_outside_lock(ctx, monkeypatch)
    result = _concurrent_instruction(ctx)

    orch._ensure_session("planner", 1)

    state = real_load()
    assert state["decisions"] == 1
    assert state["last_instruction"]["id"] == result["id"]
    assert state["active_session"]["claude"] == "planner"


def test_rollover_keeps_a_concurrent_instruction(ctx, monkeypatch):
    orch, apps, notifier, _ = build(ctx)
    real_load = _stale_outside_lock(ctx, monkeypatch)
    result = _concurrent_instruction(ctx, "survive the rollover")

    orch._rollover("reviewer", 1, "test rollover")

    state = real_load()
    assert state["decisions"] == 1
    assert state["last_instruction"]["id"] == result["id"]
    assert state["active_session"]["claude"] == "reviewer"


def test_process_turn_keeps_a_concurrent_instruction(ctx, monkeypatch):
    from relay.gui import write_sentinel
    from relay.integrity import read_sentinel, snapshot

    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    orch.run(max_steps=1)
    state = ctx.store.load()
    round_no = int(state["round"]) + 1
    (ctx.cfg.project_dir / ".relay" / "CODER_REPORT.md").write_text("no-op report\n")
    sentinel_path = write_sentinel(
        ctx.cfg.project_dir, "coder", round_no, ".relay/CODER_REPORT.md", context_pct=12
    )
    before = snapshot(ctx.cfg.project_dir)
    sentinel = read_sentinel(sentinel_path)
    real_load = _stale_outside_lock(ctx, monkeypatch)
    result = _concurrent_instruction(ctx, "survive the turn save")

    orch._process_turn(
        phase="CODE", role="coder", round_no=round_no, sentinel=sentinel,
        before=before, driver=apps.coder,
    )

    state = real_load()
    assert state["decisions"] == 1
    assert state["last_instruction"]["id"] == result["id"]
    assert state["phase"] == "REVIEW"
    assert state["pending"] is None


def test_escalate_human_allocates_the_next_decision_id(ctx, monkeypatch):
    orch, apps, notifier, _ = build(ctx)
    real_load = _stale_outside_lock(ctx, monkeypatch)
    _concurrent_instruction(ctx, "made while Relay escalates")

    decision_id = orch.escalate_human(
        "Which storage?", [{"label": "JSON", "consequence": "simple"}, {"label": "SQLite", "consequence": "concurrent"}],
    )

    state = real_load()
    assert decision_id == "D-002"
    assert state["decisions"] == 2
    assert state["pending_decision"]["id"] == "D-002"
    assert state["last_instruction"]["text"] == "made while Relay escalates"


# --- P3-16: a round cap asks the human instead of dead-ending ----------------

def test_round_cap_escalates_with_three_options(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"], max_rounds_per_milestone=1)
    state = orch.run(max_steps=1)
    assert state["counters"]["rounds_in_milestone"] == 1

    state = orch.run(max_steps=2, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    pending = state["pending_decision"]
    assert pending["kind"] == "round_cap"
    labels = [option["label"] for option in pending["options"]]
    assert labels == ["Raise the cap", "Re-plan", "Stop for now"]
    assert ctx.journal.of_type("limit.cap_reached")
    assert ctx.journal.of_type("state.paused") == []

    import re
    code = re.search(r"One-time answer code: (\S+)", notifier.sent[-1]["desktop_body"]).group(1)
    orch.answer_decision("Raise the cap", code)

    state = orch.run(max_steps=3)
    assert state["phase"] in ("REVIEW", "ROUTE", "PLAN", "DONE")
    assert state["limit_overrides"]["max_rounds_per_milestone"] == 4
    assert ctx.journal.of_type("limit.decision_applied")[-1]["detail"]["action"] == "raise"


def test_round_cap_stop_option_pauses(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"], max_rounds_per_milestone=1)
    orch.run(max_steps=1)
    state = orch.run(max_steps=2, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"

    import re
    code = re.search(r"One-time answer code: (\S+)", notifier.sent[-1]["desktop_body"]).group(1)
    orch.answer_decision("Stop for now", code)
    state = orch.run(max_steps=2)
    assert state["phase"] == "PAUSED"
    assert "human chose to stop" in state["paused_reason"]


# --- P3-17: replans reset when a milestone is approved -----------------------

def test_replans_reset_on_milestone_done(ctx):
    class StuckOnce(StubBrain):
        def __init__(self):
            super().__init__()
            self.stuck = 1

        def route(self, **kwargs):
            decision = super().route(**kwargs)
            if self.stuck and decision.route == "CODE":
                self.stuck -= 1
                return RouteDecision(
                    route="STUCK", reason="stub loop", round_summary="stuck",
                    needs_human=None, drift_flags=[], call_id=None,
                )
            return decision

    orch, apps, notifier, _ = build(ctx, verdicts=["CHANGES_REQUESTED", "MILESTONE_DONE", "PROJECT_DONE"])
    orch.brain = StuckOnce()
    state = orch.run(max_steps=4)
    assert state["counters"]["replans"] == 1
    assert state["counters"]["stalls_in_milestone"] == 1
    assert state["phase"] == "PLAN"

    state = orch.run(max_steps=30)
    assert state["phase"] == "DONE"
    assert state["milestone"] == 2
    assert state["counters"]["replans"] == 0
    assert state["counters"]["stalls_in_milestone"] == 0
