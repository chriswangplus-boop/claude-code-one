"""Part B: the deterministic model policy and the roles it introduces."""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path


from relay.brain.judgments import RouteDecision
from relay.gui import FakeAppDriver, pointer_role, pointer_round, write_sentinel
from relay.integrity import ownership_violations
from relay.model_policy import choose_turn
from relay.notify import FakeNotifier
from relay.orchestrator import Orchestrator
from relay.setup import init_project
from relay.state import new_state


class StubBrain:
    def __init__(self, *, stuck: bool = False, suggest_escalation: bool = False):
        self.calls = 0
        self.stuck = stuck
        self.suggest_escalation = suggest_escalation

    def route(self, **kwargs):
        self.calls += 1
        verdict = (kwargs.get("sentinel") or {}).get("verdict")
        route = {
            "CHANGES_REQUESTED": "CODE",
            "APPROVED": "PLAN",
            "MILESTONE_DONE": "PLAN",
            "PROJECT_DONE": "DONE",
        }.get(verdict, "NEEDS_HUMAN")
        if self.stuck and route == "CODE":
            route = "STUCK"
        return RouteDecision(
            route=route, reason="stub", round_summary=f"{route} after {verdict}",
            needs_human=None, drift_flags=[], call_id=None,
            suggest_escalation=self.suggest_escalation,
        )

    def vision(self, image, kind, question=None):
        return {}


class PolicyApps:
    """A fake pair of apps that follows the Part B roles and sentinels."""

    def __init__(
        self,
        project: Path,
        *,
        verdicts=(),
        issues=(),
        coder_blocks=(),
        fixer_files: int = 1,
        fixer_writes_plan: bool = False,
        coder_ctx: int = 40,
        reviewer_ctx: int = 40,
    ):
        self.project = Path(project)
        self.verdicts = deque(verdicts)
        self.issues = deque(issues)
        self.coder_blocks = deque(coder_blocks)
        self.fixer_files = fixer_files
        self.fixer_writes_plan = fixer_writes_plan
        self.coder_ctx = coder_ctx
        self.reviewer_ctx = reviewer_ctx
        self.claude = FakeAppDriver("claude", project, on_ring=self._claude)
        self.coder = FakeAppDriver("coder", project, on_ring=self._coder)

    def drivers(self):
        return {"claude": self.claude, "coder": self.coder}

    def _write(self, rel: str, text: str) -> Path:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

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
            if role == "strategist":
                self._write(".relay/STRATEGY.md", f"strategy {round_no}")
            self._write(".relay/PLAN.md", f"plan {round_no}")
            self._write(".relay/HANDOFF.md", f"handoff {round_no}")
            write_sentinel(self.project, role, round_no, ".relay/HANDOFF.md", context_pct=30)
        elif role == "fixer":
            if self.fixer_writes_plan:
                self._write(".relay/PLAN.md", "fixer rewrote the plan")
            for index in range(self.fixer_files):
                self._write(f"src/fix_{index}.py", f"FIX = {index}\n")
            self._write(".relay/FIX_REPORT.md", f"root cause fixed in round {round_no}")
            write_sentinel(self.project, "fixer", round_no, ".relay/FIX_REPORT.md", context_pct=35)
        elif role == "reviewer":
            verdict = self.verdicts.popleft() if self.verdicts else "PROJECT_DONE"
            issue_ids = list(self.issues.popleft()) if self.issues else []
            self._write(".relay/REVIEW.md", f"review {round_no}: {verdict} {issue_ids}")
            if "write .relay/FIX_PLAN.md" in pointer:
                self._write(".relay/FIX_PLAN.md", f"fix plan for {issue_ids}")
            write_sentinel(
                self.project, "reviewer", round_no, ".relay/REVIEW.md",
                verdict=verdict, context_pct=self.reviewer_ctx,
                blocking_issue_ids=issue_ids,
            )

    def _coder(self, driver, pointer):
        if pointer.startswith("RELAY seed") or pointer.startswith("RELAY nudge"):
            return
        round_no = pointer_round(pointer) or 0
        if pointer.startswith("RELAY checkpoint"):
            self._write(".relay/CHECKPOINT_coder.md", f"checkpoint coder {round_no}")
            write_sentinel(self.project, "coder", round_no, ".relay/CHECKPOINT_coder.md", context_pct=60)
            return
        if self.coder_blocks:
            issue_ids = list(self.coder_blocks.popleft())
            self._write(".relay/CODER_REPORT.md", f"blocked on {issue_ids}")
            write_sentinel(
                self.project, "coder", round_no, ".relay/CODER_REPORT.md",
                status="blocked", question=f"blocked on {issue_ids}",
                blocking_issue_ids=issue_ids, context_pct=self.coder_ctx,
            )
            return
        self._write("src/app.py", f"VERSION = {round_no}\n")
        self._write(".relay/CODER_REPORT.md", f"coder report {round_no}")
        write_sentinel(self.project, "coder", round_no, ".relay/CODER_REPORT.md", context_pct=self.coder_ctx)


def build(ctx, apps, *, brain=None, notifier=None, **cfg):
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="Build a tiny todo CLI")
    # These scenarios run several reviews in one milestone; the cap decision has its own test.
    cfg.setdefault("max_rounds_per_milestone", 80)
    cfg.setdefault("max_total_rounds", 400)
    cfg.setdefault("notify.ntfy", True)
    cfg.setdefault("ntfy.token", "test-token")
    cfg.setdefault("ntfy.topic", "test-topic")
    for key, value in cfg.items():
        ctx.cfg.set(key, value)
    brain = brain or StubBrain()
    notifier = notifier or FakeNotifier()
    orch = Orchestrator(ctx, apps.drivers(), brain=brain, notifier=notifier)
    return orch, notifier, brain


def claude_turns(ctx):
    return [
        event for event in ctx.journal.of_type("turn.start")
        if (event.get("detail") or {}).get("app") == "claude"
    ]


def code_from(notifier) -> str:
    return re.search(r"One-time answer code: (\S+)", notifier.sent[-1]["ntfy_body"]).group(1)


# --- B5 1 and 11: Sonnet by default, and every Claude turn is journaled -------

def test_normal_milestone_runs_sonnet_high_everywhere(ctx):
    apps = PolicyApps(ctx.cfg.project_dir, verdicts=["CHANGES_REQUESTED", "PROJECT_DONE"])
    orch, notifier, _ = build(ctx, apps)
    state = orch.run(max_steps=30)

    assert state["phase"] == "DONE"
    starts = claude_turns(ctx)
    assert starts[0]["detail"]["role"] == "strategist"
    assert starts[0]["detail"]["model"] == "Opus 5.5"
    assert starts[0]["detail"]["effort"] == "medium"
    for event in starts[1:]:
        assert event["detail"]["model"] == "Sonnet 5.5", event["detail"]
        assert event["detail"]["effort"] == "high"
    for event in ctx.journal.of_type("turn.start") + ctx.journal.of_type("turn.end"):
        if event["type"] == "turn.start" and event["detail"].get("app") == "coder":
            continue
        assert {"role", "model", "effort", "model_verified", "effort_verified"} <= set(event["detail"]), event["detail"]

    escalated = ctx.journal.of_type("model.escalated")
    assert [event["detail"]["trigger"] for event in escalated] == ["first_plan"]
    assert escalated[0]["detail"]["opus_turns_today"] == {"value": 1, "limit": 3}
    assert escalated[0]["detail"]["opus_turns_milestone"] == {"value": 0, "limit": 1}

    usage = orch.status()["opus"]
    assert usage == {"today": 1, "today_limit": 3, "milestone": 0, "milestone_limit": 1}
    # The coder is GLM and does not pretend the model was verified.
    coder_starts = [
        e for e in ctx.journal.of_type("turn.start") if e["detail"].get("app") == "coder"
    ]
    assert coder_starts and all(e["detail"]["model_verified"] == "unverifiable" for e in coder_starts)


def test_first_plan_opus_then_later_plan_is_sonnet(ctx):
    apps = PolicyApps(ctx.cfg.project_dir, verdicts=["MILESTONE_DONE", "PROJECT_DONE"])
    orch, notifier, _ = build(ctx, apps)
    state = orch.run(max_steps=30)
    assert state["phase"] == "DONE"

    roles = [event["detail"]["role"] for event in claude_turns(ctx)]
    assert roles[:4] == ["strategist", "reviewer", "planner", "reviewer"]
    planner = claude_turns(ctx)[2]
    assert planner["detail"]["model"] == "Sonnet 5.5"


# --- B5 3: the fix plan at streak 2 ------------------------------------------

def test_streak_two_writes_fix_plan_and_next_code_pointer_names_it(ctx):
    issue = "auth-token-expiry"
    apps = PolicyApps(
        ctx.cfg.project_dir,
        verdicts=["CHANGES_REQUESTED", "CHANGES_REQUESTED", "PROJECT_DONE"],
        issues=[[issue], [issue], []],
    )
    orch, notifier, _ = build(ctx, apps)
    state = orch.run(max_steps=40)
    assert state["phase"] == "DONE"

    # N4: once the issue stops blocking, the plan is archived instead of followed forever.
    archived = ctx.journal.of_type("fix.plan_archived")
    assert archived and archived[0]["detail"]["reason"] == "issues resolved"
    assert not (ctx.cfg.project_dir / ".relay/FIX_PLAN.md").exists()
    assert (ctx.cfg.project_dir / archived[0]["detail"]["archived"]).exists()
    requested = ctx.journal.of_type("fix.plan_requested")
    assert requested, "the harness must journal the fix-plan request"
    assert requested[0]["detail"]["issue_ids"] == [issue]
    assert requested[0]["detail"]["streak"] == 2

    reviewer_turns = [
        e for e in claude_turns(ctx) if e["detail"]["role"] == "reviewer"
    ]
    assert all(e["detail"]["model"] == "Sonnet 5.5" for e in reviewer_turns)

    coder_rings = [pointer for pointer in apps.coder.rings if "role=coder" in pointer]
    assert any(".relay/FIX_PLAN.md" in pointer for pointer in coder_rings)
    assert [e["detail"]["trigger"] for e in ctx.journal.of_type("model.escalated")] == ["first_plan"]


# --- B5 4: the Opus fixer at streak 4 ---------------------------------------

def test_streak_four_runs_the_opus_fixer_then_a_sonnet_review(ctx):
    issue = "hard-bug"
    apps = PolicyApps(
        ctx.cfg.project_dir,
        verdicts=["CHANGES_REQUESTED"] * 4 + ["PROJECT_DONE"],
        issues=[[issue]] * 4 + [[]],
    )
    orch, notifier, _ = build(ctx, apps)
    state = orch.run(max_steps=60)
    assert state["phase"] == "DONE"

    fixer_turns = [
        e for e in claude_turns(ctx) if e["detail"]["role"] == "fixer"
    ]
    assert len(fixer_turns) == 1
    fixer = fixer_turns[0]
    assert fixer["detail"]["model"] == "Opus 5.5"
    assert fixer["detail"]["effort"] == "medium"

    escalation = [
        e for e in ctx.journal.of_type("model.escalated") if e["detail"]["trigger"] == "hardest_bug_streak"
    ]
    assert len(escalation) == 1
    assert escalation[0]["detail"]["issue_ids"] == [issue]
    assert escalation[0]["detail"]["streak"] == 4
    assert escalation[0]["detail"]["opus_turns_milestone"] == {"value": 1, "limit": 1}

    # The fixer edited source (allowed for the baton holder) and wrote its report.
    fixer_end = [
        e for e in ctx.journal.of_type("turn.end") if e["detail"].get("role") == "fixer"
    ][0]
    assert "src/fix_0.py" in fixer_end["detail"]["changed"]
    assert (ctx.cfg.project_dir / ".relay/FIX_REPORT.md").exists()

    # The review after the fixer reads FIX_REPORT.md and runs on Sonnet.
    after = [
        e for e in ctx.journal.of_type("turn.start")
        if e["detail"].get("role") == "reviewer"
        and e["round"] == fixer["round"] + 1
    ][0]
    assert after["detail"]["model"] == "Sonnet 5.5"
    assert ".relay/FIX_REPORT.md" in after["detail"]["pointer"]


# --- B5 5: fixer ownership ---------------------------------------------------

def _force_fixer_state(ctx, issues=("hard-bug",)):
    def apply(state):
        state["phase"] = "CODE"
        state["issue_streaks"] = {issue: 4 for issue in issues}

    ctx.store.mutate(apply)


def test_fixer_over_the_file_limit_pauses(ctx):
    apps = PolicyApps(ctx.cfg.project_dir, fixer_files=11)
    orch, notifier, _ = build(ctx, apps)
    _force_fixer_state(ctx)
    state = orch.run(max_steps=2)

    assert state["phase"] == "PAUSED"
    violation = ctx.journal.latest("integrity.violation")
    assert violation["detail"]["kind"] == "fixer_scope"
    assert violation["detail"]["limit"] == 10


def test_fixer_cannot_write_the_plan(ctx):
    apps = PolicyApps(ctx.cfg.project_dir, fixer_writes_plan=True)
    orch, notifier, _ = build(ctx, apps)
    _force_fixer_state(ctx)
    state = orch.run(max_steps=2)

    assert state["phase"] == "PAUSED"
    violation = ctx.journal.latest("integrity.violation")
    assert violation["detail"]["kind"] == "ownership"
    assert violation["detail"]["violations"][0]["path"] == ".relay/PLAN.md"


def test_coder_writing_source_during_a_fixer_turn_is_a_violation():
    before = {"src/app.py": "a" * 64}
    after = {"src/app.py": "b" * 64}
    violations = ownership_violations(before, after, "coder", active_role="fixer")
    assert violations and violations[0]["owner"] == "fixer"
    assert ownership_violations(before, after, "fixer", active_role="fixer") == []


# --- B5 6: a second Opus fix needs the human --------------------------------

def test_streak_growing_after_an_opus_fix_asks_the_human(ctx):
    state = new_state()
    state["issue_streaks"] = {"hard-bug": 5}
    state["opus_fixed"] = {"hard-bug": {"streak": 4, "round": 9}}
    choice = choose_turn(state, "CODE", ctx.cfg, today="2026-10-20")
    assert choice.role is None
    assert choice.needs_human["kind"] == "fixer_retry"
    labels = [option["label"] for option in choice.needs_human["options"]]
    assert labels == ["Allow another Opus fix", "Continue on Sonnet", "Stop for now"]


# --- B5 7: the re-plan path --------------------------------------------------

def test_stall_ladder_sonnet_then_opus_then_human(ctx):
    apps = PolicyApps(
        ctx.cfg.project_dir,
        verdicts=["CHANGES_REQUESTED", "CHANGES_REQUESTED", "CHANGES_REQUESTED"],
    )
    orch, notifier, _ = build(ctx, apps, brain=StubBrain(stuck=True), poll_seconds=1)

    # First stall: the next PLAN turn is the Sonnet planner.
    state = orch.run(max_steps=4)
    assert state["phase"] == "PLAN"
    assert state["counters"]["stalls_in_milestone"] == 1
    state = orch.run(max_steps=1)
    first_replan = claude_turns(ctx)[-1]
    assert first_replan["detail"]["role"] == "planner"
    assert first_replan["detail"]["model"] == "Sonnet 5.5"

    # Second stall in the same milestone: the Opus strategist.
    state = orch.run(max_steps=3)
    assert state["phase"] == "PLAN"
    assert state["counters"]["stalls_in_milestone"] == 2
    state = orch.run(max_steps=1)
    second_replan = claude_turns(ctx)[-1]
    assert second_replan["detail"]["role"] == "strategist"
    assert second_replan["detail"]["model"] == "Opus 5.5"
    assert ctx.journal.of_type("model.escalated")[-1]["detail"]["trigger"] == "replan_after_sonnet"

    # Third stall: a human decision, not another Opus turn.
    state = orch.run(max_steps=3)
    assert state["phase"] == "PLAN"
    assert state["counters"]["stalls_in_milestone"] == 3
    state = orch.run(max_steps=1, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    assert state["pending_decision"]["kind"] == "stall_cap"
    assert len(state["pending_decision"]["options"]) == 3


# --- B5 8: a limit asks first, and the answer is applied ---------------------

def test_opus_limit_asks_with_three_options_before_the_turn(ctx):
    apps = PolicyApps(ctx.cfg.project_dir, fixer_files=1)
    orch, notifier, _ = build(ctx, apps)
    orch.run(max_steps=1)  # first plan (exempt from the milestone limit)

    def burn_day(state):
        state["opus"]["day"] = 3
        state["issue_streaks"] = {"hard-bug": 4}
        state["phase"] = "CODE"

    ctx.store.mutate(burn_day)
    state = orch.run(max_steps=2, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    assert state["pending_decision"]["kind"] == "model_limit"
    labels = [option["label"] for option in state["pending_decision"]["options"]]
    assert labels == ["Allow this Opus turn", "Continue on Sonnet", "Pause"]
    assert not [e for e in claude_turns(ctx) if e["detail"]["role"] == "fixer"]

    orch.answer_decision("Allow this Opus turn", code_from(notifier))
    state = orch.run(max_steps=3)
    fixer = [e for e in claude_turns(ctx) if e["detail"]["role"] == "fixer"]
    assert fixer, "the approved Opus turn must run"
    assert ctx.journal.of_type("model.escalated")[-1]["detail"]["limit_bypassed"] == "day"


def test_opus_limit_continue_on_sonnet_never_runs_opus(ctx):
    apps = PolicyApps(ctx.cfg.project_dir, fixer_files=1)
    orch, notifier, _ = build(ctx, apps)
    orch.run(max_steps=1)

    def burn_day(state):
        state["opus"]["day"] = 3
        state["issue_streaks"] = {"hard-bug": 4}
        state["phase"] = "CODE"

    ctx.store.mutate(burn_day)
    state = orch.run(max_steps=2, wait_human=False)
    orch.answer_decision("Continue on Sonnet", code_from(notifier))
    state = orch.run(max_steps=3)
    assert not [e for e in claude_turns(ctx) if e["detail"]["role"] == "fixer"]
    assert state["opus_declined"]["issue_ids"] == ["hard-bug"]
    assert ctx.journal.of_type("model.decision_applied")[-1]["detail"]["action"] == "sonnet"


# --- B5 9: a DeepSeek suggestion is journaled, never a trigger ---------------

def test_suggest_escalation_is_journaled_but_does_not_start_opus(ctx):
    apps = PolicyApps(ctx.cfg.project_dir, verdicts=["CHANGES_REQUESTED", "PROJECT_DONE"])
    orch, notifier, _ = build(ctx, apps, brain=StubBrain(suggest_escalation=True))
    state = orch.run(max_steps=30)
    assert state["phase"] == "DONE"

    judgments = ctx.journal.of_type("route.judgment")
    assert judgments and all(e["detail"]["suggest_escalation"] is True for e in judgments)
    triggers = [e["detail"]["trigger"] for e in ctx.journal.of_type("model.escalated")]
    assert triggers == ["first_plan"]


# --- B5 10: an unverifiable model types nothing ------------------------------

def test_model_verification_failure_types_nothing(ctx):
    apps = PolicyApps(ctx.cfg.project_dir)
    orch, notifier, _ = build(ctx, apps)
    apps.claude.model_fails = True

    state = orch.run(max_steps=2, wait_human=False)
    assert state["phase"] == "WAIT_HUMAN"
    assert ctx.journal.of_type("turn.start") == []
    assert not [pointer for pointer in apps.claude.rings if "RELAY round" in pointer]
    assert apps.claude.rings == [pointer for pointer in apps.claude.rings if pointer.startswith("RELAY seed")]
    assert "model" in ctx.paths.pending_decision.read_text().lower()


# --- B5 4 (second trigger): the coder blocked twice --------------------------

def test_coder_blocked_twice_on_one_issue_starts_the_fixer(ctx):
    issue = "db-lock-timeout"
    apps = PolicyApps(
        ctx.cfg.project_dir,
        coder_blocks=[[issue], [issue]],
        verdicts=["PROJECT_DONE"],
    )
    orch, notifier, _ = build(ctx, apps)
    orch.run(max_steps=3, wait_human=False)  # planner, then the first blocked coder
    state = ctx.store.load()
    assert state["phase"] == "WAIT_HUMAN"
    orch.answer_decision("Keep going", code_from(notifier))

    state = orch.run(max_steps=3)
    assert state["counters"]["total_rounds"] >= 3
    # The second block does not ask again; the fixer takes the next CODE turn.
    fixer = [e for e in claude_turns(ctx) if e["detail"]["role"] == "fixer"]
    assert fixer, ctx.store.load()
    assert ctx.journal.of_type("model.escalated")[-1]["detail"]["trigger"] == "hardest_bug_blocked"


# --- B5 2 + acceptance: two milestones, Opus only twice ----------------------

def test_two_milestones_use_opus_only_for_the_first_plan_and_one_fixer(ctx):
    issue = "hard-bug"
    apps = PolicyApps(
        ctx.cfg.project_dir,
        verdicts=["CHANGES_REQUESTED"] * 4 + ["MILESTONE_DONE", "PROJECT_DONE"],
        issues=[[issue]] * 4 + [[]],
    )
    orch, notifier, _ = build(ctx, apps)
    state = orch.run(max_steps=80)
    assert state["phase"] == "DONE"
    assert state["milestone"] == 2

    escalated = ctx.journal.of_type("model.escalated")
    assert [e["detail"]["trigger"] for e in escalated] == ["first_plan", "hardest_bug_streak"]

    for event in claude_turns(ctx):
        if event["detail"]["role"] in ("strategist", "fixer"):
            assert event["detail"]["model"] == "Opus 5.5"
        else:
            assert event["detail"]["model"] == "Sonnet 5.5"
    ratio = len([e for e in claude_turns(ctx) if e["detail"]["model"] == "Opus 5.5"]) / len(claude_turns(ctx))
    assert ratio <= 0.25


def test_a_repeated_streak_without_growth_may_use_the_fixer_again(ctx):
    state = new_state()
    state["issue_streaks"] = {"hard-bug": 4}
    state["opus_fixed"] = {"hard-bug": {"streak": 4, "round": 9}}
    choice = choose_turn(state, "CODE", ctx.cfg, today="2026-10-20")
    assert choice.role == "fixer"
    assert choice.needs_human is None

    state["issue_streaks"] = {"hard-bug": 5}
    choice = choose_turn(state, "CODE", ctx.cfg, today="2026-10-20")
    assert choice.role is None
    assert choice.needs_human["kind"] == "fixer_retry"


# --- N4: a stale FIX_PLAN is archived and no longer named ---------------------

def test_resolved_fix_plan_is_archived_before_the_next_code_turn(ctx):
    issue = "stale-plan-issue"
    apps = PolicyApps(
        ctx.cfg.project_dir,
        verdicts=["CHANGES_REQUESTED", "CHANGES_REQUESTED", "CHANGES_REQUESTED", "PROJECT_DONE"],
        issues=[[issue], [issue], [], []],
    )
    orch, notifier, _ = build(ctx, apps)
    state = orch.run(max_steps=50)
    assert state["phase"] == "DONE"

    archived = ctx.journal.of_type("fix.plan_archived")
    assert archived, "the resolved plan must be archived"
    assert archived[0]["detail"]["reason"] == "issues resolved"
    assert not (ctx.cfg.project_dir / ".relay/FIX_PLAN.md").exists()
    assert state["fix_plan_issues"] == []

    code_rings = [
        pointer for pointer in apps.coder.rings
        if "role=coder" in pointer and not pointer.startswith(("RELAY seed", "RELAY nudge"))
    ]
    assert any(".relay/FIX_PLAN.md" in pointer for pointer in code_rings[:-1])
    assert ".relay/FIX_PLAN.md" not in code_rings[-1], "a resolved plan must not be followed"


# --- N5: no hidden Opus checkpoint turns --------------------------------------

def test_no_uncounted_opus_turn_after_the_fixer(ctx):
    issue = "hard-bug"
    apps = PolicyApps(
        ctx.cfg.project_dir,
        verdicts=["CHANGES_REQUESTED"] * 4 + ["PROJECT_DONE"],
        issues=[[issue]] * 4 + [[]],
    )
    orch, notifier, _ = build(ctx, apps)
    state = orch.run(max_steps=60)
    assert state["phase"] == "DONE"

    opus_turns = [
        event for event in ctx.journal.of_type("turn.start")
        if event["detail"].get("model") == "Opus 5.5"
    ]
    escalated = ctx.journal.of_type("model.escalated")
    assert len(opus_turns) == len(escalated), (opus_turns, escalated)
    assert not any(
        pointer.startswith("RELAY checkpoint") and "role=fixer" in pointer
        for pointer in apps.claude.rings
    )
    assert not any(
        event["detail"].get("role") in ("fixer", "strategist")
        for event in ctx.journal.of_type("session.checkpoint")
    )


# --- N8: the reviewer is shown the previous issue ids ------------------------

def test_reviewer_pointer_lists_previous_issue_ids(ctx):
    issue = "auth-token-expiry"
    apps = PolicyApps(
        ctx.cfg.project_dir,
        verdicts=["CHANGES_REQUESTED", "CHANGES_REQUESTED", "PROJECT_DONE"],
        issues=[[issue], [issue], []],
    )
    orch, notifier, _ = build(ctx, apps)
    orch.run(max_steps=40)

    reviewer_rings = [
        pointer for pointer in apps.claude.rings
        if "role=reviewer" in pointer and not pointer.startswith(("RELAY seed", "RELAY nudge"))
    ]
    assert len(reviewer_rings) >= 2
    assert f"Previous ids: {issue}" in reviewer_rings[1]
    assert all(len(pointer) < 500 for pointer in reviewer_rings)
