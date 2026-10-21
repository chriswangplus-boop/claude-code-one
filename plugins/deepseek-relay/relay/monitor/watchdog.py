"""Independent watchdog. Reads the journal and .relay/, writes notes + HALT, never clicks or types."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..brain import Brain
from ..config import Config
from ..context import Context
from ..errors import BrainError
from ..redact import redact
from ..state import atomic_write_json, read_json
from ..util import sha256_file, truncate

DECISION_HEADING = re.compile(r"^##\s+(D-\d+)\b.*$", re.MULTILINE)
EVIDENCE = re.compile(r"evidence:\s*decision\.answered\s+(evt-\d{8}-\d+)")
ACTIVE_PHASES = {"PLAN", "CODE", "REVIEW", "ROUTE"}


@dataclass
class Finding:
    code: str
    message: str
    severity: str  # halt | correct | note
    detail: dict[str, Any] = field(default_factory=dict)


def _event_index(events: list[dict]) -> dict[str, dict]:
    return {str(event.get("id")): event for event in events}


def check_invariants(
    cfg: Config,
    paths,
    state: dict,
    baton: dict,
    events: list[dict],
    *,
    journal_ok: bool = True,
    journal_errors: list[str] | None = None,
    heartbeat_age: float | None = None,
) -> list[Finding]:
    """Layer 1: everything here is deterministic and needs no model."""
    findings: list[Finding] = []
    journal_errors = journal_errors or []
    phase = str(state.get("phase") or "INIT")
    active = phase in ACTIVE_PHASES

    if not journal_ok:
        findings.append(Finding(
            "journal_chain", f"journal hash chain is broken: {journal_errors[:2]}", "halt",
            {"errors": journal_errors[:10]},
        ))

    for event in events:
        if event.get("type") == "integrity.violation":
            findings.append(Finding(
                "ownership", f"integrity violation at {event.get('id')}: {event.get('detail')}", "halt",
                {"event": event.get("id")},
            ))
        if event.get("type") == "git.push" or (
            event.get("type") == "git.commit" and "push" in str(event.get("detail") or "").lower()
        ):
            findings.append(Finding("no_push", f"git push attempted at {event.get('id')}", "halt"))

    # decisions must be human-authored and linked to a journal event
    if paths.decisions.exists():
        text = paths.decisions.read_text(encoding="utf-8", errors="replace")
        headings = list(DECISION_HEADING.finditer(text))
        index = _event_index(events)
        for position, match in enumerate(headings):
            end = headings[position + 1].start() if position + 1 < len(headings) else len(text)
            block = text[match.start():end]
            evidence = EVIDENCE.search(block)
            if not evidence:
                findings.append(Finding(
                    "decision_authorship", f"{match.group(1)} has no decision.answered evidence", "halt",
                    {"decision": match.group(1)},
                ))
                continue
            event = index.get(evidence.group(1))
            if not event or event.get("type") != "decision.answered" or event.get("actor") != "human":
                findings.append(Finding(
                    "decision_authorship",
                    f"{match.group(1)} cites {evidence.group(1)} which is not a human decision.answered event",
                    "halt", {"decision": match.group(1)},
                ))

    # the sentinel's announced file must still match what it announced
    last = state.get("last_sentinel") or {}
    announced = last.get("file")
    if announced:
        target = cfg.project_dir / str(announced)
        if not target.exists():
            findings.append(Finding("sentinel_file", f"announced file is gone: {announced}", "halt"))
        elif last.get("sha256") and sha256_file(target) != last.get("sha256"):
            findings.append(Finding("sentinel_sha", f"{announced} changed after its sentinel", "halt"))

    # rollover must have happened when the context signal was high
    threshold = float(cfg.get("rollover_threshold", 0.7)) * 100
    for index, event in enumerate(events):
        if event.get("type") != "turn.end":
            continue
        ctx_pct = (event.get("detail") or {}).get("ctx_pct")
        try:
            high = float(ctx_pct) >= threshold
        except (TypeError, ValueError):
            high = False
        if not high:
            continue
        role = (event.get("detail") or {}).get("role")
        for later in range(index + 1, len(events)):
            candidate = events[later]
            if candidate.get("type") == "turn.start" and (candidate.get("detail") or {}).get("role") == role:
                rolled = any(
                    (events[k].get("type") == "rollover"
                     and (events[k].get("detail") or {}).get("role") == role)
                    or (events[k].get("type") == "session.open"
                        and (events[k].get("detail") or {}).get("role") == role)
                    for k in range(index + 1, later)
                )
                if not rolled:
                    findings.append(Finding(
                        "missed_rollover",
                        f"{role} continued after {ctx_pct}% context without a rollover",
                        "halt", {"round": event.get("round"), "role": role},
                    ))
                break

    # context guard and budget
    guard = float(cfg.get("deepseek.context_guard", 0.7))
    for index, event in enumerate(events):
        if event.get("type") == "deepseek.call":
            ctx_pct = float((event.get("deepseek") or {}).get("ctx_pct") or 0)
            if ctx_pct >= guard and phase != "PAUSED":
                followed = any(
                    events[k].get("type") in ("guard.tripped", "state.paused")
                    for k in range(index + 1, min(index + 4, len(events)))
                )
                if not followed:
                    findings.append(Finding(
                        "context_guard", f"DeepSeek call {event.get('deepseek', {}).get('call_id')} at {ctx_pct:.0%} was not followed by a pause",
                        "halt",
                    ))
        if event.get("type") == "guard.tripped" and phase != "PAUSED":
            if not any(e.get("type") == "state.paused" for e in events[index + 1:index + 5]):
                findings.append(Finding("guard_unhandled", f"guard tripped but never paused ({event.get('id')})", "halt"))

    counters = state.get("counters") or {}
    if phase not in ("PAUSED", "DONE"):
        if int(counters.get("total_rounds", 0)) > int(cfg.get("max_total_rounds", 60)):
            findings.append(Finding("round_cap", "total round cap exceeded while running", "halt"))
        if int(counters.get("rounds_in_milestone", 0)) > int(cfg.get("max_rounds_per_milestone", 8)):
            findings.append(Finding("milestone_cap", "milestone round cap exceeded while running", "halt"))
        budget = float(cfg.get("daily_budget_usd", 0) or 0)
        today = datetime.now(timezone.utc).astimezone().date().isoformat().replace("-", "")
        spent = sum(
            float((e.get("deepseek") or {}).get("cost_usd") or 0)
            for e in events
            if e.get("type") == "deepseek.call" and str(e.get("id", "")).startswith(f"evt-{today}-")
        )
        if budget > 0 and spent > budget:
            findings.append(Finding("budget_cap", f"daily budget exceeded (${spent:.2f} > ${budget:.2f})", "halt"))

    if active:
        stale_after = float(cfg.get("monitor.heartbeat_stale_seconds", 300))
        if heartbeat_age is None or heartbeat_age > stale_after:
            findings.append(Finding(
                "orchestrator_heartbeat", "orchestrator heartbeat is missing or stale while a step is active", "halt",
                {"age": heartbeat_age},
            ))

    # baton must match the open activity
    open_turn_role: str | None = None
    open_rollover = False
    for event in events:
        if event.get("type") == "turn.start":
            open_turn_role = (event.get("detail") or {}).get("role")
        elif event.get("type") == "turn.end":
            open_turn_role = None
        elif event.get("type") == "rollover.start":
            open_rollover = True
        elif event.get("type") == "rollover":
            open_rollover = False
    holder = baton.get("holder", "none")
    pending = state.get("pending") or {}
    expected = pending.get("role") if isinstance(pending, dict) else None
    if active:
        if expected and holder not in (expected, "human"):
            findings.append(Finding(
                "baton", f"baton is {holder!r} but the pending turn belongs to {expected!r}", "halt",
            ))
        elif open_turn_role and holder not in (open_turn_role, "human"):
            findings.append(Finding(
                "baton", f"baton is {holder!r} but the open turn belongs to {open_turn_role!r}", "halt",
            ))
        elif not open_turn_role and not open_rollover and holder not in ("none", "human"):
            last_type = events[-1].get("type") if events else None
            if last_type != "turn.end":  # a turn just closing is not a stuck baton
                findings.append(Finding("baton", f"baton is held by {holder!r} with no open turn", "halt"))

    # keystrokes must go to the app that holds the baton
    app_for_role = {"planner": "claude", "reviewer": "claude", "coder": "coder"}
    if open_turn_role:
        last_ring = None
        for event in events:
            if event.get("type") == "turn.start":
                last_ring = None
            elif event.get("type") == "gui.ring":
                last_ring = event
        if last_ring and (last_ring.get("detail") or {}).get("app") not in (None, app_for_role.get(open_turn_role)):
            findings.append(Finding(
                "wrong_app",
                f"keystrokes went to {(last_ring.get('detail') or {}).get('app')!r} during a {open_turn_role} turn",
                "halt",
            ))

    # repeated review feedback: three identical review artifacts is a loop
    review_hashes = []
    for event in events:
        if event.get("type") != "turn.end" or (event.get("detail") or {}).get("role") != "reviewer":
            continue
        for output in event.get("outputs") or []:
            if str(output.get("path", "")).endswith("REVIEW.md"):
                review_hashes.append(output.get("sha256"))
    recent = review_hashes[-3:]
    if len(recent) == 3 and len(set(recent)) == 1:
        findings.append(Finding(
            "repeated_feedback",
            "the last three reviews are byte-identical; the loop is not making progress",
            "correct", {"action": "replan"},
        ))

    return findings


class Monitor:
    def __init__(
        self,
        ctx: Context,
        *,
        brain: Brain | None = None,
        notifier: Any = None,
        wall=None,
    ):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.paths = ctx.paths
        self.store = ctx.store
        self.journal = ctx.journal
        self.clock = ctx.clock
        self.brain = brain
        self.notifier = notifier
        self.wall = wall or time.time
        self._layer2_failures = 0
        self._last_halt_key: str | None = None

    def check_once(self) -> dict:
        self.store.heartbeat(self.paths.monitor_heartbeat, actor="monitor", cycle="check")
        try:
            from ..integrity import prune_screens

            prune_screens(
                self.paths.screens_dir, int(self.cfg.get("screenshot_retention_days", 14)), self.clock.epoch()
            )
        except OSError:
            pass
        try:
            journal_ok, journal_errors = self.journal.verify()
            events = self.journal.all_events()
        except Exception as exc:  # malformed journal is itself a finding
            journal_ok = False
            journal_errors = [f"{type(exc).__name__}: {exc}"]
            try:
                events = self.journal.all_events() if False else list(self.journal.events(strict=False))
            except Exception:
                events = []
        state_error = None
        try:
            state = self.store.load()
        except Exception as exc:
            state = {"phase": "INIT"}
            state_error = f"{type(exc).__name__}: {exc}"
        baton = self.store.baton()
        age = self.store.heartbeat_age(self.paths.heartbeat)

        findings = check_invariants(
            self.cfg, self.paths, state, baton, events,
            journal_ok=journal_ok, journal_errors=journal_errors, heartbeat_age=age,
        )
        if state_error:
            findings.append(Finding("state_unreadable", f"STATE.json cannot be read: {state_error}", "halt"))
        halts = [f for f in findings if f.severity == "halt"]
        corrections = [f for f in findings if f.severity == "correct"]
        notes = [f for f in findings if f.severity == "note"]

        if halts:
            self._halt(halts, state)
            result = {"halted": True, "findings": [f.code for f in findings]}
        else:
            for finding in notes:
                self._note(finding, state)
            if corrections:
                self._correct(corrections, state)
            result = {"halted": False, "findings": [f.code for f in findings]}

        layer2 = None
        if not halts and bool(self.cfg.get("monitor.layer2", True)) and self.brain is not None:
            layer2 = self._layer2(events, state)
            if layer2 and layer2.get("halted"):
                result["halted"] = True
        self.journal.append(
            "monitor.check",
            actor="monitor",
            state=str(state.get("phase")),
            round_no=int(state.get("round") or 0),
            detail={
                "findings": [{"code": f.code, "severity": f.severity} for f in findings],
                "layer2": layer2,
                "journal_ok": journal_ok,
                "heartbeat_age": age,
            },
            result="halt" if result.get("halted") else "ok",
        )
        return result

    def run_forever(self, poll_seconds: float = 10.0) -> None:
        interval = float(self.cfg.get("monitor.interval_minutes", 10)) * 60
        last_check = 0.0
        last_signature = None
        while self.store.read_flag("stop") is None:
            self.store.heartbeat(self.paths.monitor_heartbeat, actor="monitor", cycle="alive")
            signature = self._journal_signature()
            due = self.clock.epoch() - last_check >= interval
            if signature != last_signature or due:
                try:
                    self.check_once()
                except Exception as exc:
                    self.journal.append(
                        "monitor.error", actor="monitor",
                        detail={"error": f"{type(exc).__name__}: {exc}"}, result="error",
                    )
                last_check = self.clock.epoch()
                last_signature = self._journal_signature()
            self.clock.sleep(poll_seconds)

    # --- actions ----------------------------------------------------------
    def _halt(self, findings: list[Finding], state: dict) -> None:
        reason = "; ".join(finding.message for finding in findings)
        key = "|".join(sorted(finding.code for finding in findings)) + "|" + reason[:200]
        if key == self._last_halt_key:
            return
        self._last_halt_key = key
        self.store.set_flag("halt", {
            "reason": reason,
            "findings": [{"code": f.code, "message": f.message, "detail": f.detail} for f in findings],
            "by": "monitor",
        })
        for finding in findings:
            self._note(finding, state)
        self.journal.append(
            "monitor.halt", actor="monitor", state=str(state.get("phase")),
            round_no=int(state.get("round") or 0),
            detail={"reason": reason, "codes": [f.code for f in findings]}, result="halt",
        )
        if self.notifier:
            try:
                self.notifier.send(
                    "Relay monitor halted the loop",
                    truncate(reason, 240), urgent=True,
                )
            except Exception:
                pass

    def _correct(self, findings: list[Finding], state: dict) -> None:
        action = "replan"
        for finding in findings:
            candidate = (finding.detail or {}).get("action")
            if candidate in ("replan", "rollover"):
                action = candidate
                break
        if self.paths.correction.exists():
            return
        payload = {
            "action": action,
            "reason": "; ".join(f.message for f in findings),
            "codes": [f.code for f in findings],
            "created_at": self.clock.now().isoformat(),
        }
        atomic_write_json(self.paths.correction, payload)
        for finding in findings:
            self._note(finding, state)
        self.journal.append(
            "monitor.correct", actor="monitor", state=str(state.get("phase")),
            round_no=int(state.get("round") or 0),
            detail={"action": action, "codes": [f.code for f in findings], "reason": payload["reason"]},
        )

    def _note(self, finding: Finding, state: dict) -> None:
        message = redact(finding.message).text
        line = f"- [{finding.severity}] {message} (round {state.get('round', 0)})\n"
        try:
            existing = self.paths.monitor_notes.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            existing = "# Monitor notes\n\n"
        if line.strip() not in existing:
            with self.ctx.lock:
                with open(self.paths.monitor_notes, "a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
        self.journal.append(
            "monitor.note", actor="monitor", state=str(state.get("phase")),
            round_no=int(state.get("round") or 0),
            detail={"code": finding.code, "severity": finding.severity, "text": finding.message},
        )

    def _layer2(self, events: list[dict], state: dict) -> dict | None:
        turn_ends = [e for e in events if e.get("type") == "turn.end"]
        if not turn_ends:
            return None
        last_id = turn_ends[-1].get("id")
        checkpoint = self.paths.monitor_dir / "layer2.json"
        previous = read_json(checkpoint, default={}) or {}
        if previous.get("last_event_id") == last_id:
            return None
        try:
            goal = self._read(".relay/GOAL.md", 2000)
            plan = self._read(".relay/PLAN.md", 8000)
            reviews = "\n\n".join(
                f"round {e.get('round')}: " + str((e.get("detail") or {}).get("summary") or "")
                for e in turn_ends[-3:]
            )
            reviews += "\n\nLATEST REVIEW:\n" + self._read(".relay/REVIEW.md", 4000)
            history = "\n".join(
                f"round {item.get('round')} {item.get('role')}: {item.get('verdict')} - {item.get('summary')}"
                for item in (state.get("history") or [])[-3:]
            )
            events_text = "\n".join(
                f"{e.get('id')} {e.get('actor')} {e.get('type')} r{e.get('round')}"
                for e in events[-20:]
            )
            review = self.brain.monitor_review(
                goal=goal, plan=plan, milestone=int(state.get("milestone", 1)),
                history=history or "none", reviews=reviews or self._read(".relay/REVIEW.md", 4000),
                notes=self._read(".relay/MONITOR_NOTES.md", 3000), events=events_text,
            )
        except Exception as exc:
            self._layer2_failures += 1
            self.journal.append(
                "monitor.error", actor="monitor",
                detail={"error": f"{type(exc).__name__}: {exc}", "failures": self._layer2_failures},
                result="error",
            )
            if self._layer2_failures >= 3:
                self._halt([Finding("monitor_blind", "the monitor's Layer 2 failed three times", "halt")], state)
                return {"halted": True}
            return None

        self._layer2_failures = 0
        findings = []
        for item in review.get("findings") or []:
            severity = item.get("severity")
            if severity not in ("note", "correct", "halt"):
                continue
            findings.append(Finding(
                f"layer2:{item.get('kind')}", item.get("note") or review.get("summary") or "monitor finding",
                severity, {"action": item.get("action")},
            ))
        if findings and any(f.severity == "halt" for f in findings):
            self._halt([f for f in findings if f.severity == "halt"], state)
            halted = True
        else:
            halted = False
            for finding in findings:
                if finding.severity == "correct":
                    self._correct([finding], state)
                else:
                    self._note(finding, state)
        atomic_write_json(checkpoint, {"last_event_id": last_id})
        self.journal.append(
            "monitor.layer2", actor="monitor", state=str(state.get("phase")),
            round_no=int(state.get("round") or 0),
            detail={
                "score": review.get("score"), "verdict": review.get("verdict"),
                "summary": review.get("summary"), "call_id": review.get("call_id"),
                "findings": [{"code": f.code, "severity": f.severity} for f in findings],
            },
            result="halt" if halted else "ok",
        )
        return {"score": review.get("score"), "verdict": review.get("verdict"), "halted": halted}

    # --- helpers ----------------------------------------------------------
    def _read(self, rel: str, limit: int) -> str:
        path = self.cfg.project_dir / rel
        if not path.exists():
            return ""
        return truncate(path.read_text(encoding="utf-8", errors="replace"), limit)

    def _journal_signature(self):
        try:
            files = sorted(self.paths.journal_dir.glob("*.jsonl"))
            return (len(files), sum(path.stat().st_mtime_ns for path in files), sum(path.stat().st_size for path in files))
        except OSError:
            return None
