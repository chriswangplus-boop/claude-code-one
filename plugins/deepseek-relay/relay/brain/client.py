"""DeepSeek client: stateless calls, strict JSON, redaction, per-call context guard, budget."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..clock import RealClock
from ..config import Config
from ..errors import BrainError, BudgetExceeded, ContextGuardTripped, VisionUnavailable
from ..journal import Journal
from ..redact import redact_obj
from ..util import parse_json_lenient, sha256_file

MAX_IMAGE_BYTES = 8 * 1024 * 1024


class TransportError(BrainError):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class Transport(Protocol):
    def __call__(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict: ...


class UrllibTransport:
    def __call__(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise TransportError(
                        f"DeepSeek returned non-JSON (HTTP {response.status})", retryable=True
                    ) from exc
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            status = exc.code
            retryable = status in {408, 409, 429, 500, 502, 503, 504}
            raise TransportError(
                f"DeepSeek HTTP {status}: {detail or exc.reason}", retryable=retryable, status=status
            ) from exc
        except urllib.error.URLError as exc:
            raise TransportError(f"DeepSeek network error: {exc.reason}", retryable=True) from exc
        except TimeoutError as exc:
            raise TransportError("DeepSeek request timed out", retryable=True) from exc
        except OSError as exc:
            raise TransportError(f"DeepSeek network error: {exc}", retryable=True) from exc


@dataclass
class BrainResult:
    content: dict
    call_id: str
    usage: dict = field(default_factory=dict)
    ctx_pct: float = 0.0
    cost_usd: float = 0.0
    text: str = ""


@dataclass
class CallContext:
    job: str
    system: str
    user: str
    image: Path | None = None
    schema_hint: str | None = None


class DeepSeekClient:
    def __init__(
        self,
        cfg: Config,
        journal: Journal,
        *,
        api_key: str | None,
        transport: Transport | None = None,
        clock: RealClock | None = None,
        model: str | None = None,
        context_window: int | None = None,
        guard: float | None = None,
        extra_secrets: list[str] | None = None,
        actor: str = "orchestrator",
    ):
        self.cfg = cfg
        self.actor = actor
        self.journal = journal
        self.clock = clock or RealClock()
        self.api_key = api_key
        self.transport = transport or UrllibTransport()
        self.model = model or str(cfg.get("deepseek.model", "deepseek-flash"))
        self.context_window = int(context_window or cfg.get("deepseek.context_window", 1_000_000))
        self.guard = float(guard if guard is not None else cfg.get("deepseek.context_guard", 0.7))
        self.extra_secrets = [s for s in (extra_secrets or []) if s]

    # --- public -----------------------------------------------------------
    def complete(
        self,
        ctx: CallContext,
        *,
        json_mode: bool = True,
        max_tokens: int | None = None,
    ) -> BrainResult:
        if not self.api_key:
            raise BrainError(
                "no DeepSeek API key. Set RELAY_DEEPSEEK_API_KEY or run `relay init` to store one."
            )
        if not json_mode:
            raise BrainError("Relay only accepts JSON judgments")

        user_text = ctx.user
        counts: dict[str, int] = {}
        if ctx.image is not None:
            self._check_image(ctx.image)

        clean_user, user_counts = redact_obj(user_text, self.extra_secrets)
        counts.update(user_counts)
        messages = self._messages(ctx.system, clean_user, ctx.image)
        estimated_tokens = self._estimate_tokens(ctx.system, clean_user, ctx.image)
        estimated_pct = estimated_tokens / self.context_window

        if estimated_pct >= self.guard:
            self.journal.append(
                "guard.tripped",
                detail={
                    "kind": "context_estimate",
                    "ctx_pct": round(estimated_pct, 4),
                    "job": ctx.job,
                    "limit": self.guard,
                },
                result="blocked",
            )
            raise ContextGuardTripped(estimated_pct)

        spent = self.journal.spend_for_day()
        budget = float(self.cfg.get("daily_budget_usd", 0) or 0)
        if budget > 0 and spent >= budget:
            self.journal.append(
                "guard.tripped",
                detail={"kind": "budget", "spent_usd": spent, "budget_usd": budget, "job": ctx.job},
                result="blocked",
            )
            raise BudgetExceeded(f"daily DeepSeek budget reached: ${spent:.2f} of ${budget:.2f}")

        call_id = self.journal.new_call_id()
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": float(self.cfg.get("deepseek.temperature", 0)),
            "max_tokens": int(max_tokens or self.cfg.get("deepseek.max_output_tokens", 2000)),
            "stream": False,
            "response_format": {"type": "json_object"},
            "thinking": {"type": str(self.cfg.get("deepseek.thinking", "disabled"))},
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        url = str(self.cfg.get("deepseek.base_url", "https://api.deepseek.com")).rstrip("/") + "/chat/completions"

        started = time.monotonic()
        response = self._send_with_retries(url, headers, payload)
        duration_ms = int((time.monotonic() - started) * 1000)

        shape_error: str | None = None
        try:
            text = str(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            text = ""
            shape_error = f"DeepSeek returned an unexpected response shape: {str(response)[:200]}"
        content = None
        parse_error: str | None = None
        if text:
            try:
                content = parse_json_lenient(text)
            except json.JSONDecodeError as exc:
                parse_error = f"DeepSeek did not return valid JSON: {exc}"
            if content is not None and not isinstance(content, dict):
                content = None
                parse_error = "DeepSeek JSON response must be an object"

        usage = response.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or estimated_tokens)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        ctx_pct = prompt_tokens / self.context_window
        cost = self._cost(prompt_tokens, completion_tokens)

        image_ref = None
        if ctx.image is not None:
            try:
                image_ref = {
                    "path": ctx.image.resolve().relative_to(self.cfg.project_dir).as_posix(),
                    "sha256": sha256_file(ctx.image),
                }
            except ValueError:
                image_ref = {"path": ctx.image.name, "sha256": sha256_file(ctx.image)}
        record = {
            "ts": self.clock.now().isoformat(),
            "job": ctx.job,
            "model": self.model,
            "prompt_version": self._prompt_version(),
            "messages": _strip_images(messages, image_ref),
            "response_text": text,
            "response": content,
            "error": shape_error or parse_error,
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            "ctx_pct": round(ctx_pct, 4),
            "cost_usd": cost,
            "duration_ms": duration_ms,
            "redaction_counts": counts,
        }
        if image_ref:
            record["image"] = image_ref
        self.journal.record_call(call_id, record)
        ref = f"journal/calls/{call_id}.json"
        self.journal.append(
            "deepseek.call",
            actor=self.actor,
            detail={
                "job": ctx.job, "model": self.model, "prompt_version": self._prompt_version(),
                "error": shape_error or parse_error,
            },
            deepseek={
                "call_id": call_id,
                "prompt_ref": ref,
                "response_ref": ref,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "ctx_pct": round(ctx_pct, 4),
                "cost_usd": cost,
                "redaction_counts": counts,
            },
            outputs=[{"path": ref, "sha256": sha256_file(self.journal.paths.calls_dir / f"{call_id}.json")}],
            duration_ms=duration_ms,
        )

        if shape_error or parse_error:
            raise BrainError(shape_error or parse_error or "DeepSeek judgment failed")

        if ctx_pct >= self.guard:
            self.journal.append(
                "guard.tripped",
                detail={"kind": "context_usage", "ctx_pct": round(ctx_pct, 4), "call_id": call_id},
                result="blocked",
            )
            raise ContextGuardTripped(ctx_pct, call_id)

        return BrainResult(
            content=content, call_id=call_id, usage=usage, ctx_pct=ctx_pct, cost_usd=cost, text=text
        )

    # --- internals --------------------------------------------------------
    def _prompt_version(self) -> str:
        from .prompts import PROMPT_VERSION

        return PROMPT_VERSION

    def _messages(self, system: str, user: str, image: Path | None) -> list[dict]:
        if image is None:
            return [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if not bool(self.cfg.get("deepseek.vision", True)) or bool(self.cfg.get("deepseek.text_only_mode", False)):
            raise VisionUnavailable("vision is disabled by config (text_only_mode)")
        mime = _mime(image)
        data = base64.b64encode(image.read_bytes()).decode("ascii")
        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
                ],
            },
        ]

    def _check_image(self, image: Path) -> None:
        if not image.exists():
            raise VisionUnavailable(f"screenshot missing: {image}")
        if image.stat().st_size > MAX_IMAGE_BYTES:
            raise VisionUnavailable(f"screenshot too large: {image.stat().st_size} bytes")
        if bool(self.cfg.get("deepseek.text_only_mode", False)) or not bool(self.cfg.get("deepseek.vision", True)):
            raise VisionUnavailable("vision is disabled by config (text_only_mode)")

    def _estimate_tokens(self, system: str, user: str, image: Path | None) -> int:
        approx = (len(system) + len(user)) // 4 + 16
        if image is not None:
            approx += 1200  # conservative per-image budget, independent of resolution
        return approx

    def _cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        price_in = float(self.cfg.get("deepseek.price_input_per_mtok", 0.3))
        price_out = float(self.cfg.get("deepseek.price_output_per_mtok", 1.2))
        return round(prompt_tokens / 1e6 * price_in + completion_tokens / 1e6 * price_out, 6)

    def _send_with_retries(self, url: str, headers: dict[str, str], payload: dict) -> dict:
        attempts = int(self.cfg.get("deepseek.max_retries", 2)) + 1
        timeout = float(self.cfg.get("deepseek.timeout_seconds", 90))
        last: TransportError | None = None
        for attempt in range(attempts):
            try:
                return self.transport(url, headers, payload, timeout)
            except TransportError as exc:
                last = exc
                if not exc.retryable or attempt >= attempts - 1:
                    raise
                self.clock.sleep(min(8.0, 2.0**attempt))
        raise last or TransportError("DeepSeek call failed", retryable=False)


def _strip_images(messages: list[dict], image_ref: dict | None) -> list[dict]:
    """Keep base64 pixels out of the on-disk call record; reference the crop instead."""
    out: list[dict] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    parts.append({"type": "image_ref", **(image_ref or {})})
                else:
                    parts.append(part)
            out.append({**message, "content": parts})
        else:
            out.append(message)
    return out


def _mime(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/png")
