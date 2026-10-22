"""One-time codes for human-only CLI actions (review #4, R4-1).

`relay resume --clear-halt` silences a monitor safety stop, and `--adopt-baseline` accepts
whatever files are on disk as a turn's baseline. Both must be proven human, and a journal
signature is not proof: the agent runs Relay's own code and can read the HMAC key. The code is
delivered the same way as a decision code (phone with an ntfy access token, or an interactive
terminal) and is stored only as a hash, outside the project, for one use.
"""

from __future__ import annotations

import hmac
import sys

from .config import ntfy_token, ntfy_topic
from .context import Context
from .decisions import hash_code, new_code
from .errors import StateError

CODE_TTL_SECONDS = 900.0


def _channels(ctx: Context) -> dict[str, bool]:
    phone = bool(
        bool(ctx.cfg.get("notify.ntfy", True))
        and (ctx.cfg.get("ntfy.token") or ntfy_token())
        and (ctx.cfg.get("ntfy.topic") or ntfy_topic())
    )
    return {"tty": bool(sys.stdout.isatty()), "phone": phone}


def issue_action_code(ctx: Context, action: str, *, question: str, notifier=None) -> dict:
    """Send a fresh one-time code and store only its hash. Raises when it cannot be delivered."""
    channels = _channels(ctx)
    if not (channels["tty"] or channels["phone"]):
        ctx.journal.append(
            "action.code_undelivered", detail={"action": action, "channels": channels},
            result="warn",
        )
        raise StateError(
            "cannot deliver a one-time code: configure an ntfy access token (or run from an "
            "interactive terminal). Refusing to perform a human-only action without proof."
        )
    code = new_code()
    ctx.trust.store_action_code(
        action, hash_code(code), ttl=CODE_TTL_SECONDS, now=ctx.clock.epoch()
    )
    if channels["tty"]:
        print(f"One-time code for {action}: {code}", file=sys.stderr)
    if channels["phone"] and notifier is not None:
        notifier.send(
            f"Relay: one-time code for {action}",
            question,
            urgent=True,
            ntfy_body=f"{question}. One-time code: {code}",
        )
    ctx.journal.append(
        "action.code_issued", detail={"action": action, "channels": channels}, result="ok"
    )
    return channels


def consume_action_code(ctx: Context, action: str, code: str) -> None:
    """Check and burn the one-time code for an action."""
    entry = ctx.trust.action_code(action)
    if entry is None:
        raise StateError(
            f"no pending one-time code for {action}; run the command once without --code"
        )
    if float(entry.get("expires") or 0) < ctx.clock.epoch():
        ctx.trust.clear_action_code(action)
        raise StateError(f"the one-time code for {action} expired; run the command again")
    expected = str(entry.get("hash") or "")
    if not code or not expected or not hmac.compare_digest(hash_code(code), expected):
        raise StateError(f"invalid one-time code for {action}")
    ctx.trust.clear_action_code(action)
    ctx.journal.append("action.code_used", detail={"action": action}, result="ok")


def require_action_code(ctx: Context, action: str, code: str | None, *, question: str, notifier=None) -> None:
    """No code -> issue one and refuse this attempt. With a code -> verify it."""
    if code:
        consume_action_code(ctx, action, code)
        return
    issue_action_code(ctx, action, question=question, notifier=notifier)
    raise StateError(
        f"a one-time code for {action} was sent to the phone (or printed in the terminal). "
        "Re-run the same command with `--code <code>`."
    )
