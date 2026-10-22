"""Builds a journal that exercises every event in docs/EVENTS.md.

Used by `tools/make_event_fixtures.py` (which writes the committed Swift fixture) and by
`tests/test_events_schema.py` (which checks the documented fields are present). Most events come
from real code paths: a scripted fake-driver run, the monitor, a decision, host-guard, pause/stop
and a journal repair. A handful of rare events are appended with their documented shape.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from relay.brain.judgments import RouteDecision
from relay.context import open_project
from relay.decisions import record_instruction
from relay.gui import FakeAppDriver, pointer_role, pointer_round, write_sentinel
from relay.monitor import Monitor
from relay.notify import FakeNotifier, Notifier
from relay.orchestrator import Orchestrator
from relay.setup import init_project


class ManualClock:
    def __init__(self, start: datetime | None = None):
        self.dt = start or datetime(2026, 10, 21, 9, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.dt

    def epoch(self) -> float:
        return self.dt.timestamp()

    def sleep(self, seconds: float) -> None:
        self.dt += timedelta(seconds=seconds)


class StubBrain:
    def route(self, **kwargs):
        verdict = (kwargs.get("sentinel") or {}).get("verdict")
        route = {
            "CHANGES_REQUESTED": "CODE",
            "APPROVED": "PLAN",
            "MILESTONE_DONE": "PLAN",
            "PROJECT_DONE": "DONE",
        }.get(verdict, "NEEDS_HUMAN")
        return RouteDecision(
            route=route, reason="fixture route", round_summary=f"{route} after {verdict}",
            needs_human=None, drift_flags=[], call_id=None, suggest_escalation=False,
        )


ISSUE = "auth-token-expiry"


class FixtureApps:
    def __init__(self, project: Path, ctx):
        self.project = Path(project)
        self.ctx = ctx
        self.verdicts = [
            "CHANGES_REQUESTED", "CHANGES_REQUESTED", "CHANGES_REQUESTED",
            "CHANGES_REQUESTED", "MILESTONE_DONE", "PROJECT_DONE",
        ]
        self.issues = [[ISSUE], [ISSUE], [ISSUE], [ISSUE], [], []]
        self.decision_pending = True
        self.violate_once = True
        self.claude = FakeAppDriver("claude", project, on_ring=self._claude)
        self.coder = FakeAppDriver("coder", project, on_ring=self._coder)

    def drivers(self):
        return {"claude": self.claude, "coder": self.coder}

    def _write(self, rel: str, text: str) -> None:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def _claude(self, driver, pointer):
        if pointer.startswith("RELAY seed") or pointer.startswith("RELAY nudge"):
            return
        role = pointer_role(pointer)
        round_no = pointer_round(pointer) or 0
        if pointer.startswith("RELAY checkpoint"):
            self._write(f".relay/CHECKPOINT_{role}.md", f"checkpoint {role} {round_no}")
            write_sentinel(self.project, role, round_no, f".relay/CHECKPOINT_{role}.md", context_pct=60)
            return
        if role in ("planner", "strategist"):
            self._write(".relay/PLAN.md", f"plan {round_no}")
            self._write(".relay/HANDOFF.md", f"handoff {round_no}")
            write_sentinel(self.project, role, round_no, ".relay/HANDOFF.md", context_pct=30)
        elif role == "fixer":
            self._write("src/fix.py", "FIXED = True\n")
            self._write(".relay/FIX_REPORT.md", "root cause fixed")
            write_sentinel(self.project, "fixer", round_no, ".relay/FIX_REPORT.md", context_pct=35)
        elif role == "reviewer":
            verdict = self.verdicts.pop(0) if self.verdicts else "PROJECT_DONE"
            issue_ids = self.issues.pop(0) if self.issues else []
            self._write(".relay/REVIEW.md", f"review {round_no}: {verdict}")
            if "write .relay/FIX_PLAN.md" in pointer:
                self._write(".relay/FIX_PLAN.md", f"fix plan for {issue_ids}")
            write_sentinel(
                self.project, "reviewer", round_no, ".relay/REVIEW.md",
                verdict=verdict, context_pct=40, blocking_issue_ids=issue_ids,
            )

    def _coder(self, driver, pointer):
        if pointer.startswith("RELAY seed") or pointer.startswith("RELAY nudge"):
            return
        round_no = pointer_round(pointer) or 0
        if pointer.startswith("RELAY checkpoint"):
            self._write(".relay/CHECKPOINT_coder.md", f"checkpoint coder {round_no}")
            write_sentinel(self.project, "coder", round_no, ".relay/CHECKPOINT_coder.md", context_pct=60)
            return
        if self.decision_pending:
            self.decision_pending = False
            self._write(".relay/CODER_REPORT.md", "need a storage decision")
            write_sentinel(
                self.project, "coder", round_no, ".relay/CODER_REPORT.md",
                status="needs_decision", question="Store tasks as JSON or SQLite?",
                options=[
                    {"label": "JSON", "consequence": "zero dependencies"},
                    {"label": "SQLite", "consequence": "better concurrency"},
                ],
                context_pct=55,
            )
            return
        if self.violate_once:
            self.violate_once = False
            self._write(".relay/PLAN.md", "coder overwrote the plan")
        self._write("src/app.py", f"VERSION = {round_no}\n")
        self._write(".relay/CODER_REPORT.md", f"coder report {round_no}")
        write_sentinel(self.project, "coder", round_no, ".relay/CODER_REPORT.md", context_pct=45)


def _append_rare(ctx, events: list[dict]) -> None:
    """Append the documented events that no short scripted run reaches."""
    journal = ctx.journal
    journal.append("run.resume", state="PLAN", detail={"pid": 4242})
    journal.append("run.stopped", detail={"reason": "max_steps", "steps": 40}, result="ok")
    journal.append("state.baton_recovered", detail={"note": "cleared stale baton on start"})
    journal.append("turn.resume", state="CODE", round_no=3, detail={"role": "coder", "expected": ".relay/CODER_REPORT.md", "resumed": True})
    journal.append("turn.resume_accepted", state="CODE", round_no=3, detail={"role": "coder", "note": "sentinel arrived before the crash"})
    journal.append("turn.nudge", round_no=3, detail={"role": "coder"})
    journal.append("turn.timeout", round_no=3, detail={"role": "coder", "expected": ".relay/CODER_REPORT.md"}, result="error")
    journal.append("sentinel.rejected", round_no=3, detail={"role": "coder", "reasons": ["sha256 does not match the announced file"]}, result="ignored")
    journal.append("checkpoint.oversize", round_no=3, detail={"role": "reviewer", "lines": 180, "limit": 150}, result="warn")
    journal.append("model.limit_reached", round_no=5, detail={"trigger": "hardest_bug_streak", "kind": "model_limit", "data": {"role": "fixer", "limit": "day"}}, result="blocked")
    journal.append("model.decision_applied", round_no=5, detail={"action": "allow", "role": "fixer"})
    journal.append("model.declined", round_no=6, detail={"role": "coder", "issue_ids": [ISSUE]})
    journal.append("limit.cap_reached", round_no=8, detail={"kind": "milestone", "current": 8, "limit": 8}, result="blocked")
    journal.append("limit.decision_applied", round_no=8, detail={"action": "raise", "cap": "max_rounds_per_milestone"})
    journal.append("blocked.cleared", detail={"paths": [".relay/PLAN.md"]})
    journal.append("decision.reminder", detail={"id": "D-001"})
    journal.append("decision.timeout", detail={"id": "D-001"}, result="still waiting")
    journal.append("decision.rejected", detail={"id": "D-001", "reason": "invalid one-time code"}, result="ignored")
    journal.append(
        "decision.tamper", detail={"id": "D-001", "reason": "STATE.json changed while waiting"},
        result="blocked",
    )
    journal.append("route.after_decision", round_no=4, detail={"note": "routing judgment re-raised a question answered this round; using deterministic route"})
    journal.append("stall.detected", round_no=4, detail={"reason": "three change requests with no source changes", "replans": 1})
    journal.append("gui.focus", detail={"app": "claude", "title": "Claude", "rect": [0, 0, 1200, 800]})
    journal.append("gui.click", detail={"app": "claude", "target": "message-input"})
    journal.append("gui.type", detail={"app": "claude", "chars": 210, "submit": "return"})
    journal.append("gui.ring", round_no=3, detail={"role": "reviewer", "app": "claude", "chars": 210, "pointer": "RELAY round 3 role=reviewer", "sent": True})
    journal.append("gui.ring.retry", round_no=3, detail={"role": "reviewer", "sent": True})
    journal.append("gui.model", detail={"app": "claude", "role": "reviewer", "model": "Sonnet 5.5", "effort": "high", "seen": {"model": "Sonnet 5.5", "effort": "high"}, "model_verified": True, "effort_verified": True})
    journal.append("gui.new_session", detail={"app": "claude", "confirmed_empty": True})
    journal.append("gui.screenshot", detail={"app": "claude", "tag": "model", "region": [0, 0, 300, 40], "sha256": "0" * 64})
    journal.append("vision.check", detail={"kind": "busy", "role": "coder", "busy": False})
    journal.append("popup.seen", detail={"role": "coder", "text": "Allow Bash: pytest?", "buttons": ["Allow", "Deny"]})
    journal.append("popup.approved", detail={"button": "Allow"})
    journal.append("popup.escalated", detail={"text": "Allow network access?"}, result="blocked")
    journal.append("recovery.start", detail={"app": "claude", "role": "planner", "max_actions": 6})
    journal.append("recovery.action", detail={"n": 1, "action": "refocus"}, result="failed")
    journal.append("recovery.end", detail={"actions": 2, "app": "claude"})
    journal.append("recovery.retry", detail={"app": "claude", "role": "planner"})
    journal.append("monitor.spawned", detail={"pid": 5150, "log": ".relay/monitor/monitor.log"})
    journal.append("monitor.correct", actor="monitor", state="CODE", round_no=4, detail={"action": "replan", "codes": ["drift"], "reason": "drift from the plan"})
    journal.append("monitor.correct_applied", detail={"action": "replan", "reason": "drift from the plan"})
    journal.append("monitor.error", actor="monitor", detail={"error": "BrainError: timeout"}, result="error")
    journal.append("monitor.layer2", actor="monitor", state="REVIEW", round_no=4, detail={"score": 82, "verdict": "ok", "summary": "on track", "call_id": "call-0123456789ab", "findings": []})
    journal.append(
        "deepseek.call", actor="orchestrator",
        detail={"job": "route", "model": "deepseek-flash", "prompt_version": "p1", "error": None},
        deepseek={"call_id": "call-0123456789ab", "prompt_tokens": 1200, "completion_tokens": 90, "cost_usd": 0.0005, "ctx_pct": 0.12},
    )
    journal.append("human.halt_cleared", actor="human", detail={"previous_reason": "monitor halt: ownership"})
    journal.append("notify.error", detail={"error": "OSError"}, result="error")
    journal.append("error", detail={"error": "RuntimeError: unexpected"}, result="error")
    journal.append("calibration.ok", actor="human", detail={"app": "claude", "working_dir": str(ctx.cfg.project_dir)})
    journal.append("vision.experiment", detail={"future_field": {"nested": [1, 2, 3]}})
    events.extend(journal.all_events())


def build_journal(root: str | Path) -> list[dict]:
    """Run the scenario in `root` and return every journal event it produced."""
    root = Path(root)
    clock = ManualClock()
    ctx = open_project(
        root, env={"RELAY_TRUST_DIR": str(Path(root).parent / "relay-trust")}, clock=clock
    )
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="Fixture project")
    ctx.cfg.set("claude_app.max_rounds_per_session", 1)
    ctx.cfg.set("zcode_app.max_rounds_per_session", 1)
    ctx.cfg.set("poll_seconds", 1)
    ctx.cfg.set("nudge_after_seconds", 100000)
    ctx.cfg.set("max_rounds_per_milestone", 80)
    ctx.cfg.set("max_total_rounds", 400)
    ctx.cfg.set("notify.ntfy", True)
    ctx.cfg.set("ntfy.token", "fixture-token")
    ctx.cfg.set("ntfy.topic", "fixture-topic")

    apps = FixtureApps(root, ctx)
    notifier = FakeNotifier()
    orch = Orchestrator(ctx, apps.drivers(), brain=StubBrain(), notifier=notifier)

    # 1. The coder asks a real question, and the human answers it with the one-time code.
    orch.run(max_steps=10, wait_human=False)
    import re
    code = None
    for sent in reversed(notifier.sent):
        match = re.search(r"One-time answer code: (\S+)", str(sent.get("ntfy_body") or ""))
        if match:
            code = match.group(1)
            break
    assert code, [sent.get("title") for sent in notifier.sent]
    pending_file = ctx.paths.pending_decision
    text = pending_file.read_text(encoding="utf-8").rstrip()
    pending_file.write_text(f"{text} {code} JSON, no dependencies\n", encoding="utf-8")
    orch.wait_for_decision()

    # 2. A real ownership violation pauses the loop; the human resolves it and resumes.
    orch.run(max_steps=10, wait_human=False)
    (root / ".relay/PLAN.md").unlink(missing_ok=True)
    from relay.integrity import git_commit
    git_commit(root, "fixture: resolve the blocked write")
    record_instruction(ctx, "keep JSON; add CSV export", confirmed=True, opus=True)
    apps.claude.focus_failures = 1
    state = orch.resume(max_steps=80, wait_human=False)
    for _ in range(4):
        if state.get("phase") != "WAIT_HUMAN":
            break
        pending = state.get("pending_decision") or {}
        labels = [str(option.get("label")) for option in (pending.get("options") or [])]
        answer = labels[0] if labels else "Continue"
        latest = None
        for sent in reversed(notifier.sent):
            match = re.search(r"One-time answer code: (\S+)", str(sent.get("ntfy_body") or ""))
            if match:
                latest = match.group(1)
                break
        if latest is None:
            break
        orch.answer_decision(answer, latest)
        state = orch.run(max_steps=80, wait_human=False)

    # 3. The monitor sees the historical violation and halts, leaving notes.
    Monitor(ctx, brain=None, notifier=FakeNotifier()).check_once()

    # 4. The human pause/stop and host guard, written in-process with the fixture clock.
    #    The CLI opens the project with the wall clock, which made the committed fixture depend
    #    on the day it was regenerated (and could interleave a later day file).
    ctx.store.set_flag("pause", {"reason": "fixture pause", "by": "human"})
    ctx.journal.append("human.pause", actor="human", detail={"reason": "fixture pause"})
    ctx.store.set_flag("stop", {"reason": "fixture stop", "by": "human"})
    ctx.journal.append("human.stop", actor="human", detail={"reason": "fixture stop"})
    guard = float(ctx.cfg.get("host.context_guard", 0.7)) * 100
    ctx.store.set_flag("pause", {"reason": "host context guard", "by": "host-guard"})
    ctx.journal.append(
        "guard.tripped",
        detail={"kind": "host_context", "pct": 99.0, "turns": 3, "limit": guard},
        result="blocked",
    )
    Notifier(
        ctx.cfg, ctx.journal,
        desktop_fn=lambda *args: True, ntfy_fn=lambda *args: True,
    ).send("Relay paused: host context guard", "host DeepSeek session at 99%.", urgent=True)
    ctx.store.clear_flag("pause")
    ctx.store.clear_flag("stop")

    events = list(ctx.journal.all_events())
    _append_rare(ctx, events)

    # A real repair: corrupt the tail, then let `journal repair` quarantine and chain it.
    path = sorted(ctx.paths.journal_dir.glob("*.jsonl"))[-1]
    with open(path, "ab") as handle:
        handle.write(b'{"id": "evt-partial", "type": "turn.start"')
    ctx.journal.repair()
    return list(ctx.journal.all_events())


def write_fixture(root: str | Path, dest: str | Path) -> int:
    events = build_journal(root)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(events)
