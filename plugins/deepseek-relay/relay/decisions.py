"""Human decision files: PENDING_DECISION.md, DECISIONS.md, and local answers only."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import string
import subprocess
import sys
from pathlib import Path

from .clock import utc_iso
from .context import Context
from .errors import StateError
from .paths import Paths
from .state import atomic_write_text
from .util import truncate

PLACEHOLDER = re.compile(r"^<.*>$")
CODE_ALPHABET = string.ascii_lowercase


def new_code() -> str:
    """A high-entropy code with one character class, so the outbound redactor won't mangle it."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(16))


def hash_code(code: str) -> str:
    return hashlib.sha256(str(code).encode("utf-8")).hexdigest()


def _escape_applescript(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def confirm_human(prompt: str) -> bool:
    """A local human gate for `relay instruct`.

    Always the native macOS dialog, never stdin: any agent can hand a command a pseudo-TTY
    (`echo y | script -qc "relay instruct ..." /dev/null`) and answer a y/N prompt, which would
    let it record an instruction as the human (review #2 N1, path B). If macOS cannot show the
    dialog, the instruction is refused.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False  # tests must never open a macOS dialog
    if sys.platform != "darwin":
        return False
    script = (
        'display dialog "' + _escape_applescript(prompt)[:400] + '" '
        'buttons {"Cancel", "Allow"} default button "Cancel" '
        'with title "Relay human confirmation" with icon caution'
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=120, check=False
    )
    return result.returncode == 0 and "button returned:Allow" in result.stdout


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
        "The one-time answer code is shown in the desktop notification and terminal, never here.",
        "Write it first: `ANSWER: <code> <your answer>`.",
        "Or run: `relay decide <id> --code <code> \"your answer\"`.",
        "",
        "ANSWER: ",
    ])
    atomic_write_text(paths.pending_decision, "\n".join(lines) + "\n")


def local_answer(paths: Paths) -> tuple[str, str] | None:
    """Return (code, answer) from the ANSWER line, or None."""
    if not paths.pending_decision.exists():
        return None
    text = paths.pending_decision.read_text(encoding="utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.upper().startswith("ANSWER:"):
            value = stripped.split(":", 1)[1].strip()
            if not value or PLACEHOLDER.match(value):
                return None
            parts = value.split(None, 1)
            if len(parts) != 2:
                return None
            return parts[0], parts[1].strip()
    return None


def accept_decision(ctx: Context, answer: str, code: str, by: str = "human") -> dict:
    """Record a human answer after checking the one-time code."""
    with ctx.lock:
        return _accept_locked(ctx, answer, code, by)


def record_instruction(
    ctx: Context, text: str, by: str = "human", *, confirmed: bool = False, opus: bool = False,
) -> dict:
    """Record a free-form human instruction. Both agents read DECISIONS.md every turn.

    `opus=True` (`relay instruct --opus`) also arms one Opus turn. It counts toward the limits.
    """
    if not confirmed:
        raise StateError("a human confirmation is required to record an instruction")
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
        if opus:
            state["human_opus_request"] = {
                "source": decision_id, "at_round": int(state.get("round", 0)),
            }
        ctx.store.save(state)
        if opus:
            ctx.journal.append(
                "model.requested", actor=by, round_no=int(state.get("round", 0)),
                detail={"source": decision_id, "via": "relay instruct --opus"},
            )
        return {"id": decision_id, "text": text, "event_id": event["id"], "phase": state.get("phase"),
                "opus": opus}


def _accept_locked(ctx: Context, answer: str, code: str, by: str) -> dict:
    state = ctx.store.load()
    pending = state.get("pending_decision")
    if not isinstance(pending, dict):
        raise StateError("there is no pending decision to answer")
    decision_id = str(pending.get("id") or "")
    if not decision_id:
        raise StateError("the pending decision has no id; re-ask it with `relay resume`")
    # The expected hash lives in the external trust store, never in the project (N1).
    expected = ctx.trust.code_hash(decision_id)
    if not expected:
        raise StateError(
            "this pending decision has no external one-time code; re-ask it with `relay resume`"
        )
    if not code or not hmac.compare_digest(hash_code(code), expected):
        raise StateError("invalid one-time code")
    answer = _one_line(answer, 2000)
    if not answer:
        raise StateError("the answer is empty")

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
    ctx.trust.clear_code(decision_id)
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
        "kind": pending.get("kind"),
        "options": pending.get("options") or [],
        "data": pending.get("data") or {},
    }
    ctx.store.set_baton("none", None, int(state.get("round", 0)))
    ctx.store.save(state)
    return {"id": decision_id, "answer": answer, "event_id": event["id"], "phase": return_to}
