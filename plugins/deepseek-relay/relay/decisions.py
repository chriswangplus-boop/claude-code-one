"""Human decision files: PENDING_DECISION.md, DECISIONS.md, and local answers only."""

from __future__ import annotations

import re
from pathlib import Path

from .clock import utc_iso
from .context import Context
from .errors import StateError
from .paths import Paths
from .state import atomic_write_text
from .util import truncate

PLACEHOLDER = re.compile(r"^<.*>$")


def _one_line(text: str, limit: int = 240) -> str:
    return truncate(" ".join(str(text).split()), limit, marker="...")


def write_pending(
    paths: Paths,
    *,
    decision_id: str,
    question: str,
    context_lines: list[str],
    options: list[dict],
    suggestion: str | None,
    round_no: int,
    reason: str,
    clock,
) -> None:
    lines = [
        f"# {decision_id} - {_one_line(question)}",
        "",
        f"- round: {round_no}",
        f"- asked: {utc_iso(clock)}",
        f"- reason: {_one_line(reason)}",
        "",
        "## Context",
    ]
    for item in context_lines[:10]:
        lines.append(f"- {_one_line(item)}")
    if not context_lines:
        lines.append("- (none supplied)")
    lines.extend(["", "## Options"])
    for index, option in enumerate(options):
        label = chr(ord("A") + index)
        lines.append(f"- **{label}** {_one_line(option.get('label', ''))}: {_one_line(option.get('consequence', ''))}")
    if suggestion:
        lines.extend(["", f"> Agent suggestion (not a decision): {_one_line(suggestion)}"])
    lines.extend([
        "",
        "## Answer",
        "",
        "Write your answer after `ANSWER:` (or run `relay decide <id> \"your answer\"`).",
        "",
        "ANSWER: ",
    ])
    atomic_write_text(paths.pending_decision, "\n".join(lines) + "\n")


def local_answer(paths: Paths) -> str | None:
    if not paths.pending_decision.exists():
        return None
    text = paths.pending_decision.read_text(encoding="utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.upper().startswith("ANSWER:"):
            value = stripped.split(":", 1)[1].strip()
            if value and not PLACEHOLDER.match(value):
                return value
    return None


def accept_decision(ctx: Context, answer: str, by: str = "human") -> dict:
    """Record a human answer. Used by both `relay decide` and the running orchestrator."""
    with ctx.lock:
        return _accept_locked(ctx, answer, by)


def record_instruction(ctx: Context, text: str, by: str = "human") -> dict:
    """Record a free-form human instruction. Both agents read DECISIONS.md every turn."""
    text = _one_line(text, 4000)
    if not text:
        raise StateError("the instruction is empty")
    with ctx.lock:
        state = ctx.store.load()
        if state.get("phase") == "DONE":
            raise StateError("the project is already DONE; start a new project or reset STATE.json")
        decision_id = f"D-{int(state.get('decisions', 0)) + 1:03d}"
        event = ctx.journal.append(
            "decision.answered",
            actor=by,
            round_no=int(state.get("round", 0)),
            state=str(state.get("phase")),
            detail={"id": decision_id, "answer": text, "source": "host-instruction"},
            result="ok",
        )
        entry = [
            "",
            f"## {decision_id} - Host instruction",
            f"- round: {state.get('round', 0)}",
            f"- issued: {utc_iso(ctx.clock)}",
            f"- instruction: {text}",
            f"- evidence: decision.answered {event['id']} (actor {by})",
            "",
        ]
        with open(ctx.paths.decisions, "a", encoding="utf-8") as fh:
            fh.write("\n".join(entry))
            fh.flush()
        state["decisions"] = int(state.get("decisions", 0)) + 1
        state["last_instruction"] = {
            "id": decision_id,
            "text": text,
            "event_id": event["id"],
            "at": utc_iso(ctx.clock),
        }
        ctx.store.save(state)
        return {"id": decision_id, "text": text, "event_id": event["id"], "phase": state.get("phase")}


def _accept_locked(ctx: Context, answer: str, by: str) -> dict:
    state = ctx.store.load()
    pending = state.get("pending_decision")
    if not isinstance(pending, dict):
        raise StateError("there is no pending decision to answer")
    answer = _one_line(answer, 2000)
    if not answer:
        raise StateError("the answer is empty")

    decision_id = str(pending["id"])
    event = ctx.journal.append(
        "decision.answered",
        actor=by,
        round_no=int(state.get("round", 0)),
        state="WAIT_HUMAN",
        detail={"id": decision_id, "answer": answer, "source": "local"},
        result="ok",
    )
    entry = [
        "",
        f"## {decision_id} - {_one_line(pending.get('question', ''))}",
        f"- round: {pending.get('round', 0)}",
        f"- answered: {utc_iso(ctx.clock)}",
        f"- answer: {answer}",
        f"- evidence: decision.answered {event['id']} (actor {by})",
        "",
    ]
    with ctx.lock:
        with open(ctx.paths.decisions, "a", encoding="utf-8") as fh:
            fh.write("\n".join(entry))
            fh.flush()
    atomic_write_text(ctx.paths.answers_dir / f"{decision_id}.txt", answer + "\n")
    try:
        ctx.paths.pending_decision.unlink()
    except FileNotFoundError:
        pass

    return_to = pending.get("return_to") or "PLAN"
    state["phase"] = return_to
    state["pending_decision"] = None
    state["paused_reason"] = None
    state["return_to"] = None
    state["last_decision"] = {
        "id": decision_id,
        "answer": answer,
        "event_id": event["id"],
        "answered_at": utc_iso(ctx.clock),
        "return_to": return_to,
        "round": int(state.get("round", 0)),
    }
    ctx.store.set_baton("none", None, int(state.get("round", 0)))
    ctx.store.save(state)
    return {"id": decision_id, "answer": answer, "event_id": event["id"], "phase": return_to}
