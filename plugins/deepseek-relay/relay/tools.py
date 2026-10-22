"""Investigation tools: trace, why, and replay."""

from __future__ import annotations

import json
from datetime import datetime

from .brain.client import UrllibTransport
from .config import api_key
from .context import Context
from .errors import BrainError, ConfigError, RelayError
from .util import parse_json_lenient, truncate


def _local_time(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(ts)


def summarize_event(event: dict) -> str:
    detail = event.get("detail") or {}
    for key in ("role", "status", "verdict", "reason", "message", "question", "id", "kind", "text", "note", "target"):
        value = detail.get(key)
        if value:
            return truncate(str(value), 140, marker="...")
    if detail:
        return truncate(json.dumps(detail, sort_keys=True), 140, marker="...")
    return ""


def trace(ctx: Context, *, round_no: int | None = None, verify: bool = False, limit: int = 300, event_type: str | None = None) -> str:
    events = list(ctx.journal.events())
    if round_no is not None:
        events = [event for event in events if int(event.get("round", -1)) == round_no]
    if event_type:
        events = [event for event in events if event.get("type") == event_type]
    lines = []
    if verify:
        ok, errors = ctx.journal.verify()
        lines.append(f"journal chain: {'OK' if ok else 'BROKEN'}")
        for error in errors[:20]:
            lines.append(f"  ! {error}")
        lines.append("")
    current_round = None
    for event in events[-limit:]:
        if event.get("round") != current_round:
            current_round = event.get("round")
            lines.append(f"round {current_round}  state={event.get('state')}")
        lines.append(
            f"  {event.get('id')}  {_local_time(str(event.get('ts')))[11:]}  "
            f"{str(event.get('actor')):12}  {str(event.get('type')):26}  {summarize_event(event)}"
        )
    if not events:
        lines.append("(no events)")
    return "\n".join(lines)


def why(ctx: Context, event_id: str, *, context: int = 2) -> str:
    events = ctx.journal.all_events()
    index = next((i for i, event in enumerate(events) if event.get("id") == event_id), None)
    if index is None:
        raise RelayError(f"no such event: {event_id}")
    event = events[index]
    lines = [f"EVENT {event_id}", json.dumps(event, indent=2, ensure_ascii=False), ""]
    start = max(0, index - context)
    end = min(len(events), index + context + 1)
    lines.append("NEARBY")
    for neighbour in events[start:end]:
        lines.append(
            f"  {neighbour.get('id')} {str(neighbour.get('type')):26} {summarize_event(neighbour)}"
        )
    call_id = (event.get("deepseek") or {}).get("call_id")
    if not call_id and str(event_id).startswith("call-"):
        call_id = event_id
    if call_id:
        try:
            call = ctx.journal.read_call(call_id)
        except RelayError:
            call = None
        if call:
            lines.extend([
                "",
                f"DEEPSEEK CALL {call_id}",
                f"  job={call.get('job')} model={call.get('model')} prompt_version={call.get('prompt_version')}",
                f"  ctx_pct={call.get('ctx_pct')} cost=${call.get('cost_usd')} usage={call.get('usage')}",
                "  user prompt:",
                truncate(str(call.get("messages", [{}])[-1].get("content")), 1800),
                "  response:",
                json.dumps(call.get("response"), indent=2)[:1800],
            ])
            if call.get("image"):
                lines.append(f"  image: {call['image'].get('path')} sha256={call['image'].get('sha256')}")
    screenshot = event.get("screenshot")
    if screenshot:
        path = ctx.cfg.project_dir / screenshot
        lines.append(f"SCREENSHOT {path} exists={path.exists()}")
    notes = ctx.paths.monitor_notes.read_text(encoding="utf-8", errors="replace") if ctx.paths.monitor_notes.exists() else ""
    relevant = [line for line in notes.splitlines() if f"round {event.get('round')}" in line]
    if relevant:
        lines.extend(["", "MONITOR NOTES"] + [f"  {line}" for line in relevant[-5:]])
    return "\n".join(lines)


def replay(ctx: Context, call_id: str, transport=None) -> dict:
    record = ctx.journal.read_call(call_id)
    messages = record.get("messages")
    if not messages:
        raise ConfigError(f"call {call_id} has no stored messages")
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "image_ref" for part in content
        ):
            raise ConfigError("vision calls cannot be replayed with replay; the image is referenced, not stored")
    key = api_key()
    if not key:
        raise ConfigError("replay needs a DeepSeek API key")
    base = str(ctx.cfg.get("deepseek.base_url", "https://api.deepseek.com")).rstrip("/")
    payload = {
        "model": record.get("model") or ctx.cfg.get("deepseek.model"),
        "messages": messages,
        "temperature": 0,
        "max_tokens": int(ctx.cfg.get("deepseek.max_output_tokens", 2000)),
        "stream": False,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    transport = transport or UrllibTransport()
    response = transport(
        base + "/chat/completions", headers, payload, float(ctx.cfg.get("deepseek.timeout_seconds", 90))
    )
    try:
        text = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BrainError("replay response had an unexpected shape") from exc
    try:
        new = parse_json_lenient(text)
    except json.JSONDecodeError as exc:
        raise BrainError(f"replay response was not JSON: {exc}") from exc
    original = record.get("response")
    same = new == original
    ctx.journal.append(
        "deepseek.replay",
        detail={"call_id": call_id, "same": same, "job": record.get("job")},
        result="ok" if same else "diff",
    )
    return {"call_id": call_id, "same": same, "original": original, "new": new, "raw": text}
