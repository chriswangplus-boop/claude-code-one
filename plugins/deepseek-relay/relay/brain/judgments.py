"""Typed DeepSeek judgment calls. Each is stateless and built fresh from files."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..clock import RealClock
from ..config import Config
from ..errors import BrainError
from ..journal import Journal
from ..util import truncate
from .client import BrainResult, CallContext, DeepSeekClient, Transport
from .prompts import fence, load_prompt, render

ROUTES = {"CODE", "REVIEW", "PLAN", "DONE", "NEEDS_HUMAN", "STUCK"}
SEVERITIES = {"note", "correct", "halt"}
ACTIONS = {"none", "replan", "rollover"}

VISION_SCHEMAS: dict[str, tuple[str, str]] = {
    "context_pct": (
        "Read the app's context/token usage indicator. If there is none, return null.",
        '{"context_pct": 0-100 or null, "confidence": 0.0-1.0}',
    ),
    "busy": (
        "Is the app still generating, running, or otherwise busy?",
        '{"busy": true|false, "confidence": 0.0-1.0}',
    ),
    "model_label": (
        "Read the selected model name and effort/reasoning label.",
        '{"model": "name or null", "effort": "low|medium|high or null", "matches": true|false}',
    ),
    "popup": (
        "Is a permission/approval dialog visible? Transcribe its title and button labels exactly.",
        '{"present": true|false, "text": "dialog text", "buttons": ["..."], '
        '"button_points": {"Button label": [x, y]}, "confidence": 0.0-1.0}',
    ),
    "pointer_sent": (
        "Was the typed RELAY pointer message submitted (visible as a sent user message, input now empty)?",
        '{"sent": true|false, "confidence": 0.0-1.0}',
    ),
    "empty_chat": (
        "Is this a new, empty chat session with no prior messages?",
        '{"empty": true|false, "confidence": 0.0-1.0}',
    ),
}

TRUNC = {
    "review": 12000,
    "handoff": 8000,
    "decisions": 4000,
    "history": 5000,
    "notes": 3000,
    "plan": 8000,
    "goal": 2000,
    "reviews": 12000,
}


@dataclass
class RouteDecision:
    route: str
    reason: str
    round_summary: str
    needs_human: dict | None
    drift_flags: list[str] = field(default_factory=list)
    call_id: str | None = None
    raw: dict = field(default_factory=dict)


class Brain:
    def __init__(
        self,
        cfg: Config,
        journal: Journal,
        *,
        api_key: str | None,
        transport: Transport | None = None,
        clock: RealClock | None = None,
        model: str | None = None,
        extra_secrets: list[str] | None = None,
        actor: str = "orchestrator",
    ):
        self.cfg = cfg
        self.journal = journal
        self.client = DeepSeekClient(
            cfg,
            journal,
            api_key=api_key,
            transport=transport,
            clock=clock,
            model=model,
            extra_secrets=extra_secrets,
            actor=actor,
        )

    # --- routing ----------------------------------------------------------
    def route(
        self,
        *,
        round_no: int,
        milestone: int,
        phase: str,
        sentinel: dict,
        review: str,
        handoff: str,
        history: str,
        monitor_notes: str,
        decisions: str,
    ) -> RouteDecision:
        system, user = load_prompt("route.md")
        values = {
            "round": round_no,
            "milestone": milestone,
            "phase": phase,
            "sentinel": fence(json.dumps(sentinel, indent=2), "sentinel"),
            "review": fence(truncate(review, TRUNC["review"]), "REVIEW.md"),
            "handoff": fence(truncate(handoff, TRUNC["handoff"]), "HANDOFF.md"),
            "history": fence(truncate(history, TRUNC["history"]), "round-summaries"),
            "monitor_notes": fence(truncate(monitor_notes, TRUNC["notes"]), "MONITOR_NOTES.md"),
            "decisions": fence(truncate(decisions, TRUNC["decisions"]), "DECISIONS.md"),
        }
        result = self.client.complete(CallContext(job="route", system=system, user=render(user, values)))
        return self._validate_route(result)

    def _validate_route(self, result: BrainResult) -> RouteDecision:
        data = result.content
        route = str(data.get("route", "")).upper()
        if route not in ROUTES:
            raise BrainError(f"route judgment returned invalid route: {route!r}")
        reason = str(data.get("reason") or "").strip()
        summary = str(data.get("round_summary") or reason).strip()
        flags = [str(flag) for flag in (data.get("drift_flags") or []) if str(flag).strip()]
        needs_human = data.get("needs_human")
        if needs_human is not None:
            needs_human = self._validate_question(needs_human)
        if route == "NEEDS_HUMAN" and needs_human is None:
            raise BrainError("route NEEDS_HUMAN must carry a needs_human question")
        return RouteDecision(
            route=route,
            reason=reason or "no reason given",
            round_summary=summary,
            needs_human=needs_human,
            drift_flags=flags,
            call_id=result.call_id,
            raw=data,
        )

    def _validate_question(self, value: Any) -> dict:
        if not isinstance(value, dict):
            raise BrainError("needs_human must be an object")
        question = str(value.get("question") or "").strip()
        if not question:
            raise BrainError("needs_human.question is empty")
        options = value.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            raise BrainError("needs_human.options must contain 2-4 options")
        clean = []
        for option in options:
            if not isinstance(option, dict):
                raise BrainError("each option must be an object")
            label = str(option.get("label") or "").strip()
            consequence = str(option.get("consequence") or "").strip()
            if not label or not consequence:
                raise BrainError("each option needs a label and a consequence")
            clean.append({"label": label, "consequence": consequence})
        return {"question": question, "options": clean}

    # --- vision -----------------------------------------------------------
    def vision(self, image: Path, kind: str, question: str | None = None) -> dict:
        if kind not in VISION_SCHEMAS:
            raise BrainError(f"unknown vision kind: {kind}")
        default_question, schema = VISION_SCHEMAS[kind]
        system, user = load_prompt("vision.md")
        values = {
            "task": kind,
            "question": question or default_question,
            "schema": schema,
        }
        result = self.client.complete(
            CallContext(job=f"vision.{kind}", system=system, user=render(user, values), image=image),
            max_tokens=500,
        )
        return self._normalise_vision(kind, result.content)

    def _normalise_vision(self, kind: str, data: dict) -> dict:
        if kind == "context_pct":
            raw = data.get("context_pct", data.get("value"))
            value = None
            if raw is not None:
                try:
                    value = max(0.0, min(100.0, float(raw)))
                except (TypeError, ValueError):
                    value = None
            return {"context_pct": value, "confidence": _confidence(data)}
        if kind == "busy":
            busy = data.get("busy", data.get("is_busy"))
            return {"busy": bool(busy), "confidence": _confidence(data)}
        if kind == "model_label":
            return {
                "model": data.get("model"),
                "effort": data.get("effort"),
                "matches": bool(data.get("matches")),
                "confidence": _confidence(data),
            }
        if kind == "popup":
            buttons = data.get("buttons") or []
            buttons = [str(b) for b in buttons if str(b).strip()]
            points_raw = data.get("button_points") or {}
            points: dict[str, list[float]] = {}
            if isinstance(points_raw, dict):
                for label, point in points_raw.items():
                    if isinstance(point, (list, tuple)) and len(point) == 2:
                        try:
                            points[str(label)] = [float(point[0]), float(point[1])]
                        except (TypeError, ValueError):
                            continue
            return {
                "present": bool(data.get("present")),
                "text": str(data.get("text") or ""),
                "buttons": buttons,
                "button_points": points,
                "confidence": _confidence(data),
            }
        if kind == "pointer_sent":
            return {"sent": bool(data.get("sent")), "confidence": _confidence(data)}
        if kind == "empty_chat":
            return {"empty": bool(data.get("empty")), "confidence": _confidence(data)}
        return data

    # --- monitor ----------------------------------------------------------
    def monitor_review(
        self,
        *,
        goal: str,
        plan: str,
        milestone: int,
        history: str,
        reviews: str,
        notes: str,
        events: str,
    ) -> dict:
        system, user = load_prompt("monitor.md")
        values = {
            "goal": fence(truncate(goal, TRUNC["goal"]), "GOAL.md"),
            "plan": fence(truncate(plan, TRUNC["plan"]), "PLAN.md"),
            "milestone": milestone,
            "history": fence(truncate(history, TRUNC["history"]), "round-summaries"),
            "reviews": fence(truncate(reviews, TRUNC["reviews"]), "REVIEW.md"),
            "notes": fence(truncate(notes, TRUNC["notes"]), "MONITOR_NOTES.md"),
            "events": fence(truncate(events, TRUNC["history"]), "journal-events"),
        }
        result = self.client.complete(CallContext(job="monitor", system=system, user=render(user, values)))
        return self._validate_monitor(result.content, result.call_id)

    def _validate_monitor(self, data: dict, call_id: str) -> dict:
        try:
            score = max(0.0, min(100.0, float(data.get("score", 0))))
        except (TypeError, ValueError):
            raise BrainError("monitor score is not a number")
        verdict = str(data.get("verdict") or "ok")
        findings = []
        for finding in data.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            severity = str(finding.get("severity") or "note")
            action = str(finding.get("action") or "none")
            if severity not in SEVERITIES or action not in ACTIONS:
                continue
            findings.append({
                "severity": severity,
                "kind": str(finding.get("kind") or "other"),
                "note": str(finding.get("note") or "").strip(),
                "action": action,
            })
        return {
            "score": score,
            "verdict": verdict,
            "summary": str(data.get("summary") or "").strip(),
            "findings": findings,
            "call_id": call_id,
        }


def _confidence(data: dict) -> float:
    try:
        return max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        return 0.5
