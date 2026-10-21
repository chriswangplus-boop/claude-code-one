"""The relay loop: hand the baton, ring the doorbell, wait for a valid sentinel.

The orchestrator holds no memory between steps. Every decision is reconstructed from
STATE.json, the files in .relay/, and the journal. That is what keeps it from drifting.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .brain import Brain
from .config import Config
from .context import Context
from .decisions import accept_decision, local_answer, write_pending
from .errors import (
    AppMismatch, BrainError, BudgetExceeded, ContextGuardTripped, IntegrityError,
    JournalError, PauseRequested, RelayError, SecretDetected, StateError,
    VisionUnavailable, WaitTimeout,
)
from .gui.base import AppDriver
from .gui.recovery import Recovery
from .integrity import (
    changed_paths, git_commit, git_snapshot, ownership_violations, read_sentinel,
    secret_scan, snapshot, validate_sentinel,
)
from .journal import Journal
from .policy import choose_button, decide_popup
from .redact import redact
from .state import StateStore, read_json
from .util import ascii_pointer, canonical_json, sha256_file, sha256_text, short_hash, truncate

PHASE_ROLE = {"PLAN": "planner", "CODE": "coder", "REVIEW": "reviewer"}
ROLE_APP = {"planner": "claude", "reviewer": "claude", "coder": "coder"}
ROLE_OWNER = {"planner": "claude", "reviewer": "claude", "coder": "coder"}
EXPECTED_FILE = {
    "PLAN": ".relay/HANDOFF.md",
    "CODE": ".relay/CODER_REPORT.md",
    "REVIEW": ".relay/REVIEW.md",
}
NEXT_PHASE = {"PLAN": "CODE", "CODE": "REVIEW", "REVIEW": "ROUTE"}
APPROVAL_VERDICTS = {"APPROVED", "MILESTONE_DONE"}

DEFAULT_OPTIONS = [
    {"label": "Continue", "consequence": "Relay resumes with the same plan."},
    {"label": "Stop for now", "consequence": "The loop stays paused; nothing is lost."},
]
STALL_OPTIONS = [
    {"label": "Re-plan", "consequence": "The planner gets a fresh handoff for this milestone."},
    {"label": "Stop for now", "consequence": "The loop stays paused so you can inspect the work."},
]
POPUP_OPTIONS = [
    {"label": "Allow this one action", "consequence": "The agent continues past the dialog."},
    {"label": "Deny and stop", "consequence": "The loop pauses; you handle the dialog yourself."},
]


class HumanRequired(Exception):
    def __init__(self, question: str, options: list[dict] | None = None, *, reason: str = ""):
        super().__init__(question)
        self.question = question
        self.options = options or DEFAULT_OPTIONS
        self.reason = reason or question


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
        self.clock = ctx.clock
        self.drivers = drivers
        self.brain = brain
        self.notifier = notifier or _NullNotifier()
        self.monitor_required = monitor_required
        self.wall = wall or __import__("time").time
        self._turns_completed = 0
        monitor_age = self.store.heartbeat_age(self.paths.monitor_heartbeat)
        self._monitor_seen = monitor_age is not None and monitor_age < float(
            self.cfg.get("monitor.heartbeat_stale_seconds", 300)
        )
        self._monitor_grace_until = self.clock.epoch() + 90
        self._last_busy: bool | None = None
        self._first_prepare = True
        self._run_id = uuid.uuid4().hex[:8]

    # --- public -----------------------------------------------------------
    def run(self, *, max_steps: int | None = None, wait_human: bool = True) -> dict:
        self._prepare()
        steps = 0
        while True:
            try:
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
                self.escalate_human(exc.question, exc.options, reason=exc.reason)
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

    def answer_decision(self, answer: str) -> dict:
        result = accept_decision(self.ctx, answer)
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
        if self._first_prepare:
            state = self._state()
            state["active_session"] = {}
            self.store.save(state)
            self._first_prepare = False
        state = self._state()
        if state.get("phase") == "WAIT_HUMAN":
            answer = local_answer(self.paths)
            if answer:
                self.answer_decision(answer)
        state = self._state()
        if self.store.baton().get("holder") not in (None, "none") and not state.get("pending"):
            self.store.clear_baton()
            self.journal.append("state.baton_recovered", detail={"note": "cleared stale baton on start"})
        if not state.get("started_at"):
            state["started_at"] = self.clock.now().isoformat()
        state["run_id"] = self._run_id
        self.store.save(state)
        self.store.heartbeat(phase=state.get("phase"), round=state.get("round", 0))
        self._turns_completed = len(self.journal.of_type("turn.end"))
        self.journal.append(
            "run.start",
            state=state.get("phase"),
            detail={"pid": __import__("os").getpid(), "run_id": self._run_id},
        )

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
        total = int(state.get("counters", {}).get("total_rounds", 0))
        if total >= int(self.cfg.get("max_total_rounds", 60)):
            raise PauseRequested(f"round cap reached ({total})")
        in_milestone = int(state.get("counters", {}).get("rounds_in_milestone", 0))
        if in_milestone >= int(self.cfg.get("max_rounds_per_milestone", 8)):
            raise PauseRequested(f"milestone round cap reached ({in_milestone})")
        budget = float(self.cfg.get("daily_budget_usd", 0) or 0)
        spent = self.journal.spend_for_day()
        if budget > 0 and spent >= budget:
            raise PauseRequested(f"daily budget reached (${spent:.2f} of ${budget:.2f})")

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
            state["phase"] = "PLAN"
            state["counters"]["replans"] = int(state["counters"].get("replans", 0)) + 1
            self.store.save(state)
            self._notify("Monitor requested a re-plan", truncate(str(data.get("reason") or ""), 200))
            return True
        if action == "rollover":
            state["rollover"]["forced"] = True
            self.store.save(state)
            return False
        if action == "halt":
            self.store.set_flag("halt", {"reason": data.get("reason") or "monitor requested halt"})
            return False
        return False

    def _user_edits_pending(self) -> bool:
        if (
            self._turns_completed == 0
            or not bool(self.cfg.get("git.enabled", True))
            or not bool(self.cfg.get("auto_commit", True))
        ):
            return False
        lines = git_snapshot(self.cfg.project_dir)
        # Relay and the monitor write these without a human; they are not user edits.
        ignored = (".relay/DECISIONS.md", ".relay/PENDING_DECISION.md", ".relay/MONITOR_NOTES.md")
        return [line for line in lines if not any(marker in line for marker in ignored)] != []

    # --- turns ------------------------------------------------------------
    def _turn(self, phase: str) -> None:
        role = PHASE_ROLE[phase]
        driver = self.drivers[ROLE_APP[role]]
        state = self._state()
        pending = state.get("pending") if isinstance(state.get("pending"), dict) else None
        resuming = bool(pending and pending.get("phase") == phase)

        if resuming:
            round_no = int(pending["round"])
            pointer = str(pending["pointer"])
            expected = str(pending["expected_file"])
            started_wall = float(pending["started_wall"])
            started_epoch = self.clock.epoch()
            self.journal.append(
                "turn.resume", state=phase, round_no=round_no,
                detail={"role": role, "expected": expected, "resumed": True},
            )
        else:
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
            # Switch chats before deciding on a rollover: a newly opened chat is already fresh.
            self._ensure_session(role, round_no)
            self._maybe_rollover(role, round_no, boundary=(phase == "PLAN"))
            state = self._state()
            expected = EXPECTED_FILE[phase]
            pointer = self._build_pointer(phase, role, round_no)
            started_wall = self.wall()
            started_epoch = self.clock.epoch()
            state["round"] = round_no
            state["role"] = role
            state["pending"] = {
                "phase": phase,
                "role": role,
                "round": round_no,
                "pointer": pointer,
                "expected_file": expected,
                "started_wall": started_wall,
            }
            self.store.save(state)

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
            self._ensure_session(role, round_no)

        sentinel_path.unlink(missing_ok=True)
        before = snapshot(self.cfg.project_dir)
        self.store.heartbeat(phase=phase, round=round_no)
        self.store.set_baton(role, role, round_no)
        self.journal.append(
            "turn.start",
            state=phase,
            round_no=round_no,
            detail={
                "role": role, "app": ROLE_APP[role], "expected": expected, "pointer": pointer,
                "snapshot_sha256": sha256_text(canonical_json(before)),
                "watched_files": len(before),
                "git_status": git_snapshot(self.cfg.project_dir)[:20]
                if bool(self.cfg.get("git.enabled", True)) else [],
            },
        )
        self._ring(driver, role, pointer, round_no)
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
        )

    def _ensure_session(self, role: str, round_no: int) -> None:
        """Planner and reviewer are separate chats in the same app; open + seed on switch."""
        app = ROLE_APP[role]
        state = self._state()
        active = state.setdefault("active_session", {})
        if active.get(app) == role:
            return
        driver = self.drivers[app]
        self._recover(driver, role, driver.new_session)
        self._ring(driver, role, self._seed_pointer(role), round_no)
        state = self._state()
        state.setdefault("active_session", {})[app] = role
        state["session_rounds"][role] = 0
        state["session_ctx"][role] = 0
        forced_roles = list(state.get("rollover", {}).get("forced_roles") or [])
        if role in forced_roles:
            state["rollover"]["forced_roles"] = [item for item in forced_roles if item != role]
        self.store.save(state)
        self.journal.append(
            "session.open", state=state.get("phase"), round_no=round_no,
            detail={"app": app, "role": role, "seeded": True},
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
    ) -> None:
        after = snapshot(self.cfg.project_dir)
        changed = changed_paths(before, after)
        violations = ownership_violations(before, after, ROLE_OWNER[role])
        if violations:
            text = ", ".join(f"{v['path']} (owned by {v['owner']})" for v in violations[:5])
            self.journal.append(
                "integrity.violation", state=phase, round_no=round_no,
                detail={"kind": "ownership", "violations": violations}, result="blocked",
            )
            self._abort_turn(f"{role} wrote files it does not own: {text}", phase)
            raise PauseRequested(f"ownership violation: {text}")

        hits = secret_scan(self.cfg.project_dir, changed)
        if hits:
            text = ", ".join(f"{h['path']}:{h['line']} ({h['rule']})" for h in hits[:5])
            self.journal.append(
                "integrity.violation", state=phase, round_no=round_no,
                detail={"kind": "secret", "hits": hits}, result="blocked",
            )
            self._abort_turn(f"secret scan blocked the commit: {text}", phase)
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

        state = self._state()
        state["pending"] = None
        state["last_sentinel"] = {
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
        state["session_rounds"][role] = int(state["session_rounds"].get(role, 0)) + 1
        if isinstance(ctx_pct, (int, float)):
            state["session_ctx"][role] = max(float(state["session_ctx"].get(role, 0)), float(ctx_pct))
        state["counters"]["total_rounds"] = max(int(state["counters"].get("total_rounds", 0)), round_no)
        state["counters"]["rounds_in_milestone"] = int(state["counters"].get("rounds_in_milestone", 0)) + 1
        state["history"].append({
            "round": round_no,
            "role": role,
            "status": sentinel.get("status"),
            "verdict": verdict,
            "summary": summary,
            "source_changed": source_changed,
            "changed": changed[:50],
            "ctx_pct": ctx_pct,
        })
        state["history"] = state["history"][-20:]
        state["last_turn"] = {
            "round": round_no,
            "role": role,
            "status": sentinel.get("status"),
            "verdict": verdict,
            "changed": changed,
            "source_changed": source_changed,
        }
        self.store.save(state)
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
            },
            outputs=[
                {"path": str(sentinel.get("file")), "sha256": sentinel.get("sha256")},
                {"path": f".relay/{'CODER_DONE.json' if role == 'coder' else 'CLAUDE_DONE.json'}",
                 "sha256": sha256_file(self.paths.sentinel(role))},
            ],
        )
        self._turns_completed += 1

        if sentinel.get("status") in ("needs_decision", "blocked"):
            question = str(sentinel.get("question") or summary or f"{role} is blocked")
            options = self._clean_options(sentinel.get("options"))
            self.escalate_human(
                question, options, reason=f"{role} reported {sentinel.get('status')}",
                return_to=phase, suggestion=sentinel.get("suggestion"),
            )
            return

        target = NEXT_PHASE[phase]
        state = self._state()
        state["phase"] = target
        self.store.save(state)
        self.journal.append("state.transition", state=target, round_no=round_no, detail={"from": phase, "to": target})

    # --- rollover ---------------------------------------------------------
    def _maybe_rollover(self, role: str, round_no: int, *, boundary: bool) -> None:
        state = self._state()
        if role == "coder":
            max_rounds = int(self.cfg.get("zcode_app.max_rounds_per_session", 3))
        else:
            max_rounds = int(self.cfg.get("claude_app.max_rounds_per_session", 4))
        ratio = int(state["session_rounds"].get(role, 0)) / max(1, max_rounds)
        signals = [float(state["session_ctx"].get(role, 0) or 0)]
        driver = self.drivers[ROLE_APP[role]]
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
        state["rollover"]["forced"] = False
        if role in forced_roles:
            state["rollover"]["forced_roles"] = [item for item in forced_roles if item != role]
        self.store.save(state)
        self._rollover(role, round_no, reason)

    def _rollover(self, role: str, round_no: int, reason: str) -> None:
        driver = self.drivers[ROLE_APP[role]]
        checkpoint_rel = f".relay/CHECKPOINT_{role}.md"
        pointer = self._checkpoint_pointer(role, round_no)
        sentinel_path = self.paths.sentinel(role)
        sentinel_path.unlink(missing_ok=True)
        before = snapshot(self.cfg.project_dir)
        started_wall = self.wall()
        self.store.set_baton(role, role, round_no)
        self.journal.append(
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
        violations = ownership_violations(before, after, ROLE_OWNER[role])
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
        state = self._state()
        state["session_rounds"][role] = 0
        state["session_ctx"][role] = 0
        self.store.save(state)
        seed = self._seed_pointer(role)
        self._ring(driver, role, seed, round_no)
        state = self._state()
        state.setdefault("active_session", {})[ROLE_APP[role]] = role
        self.store.save(state)
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

    def _build_pointer(self, phase: str, role: str, round_no: int) -> str:
        notes = ".relay/MONITOR_NOTES.md"
        decisions = ".relay/DECISIONS.md"
        if phase == "PLAN":
            base = ".relay/GOAL.md" if not (self.cfg.project_dir / ".relay/HANDOFF.md").exists() else ".relay/HANDOFF.md"
            text = (
                f"RELAY round {round_no} role=planner: read {base} (sha256 {self._hint(base)}), "
                f".relay/PLAN.md, .relay/REVIEW.md, .relay/CODER_REPORT.md, {decisions}, {notes} "
                "and .relay/RULES_CLAUDE.md (planner section). "
                "Write .relay/PLAN.md and .relay/HANDOFF.md, then .relay/CLAUDE_DONE.json last."
            )
        elif phase == "CODE":
            text = (
                f"RELAY round {round_no} role=coder: read .relay/HANDOFF.md (sha256 {self._hint('.relay/HANDOFF.md')}), "
                f".relay/PLAN.md, {decisions}, {notes} and .relay/RULES_CODER.md. "
                "Write .relay/CODER_REPORT.md, then .relay/CODER_DONE.json last."
            )
        else:
            text = (
                f"RELAY round {round_no} role=reviewer: read .relay/HANDOFF.md "
                f"(sha256 {self._hint('.relay/HANDOFF.md')}), .relay/CODER_REPORT.md "
                f"(sha256 {self._hint('.relay/CODER_REPORT.md')}), .relay/PLAN.md, {decisions}, {notes} and "
                ".relay/RULES_CLAUDE.md (reviewer section). Write .relay/REVIEW.md, then .relay/CLAUDE_DONE.json last."
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
        text = (
            f"RELAY seed role={role}: read {rules} ({section} section), .relay/PLAN.md, .relay/HANDOFF.md, "
            f".relay/DECISIONS.md, .relay/CHECKPOINT_{role}.md and .relay/MONITOR_NOTES.md. "
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
        if role == "planner":
            return str(self.cfg.get("claude_app.planner_model", "")), str(self.cfg.get("claude_app.planner_effort", "medium"))
        if role == "reviewer":
            return str(self.cfg.get("claude_app.reviewer_model", "")), str(self.cfg.get("claude_app.reviewer_effort", "high"))
        return str(self.cfg.get("zcode_app.model", "")), str(self.cfg.get("zcode_app.effort", "high"))

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
            driver.select_model(model, effort)
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
        try:
            popup = driver.read_popup()
        except VisionUnavailable:
            self.journal.append("vision.check", detail={"kind": "popup", "role": role, "result": "vision unavailable"})
            popup = None
        except BrainError as exc:
            self.journal.append("vision.check", detail={"kind": "popup", "role": role, "error": str(exc)}, result="error")
            popup = None
        if popup is not None:
            safe_text = truncate(redact(popup.text).text, 300)
            self.journal.append(
                "popup.seen", detail={"role": role, "text": safe_text, "buttons": popup.buttons}
            )
            if decide_popup(popup, list(self.cfg.get("popup_allow_list", []))) == "approve":
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
        driver = self.drivers[ROLE_APP[spec.role]]
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
        elif verdict in APPROVAL_VERDICTS:
            target = "PLAN"
            approval = True
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

        if target == "PLAN" and int(state["counters"].get("replans", 0)) > 2:
            self.escalate_human(
                "Relay is stuck: two re-plans have not produced progress. Decide what to change.",
                options=STALL_OPTIONS,
                reason=stall_reason or "re-plan cap reached",
                return_to="PLAN",
            )
            return

        if approval:
            state["milestone"] = int(state.get("milestone", 1)) + 1
            state["counters"]["rounds_in_milestone"] = 0
            milestone_pct = float(self.cfg.get("rollover_at_milestone_pct", 0.5)) * 100
            state["rollover"]["forced_roles"] = [
                role for role in ("planner", "reviewer", "coder")
                if float(state.get("session_ctx", {}).get(role, 0) or 0) >= milestone_pct
            ]
        state["last_route"] = {
            "base": base_target,
            "target": target,
            "brain": brain_route,
            "reason": stall_reason,
            "summary": summary,
            "round": state.get("round"),
        }
        state["phase"] = target
        self.store.save(state)
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
        if target == "DONE":
            self.journal.append("project.done", round_no=state.get("round"), detail={"summary": summary})
            self._notify("Relay finished", "The project reported PROJECT_DONE.")
        elif target == "PLAN" and not approval:
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
    ) -> str:
        state = self._state()
        existing = state.get("pending_decision")
        if state.get("phase") == "WAIT_HUMAN" and isinstance(existing, dict):
            return str(existing.get("id"))
        decision_id = f"D-{int(state.get('decisions', 0)) + 1:03d}"
        clean_options = self._clean_options(options)
        context_lines = [reason or question]
        last = state.get("last_sentinel") or {}
        if last.get("summary"):
            context_lines.append(f"last round: {last.get('summary')}")
        for line in self._read(".relay/MONITOR_NOTES.md", 2000).splitlines():
            if line.strip().startswith("- ["):
                context_lines.append(line.strip())
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
        state["decisions"] = int(state.get("decisions", 0)) + 1
        state["pending_decision"] = {
            "id": decision_id,
            "question": question,
            "options": clean_options,
            "round": int(state.get("round", 0)),
            "return_to": return_to or state.get("phase"),
            "asked_at": self.clock.now().isoformat(),
        }
        state["phase"] = "WAIT_HUMAN"
        state["return_to"] = state["pending_decision"]["return_to"]
        state["paused_reason"] = question
        state["notification"] = {
            "last_sent_at": self.clock.now().isoformat(),
            "count": int((state.get("notification") or {}).get("count", 0)) + 1,
        }
        self.store.set_baton("human", "human", int(state.get("round", 0)))
        self.store.save(state)
        self.journal.append(
            "decision.asked",
            state="WAIT_HUMAN",
            round_no=state.get("round"),
            detail={"id": decision_id, "question": question, "options": clean_options, "reason": reason},
        )
        self._notify(
            f"Relay needs a decision ({decision_id})",
            f"{decision_id}: {truncate(question, 220)}",
            decision_id=decision_id,
            urgent=True,
        )
        return decision_id

    def wait_for_decision(self) -> None:
        state = self._state()
        pending = state.get("pending_decision") or {}
        decision_id = pending.get("id")
        self.journal.append("decision.waiting", detail={"id": decision_id})
        reminder_seconds = float(self.cfg.get("decision_reminder_hours", 4)) * 3600
        last_reminder = self.clock.epoch()
        timeout_minutes = float(self.cfg.get("decision_wait_timeout_minutes", 0) or 0)
        deadline = self.clock.epoch() + timeout_minutes * 60 if timeout_minutes > 0 else None
        while True:
            self._check_wait_guards()
            state = self._state()
            if state.get("phase") != "WAIT_HUMAN":
                return
            if not state.get("pending_decision"):
                return
            answer = local_answer(self.paths)
            if answer:
                try:
                    self.answer_decision(answer)
                except StateError:
                    pass
                return
            if reminder_seconds > 0 and self.clock.epoch() - last_reminder >= reminder_seconds:
                self._notify(
                    f"Reminder: Relay is waiting ({decision_id})",
                    f"{decision_id}: {truncate(str((state.get('pending_decision') or {}).get('question', '')), 200)}",
                    decision_id=decision_id,
                    urgent=True,
                )
                self.journal.append("decision.reminder", detail={"id": decision_id})
                state["notification"]["last_sent_at"] = self.clock.now().isoformat()
                self.store.save(state)
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
        state = self._state()
        if state.get("phase") in ("PAUSED", "DONE", "HALTED"):
            return
        return_to = "WAIT_HUMAN" if state.get("phase") == "WAIT_HUMAN" else state.get("phase")
        state["phase"] = "PAUSED"
        state["return_to"] = return_to
        state["paused_reason"] = reason
        self.store.save(state)
        self.store.clear_baton()
        try:
            self.journal.append("state.paused", state="PAUSED", detail={"reason": reason, "return_to": return_to})
        except JournalError:
            pass
        self._notify("Relay paused", truncate(reason, 220))

    def _abort_turn(self, reason: str, phase: str | None) -> None:
        state = self._state()
        state["pending"] = None
        state["phase"] = "PAUSED"
        state["return_to"] = phase
        state["paused_reason"] = reason
        self.store.save(state)
        self.store.clear_baton()
        try:
            self.journal.append("state.paused", state="PAUSED", detail={"reason": reason, "aborted_turn": True})
        except JournalError:
            pass
        self._notify("Relay paused", truncate(reason, 220))

    def _notify(self, title: str, body: str, *, decision_id: str | None = None, urgent: bool = False) -> None:
        try:
            self.notifier.send(title, body, decision_id=decision_id, urgent=urgent)
        except Exception as exc:  # never let a notification failure stop the loop
            self.journal.append("notify.error", detail={"error": type(exc).__name__}, result="error")
