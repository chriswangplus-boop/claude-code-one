import json
import re
import subprocess
from collections import deque
from pathlib import Path

import pytest

from relay.brain.judgments import RouteDecision
from relay.gui import FakeAppDriver, Popup, pointer_role, pointer_round, write_sentinel
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
                 reviewer_ctx: int = 45, coder_ctx: int = 45, supervisor: str = "claude"):
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
        self.hook_once = False
        self.coder_reads: list[list[str]] = []
        self.supervisor = supervisor
        self.claude = FakeAppDriver(supervisor, project, on_ring=self._claude_ring)
        self.coder = FakeAppDriver("coder", project, on_ring=self._coder_ring)

    def drivers(self):
        return {self.supervisor: self.claude, "coder": self.coder}

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
            write_sentinel(self.project, role, round_no, f".relay/CHECKPOINT_{role}.md", context_pct=70, backend=self.supervisor)
            return
        if role in ("planner", "strategist"):
            if role == "strategist":
                self._write(".relay/STRATEGY.md", f"strategy for round {round_no}")
            self._write(".relay/PLAN.md", f"plan for round {round_no}")
            self._write(".relay/HANDOFF.md", f"handoff content {round_no}")
            write_sentinel(self.project, role, round_no, ".relay/HANDOFF.md", context_pct=30, backend=self.supervisor)
        elif role == "fixer":
            self._write(".relay/FIX_REPORT.md", f"fix report {round_no}")
            write_sentinel(self.project, "fixer", round_no, ".relay/FIX_REPORT.md", context_pct=35, backend=self.supervisor)
        elif role == "reviewer":
            if self.crash_review_once:
                self.crash_review_once = False
                raise RuntimeError("simulated reviewer crash before sentinel")
            verdict = self.verdicts.popleft() if self.verdicts else "PROJECT_DONE"
            self._write(".relay/REVIEW.md", f"review {round_no}: {verdict}")
            write_sentinel(
                self.project, "reviewer", round_no, ".relay/REVIEW.md",
                verdict=verdict, context_pct=self.reviewer_ctx, backend=self.supervisor,
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
        import re as _re
        named = sorted(set(_re.findall(r"\.relay/[A-Za-z_]+\.md", pointer)))
        self.coder_reads.append(named)
        seen_review = ""
        if ".relay/REVIEW.md" in named and (self.project / ".relay/REVIEW.md").exists():
            seen_review = (self.project / ".relay/REVIEW.md").read_text()
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
        if self.hook_once:
            self.hook_once = False
            hook = self.project / ".git/hooks/pre-commit"
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text("#!/bin/sh\ntouch \"$(dirname \"$0\")/../../../hook-ran\"\n")
            hook.chmod(0o755)
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
        self._write(".relay/CODER_REPORT.md", f"coder report {round_no}\n{seen_review}")
        write_sentinel(self.project, role, round_no, ".relay/CODER_REPORT.md", context_pct=self.coder_ctx)
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("simulated crash after writing the sentinel")


def build(ctx, *, verdicts=("PROJECT_DONE",), decision=None, notifier=None,
          reviewer_ctx=45, coder_ctx=45, supervisor="claude", **cfg):
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="Build a tiny todo CLI")
    # R3-7: the code is delivered only by phone (ntfy with a token) or an interactive terminal.
    ctx.cfg.set("notify.ntfy", True)
    ctx.cfg.set("ntfy.token", "test-token")
    ctx.cfg.set("ntfy.topic", "test-topic")
    for key, value in cfg.items():
        ctx.cfg.set(key, value)
    apps = ScriptedApps(
        ctx.cfg.project_dir, verdicts, decision, reviewer_ctx=reviewer_ctx,
        coder_ctx=coder_ctx, supervisor=supervisor,
    )
    ctx.cfg.set("supervisor.backend", supervisor)
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
    # init + 8 turns + 2 role-switch checkpoints (the strategist chat is never checkpointed).
    assert len(git_log(ctx.cfg.project_dir)) == 11
    checkpoints = ctx.journal.of_type("session.checkpoint")
    assert len(checkpoints) == 2
    assert {item["detail"]["role"] for item in checkpoints} == {"planner", "reviewer"}
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
    code = re.search(r"One-time answer code: (\S+)", notifier.sent[-1]["ntfy_body"]).group(1)
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
    code = re.search(r"One-time answer code: (\S+)", notifier.sent[-1]["ntfy_body"]).group(1)

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
    code = re.search(r"One-time answer code: (\S+)", notifier.sent[-1]["ntfy_body"]).group(1)
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
    code = re.search(r"One-time answer code: (\S+)", notifier.sent[-1]["ntfy_body"]).group(1)
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


# --- N2: git hooks planted before or during a turn never run -----------------

def test_planted_hook_does_not_run_on_a_turn_commit(ctx):
    from relay import integrity

    orch, apps, notifier, _ = build(ctx)
    marker = ctx.cfg.project_dir / "hook-ran"
    hook = ctx.cfg.project_dir / ".git/hooks/pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\ntouch \"$(dirname \"$0\")/../../../hook-ran\"\n")
    hook.chmod(0o755)
    # A hook that exists before Relay's verified baseline is neutralised, not fatal.
    integrity.record_git_control(ctx.cfg.project_dir)

    orch.run(max_steps=2)

    assert ctx.journal.of_type("git.commit")
    assert not marker.exists(), "Relay's commit ran the planted hook"


def test_a_hook_planted_between_turns_pauses_before_any_git_command(ctx):
    """R4-2: a change made while Relay was stopped is caught before git runs it."""
    orch, apps, notifier, _ = build(ctx)
    marker = ctx.cfg.project_dir / "hook-ran"
    hook = ctx.cfg.project_dir / ".git/hooks/pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\ntouch \"$(dirname \"$0\")/../../../hook-ran\"\n")
    hook.chmod(0o755)

    state = orch.run(max_steps=2, wait_human=False)

    assert state["phase"] == "PAUSED"
    assert "changed since the last verified turn" in str(state.get("paused_reason"))
    assert not ctx.journal.of_type("git.commit")
    assert not marker.exists()


def test_hook_planted_during_a_turn_is_a_violation_and_does_not_run(ctx):
    orch, apps, notifier, _ = build(ctx)
    apps.hook_once = True
    state = orch.run(max_steps=4)

    assert state["phase"] == "PAUSED"
    violation = ctx.journal.latest("integrity.violation")
    assert violation["detail"]["kind"] == "git_control"
    assert any(path.endswith("pre-commit") for path in violation["detail"]["paths"])
    assert not (ctx.cfg.project_dir / "hook-ran").exists()


# --- N3: after CHANGES_REQUESTED the coder is pointed at REVIEW.md -----------

def test_coder_is_pointed_at_the_review_after_changes_requested(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["CHANGES_REQUESTED", "PROJECT_DONE"])
    state = orch.run(max_steps=30)
    assert state["phase"] == "DONE"

    rings = [
        pointer for pointer in apps.coder.rings
        if "role=coder" in pointer
        and not pointer.startswith(("RELAY seed", "RELAY nudge", "RELAY checkpoint"))
    ]
    assert len(rings) >= 2
    assert ".relay/REVIEW.md" not in rings[0], "the first code turn has no review yet"
    assert ".relay/REVIEW.md" in rings[1], "the coder must be pointed at the review"
    assert len(rings[1]) < 500

    # The coder only sees what the pointer names, and its report now carries the feedback.
    assert ".relay/REVIEW.md" not in apps.coder_reads[0]
    assert ".relay/REVIEW.md" in apps.coder_reads[1]
    report = (ctx.cfg.project_dir / ".relay/CODER_REPORT.md").read_text()
    assert "review" in report.lower() or "CHANGES_REQUESTED" in report


# --- N1: human-only actions need proof from outside the project --------------

def _decision_build(ctx, **kw):
    decision = {
        "question": "Store tasks as JSON or SQLite?",
        "options": [
            {"label": "JSON", "consequence": "zero dependencies"},
            {"label": "SQLite", "consequence": "better concurrency"},
        ],
    }
    return build(ctx, verdicts=["CHANGES_REQUESTED", "PROJECT_DONE"], decision=decision, **kw)


def test_rewriting_state_json_cannot_answer_a_decision(ctx):
    from relay.decisions import accept_decision
    from relay.errors import IntegrityError, StateError

    orch, apps, notifier, _ = _decision_build(ctx)
    state = orch.run(max_steps=10, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    real_code = re.search(r"One-time answer code: (\S+)", notifier.sent[-1]["ntfy_body"]).group(1)
    assert "code_hash" not in ctx.paths.state.read_text()
    original = ctx.store.load()["pending_decision"]

    # A fake code written into the ANSWER line is simply wrong.
    fake_code = "agentchosen"
    pending = ctx.paths.pending_decision.read_text().rstrip()
    ctx.paths.pending_decision.write_text(pending + f" {fake_code} A, use JSON\n")
    with pytest.raises(StateError, match="invalid one-time code"):
        accept_decision(ctx, "A, use JSON", fake_code)
    assert ctx.journal.of_type("decision.answered") == []
    assert ctx.store.load()["phase"] == "WAIT_HUMAN"

    # Editing the pending question in STATE.json is tampering; the answer is refused.
    ctx.store.mutate(lambda current: current["pending_decision"].__setitem__("question", "pwned"))
    with pytest.raises(IntegrityError, match="changed"):
        accept_decision(ctx, "A, use JSON", real_code)
    assert ctx.journal.of_type("decision.answered") == []

    # Once the edit is gone, the real one-time code (held only outside the project) works.
    ctx.store.mutate(lambda current: current.__setitem__("pending_decision", original))
    result = accept_decision(ctx, "A, use JSON", real_code)
    assert result["phase"] == "CODE"
    assert len(ctx.journal.of_type("decision.answered")) == 1


def test_state_edits_while_waiting_for_a_decision_are_reasked(ctx, monkeypatch):
    import relay.orchestrator as orchestrator_module

    orch, apps, notifier, _ = _decision_build(ctx)
    state = orch.run(max_steps=10, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    old_id = state["pending_decision"]["id"]

    calls = {"n": 0}

    def tampering(paths):
        calls["n"] += 1
        if calls["n"] == 1:
            ctx.store.mutate(lambda current: current["pending_decision"].__setitem__("question", "pwned"))
        return None

    monkeypatch.setattr(orchestrator_module, "local_answer", tampering)
    orch.wait_for_decision()

    # R4-4: the tamper is kept as evidence and the question is re-asked with a new id/code.
    state = ctx.store.load()
    assert state["phase"] == "WAIT_HUMAN"
    assert state["pending_decision"]["id"] != old_id
    assert ctx.journal.of_type("decision.tamper")


def test_waiting_for_a_decision_accepts_the_real_code(ctx, monkeypatch):
    import relay.orchestrator as orchestrator_module

    orch, apps, notifier, _ = _decision_build(ctx)
    state = orch.run(max_steps=10, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    real_code = re.search(r"One-time answer code: (\S+)", notifier.sent[-1]["ntfy_body"]).group(1)

    monkeypatch.setattr(orchestrator_module, "local_answer", lambda paths: (real_code, "JSON"))
    orch.wait_for_decision()

    assert ctx.store.load()["phase"] == "CODE"
    assert len(ctx.journal.of_type("decision.answered")) == 1
    assert ctx.journal.of_type("decision.tamper") == []


# --- P3 N9/N10 ---------------------------------------------------------------

def test_opus_count_and_pending_are_saved_together(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    ctx.store.mutate(lambda state: state.update({"phase": "CODE", "issue_streaks": {"hard-bug": 4}}))
    original_append = ctx.journal.append

    def fail_on_escalation(event_type, **kwargs):
        if event_type == "model.escalated":
            raise RuntimeError("simulated crash after the state save")
        return original_append(event_type, **kwargs)

    ctx.journal.append = fail_on_escalation
    with pytest.raises(RuntimeError, match="after the state save"):
        orch.run(max_steps=2)
    state = ctx.store.load()
    assert state["pending"]["escalated"] is True
    assert state["opus"]["day"] == 1

    ctx.journal.append = original_append
    orch2 = Orchestrator(ctx, apps.drivers(), brain=StubBrain(), notifier=notifier)
    state = orch2.resume(max_steps=4, wait_human=False)
    assert ctx.store.load()["opus"]["day"] == 1, "a resumed turn must not be counted twice"


def test_first_turn_does_not_overwrite_user_edits(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    (ctx.cfg.project_dir / "user-notes.txt").write_text("do not touch")
    state = orch.run(max_steps=1, wait_human=False)

    assert state["phase"] == "WAIT_HUMAN"
    assert "uncommitted" in ctx.paths.pending_decision.read_text().lower()
    commits = ctx.journal.of_type("git.commit")
    assert not any("user-notes" in str(commit.get("detail", {}).get("changed", "")) for commit in commits)


# --- R3-5: git filters/attributes never run, and every commit path refuses -----------

def _plant_filter(project: Path) -> Path:
    marker = Path(project) / "filter-ran"
    subprocess.run(
        ["git", "-C", str(project), "config", "filter.evil.clean", f"touch {marker}; cat"],
        check=True, capture_output=True,
    )
    (Path(project) / ".gitattributes").write_text("*.txt filter=evil\n")
    return marker


def test_a_planted_filter_pauses_before_git_runs(ctx):
    orch, apps, notifier, _ = build(ctx)
    marker = _plant_filter(ctx.cfg.project_dir)
    state = orch.run(max_steps=2, wait_human=False)

    assert state["phase"] == "PAUSED"
    assert not marker.exists(), "the clean filter ran"
    assert not ctx.journal.of_type("git.commit")


def test_the_checkpoint_commit_refuses_a_planted_filter(ctx):
    orch, apps, notifier, _ = build(ctx)
    orch._prepare()
    marker = _plant_filter(ctx.cfg.project_dir)

    from relay.errors import IntegrityError, PauseRequested
    with pytest.raises((IntegrityError, PauseRequested)):
        orch._checkpoint_before_switch("planner", 1)
    assert not marker.exists()


def test_the_rollover_commit_refuses_a_planted_filter(ctx):
    orch, apps, notifier, _ = build(ctx)
    orch._prepare()
    marker = _plant_filter(ctx.cfg.project_dir)

    from relay.errors import IntegrityError, PauseRequested
    with pytest.raises((IntegrityError, PauseRequested)):
        orch._rollover("coder", 1, "test")
    assert not marker.exists()


def test_the_fix_plan_archive_commit_refuses_a_planted_filter(ctx):
    orch, apps, notifier, _ = build(ctx)
    orch._prepare()
    (ctx.cfg.project_dir / ".relay/FIX_PLAN.md").write_text("fix plan\n")
    marker = _plant_filter(ctx.cfg.project_dir)

    from relay.errors import IntegrityError, PauseRequested
    with pytest.raises((IntegrityError, PauseRequested)):
        orch._archive_fix_plan(1, "test")
    assert not marker.exists()


# --- R3-1: authorization needs an intact, signed journal -----------------------------

def test_run_pauses_when_a_journal_line_was_rewritten(ctx):
    orch, apps, notifier, _ = build(ctx)
    ctx.journal.append("turn.start", round_no=0, detail={"role": "coder"})
    file = sorted(ctx.paths.journal_dir.glob("*.jsonl"))[-1]
    lines = file.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[-1])
    event["detail"]["role"] = "planner"  # body changed, hash/sig kept
    lines[-1] = json.dumps(event, sort_keys=True)
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    state = orch.run(max_steps=1, wait_human=False)

    assert state["phase"] == "PAUSED"
    assert any(
        event["detail"].get("kind") == "journal"
        for event in ctx.journal.of_type("integrity.violation")
    )


# --- R3-7: the answer code only reaches the phone or an interactive terminal ---------

OPTIONS = [
    {"label": "JSON", "consequence": "simple"},
    {"label": "SQLite", "consequence": "robust"},
]


def test_the_answer_code_never_reaches_the_desktop_notification(ctx):
    orch, apps, notifier, _ = build(ctx)
    ctx.cfg.set("notify.ntfy", True)
    ctx.cfg.set("ntfy.token", "tok")
    ctx.cfg.set("ntfy.topic", "topic")

    decision_id = orch.escalate_human("Which storage?", OPTIONS)
    code = orch._decision_codes[decision_id]
    sent = notifier.sent[-1]

    assert code not in sent["desktop_body"]
    assert "code" not in sent["desktop_body"].lower()
    assert code in sent["ntfy_body"]
    asked = ctx.journal.latest("decision.asked")
    assert asked["detail"]["code_delivery"] == {"tty": False, "phone": True, "desktop": False}


def test_the_code_is_withheld_without_a_phone_or_terminal(ctx):
    orch, apps, notifier, _ = build(ctx)
    ctx.cfg.set("notify.ntfy", False)

    decision_id = orch.escalate_human("Which storage?", OPTIONS)
    code = orch._decision_codes[decision_id]
    sent = notifier.sent[-1]

    assert code not in sent["desktop_body"]
    assert code not in sent["ntfy_body"]
    assert ctx.journal.of_type("decision.code_undelivered")
    asked = ctx.journal.latest("decision.asked")
    assert asked["detail"]["code_delivery"]["phone"] is False


def test_the_code_is_never_on_the_osascript_command_line(ctx, monkeypatch):
    from relay import notify as notify_module
    from relay.notify import Notifier

    ctx.cfg.set("notify.ntfy", False)
    ctx.cfg.set("notify.desktop", True)
    captured: dict = {}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return Result()

    monkeypatch.setattr(notify_module.subprocess, "run", fake_run)
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="code delivery")
    notifier = Notifier(ctx.cfg, ctx.journal)
    orch = Orchestrator(ctx, {}, notifier=notifier)

    decision_id = orch.escalate_human("Which storage?", OPTIONS)
    code = orch._decision_codes[decision_id]

    assert captured["cmd"], "the desktop notification never ran"
    assert code not in " ".join(str(part) for part in captured["cmd"])


def test_start_refuses_a_supervisor_that_can_read_the_screen(ctx, monkeypatch):
    from relay.cli import _refuse_unsafe_supervisors
    from relay.errors import ConfigError

    ctx.cfg.set("security.supervisor_bundle_ids", ["com.example.claude"])
    monkeypatch.setattr(
        "relay.guard.system_tcc_grants",
        lambda: {"com.example.claude": {"kTCCServiceAccessibility"}},
    )
    with pytest.raises(ConfigError, match="one-time answer code"):
        _refuse_unsafe_supervisors(ctx)
    assert ctx.journal.of_type("security.refused")


def test_the_start_guard_passes_when_the_probe_cannot_be_read(ctx, monkeypatch):
    from relay.cli import _refuse_unsafe_supervisors

    ctx.cfg.set("security.supervisor_bundle_ids", ["com.example.claude"])
    monkeypatch.setattr("relay.guard.system_tcc_grants", lambda: None)
    _refuse_unsafe_supervisors(ctx)


# --- R3-3: a "Use Opus" answer must not crash on an undefined name -------------------

def test_answering_use_opus_arms_an_opus_turn_without_crashing(ctx):
    orch, apps, notifier, _ = build(ctx)
    last = {
        "id": "D-900", "answer": "Use Opus for the next turn",
        "kind": "round_cap", "options": [], "data": {},
    }
    ctx.store.mutate(lambda state: state.update({"last_decision": last, "round": 1}))

    orch._apply_human_opus_request(last)  # raised NameError before the fix

    state = ctx.store.load()
    assert state["human_opus_request"]["source"] == "D-900"
    assert ctx.journal.of_type("model.requested")


# --- R3-4: a crash cannot launder what the agent wrote -------------------------------

def test_a_crash_cannot_launder_a_violating_write(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    apps.violate_once = True   # coder overwrites .relay/PLAN.md
    apps.crash_once = True     # then dies right after writing its sentinel
    with pytest.raises(RuntimeError, match="simulated crash"):
        orch.run(max_steps=10)

    pending = ctx.store.load()["pending"]
    assert pending["phase"] == "CODE"
    baseline = ctx.trust.attempt(pending["attempt_id"])
    assert baseline is not None, "the baseline must be persisted before ringing"
    assert "files" in baseline and "git" in baseline
    assert not str(ctx.trust.root).startswith(str(ctx.cfg.project_dir))

    orch2 = Orchestrator(ctx, apps.drivers(), brain=StubBrain(), notifier=notifier)
    state = orch2.resume(max_steps=20, wait_human=False)

    assert state["phase"] == "PAUSED"
    violation = ctx.journal.latest("integrity.violation")
    assert any(item["path"] == ".relay/PLAN.md" for item in violation["detail"]["violations"])


def test_a_legacy_pending_turn_without_a_baseline_pauses_and_can_be_adopted(ctx):
    from relay.cli import _adopt_baseline

    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    apps.crash_once = True
    with pytest.raises(RuntimeError, match="simulated crash"):
        orch.run(max_steps=10)

    pending = ctx.store.load()["pending"]
    ctx.trust.clear_attempt(str(pending["attempt_id"]))
    ctx.store.mutate(lambda current: current["pending"].pop("attempt_id", None))

    orch2 = Orchestrator(ctx, apps.drivers(), brain=StubBrain(), notifier=notifier)
    state = orch2.resume(max_steps=20, wait_human=False)
    assert state["phase"] == "PAUSED"
    assert "no signed baseline" in str(state.get("paused_reason"))

    _adopt_baseline(ctx)
    adopted = ctx.store.load()["pending"]["attempt_id"]
    assert ctx.trust.attempt(adopted) is not None
    assert ctx.journal.of_type("baseline.adopted")

    orch3 = Orchestrator(ctx, apps.drivers(), brain=StubBrain(), notifier=notifier)
    state = orch3.resume(max_steps=20, wait_human=False)
    assert state["phase"] != "PAUSED"


# --- R3-8: every message typed into an app is journaled as app.request ---------------

def test_every_message_typed_into_an_app_is_journaled(ctx):
    orch, apps, notifier, _ = build(ctx, verdicts=["PROJECT_DONE"])
    orch.run(max_steps=30)

    requests = ctx.journal.of_type("app.request")
    assert requests
    kinds = {event["detail"]["kind"] for event in requests}
    assert {"task", "seed"} <= kinds
    for event in requests:
        detail = event["detail"]
        assert {
            "app", "backend", "role", "kind", "model", "effort", "chars", "attempt_id", "sent",
        } <= set(detail)
        assert detail["backend"] in ("claude", "coder")
    assert len(requests) >= len(ctx.journal.of_type("gui.ring"))
