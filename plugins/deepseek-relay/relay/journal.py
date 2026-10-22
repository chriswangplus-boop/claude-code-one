"""Append-only, hash-chained JSONL journal: the only complete record of what happened.

Every line includes `prev_hash` = the previous line's `hash`, chained across day files.
Any edit, deletion, or reordering is detectable with `Journal.verify()`.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import timezone
from pathlib import Path
from typing import Any, Iterator

from .clock import RealClock
from .errors import JournalError
from .paths import Paths
from .state import FileLock, atomic_write_bytes, atomic_write_json, atomic_write_text
from .trust import TrustStore
from .util import canonical_json, sha256_text

DAY_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")
CALL_ID = re.compile(r"^call-[0-9a-f]{12}$")
EVENT_ID = re.compile(r"^evt-\d{8}-\d{5,}$")
ORDERED_ID = re.compile(r"^evt-(\d{8})-(\d{5,})$")

REQUIRED_FIELDS = ("id", "ts", "round", "actor", "type", "prev_hash", "hash")


class Journal:
    def __init__(
        self,
        paths: Paths,
        lock: FileLock,
        clock: RealClock | None = None,
        trust: TrustStore | None = None,
    ):
        self.paths = paths
        self.lock = lock
        self.clock = clock or RealClock()
        # Without a trust store (unit tests that build a Journal directly) events are unsigned.
        self.trust = trust

    # --- writing ----------------------------------------------------------
    def new_call_id(self) -> str:
        return f"call-{uuid.uuid4().hex[:12]}"

    def append(
        self,
        event_type: str,
        *,
        actor: str = "orchestrator",
        round_no: int = 0,
        state: str | None = None,
        detail: dict | str | None = None,
        inputs: list[dict] | None = None,
        outputs: list[dict] | None = None,
        screenshot: str | None = None,
        deepseek: dict | None = None,
        duration_ms: int | None = None,
        result: str = "ok",
        error: str | None = None,
    ) -> dict:
        now = self.clock.now().astimezone()
        day = now.date().isoformat()
        with self.lock:
            tail = self._tail_event()
            seq = 1
            prev_hash = ""
            if tail:
                prev_hash = str(tail.get("hash", ""))
                match = re.match(r"^evt-(\d{8})-(\d+)$", str(tail.get("id", "")))
                if match:
                    tail_day = match.group(1)
                    today = day.replace("-", "")
                    if tail_day > today:
                        # A clock that moved backwards (NTP, manual change) must not restart
                        # the id sequence inside the old file: that would duplicate ids and
                        # fork the chain. Keep the tail's day and go forward.
                        day = f"{tail_day[:4]}-{tail_day[4:6]}-{tail_day[6:]}"
                        seq = int(match.group(2)) + 1
                    elif tail_day == today:
                        seq = int(match.group(2)) + 1
            path = self.paths.journal_dir / f"{day}.jsonl"
            event: dict[str, Any] = {
                "id": f"evt-{day.replace('-', '')}-{seq:05d}",
                "ts": now.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
                "round": int(round_no),
                "state": state,
                "actor": actor,
                "type": event_type,
                "detail": detail if isinstance(detail, dict) else ({"text": detail} if detail else {}),
                "inputs": inputs or [],
                "outputs": outputs or [],
                "screenshot": screenshot,
                "deepseek": deepseek,
                "duration_ms": duration_ms,
                "result": result,
                "error": error,
                "prev_hash": prev_hash,
            }
            event["hash"] = sha256_text(canonical_json({k: v for k, v in event.items() if k != "hash"}))
            # N1: the HMAC key lives outside the project; the hash chain alone is forgeable.
            if self.trust is not None:
                event["sig"] = self.trust.sign(event["hash"])
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
                payload = line.encode("utf-8")
                while payload:
                    written = os.write(fd, payload)
                    payload = payload[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            if self.trust is not None:
                # The anchor is the fixed start of trust; the tip moves so truncation is caught.
                if self.trust.anchor() is None:
                    self.trust.set_anchor(str(event["id"]), str(event["hash"]))
                self.trust.set_tip(str(event["id"]), str(event["hash"]))
            return event

    # --- reading ----------------------------------------------------------
    def _files(self) -> list[Path]:
        if not self.paths.journal_dir.exists():
            return []
        return sorted(
            p for p in self.paths.journal_dir.glob("*.jsonl") if DAY_FILE.match(p.name)
        )

    def _last_line(self, path: Path) -> str | None:
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
        except FileNotFoundError:
            return None
        if size == 0:
            return None
        window = 65536
        while True:
            start = max(0, size - window)
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read()
            body = data[:-1] if data.endswith(b"\n") else data
            cut = body.rfind(b"\n")
            if cut >= 0:
                body = body[cut + 1:]
                break
            if start == 0:
                break
            window *= 2
        text = body.decode("utf-8", "replace").strip()
        return text or None

    def _tail_event(self) -> dict | None:
        for path in reversed(self._files()):
            line = self._last_line(path)
            if line:
                try:
                    return json.loads(line)
                except json.JSONDecodeError as exc:
                    raise JournalError(
                        f"{path.name} ends with a partial or corrupt event; refusing to append and fork the chain"
                    ) from exc
        return None

    def events(self, strict: bool = True) -> Iterator[dict]:
        for path in self._files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    if strict:
                        raise JournalError(f"{path.name}:{lineno} is not valid JSON: {exc}") from exc
                    continue
                if not isinstance(event, dict):
                    if strict:
                        raise JournalError(f"{path.name}:{lineno} is not a JSON object")
                    continue
                yield event

    def all_events(self) -> list[dict]:
        return list(self.events())

    def trusted_events(self) -> list[dict]:
        """Events that are provably Relay's: recomputed hash, chain, order and HMAC.

        R3-1: verifying the *stored* `hash` field and its signature is not enough. An agent can
        copy a genuine signed event, change its body, keep the old `hash`/`sig` and append it.
        Every event is therefore rebuilt from its body, the chain and signature are checked, and
        only events after the trusted anchor are counted. Any break stops trust: nothing after a
        tampered or missing line is evidence.
        """
        if self.trust is None:
            return self.all_events()
        anchor = self.trust.anchor()
        tip = self.trust.tip()
        out: list[dict] = []
        prev_hash = ""
        last_id: str | None = None
        last_seen: dict | None = None
        broken = False
        started = anchor is None
        for event in self.events(strict=False):
            last_seen = event
            event_id = str(event.get("id") or "")
            if not EVENT_ID.match(event_id):
                broken = True
                break
            body = {key: value for key, value in event.items() if key not in ("hash", "sig")}
            expected = sha256_text(canonical_json(body))
            chain_ok = str(event.get("prev_hash") or "") == prev_hash
            hash_ok = str(event.get("hash") or "") == expected
            order_ok = True
            if last_id is not None:
                previous = ORDERED_ID.match(str(last_id))
                current = ORDERED_ID.match(event_id)
                order_ok = bool(previous and current) and (
                    previous.group(1), int(previous.group(2))
                ) < (current.group(1), int(current.group(2)))
            signature = str(event.get("sig") or "")
            signature_ok = bool(signature) and self.trust.verify(expected, signature)
            prev_hash = str(event.get("hash") or "")
            last_id = event_id
            if not started:
                if event_id != anchor["event_id"]:  # type: ignore[index]
                    continue
                # The anchor itself may predate signing (one-time migration); only its hash
                # and position must match exactly.
                if (
                    not (chain_ok and hash_ok and order_ok)
                    or str(event.get("hash") or "") != anchor["hash"]  # type: ignore[index]
                ):
                    return []
                started = True
                continue
            if not (chain_ok and hash_ok and order_ok and signature_ok):
                broken = True
                break
            out.append(event)
        if tip is not None and not broken:
            # Deleting the tail (a truncation) leaves the chain internally consistent, so the
            # moving tip outside the project is what exposes it. Fail closed on a truncation;
            # a bad line inside the file only stops trust from that line on.
            if last_seen is None:
                return []
            if (
                str(last_seen.get("id") or "") != tip["event_id"]
                or str(last_seen.get("hash") or "") != tip["hash"]
            ):
                return []
        return out

    def ensure_anchor(self) -> None:
        """Migrate a legacy (unsigned) journal by anchoring its current tail once."""
        if self.trust is None:
            return
        anchor = self.trust.anchor()
        if anchor is None:
            try:
                tail = self._tail_event()
            except JournalError:
                return
            if tail:
                self.trust.set_anchor(str(tail.get("id")), str(tail.get("hash")))
                self.trust.set_tip(str(tail.get("id")), str(tail.get("hash")))
            return
        if self.trust.tip() is None:
            # v0.3 upgrade: the old anchor moved with every append, so it can only serve as the
            # tip now; new events become the trusted range.
            self.trust.set_tip(str(anchor.get("event_id")), str(anchor.get("hash")))

    def find(self, event_id: str) -> dict | None:
        for event in self.events():
            if event.get("id") == event_id:
                return event
        return None

    def for_round(self, round_no: int) -> list[dict]:
        return [event for event in self.events() if int(event.get("round", -1)) == round_no]

    def of_type(self, event_type: str) -> list[dict]:
        return [event for event in self.events() if event.get("type") == event_type]

    def latest(self, event_type: str) -> dict | None:
        found = None
        for event in self.events():
            if event.get("type") == event_type:
                found = event
        return found

    def spend_for_day(self, day: str | None = None) -> float:
        """Sum recorded DeepSeek cost for a local day (`YYYY-MM-DD`), restart-safe."""
        day = day or self.clock.now().astimezone().date().isoformat()
        compact = day.replace("-", "")
        total = 0.0
        for event in self.events(strict=False):
            if event.get("type") != "deepseek.call":
                continue
            if not str(event.get("id", "")).startswith(f"evt-{compact}-"):
                continue
            try:
                total += float((event.get("deepseek") or {}).get("cost_usd") or 0.0)
            except (TypeError, ValueError):
                continue
        return round(total, 6)

    # --- calls ------------------------------------------------------------
    def record_call(self, call_id: str, record: dict) -> Path:
        if not CALL_ID.match(call_id):
            raise JournalError(f"invalid call id: {call_id!r}")
        path = self.paths.calls_dir / f"{call_id}.json"
        record = {"call_id": call_id, **record}
        with self.lock:
            atomic_write_json(path, record, mode=0o600)
        return path

    def read_call(self, call_id: str) -> dict:
        if not CALL_ID.match(call_id):
            raise JournalError(f"invalid call id: {call_id!r}")
        path = self.paths.calls_dir / f"{call_id}.json"
        if not path.exists():
            raise JournalError(f"no such DeepSeek call: {call_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def record_text(self, name: str, text: str) -> Path:
        if not re.match(r"^[a-z0-9_-]{1,64}$", name):
            raise JournalError(f"invalid record name: {name!r}")
        path = self.paths.calls_dir / f"{name}.txt"
        with self.lock:
            atomic_write_text(path, text, mode=0o600)
        return path

    # --- repair -----------------------------------------------------------
    def repair(self) -> dict:
        """Quarantine a partial/corrupt tail and append a `journal.repaired` event."""
        files = self._files()
        if not files:
            return {"repaired": False, "reason": "no journal files"}
        path = files[-1]
        data = path.read_bytes()
        if not data:
            return {"repaired": False, "reason": "empty journal"}
        lines = data.splitlines(keepends=True)
        offsets = []
        offset = 0
        for line in lines:
            offsets.append(offset)
            offset += len(line)
        valid_end = 0
        invalid_start: int | None = None
        invalid_index: int | None = None
        for index, raw in enumerate(lines):
            stripped = raw.rstrip(b"\r\n")
            if not stripped:
                valid_end = offsets[index] + len(raw)
                continue
            try:
                json.loads(stripped.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                invalid_start = offsets[index]
                invalid_index = index
                break
            valid_end = offsets[index] + len(raw)
        if invalid_start is None:
            if data and not data.endswith(b"\n"):
                with self.lock:
                    with open(path, "ab") as fh:
                        fh.write(b"\n")
                        fh.flush()
                        os.fsync(fh.fileno())
                event = self.append(
                    "journal.repaired", actor="harness",
                    detail={"file": path.name, "quarantined": None, "note": "added missing newline"},
                )
                return {"repaired": True, "event": event["id"], "quarantined": None}
            return {"repaired": False, "reason": "tail is valid"}
        if invalid_index is not None and any(
            raw.strip() for raw in lines[invalid_index + 1:]
        ):
            raise JournalError("journal corruption is not at the tail; refusing automatic repair")
        tail = data[invalid_start:]
        quarantine = self.paths.journal_dir / f"quarantine-{int(self.clock.epoch())}.txt"
        atomic_write_bytes(quarantine, tail)
        atomic_write_bytes(path, data[:valid_end])
        event = self.append(
            "journal.repaired", actor="harness",
            detail={
                "file": path.name,
                "quarantined": quarantine.name,
                "bytes": len(tail),
            },
            outputs=[{"path": str(quarantine.relative_to(self.paths.project_dir)), "sha256": sha256_text(tail.decode("utf-8", "replace"))}],
        )
        return {"repaired": True, "event": event["id"], "quarantined": str(quarantine)}

    # --- integrity --------------------------------------------------------
    def verify(self) -> tuple[bool, list[str]]:
        errors: list[str] = []
        parsed: list[dict] = []
        prev_hash: str | None = None
        seen: set[str] = set()
        last_key: tuple[str, int] | None = None
        for path in self._files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                where = f"{path.name}:{lineno}"
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"{where}: invalid JSON ({exc})")
                    prev_hash = None
                    continue
                missing = [field for field in REQUIRED_FIELDS if field not in event]
                if missing:
                    errors.append(f"{where}: missing {', '.join(missing)}")
                if prev_hash is not None and event.get("prev_hash") != prev_hash:
                    errors.append(f"{where}: prev_hash does not match previous event")
                body = {k: v for k, v in event.items() if k not in ("hash", "sig")}
                expected = sha256_text(canonical_json(body))
                if event.get("hash") != expected:
                    errors.append(f"{where}: hash mismatch (line was modified)")
                if event.get("id") in seen:
                    errors.append(f"{where}: duplicate event id {event.get('id')}")
                match = ORDERED_ID.match(str(event.get("id") or ""))
                if not match:
                    errors.append(f"{where}: malformed event id {event.get('id')!r}")
                else:
                    key = (match.group(1), int(match.group(2)))
                    if last_key is not None and key <= last_key:
                        errors.append(f"{where}: event ids are not in order")
                    last_key = key
                seen.add(str(event.get("id")))
                prev_hash = event.get("hash")
                parsed.append(event)
        if self.trust is not None:
            tip = self.trust.tip()
            if tip:
                if (
                    not parsed
                    or str(parsed[-1].get("id") or "") != tip["event_id"]
                    or str(parsed[-1].get("hash") or "") != tip["hash"]
                ):
                    errors.append(
                        "the trusted journal tip (anchor) event is missing (the journal was "
                        "truncated or replaced)"
                    )
            anchor = self.trust.anchor()
            if anchor:
                ids = [str(event.get("id")) for event in parsed]
                if anchor["event_id"] not in ids:
                    errors.append(
                        "trusted journal anchor event is missing (the journal was truncated or replaced)"
                    )
                else:
                    index = ids.index(anchor["event_id"])
                    if parsed[index].get("hash") != anchor["hash"]:
                        errors.append("the trusted anchor hash does not match the journal")
                    for event in parsed[index + 1:]:
                        signature = event.get("sig")
                        where = str(event.get("id"))
                        if not signature:
                            errors.append(f"{where}: unsigned event (not written by Relay)")
                        elif not self.trust.verify(str(event.get("hash") or ""), str(signature)):
                            errors.append(f"{where}: bad journal signature")
        return (not errors, errors)
