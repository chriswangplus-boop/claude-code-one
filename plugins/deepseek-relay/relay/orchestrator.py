"""The relay loop: hand the baton, ring the doorbell, wait for a valid sentinel.

The orchestrator holds no memory between steps. Every decision is reconstructed from
STATE.json, the files in .relay/, and the journal. That is what keeps it from drifting.
"""

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .brain import Brain
from .config import Config
from .context import Context
from .decisions import accept_decision, hash_code, local_answer, new_code, write_pending
from .errors import (
    AppMismatch, BrainError, BudgetExceeded, ContextGuardTripped, HumanRequired, IntegrityError,
    JournalError, PauseRequested, RelayError, SecretDetected, StateError,
    VisionUnavailable, WaitTimeout,
)
from .gui.base import AppDriver
from .gui.recovery import Recovery
from .gui.screen import ScreenError
from .integrity import (
    changed_paths, git_commit, git_control_changes, git_control_snapshot, git_snapshot,
    ownership_violations, read_sentinel, secret_scan, snapshot, validate_sentinel,
)
from .journal import Journal
from .model_policy import (
    CLAUDE_ROLES, choose_turn, expected_file, is_claude_role, opus_usage, policy, role_app,
)
from .policy import choose_button, decide_popup
from .redact import redact
from .state import StateStore, read_json
from .util import ascii_pointer, canonical_json, sha256_file, sha256_text, short_hash, truncate

PHASE_ROLE = {"PLAN": "planner", "CODE": "coder", "REVIEW": "reviewer"}
NEXT_PHASE = {"PLAN": "CODE", "CODE": "REVIEW", "REVIEW": "ROUTE"}

DEFAULT_OPTIONS = [
    {"label": "Continue", "consequence": "Relay resumes with the same plan."},
    {"label": "Stop for now", "consequence": "The loop stays paused; nothing is lost."},
]
POPUP_OPTIONS = [
    {"label": "Allow this one action", "consequence": "The agent continues past the dialog."},
    {"label": "Deny and stop", "consequence": "The loop pauses; you handle the dialog yourself."},
]


@dataclass
class WaitSpec:
    role: str
    round_no: int
    expected_file: str
    pointer: str
    timeout_minutes: int
    started_epoch: float
    started_wall: float


class _NullNotifier:
    def send(self, *args: Any, **kwargs: Any) -> dict:
        return {"channels": {}, "errors": []}


ISSUE_ID = re.compile(r"^[a-z0-9-]{3,40}$")


def _issue_ids(sentinel: dict) -> list[str]:
    """The reviewer/coder stable issue slugs, validated and de-duplicated in order."""
    raw = sentinel.get("blocking_issue_ids") if isinstance(sentinel, dict) else None
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if ISSUE_ID.match(text) and text not in seen:
            seen.append(text)
    return seen[:10]


def _strip_answer_line(text: str) -> str:
    """Drop the human's ANSWER line: editing only that line is the intended way to answer."""
    kept = [
        line for line in str(text or "").splitlines()
        if not line.strip().upper().startswith("ANSWER:")
    ]
    return "\n".join(kept)


def _match_option(answer: str, options: list[dict]) -> str | None:
    """Match a free-form human answer to one option label, deterministically.

    An exact label wins; otherwise a label contained in the answer. Unknown answers return
    None, and the caller picks the safe option (never silently exceed a limit).
    """
    if not isinstance(options, list):
        return None
    text = " ".join(str(answer or "").lower().split())
    labels = [str((option or {}).get("label") or "").strip() for option in options]
    for label in labels:
        if label and text == label.lower():
            return label
    for label in labels:
        if label and label.lower() in text:
            return label
    return None


class Orchestrator:
    def __init__(
        self,
        ctx: Context,
        drivers: dict[str, AppDriver],
        *,
        brain: Brain | None = None,
        notifier: Any = None,
        monitor_required: bool = False,
        wall: Callable[[], float] | None = None,
    ):
        self.ctx = ctx
        self.cfg: Config = ctx.cfg
        self.paths = ctx.paths
        self.store: StateStore = ctx.store
        self.journal: Journal = ctx.journal
        self.trust = ctx.trust
        self.clock = ctx.clock
        self.drivers = drivers
        self.brain = brain
        self.notifier = notifier or _NullNotifier()
        self.monitor_required = monitor_required
        self.wall = wall or __import__("time").time
        monitor_age = self.store.heartbeat_age(self.paths.monitor_heartbeat)
        self._monitor_seen = monitor_age is not None and monitor_age < float(
            self.cfg.get("monitor.heartbeat_stale_seconds", 300)
        )
        self._monitor_grace_until = self.clock.epoch() + 90
        self._last_busy: bool | None = None
        self._first_prepare = True
        self._decision_codes: dict[str, str] = {}
        self._run_id = uuid.uuid4().hex[:8]

    # --- public -----------------------------------------------------------
    def run(self, *, max_steps: int | None = None, wait_human: bool = True) -> dict:
        self._prepare()
        steps = 0
        while True:
            try:
                self._apply_decision_effects()
                self._check_guards()
                state = self._state()
                phase = state["phase"]

                if phase == "DONE":
                    self._notify("Relay finished", "The project reported PROJECT_DONE.")
                    break
                if phase in ("PAUSED", "HALTED"):
                    break
                if phase == "WAIT_HUMAN":
                    if not wait_human:
                        break
                    self.wait_for_decision()
                    continue
                if phase == "INIT":
                    self.store.set_phase("PLAN")
                    self.journal.append("state.transition", state="PLAN", detail={"from": "INIT", "to": "PLAN"})
                    continue
                if self._apply_correction():
                    continue
                if phase == "ROUTE":
                    self._route()
                elif phase in PHASE_ROLE:
                    self._turn(phase)
                else:
                    raise StateError(f"unknown phase: {phase}")
                steps += 1
                if max_steps is not None and steps >= max_steps:
                    self.journal.append(
                        "run.stopped", detail={"reason": "max_steps", "steps": steps}, result="ok"
                    )
                    break
            except PauseRequested as exc:
                self._pause(str(exc) or "paused")
                break
            except HumanRequired as exc:
                self.escalate_human(exc.question, exc.options or DEFAULT_OPTIONS, reason=exc.reason)
                if not wait_human:
                    break
            except WaitTimeout as exc:
                self.escalate_human(
                    f"An app stopped responding: {exc}",
                    options=[
                        {"label": "Retry this step", "consequence": "Relay re-sends the pointer and waits again."},
                        {"label": "Stop for now", "consequence": "The loop pauses; inspect the app."},
                    ],
                    reason="timeout",
                    return_to=self._state().get("phase"),
                )
                if not wait_human:
                    break
            except ContextGuardTripped as exc:
                self._pause(
                    f"DeepSeek context guard tripped at {exc.ctx_pct:.0%}"
                    + (f" (call {exc.call_id})" if exc.call_id else "")
                )
                break
            except BudgetExceeded as exc:
                self._pause(str(exc))
                break
            except RelayError as exc:
                self._pause(f"{type(exc).__name__}: {exc}")
                break
        return self._state()

    def resume(self, *, max_steps: int | None = None, wait_human: bool = True) -> dict:
        self.paths.pause_flag.unlink(missing_ok=True)
        self.paths.stop_flag.unlink(missing_ok=True)
        state = self._state()
        if state.get("phase") == "PAUSED":
            target = state.get("return_to") or "PLAN"
            state["phase"] = target
            state["paused_reason"] = None
            state["return_to"] = None
            self.store.save(state)
            self.journal.append("run.resume", state=target, detail={"pid": __import__("os").getpid()})
        return self.run(max_steps=max_steps, wait_human=wait_human)

    def answer_decision(self, answer: str, code: str) -> dict:
        result = accept_decision(self.ctx, answer, code)
        self._notify("Relay resumed", f"{result['id']} answered. Continuing to {result['phase']}.")
        return result

    def status(self) -> dict:
        state = self._state() if self.paths.state.exists() else {}
        baton = self.store.baton()
        monitor_age = self.store.heartbeat_age(self.paths.monitor_heartbeat)
        events = self.journal.all_events()[-8:]
        return {
            "phase": state.get("phase"),
            "round": state.get("round"),
            "milestone": state.get("milestone"),
            "role": state.get("role"),
            "paused_reason": state.get("paused_reason"),
            "baton": baton,
            "session_rounds": state.get("session_rounds"),
            "counters": state.get("counters"),
            "pending_decision": state.get("pending_decision"),
            "monitor_heartbeat_age": monitor_age,
            "spent_today_usd": self.journal.spend_for_day(),
            "opus": opus_usage(state, self._today(), self.cfg) if state else {},
            "recent_events": [
                {"id": e.get("id"), "ts": e.get("ts"), "actor": e.get("actor"), "type": e.get("type")}
                for e in events
            ],
        }

    # --- lifecycle --------------------------------------------------------
    def _state(self) -> dict:
        return self.store.load()

    def _prepare(self) -> None:
        if not self.paths.state.exists():
            raise StateError("this project is not initialized; run `relay init` first")
        # Migrate a legacy journal once: the HMAC anchor starts at today's tail.
        self.journal.ensure_anchor()
        if self._first_prepare:
            self.store.mutate(lambda current: current.__setitem__("active_session", {}))
            self._first_prepare = False
        state = self._state()
        if state.get("phase") == "WAIT_HUMAN" and not state.get("pending_decision") and state.get("return_to"):
            # Crash window between the turn's single state save and materialising the decision.
            recovered: dict[str, str] = {}

            def recover(current: dict) -> None:
                if current.get("phase") != "WAIT_HUMAN" or current.get("pending_decision"):
                    return
                target = current.get("return_to")
                if not target:
                    return
                current["phase"] = target
                current["return_to"] = None
                recovered["target"] = str(target)

            self.store.mutate(recover)
            if recovered:
                self.journal.append(
                    "state.transition", state=recovered["target"],
                    detail={"recovered": "missing pending decision"},
                )
        state = self._state()
        if state.get("phase") == "WAIT_HUMAN":
            parsed = local_answer(self.paths)
            if parsed:
                self.answer_decision(parsed[1], parsed[0])
        state = self._state()
        if self.store.baton().get("holder") not in (None, "none") and not state.get("pending"):
            self.store.clear_baton()
            self.journal.append("state.baton_recovered", detail={"note": "cleared stale baton on start"})
        started_at = self.clock.now().isoformat()

        def stamp(current: dict) -> None:
            if not current.get("started_at"):
                current["started_at"] = started_at
            current["run_id"] = self._run_id

        self.store.mutate(stamp)
        state = self._state()
        self.store.heartbeat(phase=state.get("phase"), round=state.get("round", 0))
        try:
            self.journal.append(
                "run.start",
                state=state.get("phase"),
                detail={"pid": __import__("os").getpid(), "run_id": self._run_id},
            )
        except JournalError as exc:
            raise JournalError(f"{exc}; run `relay journal repair`") from exc

    # --- guards -----------------------------------------------------------
    def _check_guards(self) -> None:
        for name, message in (
            ("stop", "stopped by user"),
            ("halt", "monitor halt"),
            ("pause", "paused by user"),
        ):
            flag = self.store.read_flag(name)
            if flag is not None:
                reason = str(flag.get("reason") or message)
                raise PauseRequested(reason)
        if self.monitor_required:
            age = self.store.heartbeat_age(self.paths.monitor_heartbeat)
            stale_after = float(self.cfg.get("monitor.heartbeat_stale_seconds", 300))
            if age is not None and age <= stale_after:
                self._monitor_seen = True
            if self._monitor_seen and (age is None or age > stale_after):
                raise PauseRequested("monitor heartbeat lost (who watches the watcher)")
            if not self._monitor_seen and self.clock.epoch() > self._monitor_grace_until:
                raise PauseRequested("monitor never started")
        state = self._state()
        if state.get("phase") not in PHASE_ROLE:
            # Caps gate new turns only: routing, INIT and finished states must not ask again.
            return
        counters = state.get("counters", {})
        overrides = state.get("limit_overrides") or {}
        for kind, current, key in (
            ("total", int(counters.get("total_rounds", 0)), "max_total_rounds"),
            ("milestone", int(counters.get("rounds_in_milestone", 0)), "max_rounds_per_milestone"),
        ):
            limit = int(overrides.get(key, self.cfg.get(key, 0)))
            if limit > 0 and current >= limit:
                # Never silently exceed a cap, and never leave a pause that needs a config edit.
                self._escalate_round_cap(kind, current, limit, key)
                return
        budget = float(self.cfg.get("daily_budget_usd", 0) or 0)
        spent = self.journal.spend_for_day()
        if budget > 0 and spent >= budget:
            raise PauseRequested(f"daily budget reached (${spent:.2f} of ${budget:.2f})")

    def _escalate_round_cap(self, kind: str, current: int, limit: int, key: str) -> str:
        raised = limit + max(3, int(round(limit * 0.5)))
        self.journal.append(
            "limit.cap_reached", round_no=int(self._state().get("round", 0)),
            detail={"kind": kind, "current": current, "limit": limit},
            result="blocked",
        )
        options = [
            {
                "label": "Raise the cap",
                "consequence": f"The {kind} round cap rises from {limit} to {raised} and work continues.",
            },
            {
                "label": "Re-plan",
                "consequence": "The planner gets a fresh handoff and the current cycle's round count restarts.",
            },
            {
                "label": "Stop for now",
                "consequence": "The loop pauses so you can inspect the work; nothing is lost.",
            },
        ]
        question = (
            f"The {kind} round cap is reached ({current} of {limit}). "
            "Choose what Relay should do. Answer with one of the exact option labels."
        )
        return self.escalate_human(
            question,
            options,
            reason=f"{kind} round cap reached",
            return_to=self._state().get("phase"),
            kind="round_cap",
            data={"cap": key, "kind": kind, "current": current, "limit": limit, "raised": raised},
        )

    def _apply_decision_effects(self) -> None:
        """Apply a human answer to a harness decision (round cap, model limit) exactly once."""
        state = self._state()
        last = state.get("last_decision") or {}
        decision_id = str(last.get("id") or "")
        if not decision_id:
            return
        self._apply_human_opus_request(last)
        if state.get("last_effect_id") == decision_id:
            return
        kind = str(last.get("kind") or "")
        if kind not in ("round_cap", "model_limit", "fixer_retry", "stall_cap"):
            return
        options = last.get("options") or []
        data = last.get("data") or {}
        answer = str(last.get("answer") or "")
        choice = _match_option(answer, options)
        effects: dict[str, Any] = {"decision": decision_id, "kind": kind}
        if kind == "round_cap":
            key = str(data.get("cap") or "")
            limit = int(data.get("limit") or 0)
            raised = int(data.get("raised") or (limit + max(3, int(round(limit * 0.5)))))
            if choice == "Raise the cap":
                effects.update({"action": "raise", "cap": key, "from": limit, "to": raised})
            elif choice == "Re-plan":
                effects.update({"action": "replan", "cap": key, "from": limit, "to": raised})
            else:
                choice = "Stop for now"
                effects.update({"action": "stop", "cap": key})

            def apply_cap(current: dict) -> None:
                overrides = current.setdefault("limit_overrides", {})
                if choice == "Raise the cap":
                    overrides[key] = raised
                elif choice == "Re-plan":
                    counters = current.setdefault("counters", {})
                    counters["stalls_in_milestone"] = int(counters.get("stalls_in_milestone", 0)) + 1
                    if key == "max_rounds_per_milestone":
                        counters["rounds_in_milestone"] = 0
                    else:
                        # A re-plan without headroom would re-pause on the very next step.
                        overrides[key] = raised
                    current["phase"] = "PLAN"
                    current["paused_reason"] = None
                    current["return_to"] = None
                current["last_effect_id"] = decision_id

            self.store.mutate(apply_cap)
            self.journal.append("limit.decision_applied", detail=effects)
            if choice == "Stop for now":
                self._pause("round cap: human chose to stop")
            elif choice == "Re-plan":
                self._notify("Relay is re-planning", f"{kind} round cap: the human chose a re-plan.")
            return

        if kind in ("model_limit", "fixer_retry"):
            escalated_role = str(data.get("role") or ("fixer" if kind == "fixer_retry" else ""))
            allow_label = "Allow another Opus fix" if kind == "fixer_retry" else "Allow this Opus turn"
            if choice == allow_label:
                def grant(current: dict) -> None:
                    current["opus_override"] = {
                        "role": escalated_role, "decision": decision_id,
                        "retry": bool(data.get("retry")) or kind == "fixer_retry",
                    }
                    current["last_effect_id"] = decision_id
                self.store.mutate(grant)
                effects.update({"action": "allow", "role": escalated_role})
            elif choice == "Continue on Sonnet":
                def decline(current: dict) -> None:
                    current["opus_declined"] = {
                        "trigger": data.get("trigger"),
                        "issue_ids": list(data.get("issue_ids") or []),
                        "at_round": int(current.get("round", 0)),
                    }
                    current["last_effect_id"] = decision_id
                self.store.mutate(decline)
                effects.update({"action": "sonnet", "role": escalated_role})
            else:
                choice = "Stop for now"

                def stop(current: dict) -> None:
                    current["last_effect_id"] = decision_id
                self.store.mutate(stop)
                effects.update({"action": "pause", "role": escalated_role})
            self.journal.append("model.decision_applied", detail=effects)
            if choice == "Stop for now":
                self._pause("Opus decision: human chose to stop")
            return

        # kind == "stall_cap"
        restart = choice in ("Re-plan again", "Continue on Sonnet")

        def apply_stall(current: dict) -> None:
            if restart:
                counters = current.setdefault("counters", {})
                counters["stalls_in_milestone"] = 0
                current["phase"] = "PLAN"
                current["paused_reason"] = None
                current["return_to"] = None
            current["last_effect_id"] = decision_id

        self.store.mutate(apply_stall)
        effects.update({"action": "replan" if restart else "stop", "stalls": int(data.get("stalls") or 0)})
        self.journal.append("model.decision_applied", detail=effects)
        if not restart:
            self._pause("stall cap: human chose to stop")

    def _apply_human_opus_request(self, last: dict) -> None:
        """`relay instruct --opus` or a decision answer that says "Use Opus" arms an Opus turn."""
        answer = str(last.get("answer") or "")
        decision_id = str(last.get("id") or "")
        if "use opus" not in answer.lower() or state.get("human_opus_applied") == decision_id:
            return

        def apply(current: dict) -> None:
            current["human_opus_request"] = {"source": decision_id, "at_round": int(current.get("round", 0))}
            current["human_opus_applied"] = decision_id

        self.store.mutate(apply)
        self.journal.append("model.requested", detail={"source": decision_id, "via": "decision answer"})

    def _check_wait_guards(self) -> None:
        for name, message in (("stop", "stopped by user"), ("halt", "monitor halt"), ("pause", "paused by user")):
            if self.store.read_flag(name) is not None:
                raise PauseRequested(message)
        if self.monitor_required:
            age = self.store.heartbeat_age(self.paths.monitor_heartbeat)
            stale_after = float(self.cfg.get("monitor.heartbeat_stale_seconds", 300))
            if age is not None and age <= stale_after:
                self._monitor_seen = True
            if self._monitor_seen and (age is None or age > stale_after):
                raise PauseRequested("monitor heartbeat lost")
            if not self._monitor_seen and self.clock.epoch() > self._monitor_grace_until:
                raise PauseRequested("monitor never started")

    def _apply_correction(self) -> bool:
        data = read_json(self.paths.correction, default=None)
        if not isinstance(data, dict):
            return False
        try:
            self.paths.correction.unlink()
        except FileNotFoundError:
            pass
        action = str(data.get("action") or "")
        self.journal.append(
            "monitor.correct_applied",
            detail={"action": action, "reason": truncate(str(data.get("reason") or ""), 500)},
        )
        state = self._state()
        if action == "replan" and state.get("phase") not in ("DONE", "WAIT_HUMAN"):
            def apply(current: dict) -> None:
                if current.get("phase") in ("DONE", "WAIT_HUMAN"):
                    return
                current["phase"] = "PLAN"
                counters = current.setdefault("counters", {})
                counters["replans"] = int(counters.get("replans", 0)) + 1
                counters["stalls_in_milestone"] = int(counters.get("stalls_in_milestone", 0)) + 1

            self.store.mutate(apply)
            self._notify("Monitor requested a re-plan", truncate(str(data.get("reason") or ""), 200))
            return True
        if action == "rollover":
            self.store.mutate(lambda current: current.setdefault("rollover", {}).__setitem__("forced", True))
            return False
        if action == "halt":
            self.store.set_flag("halt", {"reason": data.get("reason") or "monitor requested halt"})
            return False
        return False

    def _user_edits_pending(self) -> bool:
        if not bool(self.cfg.get("git.enabled", True)) or not bool(self.cfg.get("auto_commit", True)):
            return False
        lines = git_snapshot(self.cfg.project_dir)
        # Relay and the monitor write these without a human; they are not user edits.
        ignored = (".relay/DECISIONS.md", ".relay/PENDING_DECISION.md", ".relay/MONITOR_NOTES.md")
        return [line for line in lines if not any(marker in line for marker in ignored)] != []

    # --- turns ------------------------------------------------------------
    def _today(self) -> str:
        return self.clock.now().astimezone().date().isoformat()

    def _choose_turn(self, phase: str, state: dict):
        """Pick the turn's role/model with the deterministic policy; ask the human if blocked."""
        has_plan = (self.cfg.project_dir / ".relay" / "PLAN.md").exists()
        choice = choose_turn(state, phase, self.cfg, has_plan=has_plan, today=self._today())
        if choice.needs_human:
            needs = choice.needs_human
            self.journal.append(
                "model.limit_reached", round_no=state.get("round"),
                detail={"trigger": choice.trigger, "kind": needs.get("kind"), "data": needs.get("data")},
                result="blocked",
            )
            self.escalate_human(
                str(needs["question"]), list(needs["options"]),
                reason=choice.reason or choice.trigger,
                return_to=state.get("phase"),
                kind=needs.get("kind"), data=needs.get("data"),
            )
            return None
        if choice.trigger.startswith("declined") or choice.trigger == "declined":
            self.journal.append(
                "model.declined", round_no=state.get("round"),
                detail={"role": choice.role, "issue_ids": list(choice.issue_ids)},
            )
        return choice

    def _select_model(self, role: str, model: str | None, effort: str | None) -> dict:
        """Select and verify model/effort before anything is typed. Raises HumanRequired.

        Returns the two journal values: `model_verified` and `effort_verified`. An app without an
        effort control reports "unverifiable" for the effort instead of pretending (B3).
        """
        if not is_claude_role(role):
            return {"model_verified": "unverifiable", "effort_verified": "unverifiable"}
        driver = self.drivers[role_app(role)]
        result = driver.select_model(str(model), str(effort), role) or {}
        return {
            "model_verified": result.get("model_verified", True),
            "effort_verified": result.get("effort_verified", "unverifiable"),
        }

    def _count_escalation(self, current: dict, choice) -> dict:
        """Increment the Opus counters inside the caller's mutate (N9: atomic with pending)."""
        today = self._today()
        milestone = int(current.get("milestone", 1))
        opus = current.setdefault("opus", {})
        if opus.get("date") != today:
            opus["date"] = today
            opus["day"] = 0
        if int(opus.get("milestone", 0)) != milestone:
            opus["milestone"] = milestone
            opus["milestone_turns"] = 0
        opus["day"] = int(opus.get("day", 0)) + 1
        if not choice.exempt_milestone:
            opus["milestone_turns"] = int(opus.get("milestone_turns", 0)) + 1
        current["opus_override"] = None
        current["human_opus_request"] = None
        current["opus_declined"] = None
        return {
            "day": int(opus["day"]),
            "milestone_turns": int(opus.get("milestone_turns", 0)),
            "milestone": milestone,
        }

    def _journal_escalation(self, choice, role: str, round_no: int, counted: dict) -> None:
        p = policy(self.cfg)
        self.journal.append(
            "model.escalated", round_no=round_no,
            detail={
                "trigger": choice.trigger,
                "role": role,
                "model": choice.model,
                "effort": choice.effort,
                "issue_ids": list(choice.issue_ids),
                "streak": choice.streak,
                "exempt_milestone": bool(choice.exempt_milestone),
                "limit_bypassed": choice.limit_bypassed,
                "opus_turns_today": {
                    "value": counted.get("day", 0), "limit": int(p["max_opus_turns_per_day"]),
                },
                "opus_turns_milestone": {
                    "value": counted.get("milestone_turns", 0),
                    "limit": int(p["max_opus_turns_per_milestone"]),
                },
            },
        )

    def _turn(self, phase: str) -> None:
        state = self._state()
        pending = state.get("pending") if isinstance(state.get("pending"), dict) else None
        resuming = bool(pending and pending.get("phase") == phase)

        if resuming:
            role = str(pending["role"])
            round_no = int(pending["round"])
            pointer = str(pending["pointer"])
            expected = str(pending["expected_file"])
            started_wall = float(pending["started_wall"])
            started_epoch = self.clock.epoch()
            default_model, default_effort = self._model(role)
            model = str(pending.get("model") or default_model)
            effort = str(pending.get("effort") or default_effort)
            model_verified = pending.get("model_verified", "unverifiable")
            effort_verified = pending.get("effort_verified", "unverifiable")
            escalated = bool(pending.get("escalated"))
            self.journal.append(
                "turn.resume", state=phase, round_no=round_no,
                detail={"role": role, "expected": expected, "resumed": True},
            )
        else:
            choice = self._choose_turn(phase, state)
            if choice is None:
                return
            role = str(choice.role)
            model, effort = str(choice.model), str(choice.effort)
            escalated = bool(choice.escalated)
            blocked = state.get("blocked")
            if isinstance(blocked, dict):
                still_bad = []
                for path in blocked.get("paths") or []:
                    target = self.cfg.project_dir / path
                    if target.exists():
                        digest = sha256_file(target)
                        if digest == (blocked.get("hashes") or {}).get(path):
                            still_bad.append(path)
                if still_bad:
                    raise HumanRequired(
                        "A previous turn was blocked for unsafe writes: "
                        + ", ".join(still_bad[:5])
                        + ". Revert or remove those changes, then resume.",
                        reason="blocked turn is unresolved",
                    )
                self.store.mutate(lambda current: current.__setitem__("blocked", None))
                self.journal.append("blocked.cleared", detail={"paths": blocked.get("paths")})
                state = self._state()
            round_no = int(state.get("round", 0)) + 1
            if self._user_edits_pending():
                raise HumanRequired(
                    "Uncommitted changes appeared in the project while nobody held the baton. "
                    "Relay will not overwrite your work. Commit or stash them, then resume.",
                    options=[
                        {"label": "I committed/stashed my changes", "consequence": "Relay re-checks and continues."},
                        {"label": "Stop for now", "consequence": "Nothing is changed."},
                    ],
                    reason="uncommitted user edits",
                )
            # Opus turns always use a fresh chat, so their context stays small (B3).
            self._ensure_session(role, round_no, force_new=escalated and is_claude_role(role))
            self._maybe_rollover(role, round_no, boundary=(phase == "PLAN"))
            state = self._state()
            expected = expected_file(role)
            pointer = self._build_pointer(phase, role, round_no, state=state, choice=choice)
            started_wall = self.wall()
            started_epoch = self.clock.epoch()
            # Anything that can make git run an agent's command is checked across the turn.
            git_before = git_control_snapshot(self.cfg.project_dir)
            # Choose the model before typing anything: an unverified label raises HumanRequired.
            verified = self._select_model(role, model, effort)
            model_verified = verified["model_verified"]
            effort_verified = verified["effort_verified"]
            issue_ids = list(choice.issue_ids)
            plan_issues = list(getattr(choice, "fix_plan_issues", ()) or ())
            counted: dict = {}

            def apply(current: dict) -> None:
                # N9: the Opus counter and `pending` are in one save, so a crash cannot
                # count the turn without an intent record (or the other way round).
                if escalated:
                    counted.update(self._count_escalation(current, choice))
                if plan_issues:
                    current["fix_plan_issues"] = plan_issues
                    current["fix_plan_round"] = round_no
                current["round"] = round_no
                current["role"] = role
                current["pending"] = {
                    "phase": phase,
                    "role": role,
                    "round": round_no,
                    "pointer": pointer,
                    "expected_file": expected,
                    "started_wall": started_wall,
                    "model": model,
                    "effort": effort,
                    "model_verified": model_verified,
                    "effort_verified": effort_verified,
                    "escalated": escalated,
                    "issue_ids": issue_ids,
                    "trigger": choice.trigger,
                }

            self.store.mutate(apply)
            if escalated:
                self._journal_escalation(choice, role, round_no, counted)
            if plan_issues:
                self.journal.append(
                    "fix.plan_requested", round_no=round_no,
                    detail={
                        "issue_ids": plan_issues,
                        "streak": int(choice.fix_plan_streak),
                    },
                )

        driver = self.drivers[role_app(role)]
        sentinel_path = self.paths.sentinel(role)
        if resuming and sentinel_path.exists():
            try:
                sentinel = read_sentinel(sentinel_path)
                issues = validate_sentinel(
                    sentinel, root=self.cfg.project_dir, expected_round=round_no,
                    expected_role=role, expected_file=expected,
                    round_started_wall=started_wall,
                )
            except IntegrityError as exc:
                sentinel = None
                issues = [str(exc)]
            if sentinel is not None and not issues:
                self.journal.append(
                    "turn.resume_accepted", state=phase, round_no=round_no,
                    detail={"role": role, "note": "sentinel arrived before the crash"},
                )
                self._process_turn(
                    phase=phase, role=role, round_no=round_no, sentinel=sentinel,
                    before=snapshot(self.cfg.project_dir), driver=driver,
                )
                return
            sentinel_path.unlink(missing_ok=True)

        if resuming:
            sentinel_path.unlink(missing_ok=True)
            self._ensure_session(role, round_no)

        sentinel_path.unlink(missing_ok=True)
        self.store.heartbeat(phase=phase, round=round_no)
        self.store.set_baton(role, role, round_no)
        before = snapshot(self.cfg.project_dir)
        turn_event = self.journal.append(
            "turn.start",
            state=phase,
            round_no=round_no,
            detail={
                "role": role, "app": role_app(role), "expected": expected, "pointer": pointer,
                "model": model, "effort": effort, "model_verified": model_verified,
                "effort_verified": effort_verified,
                "trigger": (pending.get("trigger") if resuming else choice.trigger),
                "snapshot_sha256": sha256_text(canonical_json(before)),
                "watched_files": len(before),
                "git_status": git_snapshot(self.cfg.project_dir)[:20]
                if bool(self.cfg.get("git.enabled", True)) else [],
            },
        )
        self._ring(driver, role, pointer, round_no, do_model=False)
        spec = WaitSpec(
            role=role,
            round_no=round_no,
            expected_file=expected,
            pointer=pointer,
            timeout_minutes=self._timeout_minutes(role),
            started_epoch=started_epoch,
            started_wall=started_wall,
        )
        sentinel = self._wait(spec)
        self._process_turn(
            phase=phase, role=role, round_no=round_no, sentinel=sentinel, before=before, driver=driver,
            turn_start_id=turn_event["id"],
            git_before=git_before if not resuming else None,
        )

    def _ensure_session(self, role: str, round_no: int, *, force_new: bool = False) -> None:
        """Planner and reviewer are separate chats in the same app; open + seed on switch.

        `force_new` opens a fresh chat even when the role is unchanged. Opus turns use it so
        their context stays small (Part B3).
        """
        app = role_app(role)
        state = self._state()
        active = state.get("active_session") or {}
        outgoing = active.get(app)
        if outgoing == role and not force_new:
            return
        if (
            app == "claude"
            and outgoing in CLAUDE_ROLES
            and outgoing not in ("strategist", "fixer")
            and (outgoing != role or force_new)
        ):
            if int(state.get("session_rounds", {}).get(str(outgoing), 0)) > 0:
                self._checkpoint_before_switch(str(outgoing), round_no)
        driver = self.drivers[app]
        self._recover(driver, role, driver.new_session)
        self._ring(driver, role, self._seed_pointer(role), round_no)
        phase = state.get("phase")

        def apply(current: dict) -> None:
            # Reload under the lock: a concurrent `relay decide`/`instruct` must survive.
            current.setdefault("active_session", {})[app] = role
            current.setdefault("session_rounds", {})[role] = 0
            current.setdefault("session_ctx", {})[role] = 0
            rollover = current.setdefault("rollover", {})
            forced = list(rollover.get("forced_roles") or [])
            if role in forced:
                rollover["forced_roles"] = [item for item in forced if item != role]

        self.store.mutate(apply)
        self.journal.append(
            "session.open", state=phase, round_no=round_no,
            detail={"app": app, "role": role, "seeded": True},
        )

    def _checkpoint_before_switch(self, outgoing: str, round_no: int) -> None:
        """Write the outgoing role's checkpoint before its chat is replaced."""
        driver = self.drivers[role_app(outgoing)]
        checkpoint_rel = f".relay/CHECKPOINT_{outgoing}.md"
        pointer = self._checkpoint_pointer(outgoing, round_no)
        sentinel_path = self.paths.sentinel(outgoing)
        sentinel_path.unlink(missing_ok=True)
        started_wall = self.wall()
        self.store.set_baton(outgoing, outgoing, round_no)
        before = snapshot(self.cfg.project_dir)
        start = self.journal.append(
            "session.checkpoint.start", round_no=round_no,
            detail={"role": outgoing, "reason": "role switch", "checkpoint": checkpoint_rel},
        )
        self._ring(driver, outgoing, pointer, round_no)
        spec = WaitSpec(
            role=outgoing, round_no=round_no, expected_file=checkpoint_rel, pointer=pointer,
            timeout_minutes=self._timeout_minutes(outgoing), started_epoch=self.clock.epoch(),
            started_wall=started_wall,
        )
        sentinel = self._wait(spec)
        if sentinel.get("status") != "done":
            raise HumanRequired(
                f"{outgoing} reported {sentinel.get('status')} while writing its checkpoint.",
                reason="checkpoint not completed",
            )
        after = snapshot(self.cfg.project_dir)
        violations = ownership_violations(
            before, after, outgoing, active_role=outgoing,
            events=self.journal.trusted_events(), turn_start_id=start["id"],
        )
        if violations:
            raise PauseRequested(f"checkpoint wrote files it does not own: {violations[0]['path']}")
        commit = None
        if bool(self.cfg.get("auto_commit", True)) and bool(self.cfg.get("git.enabled", True)):
            commit = git_commit(self.cfg.project_dir, f"relay: round {round_no} checkpoint {outgoing}")
            if commit:
                self.journal.append(
                    "git.commit", round_no=round_no,
                    detail={"hash": commit, "message": "checkpoint before role switch"},
                )
        self.store.clear_baton()
        state = self._state()
        state["session_rounds"][outgoing] = 0
        state["session_ctx"][outgoing] = 0
        self.store.save(state)
        checkpoint = self.cfg.project_dir / checkpoint_rel
        self.journal.append(
            "session.checkpoint", round_no=round_no,
            detail={
                "role": outgoing, "reason": "role switch",
                "sha256": sha256_file(checkpoint) if checkpoint.exists() else None,
            },
        )

    def _process_turn(
        self,
        *,
        phase: str,
        role: str,
        round_no: int,
        sentinel: dict,
        before: dict[str, str],
        driver: AppDriver,
        turn_start_id: str | None = None,
        git_before: dict[str, str] | None = None,
    ) -> None:
        after = snapshot(self.cfg.project_dir)
        changed = changed_paths(before, after)

        if git_before is not None:
            git_changed = git_control_changes(git_before, git_control_snapshot(self.cfg.project_dir))
            if git_changed:
                # A hook, fsmonitor or config change can make git run arbitrary code.
                self.journal.append(
                    "integrity.violation", state=phase, round_no=round_no,
                    detail={"kind": "git_control", "paths": git_changed}, result="blocked",
                )
                self._abort_turn(
                    "git control files changed during the turn: " + ", ".join(git_changed[:5]),
                    phase, paths=git_changed,
                )
                raise PauseRequested("git control file changed during the turn")
        pending = self._state().get("pending") if isinstance(self._state().get("pending"), dict) else {}
        model = pending.get("model")
        effort = pending.get("effort")
        model_verified = pending.get("model_verified", "unverifiable")
        effort_verified = pending.get("effort_verified", "unverifiable")
        issue_ids = _issue_ids(sentinel)

        if role == "fixer":
            limit = int(policy(self.cfg)["fixer_max_files"])
            source_changed = [path for path in changed if not path.startswith(".relay/")]
            if len(source_changed) > limit:
                text = f"fixer changed {len(source_changed)} source files (limit {limit})"
                self.journal.append(
                    "integrity.violation", state=phase, round_no=round_no,
                    detail={"kind": "fixer_scope", "changed": source_changed, "limit": limit},
                    result="blocked",
                )
                self._abort_turn(
                    text, phase, paths=source_changed,
                    hashes={path: after.get(path) for path in source_changed},
                )
                raise PauseRequested(text)

        violations = ownership_violations(
            before, after, role, active_role=role,
            events=self.journal.trusted_events(), turn_start_id=turn_start_id,
        )
        if violations:
            text = ", ".join(f"{v['path']} (owned by {v['owner']})" for v in violations[:5])
            self.journal.append(
                "integrity.violation", state=phase, round_no=round_no,
                detail={"kind": "ownership", "violations": violations}, result="blocked",
            )
            self._abort_turn(
                f"{role} wrote files it does not own: {text}", phase,
                paths=[item["path"] for item in violations],
                hashes={item["path"]: after.get(item["path"]) for item in violations},
            )
            raise PauseRequested(f"ownership violation: {text}")

        hits = secret_scan(self.cfg.project_dir, changed)
        if hits:
            text = ", ".join(f"{h['path']}:{h['line']} ({h['rule']})" for h in hits[:5])
            self.journal.append(
                "integrity.violation", state=phase, round_no=round_no,
                detail={"kind": "secret", "hits": hits}, result="blocked",
            )
            self._abort_turn(
                f"secret scan blocked the commit: {text}", phase,
                paths=changed, hashes={path: after.get(path) for path in changed},
            )
            raise PauseRequested("secret detected in changed files")

        commit = None
        if bool(self.cfg.get("auto_commit", True)) and bool(self.cfg.get("git.enabled", True)):
            verdict = sentinel.get("verdict")
            message = f"relay: round {round_no} {role} {verdict or sentinel.get('status') or 'done'}"
            try:
                commit = git_commit(self.cfg.project_dir, message)
            except SecretDetected as exc:
                self.journal.append(
                    "integrity.violation", state=phase, round_no=round_no,
                    detail={"kind": "secret_staged", "error": str(exc)}, result="blocked",
                )
                self._abort_turn(str(exc), phase)
                raise PauseRequested("secret detected in staged files") from exc
            if commit:
                self.journal.append(
                    "git.commit", state=phase, round_no=round_no,
                    detail={"hash": commit, "message": message},
                )

        self.journal.append(
            "integrity.ok", state=phase, round_no=round_no,
            detail={"changed": changed, "snapshot_sha256": sha256_text(canonical_json(before))},
        )
        ctx_pct = sentinel.get("context_estimate_pct")
        verdict = sentinel.get("verdict")
        summary = str(sentinel.get("summary") or "")
        source_changed = any(not path.startswith(".relay/") for path in changed)

        archive_plan: dict = {}
        is_decision = sentinel.get("status") in ("needs_decision", "blocked")
        double_blocked = False
        if role == "coder" and sentinel.get("status") == "blocked" and issue_ids:
            previous = self._state().get("blocked_streaks") or {}
            double_blocked = any(int(previous.get(key, 0)) + 1 >= 2 for key in issue_ids)
        if double_blocked:
            # The coder is stuck on the same issue twice: the Opus fixer takes the next turn.
            is_decision = False
        target = ("CODE" if double_blocked else
                  ("WAIT_HUMAN" if is_decision else NEXT_PHASE[phase]))

        def apply(current: dict) -> None:
            # Reload under the lock so a concurrent `relay decide`/`instruct` survives.
            current["pending"] = None
            current["phase"] = target
            if is_decision:
                current["return_to"] = phase
            current["last_sentinel"] = {
                "round": round_no,
                "role": role,
                "status": sentinel.get("status"),
                "verdict": verdict,
                "file": sentinel.get("file"),
                "sha256": sentinel.get("sha256"),
                "summary": summary,
                "context_estimate_pct": ctx_pct,
                "question": sentinel.get("question"),
            }
            session_rounds = current.setdefault("session_rounds", {})
            session_rounds[role] = int(session_rounds.get(role, 0)) + 1
            session_ctx = current.setdefault("session_ctx", {})
            if isinstance(ctx_pct, (int, float)):
                session_ctx[role] = max(float(session_ctx.get(role, 0) or 0), float(ctx_pct))
            counters = current.setdefault("counters", {})
            counters["total_rounds"] = max(int(counters.get("total_rounds", 0)), round_no)
            counters["rounds_in_milestone"] = int(counters.get("rounds_in_milestone", 0)) + 1
            current.setdefault("history", []).append({
                "round": round_no,
                "role": role,
                "status": sentinel.get("status"),
                "verdict": verdict,
                "summary": summary,
                "source_changed": source_changed,
                "changed": changed[:50],
                "ctx_pct": ctx_pct,
            })
            current["history"] = current["history"][-20:]
            current["last_turn"] = {
                "round": round_no,
                "role": role,
                "status": sentinel.get("status"),
                "verdict": verdict,
                "changed": changed,
                "source_changed": source_changed,
                "model": model,
                "effort": effort,
                "model_verified": model_verified,
                "issue_ids": issue_ids,
            }
            if role == "reviewer" and sentinel.get("status") == "done":
                streaks = current.setdefault("issue_streaks", {})
                for key in [item for item in streaks if item not in issue_ids]:
                    del streaks[key]
                for key in issue_ids:
                    streaks[key] = int(streaks.get(key, 0)) + 1
                current["last_review_issues"] = list(issue_ids)
                planned = set(current.get("fix_plan_issues") or [])
                live = set(current.get("issue_streaks") or {})
                if (self.cfg.project_dir / ".relay/FIX_PLAN.md").exists() and (
                    not planned or not (planned & live)
                ):
                    archive_plan["reason"] = "issues resolved"
            if role == "coder":
                blocked = current.setdefault("blocked_streaks", {})
                if sentinel.get("status") == "blocked" and issue_ids:
                    for key in issue_ids:
                        blocked[key] = int(blocked.get(key, 0)) + 1
                elif sentinel.get("status") != "blocked":
                    current["blocked_streaks"] = {}
            if role == "fixer":
                fixed = current.setdefault("opus_fixed", {})
                for key in (pending.get("issue_ids") or []):
                    fixed[key] = {
                        "streak": int((current.get("issue_streaks") or {}).get(key, 0)),
                        "round": round_no,
                    }
                current["blocked_streaks"] = {}
                current["opus_declined"] = None

        self.store.mutate(apply)
        self.store.clear_baton()
        self.journal.append(
            "turn.end",
            state=phase,
            round_no=round_no,
            detail={
                "role": role,
                "status": sentinel.get("status"),
                "verdict": verdict,
                "summary": summary,
                "changed": changed,
                "source_changed": source_changed,
                "commit": commit,
                "ctx_pct": ctx_pct,
                "model": model,
                "effort": effort,
                "model_verified": model_verified,
                "effort_verified": effort_verified,
                "issue_ids": issue_ids,
                "trigger": (pending.get("trigger") if pending else None),
            },
            outputs=[
                {"path": str(sentinel.get("file")), "sha256": sentinel.get("sha256")},
                {"path": f".relay/{self.paths.sentinel(role).name}",
                 "sha256": sha256_file(self.paths.sentinel(role))},
            ],
        )
        if archive_plan.get("reason"):
            self._archive_fix_plan(round_no, str(archive_plan["reason"]))

        if is_decision:
            question = str(sentinel.get("question") or summary or f"{role} is blocked")
            options = self._clean_options(sentinel.get("options"))
            self.escalate_human(
                question, options, reason=f"{role} reported {sentinel.get('status')}",
                return_to=phase, suggestion=sentinel.get("suggestion"),
            )
            return

        self.journal.append("state.transition", state=target, round_no=round_no, detail={"from": phase, "to": target})

    # --- rollover ---------------------------------------------------------
    def _maybe_rollover(self, role: str, round_no: int, *, boundary: bool) -> None:
        if role in ("strategist", "fixer"):
            # Opus turns always get a fresh chat and write their outcome to STRATEGY/FIX_REPORT;
            # checkpointing them here would run an extra, uncounted Opus turn (N5).
            return
        state = self._state()
        if role == "coder":
            max_rounds = int(self.cfg.get("zcode_app.max_rounds_per_session", 3))
        else:
            max_rounds = int(self.cfg.get("claude_app.max_rounds_per_session", 4))
        ratio = int(state["session_rounds"].get(role, 0)) / max(1, max_rounds)
        signals = [float(state["session_ctx"].get(role, 0) or 0)]
        driver = self.drivers[role_app(role)]
        try:
            live = driver.read_context_pct()
        except (BrainError, VisionUnavailable):
            live = None
        if live is not None:
            signals.append(float(live))
        threshold = float(self.cfg.get("rollover_threshold", 0.7)) * 100
        max_ctx = max(signals) if signals else 0.0
        forced_roles = list(state.get("rollover", {}).get("forced_roles") or [])
        forced = bool(state.get("rollover", {}).get("forced")) or role in forced_roles
        boundary_hit = boundary and max_ctx >= float(self.cfg.get("rollover_at_milestone_pct", 0.5)) * 100 and max_ctx > 0
        should = ratio >= float(self.cfg.get("rollover_threshold", 0.7)) or max_ctx >= threshold or forced or boundary_hit
        if not should:
            return
        if forced:
            reason = "forced by monitor"
        elif boundary_hit:
            reason = f"milestone boundary at {max_ctx:.0f}% context"
        elif max_ctx >= threshold:
            reason = f"context at {max_ctx:.0f}%"
        else:
            reason = f"{state['session_rounds'].get(role, 0)} rounds in this session"
        def clear_forced(current: dict) -> None:
            rollover = current.setdefault("rollover", {})
            rollover["forced"] = False
            remaining = [item for item in (rollover.get("forced_roles") or []) if item != role]
            rollover["forced_roles"] = remaining

        self.store.mutate(clear_forced)
        self._rollover(role, round_no, reason)

    def _rollover(self, role: str, round_no: int, reason: str) -> None:
        driver = self.drivers[role_app(role)]
        checkpoint_rel = f".relay/CHECKPOINT_{role}.md"
        pointer = self._checkpoint_pointer(role, round_no)
        sentinel_path = self.paths.sentinel(role)
        sentinel_path.unlink(missing_ok=True)
        started_wall = self.wall()
        self.store.set_baton(role, role, round_no)
        before = snapshot(self.cfg.project_dir)
        rollover_start = self.journal.append(
            "rollover.start", round_no=round_no,
            detail={"role": role, "reason": reason, "checkpoint": checkpoint_rel},
        )
        self._ring(driver, role, pointer, round_no)
        spec = WaitSpec(
            role=role, round_no=round_no, expected_file=checkpoint_rel, pointer=pointer,
            timeout_minutes=self._timeout_minutes(role), started_epoch=self.clock.epoch(),
            started_wall=started_wall,
        )
        checkpoint_sentinel = self._wait(spec)
        if checkpoint_sentinel.get("status") != "done":
            raise HumanRequired(
                f"{role} reported {checkpoint_sentinel.get('status')} while writing its checkpoint.",
                options=DEFAULT_OPTIONS,
                reason="checkpoint not completed",
            )
        after = snapshot(self.cfg.project_dir)
        violations = ownership_violations(
            before, after, role, active_role=role,
            events=self.journal.trusted_events(), turn_start_id=rollover_start["id"],
        )
        if violations:
            self._abort_turn(f"checkpoint wrote files it does not own: {violations[0]['path']}", self._state().get("phase"))
            raise PauseRequested("ownership violation during rollover")
        if bool(self.cfg.get("auto_commit", True)) and bool(self.cfg.get("git.enabled", True)):
            commit = git_commit(self.cfg.project_dir, f"relay: round {round_no} checkpoint {role}")
            if commit:
                self.journal.append(
                    "git.commit", round_no=round_no, detail={"hash": commit, "message": "checkpoint"}
                )
        self._recover(driver, role, driver.new_session)
        seed = self._seed_pointer(role)
        self._ring(driver, role, seed, round_no)

        def apply(current: dict) -> None:
            # Reload under the lock so a concurrent decision/instruct is not lost.
            current.setdefault("session_rounds", {})[role] = 0
            current.setdefault("session_ctx", {})[role] = 0
            current.setdefault("active_session", {})[role_app(role)] = role

        self.store.mutate(apply)
        self.store.clear_baton()
        checkpoint = self.cfg.project_dir / checkpoint_rel
        if checkpoint.exists():
            try:
                lines = len(checkpoint.read_text(encoding="utf-8", errors="replace").splitlines())
                if lines > 150:
                    self.journal.append(
                        "checkpoint.oversize", round_no=round_no,
                        detail={"role": role, "lines": lines, "limit": 150}, result="warn",
                    )
            except OSError:
                pass
        self.journal.append(
            "rollover", round_no=round_no,
            detail={
                "role": role, "reason": reason, "new_session": True,
                "checkpoint_sha256": sha256_file(checkpoint) if checkpoint.exists() else None,
            },
            outputs=[{"path": checkpoint_rel, "sha256": sha256_file(checkpoint)}] if checkpoint.exists() else [],
        )

    # --- pointers ---------------------------------------------------------
    def _read(self, rel: str, limit: int = 8000) -> str:
        path = self.cfg.project_dir / rel
        if not path.exists():
            return ""
        try:
            return truncate(path.read_text(encoding="utf-8", errors="replace"), limit)
        except OSError:
            return ""

    def _hint(self, rel: str) -> str:
        path = self.cfg.project_dir / rel
        if not path.exists():
            return "missing"
        return short_hash(sha256_file(path))

    def _fix_plan_is_current(self, state: dict) -> bool:
        """A FIX_PLAN.md is followed only while its issues are still blocking (N4)."""
        if not (self.cfg.project_dir / ".relay/FIX_PLAN.md").exists():
            return False
        planned = set(state.get("fix_plan_issues") or [])
        live = set(state.get("issue_streaks") or {})
        return not planned or bool(planned & live)

    def _archive_fix_plan(self, round_no: int, reason: str) -> None:
        source = self.cfg.project_dir / ".relay/FIX_PLAN.md"
        if not source.exists():
            return
        self.paths.archive_dir.mkdir(parents=True, exist_ok=True)
        target = self.paths.archive_dir / f"FIX_PLAN-{round_no}.md"
        suffix = 2
        while target.exists():
            target = self.paths.archive_dir / f"FIX_PLAN-{round_no}-{suffix}.md"
            suffix += 1
        digest = sha256_file(source)
        source.replace(target)
        rel = target.relative_to(self.cfg.project_dir).as_posix()
        self.journal.append(
            "fix.plan_archived", round_no=round_no,
            detail={"reason": reason, "archived": rel, "sha256": digest},
            outputs=[{"path": rel, "sha256": digest}],
        )
        self.store.mutate(lambda current: current.update({"fix_plan_issues": []}))
        if bool(self.cfg.get("auto_commit", True)) and bool(self.cfg.get("git.enabled", True)):
            commit = git_commit(self.cfg.project_dir, f"relay: archive FIX_PLAN after round {round_no}")
            if commit:
                self.journal.append(
                    "git.commit", round_no=round_no,
                    detail={"hash": commit, "message": "archive FIX_PLAN"},
                )

    def _build_pointer(
        self, phase: str, role: str, round_no: int, *,
        state: dict | None = None, choice=None,
    ) -> str:
        state = state or self._state()
        notes = ".relay/MONITOR_NOTES.md"
        decisions = ".relay/DECISIONS.md"
        fix_plan = self._fix_plan_is_current(state)
        fix_plan_issues = ", ".join(getattr(choice, "fix_plan_issues", ()) or ())
        last_role = str((state.get("last_turn") or {}).get("role") or "")
        if phase == "PLAN":
            base = ".relay/HANDOFF.md" if (self.cfg.project_dir / ".relay/HANDOFF.md").exists() else ".relay/GOAL.md"
            if role == "strategist":
                text = (
                    f"RELAY round {round_no} role=strategist: re-think the approach. Read "
                    f".relay/GOAL.md, .relay/PLAN.md, .relay/REVIEW.md, .relay/CODER_REPORT.md, "
                    f"{decisions}, {notes} and .relay/RULES_CLAUDE.md (strategist section). "
                    "Write .relay/STRATEGY.md, update .relay/PLAN.md and .relay/HANDOFF.md, then "
                    ".relay/CLAUDE_DONE.json last."
                )
            else:
                text = (
                    f"RELAY round {round_no} role=planner: read {base} (sha256 {self._hint(base)}), "
                    f".relay/PLAN.md, .relay/REVIEW.md, .relay/CODER_REPORT.md, {decisions}, {notes} "
                    "and .relay/RULES_CLAUDE.md (planner section). "
                    "Write .relay/PLAN.md and .relay/HANDOFF.md, then .relay/CLAUDE_DONE.json last."
                )
        elif phase == "CODE" and role == "fixer":
            issues = ", ".join(getattr(choice, "issue_ids", ()) or ()) or "the named issues"
            plan = f".relay/FIX_PLAN.md, " if fix_plan else ""
            text = (
                f"RELAY round {round_no} role=fixer: fix only {issues}. Read .relay/REVIEW.md "
                f"(sha256 {self._hint('.relay/REVIEW.md')}), {plan}.relay/HANDOFF.md, {decisions}, {notes} "
                "and .relay/RULES_CLAUDE.md (fixer section). Change at most "
                f"{int(policy(self.cfg)['fixer_max_files'])} source files, run the tests, write "
                ".relay/FIX_REPORT.md, then .relay/CLAUDE_DONE.json last."
            )
        elif phase == "CODE":
            plan = f".relay/FIX_PLAN.md, " if fix_plan else ""
            # N3: after CHANGES_REQUESTED the review IS the handoff for this turn.
            review_first = (state.get("last_turn") or {}).get("verdict") == "CHANGES_REQUESTED"
            review = (
                f".relay/REVIEW.md (sha256 {self._hint('.relay/REVIEW.md')}), " if review_first else ""
            )
            text = (
                f"RELAY round {round_no} role=coder: read {review}.relay/HANDOFF.md "
                f"(sha256 {self._hint('.relay/HANDOFF.md')}), {plan}.relay/PLAN.md, {decisions}, {notes} "
                "and .relay/RULES_CODER.md. Address every requested change. Write "
                ".relay/CODER_REPORT.md, then .relay/CODER_DONE.json last."
            )
        else:
            report = ".relay/FIX_REPORT.md" if last_role == "fixer" else ".relay/CODER_REPORT.md"
            plan = (
                f" If an issue repeats, also write .relay/FIX_PLAN.md for: {fix_plan_issues}."
                if fix_plan_issues else ""
            )
            previous = [str(item) for item in (state.get("last_review_issues") or [])][:5]
            previous_text = (
                " Previous ids: " + ", ".join(previous) + " (reuse if same)."
                if previous else ""
            )
            text = (
                f"RELAY round {round_no} role=reviewer: read .relay/HANDOFF.md "
                f"(sha256 {self._hint('.relay/HANDOFF.md')}), {report} (sha256 {self._hint(report)}), "
                f".relay/PLAN.md, {decisions}, {notes}, .relay/RULES_CLAUDE.md (reviewer)."
                + previous_text + plan +
                " Write .relay/REVIEW.md, then .relay/CLAUDE_DONE.json last."
            )
        return ascii_pointer(text)

    def _checkpoint_pointer(self, role: str, round_no: int) -> str:
        sentinel = "CODER_DONE.json" if role == "coder" else "CLAUDE_DONE.json"
        text = (
            f"RELAY checkpoint round {round_no} role={role}: write .relay/CHECKPOINT_{role}.md "
            f"(max 150 lines: decisions, status, open issues not already in files), then .relay/{sentinel} "
            f"last with role={role} and file .relay/CHECKPOINT_{role}.md. Do not start new work."
        )
        return ascii_pointer(text)

    def _seed_pointer(self, role: str) -> str:
        rules = ".relay/RULES_CODER.md" if role == "coder" else ".relay/RULES_CLAUDE.md"
        section = "coder" if role == "coder" else role
        extras = ""
        if role == "reviewer":
            extras = " .relay/FIX_PLAN.md,"
        elif role == "fixer":
            extras = " .relay/FIX_PLAN.md, .relay/FIX_REPORT.md,"
        text = (
            f"RELAY seed role={role}: read {rules} ({section} section), .relay/PLAN.md, .relay/HANDOFF.md,"
            f"{extras} .relay/DECISIONS.md, .relay/CHECKPOINT_{role}.md and .relay/MONITOR_NOTES.md. "
            "Then wait for the next relay pointer."
        )
        return ascii_pointer(text)

    def _nudge_pointer(self, spec: WaitSpec) -> str:
        sentinel = "CODER_DONE.json" if spec.role == "coder" else "CLAUDE_DONE.json"
        return ascii_pointer(
            f"RELAY nudge round {spec.round_no} role={spec.role}: you stopped before writing "
            f".relay/{sentinel}. Write it now, last, announcing {spec.expected_file} with its sha256."
        )

    def _model(self, role: str) -> tuple[str, str]:
        """The model a role uses outside `choose_turn` (seeds, checkpoints, nudges)."""
        p = policy(self.cfg)
        if role == "coder":
            return str(self.cfg.get("zcode_app.model", "")), str(self.cfg.get("zcode_app.effort", "high"))
        slot = p["escalated"] if role in ("strategist", "fixer") else p["default"]
        return str(slot["model"]), str(slot["effort"])

    def _timeout_minutes(self, role: str) -> int:
        if role == "coder":
            return int(self.cfg.get("zcode_app.timeout_minutes", 60))
        return int(self.cfg.get("claude_app.timeout_minutes", 30))

    # --- GUI --------------------------------------------------------------
    def _recover(self, driver: AppDriver, role: str, action):
        try:
            return action()
        except AppMismatch:
            self.journal.append(
                "recovery.start", detail={"app": driver.name, "role": role, "max_actions": int(self.cfg.get("gui.max_recovery_actions", 6))}
            )
            Recovery(driver, self.journal, self.cfg).ensure_ready()
            self.journal.append("recovery.retry", detail={"app": driver.name, "role": role})
            return action()

    def _ring(self, driver: AppDriver, role: str, pointer: str, round_no: int, *, do_model: bool = True) -> bool | None:
        driver.wait_for_idle(int(self.cfg.get("user_idle_seconds", 30)))
        self._recover(driver, role, driver.focus)
        if do_model and role != "coder":
            model, effort = self._model(role)
            driver.select_model(model, effort, role)
        driver.ring(pointer)
        sent = driver.verify_sent()
        self.journal.append(
            "gui.ring",
            round_no=round_no,
            detail={
                "role": role,
                "app": driver.name,
                "chars": len(pointer),
                "pointer": pointer,
                "sent": sent,
            },
            result="ok" if sent is not False else "unconfirmed",
        )
        if sent is False:
            driver.ring(pointer)
            sent = driver.verify_sent()
            self.journal.append(
                "gui.ring.retry", round_no=round_no, detail={"role": role, "sent": sent},
                result="ok" if sent is not False else "failed",
            )
            if sent is False:
                raise HumanRequired(
                    "Relay typed the pointer but could not confirm it was submitted. "
                    "Check the app, then resume.",
                    options=[
                        {"label": "I fixed the app", "consequence": "Relay retries this step."},
                        {"label": "Stop for now", "consequence": "The loop pauses."},
                    ],
                    reason="pointer not confirmed",
                )
        return sent

    def _screen_check(self, driver: AppDriver, role: str) -> None:
        if not driver.is_user_idle(int(self.cfg.get("user_idle_seconds", 30))):
            self.journal.append(
                "vision.check", detail={"role": role, "result": "skipped: user active"}
            )
            return
        try:
            popup = driver.read_popup()
        except VisionUnavailable:
            self.journal.append("vision.check", detail={"kind": "popup", "role": role, "result": "vision unavailable"})
            popup = None
        except BrainError as exc:
            self.journal.append("vision.check", detail={"kind": "popup", "role": role, "error": str(exc)}, result="error")
            popup = None
        except ScreenError as exc:
            self.journal.append(
                "vision.check", detail={"kind": "popup", "role": role, "error": str(exc)}, result="error"
            )
            raise HumanRequired(
                "Relay could not read an approval dialog through Accessibility. Check the Automation "
                "and Accessibility permissions, then resume. Nothing was approved.",
                reason="dialog could not be read",
            )
        if popup is not None:
            safe_text = truncate(redact(popup.text).text, 300)
            self.journal.append(
                "popup.seen", detail={"role": role, "text": safe_text, "buttons": popup.buttons}
            )
            if decide_popup(
                popup,
                list(self.cfg.get("popup_allow_list", [])),
                list(self.cfg.get("popup_allow_commands", [])),
            ) == "approve":
                button = choose_button(popup)
                if button:
                    driver.click_popup(popup, button)
                    self.journal.append("popup.approved", detail={"button": button}, result="ok")
            else:
                self.journal.append("popup.escalated", detail={"text": safe_text}, result="blocked")
                raise HumanRequired(
                    f"An approval dialog needs you: {safe_text}",
                    options=POPUP_OPTIONS,
                    reason="non-allowlisted popup",
                )
        try:
            busy = driver.is_busy()
            self._last_busy = busy
            self.journal.append("vision.check", detail={"kind": "busy", "role": role, "busy": busy})
        except (BrainError, VisionUnavailable):
            self._last_busy = None

    def _wait(self, spec: WaitSpec) -> dict:
        driver = self.drivers[role_app(spec.role)]
        sentinel_path = self.paths.sentinel(spec.role)
        deadline = spec.started_epoch + spec.timeout_minutes * 60
        next_screen = spec.started_epoch
        last_reject: str | None = None
        rejections = 0
        nudges = 0
        max_rejected = int(self.cfg.get("max_rejected_sentinels", 3))
        nudge_after = float(self.cfg.get("nudge_after_seconds", 180))
        max_nudges = int(self.cfg.get("max_nudges", 1))

        while self.clock.epoch() < deadline:
            self._check_wait_guards()
            if sentinel_path.exists():
                try:
                    sentinel = read_sentinel(sentinel_path)
                    issues = validate_sentinel(
                        sentinel, root=self.cfg.project_dir, expected_round=spec.round_no,
                        expected_role=spec.role, expected_file=spec.expected_file,
                        round_started_wall=spec.started_wall,
                    )
                except IntegrityError as exc:
                    sentinel = None
                    issues = [str(exc)]
                if sentinel is not None and not issues:
                    self.journal.append(
                        "sentinel.seen", round_no=spec.round_no,
                        detail={
                            "role": spec.role, "status": sentinel.get("status"),
                            "verdict": sentinel.get("verdict"), "file": sentinel.get("file"),
                            "sha256": sentinel.get("sha256"),
                        },
                    )
                    return sentinel
                digest = sha256_file(sentinel_path)
                if digest != last_reject:
                    rejections += 1
                    last_reject = digest
                    self.journal.append(
                        "sentinel.rejected", round_no=spec.round_no,
                        detail={"role": spec.role, "reasons": issues[:6]}, result="ignored",
                    )
                if rejections > max_rejected:
                    raise WaitTimeout(
                        f"{spec.role} wrote {rejections} unusable sentinels; last: {'; '.join(issues[:3])}"
                    )
            now = self.clock.epoch()
            if now >= next_screen:
                self._screen_check(driver, spec.role)
                next_screen = now + float(self.cfg.get("screen_check_seconds", 60))
            idle = self._last_busy is False or (
                self._last_busy is None and now - spec.started_epoch >= max(nudge_after * 2, 600)
            )
            if nudges < max_nudges and now - spec.started_epoch >= nudge_after and idle:
                self._ring(driver, spec.role, self._nudge_pointer(spec), spec.round_no, do_model=False)
                nudges += 1
                self.journal.append("turn.nudge", round_no=spec.round_no, detail={"role": spec.role})
            self.store.heartbeat(phase=self._state().get("phase"), round=spec.round_no)
            self.clock.sleep(float(self.cfg.get("poll_seconds", 25)))

        self.journal.append(
            "turn.timeout", round_no=spec.round_no,
            detail={"role": spec.role, "expected": spec.expected_file}, result="error",
        )
        raise WaitTimeout(
            f"{spec.role} did not write {spec.expected_file} within {spec.timeout_minutes} minutes"
        )

    # --- routing ----------------------------------------------------------
    def _route(self) -> None:
        state = self._state()
        sentinel = state.get("last_sentinel") or {}
        verdict = sentinel.get("verdict")
        if verdict == "CHANGES_REQUESTED":
            target = "CODE"
            approval = False
            state["counters"]["consecutive_changes"] = int(state["counters"].get("consecutive_changes", 0)) + 1
        elif verdict == "MILESTONE_DONE":
            target = "PLAN"
            approval = True
            state["counters"]["consecutive_changes"] = 0
        elif verdict == "APPROVED":
            # Accepted work, but the milestone is not done: do not advance or reset its cap.
            target = "PLAN"
            approval = False
            state["counters"]["consecutive_changes"] = 0
        elif verdict == "PROJECT_DONE":
            target = "DONE"
            approval = False
            state["counters"]["consecutive_changes"] = 0
        else:
            self.escalate_human(
                "The reviewer finished without a usable verdict. Decide how to proceed.",
                options=[
                    {"label": "Send it back for review", "consequence": "The reviewer gets another attempt."},
                    {"label": "Re-plan", "consequence": "The planner rewrites the handoff."},
                ],
                reason=f"unusable verdict: {verdict!r}",
                return_to="REVIEW",
            )
            return

        base_target = target
        stall_reason = ""
        if target == "CODE":
            history = state.get("history", [])[-3:]
            no_progress = len(history) == 3 and all(not item.get("source_changed") for item in history)
            if int(state["counters"].get("consecutive_changes", 0)) >= 3 and no_progress:
                target = "PLAN"
                stall_reason = "three change requests with no source changes"
                state["counters"]["replans"] = int(state["counters"].get("replans", 0)) + 1
                state["counters"]["stalls_in_milestone"] = int(state["counters"].get("stalls_in_milestone", 0)) + 1
                self.journal.append(
                    "stall.detected", round_no=state.get("round"),
                    detail={"reason": stall_reason, "replans": state["counters"]["replans"]},
                )

        decisions_text = self._read(".relay/DECISIONS.md", 4000)
        history_text = "\n".join(
            f"round {item.get('round')} {item.get('role')}: {item.get('status')} "
            f"{item.get('verdict') or ''} - {truncate(str(item.get('summary') or ''), 200)}"
            for item in state.get("history", [])[-3:]
        )
        brain_route = None
        summary = str(sentinel.get("summary") or "")
        last_decision = state.get("last_decision") or {}
        answered_this_route = (
            last_decision.get("return_to") == "ROUTE"
            and int(last_decision.get("round", -1)) == int(state.get("round", -2))
        )
        if self.brain is not None:
            decision = self.brain.route(
                round_no=int(state.get("round", 0)),
                milestone=int(state.get("milestone", 1)),
                phase="ROUTE",
                sentinel=sentinel,
                review=self._read(".relay/REVIEW.md", 12000),
                handoff=self._read(".relay/HANDOFF.md", 8000),
                history=history_text,
                monitor_notes=self._read(".relay/MONITOR_NOTES.md", 3000),
                decisions=decisions_text,
            )
            brain_route = decision.route
            summary = decision.round_summary or summary
            self.journal.append(
                "route.judgment",
                round_no=state.get("round"),
                detail={
                    "route": decision.route,
                    "reason": decision.reason,
                    "drift_flags": decision.drift_flags,
                    "call_id": decision.call_id,
                    "suggest_escalation": decision.suggest_escalation,
                },
            )
            if decision.route == "NEEDS_HUMAN" and decision.needs_human:
                if answered_this_route:
                    self.journal.append(
                        "route.after_decision",
                        round_no=state.get("round"),
                        detail={"note": "routing judgment re-raised a question answered this round; using deterministic route"},
                    )
                else:
                    self.escalate_human(
                        decision.needs_human["question"],
                        decision.needs_human["options"],
                        reason=decision.reason,
                        return_to="ROUTE",
                    )
                    return
            if decision.route == "STUCK" and target == "CODE":
                target = "PLAN"
                stall_reason = decision.reason or "DeepSeek detected a loop"
                state["counters"]["replans"] = int(state["counters"].get("replans", 0)) + 1
                state["counters"]["stalls_in_milestone"] = int(state["counters"].get("stalls_in_milestone", 0)) + 1
            if base_target == "DONE" and decision.route in ("NEEDS_HUMAN", "STUCK") and not answered_this_route:
                self.escalate_human(
                    "The reviewer says PROJECT_DONE but the routing judgment disagrees. "
                    "Confirm whether the project is actually complete.",
                    options=[
                        {"label": "Accept PROJECT_DONE", "consequence": "Relay stops and reports success."},
                        {"label": "Re-plan", "consequence": "Work continues with a new handoff."},
                    ],
                    reason=decision.reason,
                    return_to="ROUTE",
                )
                return

        route_record = {
            "base": base_target,
            "target": target,
            "brain": brain_route,
            "reason": stall_reason,
            "summary": summary,
            "round": state.get("round"),
        }
        consecutive_changes = int(state["counters"].get("consecutive_changes", 0))
        replans = int(state["counters"].get("replans", 0))
        stalls = int(state["counters"].get("stalls_in_milestone", 0))
        forced_roles = list(state.get("rollover", {}).get("forced_roles") or [])
        if approval:
            milestone_pct = float(self.cfg.get("rollover_at_milestone_pct", 0.5)) * 100
            forced_roles = [
                role for role in ("planner", "reviewer", "coder")
                if float(state.get("session_ctx", {}).get(role, 0) or 0) >= milestone_pct
            ]

        def apply_route(current: dict) -> None:
            # Reload under the lock: a concurrent `relay decide`/`instruct` must survive.
            current["counters"]["consecutive_changes"] = consecutive_changes
            current["counters"]["replans"] = 0 if approval else replans
            current["counters"]["stalls_in_milestone"] = 0 if approval else stalls
            if approval:
                current["milestone"] = int(current.get("milestone", 1)) + 1
                current["counters"]["rounds_in_milestone"] = 0
                current["rollover"]["forced_roles"] = forced_roles
            current["last_route"] = route_record
            current["phase"] = target

        self.store.mutate(apply_route)
        state = self._state()
        self.journal.append(
            "route.decided",
            round_no=state.get("round"),
            detail={
                "base": base_target,
                "target": target,
                "brain": brain_route,
                "stall": stall_reason,
                "summary": summary,
            },
        )
        if approval:
            self._archive_fix_plan(int(state.get("round", 0)), "milestone")
        if target == "DONE":
            self.journal.append("project.done", round_no=state.get("round"), detail={"summary": summary})
            self._notify("Relay finished", "The project reported PROJECT_DONE.")
        elif target == "PLAN" and stall_reason:
            self._notify("Relay is re-planning", truncate(stall_reason, 200))

    # --- decisions --------------------------------------------------------
    def escalate_human(
        self,
        question: str,
        options: list[dict] | None = None,
        *,
        reason: str = "",
        return_to: str | None = None,
        suggestion: str | None = None,
        kind: str | None = None,
        data: dict | None = None,
    ) -> str:
        state = self._state()
        existing = state.get("pending_decision")
        if state.get("phase") == "WAIT_HUMAN" and isinstance(existing, dict):
            return str(existing.get("id"))
        code = new_code()
        clean_options = self._clean_options(options)
        context_lines = [reason or question]
        last = state.get("last_sentinel") or {}
        if last.get("summary"):
            context_lines.append(f"last round: {last.get('summary')}")
        for line in self._read(".relay/MONITOR_NOTES.md", 2000).splitlines():
            if line.strip().startswith("- ["):
                context_lines.append(line.strip())
        asked_at = self.clock.now().isoformat()
        chosen: dict[str, str] = {}

        def apply(current: dict) -> None:
            # Allocate the decision id under the lock, so two contexts cannot reuse one id.
            pending_now = current.get("pending_decision")
            if current.get("phase") == "WAIT_HUMAN" and isinstance(pending_now, dict):
                chosen["existing"] = str(pending_now.get("id"))
                return
            decision_id = f"D-{int(current.get('decisions', 0)) + 1:03d}"
            chosen["id"] = decision_id
            round_no = int(current.get("round", 0))
            return_to_value = return_to or current.get("phase")
            current["decisions"] = int(current.get("decisions", 0)) + 1
            # Only the id is in the project; the code hash is in the external trust store.
            self.ctx.trust.store_code(
                decision_id, hash_code(code), {"round": round_no, "asked_at": asked_at}
            )
            current["pending_decision"] = {
                "id": decision_id,
                "question": question,
                "options": clean_options,
                "round": round_no,
                "return_to": return_to_value,
                "asked_at": asked_at,
                "kind": kind,
                "data": data,
            }
            current["phase"] = "WAIT_HUMAN"
            current["return_to"] = return_to_value
            current["paused_reason"] = question
            notification = current.setdefault("notification", {})
            notification["last_sent_at"] = asked_at
            notification["count"] = int(notification.get("count", 0)) + 1

        self.store.mutate(apply)
        if "existing" in chosen:
            return chosen["existing"]
        decision_id = chosen["id"]
        write_pending(
            self.paths,
            decision_id=decision_id,
            question=question,
            context_lines=context_lines,
            options=clean_options,
            suggestion=suggestion,
            round_no=int(state.get("round", 0)),
            reason=reason or question,
            clock=self.clock,
        )
        self.store.set_baton("human", "human", int(state.get("round", 0)))
        self.journal.append(
            "decision.asked",
            state="WAIT_HUMAN",
            round_no=int(state.get("round", 0)),
            detail={
                "id": decision_id, "question": question, "options": clean_options,
                "reason": reason, "kind": kind,
            },
        )
        self._decision_codes[decision_id] = code
        if sys.stdout.isatty():
            print(
                f"Relay needs a decision {decision_id}. One-time answer code: {code}",
                file=sys.stderr,
            )
        self._notify(
            f"Relay needs a decision ({decision_id})",
            f"{decision_id}: {truncate(question, 220)}",
            decision_id=decision_id,
            urgent=True,
            desktop_extra=f"One-time answer code: {code}",
            ntfy_body=(
                f"{decision_id}: {truncate(question, 180)}. "
                "The answer code is shown in the desktop notification."
            ),
        )
        return decision_id

    def _decision_fingerprint(self) -> tuple[str, str]:
        state_digest = sha256_file(self.paths.state) if self.paths.state.exists() else ""
        pending_digest = ""
        if self.paths.pending_decision.exists():
            try:
                text = self.paths.pending_decision.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            pending_digest = sha256_text(_strip_answer_line(text))
        return state_digest, pending_digest

    def _decision_tampered(self, decision_id: str, reason: str) -> None:
        self.journal.append(
            "decision.tamper", detail={"id": decision_id, "reason": reason}, result="blocked"
        )
        self._pause(f"decision tampering detected: {reason}")

    def wait_for_decision(self) -> None:
        state = self._state()
        pending = state.get("pending_decision") or {}
        decision_id = pending.get("id")
        self.journal.append("decision.waiting", detail={"id": decision_id})
        # N1 fix 4: any change to STATE.json or the question text while we wait is tampering.
        baseline = self._decision_fingerprint()
        reminder_seconds = float(self.cfg.get("decision_reminder_hours", 4)) * 3600
        last_reminder = self.clock.epoch()
        last_bad_answer: str | None = None
        timeout_minutes = float(self.cfg.get("decision_wait_timeout_minutes", 0) or 0)
        deadline = self.clock.epoch() + timeout_minutes * 60 if timeout_minutes > 0 else None
        while True:
            self._check_wait_guards()
            state = self._state()
            if state.get("phase") != "WAIT_HUMAN":
                last = state.get("last_decision") or {}
                if str(last.get("id") or "") == str(decision_id):
                    return
                self._decision_tampered(decision_id, "phase moved without an accepted answer")
                return
            if not state.get("pending_decision"):
                last = state.get("last_decision") or {}
                if str(last.get("id") or "") == str(decision_id):
                    return
                self._decision_tampered(decision_id, "pending decision cleared without an answer")
                return
            parsed = local_answer(self.paths)
            if parsed:
                try:
                    self.answer_decision(parsed[1], parsed[0])
                    return
                except StateError as exc:
                    marker = f"{parsed[0]}:{parsed[1]}"
                    if marker != last_bad_answer:
                        last_bad_answer = marker
                        self.journal.append(
                            "decision.rejected",
                            detail={"id": decision_id, "reason": str(exc)},
                            result="ignored",
                        )
                    self.store.heartbeat(phase="WAIT_HUMAN", round=state.get("round", 0))
                    self.clock.sleep(float(self.cfg.get("decision_poll_seconds", 60)))
                    continue
            if self._decision_fingerprint() != baseline:
                self._decision_tampered(
                    decision_id, "STATE.json or PENDING_DECISION.md changed while waiting"
                )
                return
            if reminder_seconds > 0 and self.clock.epoch() - last_reminder >= reminder_seconds:
                code = self._decision_codes.get(str(decision_id))
                self._notify(
                    f"Reminder: Relay is waiting ({decision_id})",
                    f"{decision_id}: {truncate(str((state.get('pending_decision') or {}).get('question', '')), 200)}",
                    decision_id=decision_id,
                    urgent=True,
                    desktop_extra=(f"One-time answer code: {code}" if code else
                                   "Use the code from the original desktop notification."),
                )
                self.journal.append("decision.reminder", detail={"id": decision_id})
                state["notification"]["last_sent_at"] = self.clock.now().isoformat()
                self.store.save(state)
                baseline = self._decision_fingerprint()
                last_reminder = self.clock.epoch()
            if deadline is not None and self.clock.epoch() >= deadline:
                self.journal.append("decision.timeout", detail={"id": decision_id}, result="still waiting")
                return
            self.store.heartbeat(phase="WAIT_HUMAN", round=state.get("round", 0))
            self.clock.sleep(float(self.cfg.get("decision_poll_seconds", 60)))

    def _clean_options(self, options: Any) -> list[dict]:
        if isinstance(options, list) and 2 <= len(options) <= 4:
            clean = []
            for option in options:
                if isinstance(option, dict) and str(option.get("label", "")).strip() and str(option.get("consequence", "")).strip():
                    clean.append({"label": str(option["label"]), "consequence": str(option["consequence"])})
            if len(clean) == len(options):
                return clean
        return DEFAULT_OPTIONS

    # --- pause / notify ---------------------------------------------------
    def _pause(self, reason: str) -> None:
        changed: dict[str, Any] = {}

        def apply(current: dict) -> None:
            if current.get("phase") in ("PAUSED", "DONE", "HALTED"):
                return
            return_to = "WAIT_HUMAN" if current.get("phase") == "WAIT_HUMAN" else current.get("phase")
            current["phase"] = "PAUSED"
            current["return_to"] = return_to
            current["paused_reason"] = reason
            changed["return_to"] = return_to

        if self.store.mutate(apply).get("phase") != "PAUSED" or "return_to" not in changed:
            return
        return_to = changed.get("return_to")
        self.store.clear_baton()
        try:
            self.journal.append("state.paused", state="PAUSED", detail={"reason": reason, "return_to": return_to})
        except JournalError:
            pass
        self._notify("Relay paused", truncate(reason, 220))

    def _abort_turn(
        self, reason: str, phase: str | None,
        *, paths: list[str] | None = None, hashes: dict[str, str | None] | None = None,
    ) -> None:
        def apply(current: dict) -> None:
            current["pending"] = None
            current["blocked"] = {
                "reason": reason,
                "paths": paths or [],
                "hashes": hashes or {},
                "round": int(current.get("round", 0)),
            }
            if int(current.get("round", 0)) > 0:
                current["round"] = int(current["round"]) - 1
            current["phase"] = "PAUSED"
            current["return_to"] = phase
            current["paused_reason"] = reason

        self.store.mutate(apply)
        self.store.clear_baton()
        try:
            self.journal.append(
                "state.paused", state="PAUSED",
                detail={"reason": reason, "return_to": phase, "aborted_turn": True},
            )
        except JournalError:
            pass
        self._notify("Relay paused", truncate(reason, 220))

    def _notify(
        self,
        title: str,
        body: str,
        *,
        decision_id: str | None = None,
        urgent: bool = False,
        desktop_extra: str | None = None,
        ntfy_body: str | None = None,
    ) -> None:
        try:
            self.notifier.send(
                title, body, decision_id=decision_id, urgent=urgent,
                desktop_extra=desktop_extra, ntfy_body=ntfy_body,
            )
        except Exception as exc:  # never let a notification failure stop the loop
            self.journal.append("notify.error", detail={"error": type(exc).__name__}, result="error")
