"""Deterministic model policy (Part B): Sonnet by default, Opus only by an explicit rule.

`choose_turn` is a pure function of `STATE.json`, the phase and the config. No model, prompt
or DeepSeek judgment can start an Opus turn; `suggest_escalation` is journaled only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

CLAUDE_ROLES = ("planner", "reviewer", "strategist", "fixer")
ESCALATED_ROLES = ("strategist", "fixer")

DEFAULT_POLICY: dict[str, Any] = {
    "default": {"model": "Sonnet 5.5", "effort": "high"},
    "escalated": {"model": "Opus 5.5", "effort": "medium"},
    "escalate_on_first_plan": True,
    "fix_plan_after": 2,
    "opus_fix_after": 4,
    "fixer_max_files": 10,
    "max_opus_turns_per_milestone": 1,
    "max_opus_turns_per_day": 3,
}

ROLE_APP = {"planner": "claude", "reviewer": "claude", "strategist": "claude",
            "fixer": "claude", "coder": "coder"}
EXPECTED_FILE = {
    "planner": ".relay/HANDOFF.md",
    "strategist": ".relay/HANDOFF.md",
    "reviewer": ".relay/REVIEW.md",
    "fixer": ".relay/FIX_REPORT.md",
    "coder": ".relay/CODER_REPORT.md",
}


def policy(cfg: Any) -> dict[str, Any]:
    """The effective `model_policy` block, with defaults filled in."""
    raw = cfg.get("model_policy", {}) or {}
    merged: dict[str, Any] = {}
    for key, value in DEFAULT_POLICY.items():
        if isinstance(value, dict):
            merged[key] = {**value, **(raw.get(key) or {})}
        else:
            merged[key] = raw.get(key, value)
    return merged


def role_app(role: str) -> str:
    return ROLE_APP[role]


def expected_file(role: str) -> str:
    return EXPECTED_FILE[role]


def is_claude_role(role: str) -> bool:
    return role in CLAUDE_ROLES


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def opus_usage(state: dict, today: str, cfg: Any) -> dict[str, int]:
    """Opus turns used today and in this milestone, with their limits."""
    p = policy(cfg)
    opus = state.get("opus") or {}
    day = _int(opus.get("day")) if str(opus.get("date") or "") == str(today) else 0
    milestone = (
        _int(opus.get("milestone_turns"))
        if _int(opus.get("milestone")) == _int(state.get("milestone"), 1)
        else 0
    )
    return {
        "today": day,
        "today_limit": _int(p["max_opus_turns_per_day"], 3),
        "milestone": milestone,
        "milestone_limit": _int(p["max_opus_turns_per_milestone"], 1),
    }


@dataclass(frozen=True)
class TurnChoice:
    role: str | None
    model: str | None
    effort: str | None
    trigger: str = "normal"
    reason: str = ""
    issue_ids: tuple[str, ...] = ()
    streak: int = 0
    escalated: bool = False
    exempt_milestone: bool = False
    limit_bypassed: str | None = None
    fix_plan_issues: tuple[str, ...] = ()
    fix_plan_streak: int = 0
    needs_human: dict | None = None


def _hardest(streaks: Any) -> tuple[str, int]:
    if not isinstance(streaks, dict) or not streaks:
        return "", 0
    items = [(str(key), _int(value)) for key, value in streaks.items() if _int(value) > 0]
    if not items:
        return "", 0
    # Highest streak wins; ties break on the id so the choice is deterministic.
    return max(items, key=lambda item: (item[1], item[0]))


def _declined(state: dict, trigger: str, issue_ids: tuple[str, ...]) -> bool:
    declined = state.get("opus_declined") or {}
    if not isinstance(declined, dict) or not declined:
        return False
    if str(declined.get("trigger") or "") != trigger:
        return False
    recorded = {str(item) for item in (declined.get("issue_ids") or [])}
    return recorded == set(issue_ids)


def _overridden(state: dict, role: str, *, retry: bool = False) -> bool:
    override = state.get("opus_override") or {}
    if not isinstance(override, dict) or override.get("role") != role:
        return False
    return bool(override.get("retry")) if retry else True


def _limit_question(role: str, trigger: str, issue_ids: tuple[str, ...], streak: int,
                    usage: dict[str, int], limit: str, *, retry: bool = False) -> dict:
    used = usage["today"] if limit == "day" else usage["milestone"]
    ceiling = usage["today_limit"] if limit == "day" else usage["milestone_limit"]
    options = [
        {"label": "Allow this Opus turn",
         "consequence": f"Runs {role} on Opus once (today {usage['today']}/{usage['today_limit']}, "
                        f"this milestone {usage['milestone']}/{usage['milestone_limit']})."},
        {"label": "Continue on Sonnet",
         "consequence": "Keeps the normal model for this condition and does not ask again until it changes."},
        {"label": "Pause",
         "consequence": "The loop pauses so you can change model_policy limits."},
    ]
    question = (
        f"The Opus rule '{trigger}' fired, but the {limit} limit is used up "
        f"({used} of {ceiling}). What should Relay do?"
    )
    return {
        "question": question,
        "options": options,
        "kind": "model_limit",
        "data": {
            "role": role, "trigger": trigger, "condition": trigger,
            "issue_ids": list(issue_ids), "streak": streak, "limit": limit, "retry": retry,
        },
    }


def choose_turn(
    state: dict,
    phase: str,
    cfg: Any,
    *,
    has_plan: bool = True,
    today: str | None = None,
) -> TurnChoice:
    """Decide who runs this turn, on which model, and why.

    Deterministic: only STATE.json, the phase and model_policy are read.
    """
    p = policy(cfg)
    usage = opus_usage(state, str(today or ""), cfg)
    counters = state.get("counters") or {}
    default = p["default"]
    escalated = p["escalated"]
    stalled = _int(counters.get("stalls_in_milestone"))

    def sonnet(role: str, trigger: str = "normal", reason: str = "", **extra: Any) -> TurnChoice:
        return TurnChoice(
            role=role, model=str(default["model"]), effort=str(default["effort"]),
            trigger=trigger, reason=reason, **extra,
        )

    def opus(role: str, trigger: str, reason: str, *, issue_ids: tuple[str, ...] = (),
             streak: int = 0, retry: bool = False) -> TurnChoice:
        exempt = trigger == "first_plan"
        choice = TurnChoice(
            role=role, model=str(escalated["model"]), effort=str(escalated["effort"]),
            trigger=trigger, reason=reason, issue_ids=issue_ids, streak=streak,
            escalated=True, exempt_milestone=exempt,
        )
        hit = ""
        if usage["today"] >= usage["today_limit"]:
            hit = "day"
        if not exempt and usage["milestone"] >= usage["milestone_limit"]:
            hit = hit or "milestone"
        if not hit:
            return choice
        if _overridden(state, role, retry=retry):
            return replace(choice, limit_bypassed=hit)
        needs = _limit_question(role, trigger, issue_ids, streak, usage, hit, retry=retry)
        return TurnChoice(
            role=None, model=None, effort=None, trigger=trigger, reason=reason,
            issue_ids=issue_ids, streak=streak, needs_human=needs,
        )

    if phase == "REVIEW":
        predicted, predicted_streak = _fix_plan_prediction(state, p)
        choice = sonnet("reviewer")
        if predicted:
            choice = replace(
                choice, fix_plan_issues=predicted, fix_plan_streak=predicted_streak,
                reason=f"issues repeated {predicted_streak} times",
            )
        return choice

    if phase == "PLAN":
        request = state.get("human_opus_request") or {}
        if request:
            return opus("strategist", "human", "the human asked for an Opus turn")
        if stalled >= 3:
            options = [
                {"label": "Re-plan again",
                 "consequence": "Starts a new planning cycle with the Sonnet planner."},
                {"label": "Continue on Sonnet",
                 "consequence": "Same thing without another Opus turn; the stall count restarts."},
                {"label": "Stop for now",
                 "consequence": "The loop pauses so you can inspect the work."},
            ]
            return TurnChoice(
                role=None, model=None, effort=None, trigger="stall_cap",
                reason=f"{stalled} stalls in this milestone",
                needs_human={
                    "question": (
                        f"Relay has stalled {stalled} times in this milestone, past the Sonnet and "
                        "Opus re-plans. Decide what to change."
                    ),
                    "options": options,
                    "kind": "stall_cap",
                    "data": {"stalls": stalled},
                },
            )
        if not has_plan and bool(p.get("escalate_on_first_plan", True)):
            return opus("strategist", "first_plan", "the first master plan")
        if stalled >= 2:
            return opus("strategist", "replan_after_sonnet",
                        f"{stalled} stalls after the Sonnet re-plan")
        return sonnet("planner")

    if phase == "CODE":
        blocked_id, blocked_streak = _hardest(state.get("blocked_streaks"))
        issue_id, streak = _hardest(state.get("issue_streaks"))
        request = state.get("human_opus_request") or {}
        if request:
            return opus("fixer", "human", "the human asked for an Opus fix",
                        issue_ids=tuple(filter(None, [issue_id])), streak=streak)
        if blocked_streak >= 2 and blocked_id:
            return _fixer_choice(state, opus, cfg, blocked_id, blocked_streak,
                                 "hardest_bug_blocked",
                                 f"the coder reported blocked twice on {blocked_id}")
        if streak >= _int(p["opus_fix_after"], 4) and issue_id:
            fixed = (state.get("opus_fixed") or {}).get(issue_id)
            grew = isinstance(fixed, dict) and streak > _int(fixed.get("streak"))
            if grew:
                if _overridden(state, "fixer", retry=True):
                    return opus("fixer", "hardest_bug_streak",
                                f"{issue_id} still blocking after an Opus fix (approved retry)",
                                issue_ids=(issue_id,), streak=streak, retry=True)
                options = [
                    {"label": "Allow another Opus fix",
                     "consequence": "Runs the Opus fixer on the same issue again."},
                    {"label": "Continue on Sonnet",
                     "consequence": "The GLM coder tries again without Opus."},
                    {"label": "Stop for now",
                     "consequence": "The loop pauses so you can change the approach."},
                ]
                return TurnChoice(
                    role=None, model=None, effort=None, trigger="hardest_bug_streak",
                    reason=f"{issue_id} did not improve after an Opus fix",
                    issue_ids=(issue_id,), streak=streak,
                    needs_human={
                        "question": (
                            f"Issue {issue_id} is still blocking (streak {streak}) after an Opus fix. "
                            "Decide what Relay should do."
                        ),
                        "options": options,
                        "kind": "fixer_retry",
                        "data": {"role": "fixer", "trigger": "hardest_bug_streak",
                                 "issue_ids": [issue_id], "streak": streak},
                    },
                )
            return _fixer_choice(state, opus, cfg, issue_id, streak, "hardest_bug_streak",
                                 f"issue {issue_id} has blocked {streak} reviews")
        return _coder_choice(cfg, state)

    return sonnet("planner", reason=f"unknown phase {phase!r}")


def _fixer_choice(state: dict, opus, cfg: Any, issue_id: str, streak: int, trigger: str,
                  reason: str) -> TurnChoice:
    if _declined(state, trigger, (issue_id,)):
        choice = _coder_choice(cfg, state, reason=reason)
        return replace(choice, trigger="declined", issue_ids=(issue_id,), streak=streak)
    return opus("fixer", trigger, reason, issue_ids=(issue_id,), streak=streak)


def _coder_choice(cfg: Any, state: dict, reason: str = "") -> TurnChoice:
    return TurnChoice(
        role="coder",
        model=str(cfg.get("zcode_app.model", "GLM 5.3 flash")),
        effort=str(cfg.get("zcode_app.effort", "high")),
        reason=reason,
    )


def _fix_plan_prediction(state: dict, p: dict) -> tuple[tuple[str, ...], int]:
    """Issues likely to repeat and reach the fix-plan step in the coming review."""
    threshold = _int(p["fix_plan_after"], 2)
    opus_after = _int(p["opus_fix_after"], 4)
    streaks = state.get("issue_streaks") or {}
    picked = [
        (str(issue), _int(streak) + 1)
        for issue, streak in streaks.items()
        if threshold <= _int(streak) + 1 < opus_after
    ]
    if not picked:
        return (), 0
    picked.sort(key=lambda item: (-item[1], item[0]))
    return tuple(issue for issue, _ in picked[:10]), max(streak for _, streak in picked)
