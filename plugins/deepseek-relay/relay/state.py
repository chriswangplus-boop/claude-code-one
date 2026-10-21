"""Crash-safe STATE.json / BATON.json, atomic writes, and a cross-process file lock."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .clock import RealClock, utc_iso
from .errors import StateError
from .paths import Paths

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore
    import msvcrt  # type: ignore

PHASES = ("INIT", "PLAN", "CODE", "REVIEW", "ROUTE", "WAIT_HUMAN", "PAUSED", "DONE", "HALTED")
TERMINAL_PHASES = {"DONE", "HALTED"}
STATE_SCHEMA = 1


def atomic_write_text(path: str | Path, text: str, mode: int = 0o600) -> None:
    """Write via temp file + fsync + rename, so a crash leaves either old or new content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".relay-tmp-", suffix=path.suffix)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_json(path: str | Path, obj: Any, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True) + "\n", mode=mode)


def read_json(path: str | Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StateError(f"{path} is not valid JSON: {exc}") from exc


def _lock_fd(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:  # pragma: no cover - Windows
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _unlock_fd(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:  # pragma: no cover - Windows
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class FileLock:
    """Re-entrant in-process lock backed by an advisory OS file lock.

    Share one instance between StateStore and Journal so nested writes cannot deadlock.
    """

    def __init__(self, path: str | Path, timeout: float = 30.0):
        self.path = Path(path)
        self.timeout = timeout
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    def acquire(self) -> None:
        self._rlock.acquire()
        try:
            if self._depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
                deadline = time.monotonic() + self.timeout
                while True:
                    try:
                        _lock_fd(fd)
                        break
                    except (BlockingIOError, OSError):
                        if time.monotonic() >= deadline:
                            os.close(fd)
                            raise StateError(f"timed out waiting for lock {self.path}")
                        time.sleep(0.05)
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            _unlock_fd(self._fd)
            os.close(self._fd)
            self._fd = None
        self._rlock.release()

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    @contextmanager
    def maybe(self) -> Iterator[None]:
        self.acquire()
        try:
            yield
        finally:
            self.release()


def new_state(clock: RealClock | None = None) -> dict:
    now = utc_iso(clock)
    return {
        "schema": STATE_SCHEMA,
        "phase": "INIT",
        "round": 0,
        "milestone": 1,
        "role": None,
        "session_rounds": {"planner": 0, "reviewer": 0, "coder": 0},
        "counters": {
            "total_rounds": 0,
            "rounds_in_milestone": 0,
            "consecutive_changes": 0,
            "replans": 0,
            "deepseek_calls": 0,
        },
        "budget": {"date": now[:10], "spent_usd": 0.0},
        "decisions": 0,
        "pending": None,
        "pending_decision": None,
        "last_sentinel": None,
        "last_route": None,
        "last_turn": None,
        "history": [],
        "session_ctx": {"planner": 0, "reviewer": 0, "coder": 0},
        "active_session": {},
        "paused_reason": None,
        "return_to": None,
        "started_at": None,
        "run_id": None,
        "created_at": now,
        "updated_at": now,
        "notification": {"last_sent_at": None, "count": 0},
        "rollover": {"pending": None, "forced": False, "forced_roles": []},
    }


class StateStore:
    def __init__(self, paths: Paths, clock: RealClock | None = None, lock: FileLock | None = None):
        self.paths = paths
        self.clock = clock or RealClock()
        self.lock = lock or FileLock(paths.lock)

    # --- state ------------------------------------------------------------
    def load(self) -> dict:
        try:
            state = read_json(self.paths.state)
        except StateError:
            raise
        if not isinstance(state, dict):
            raise StateError(f"{self.paths.state} does not contain a JSON object")
        if state.get("schema") != STATE_SCHEMA:
            raise StateError(f"unsupported state schema: {state.get('schema')!r}")
        return state

    def save(self, state: dict) -> dict:
        state["schema"] = STATE_SCHEMA
        state["updated_at"] = utc_iso(self.clock)
        with self.lock:
            atomic_write_json(self.paths.state, state)
        return state

    def ensure(self) -> dict:
        if self.paths.state.exists():
            return self.load()
        state = new_state(self.clock)
        return self.save(state)

    def mutate(self, fn: Callable[[dict], Any]) -> dict:
        with self.lock:
            state = self.load() if self.paths.state.exists() else new_state(self.clock)
            fn(state)
            return self.save(state)

    def set_phase(self, phase: str, **fields: Any) -> dict:
        if phase not in PHASES:
            raise StateError(f"unknown phase: {phase}")

        def update(state: dict) -> None:
            state["phase"] = phase
            state.update(fields)

        return self.mutate(update)

    # --- baton ------------------------------------------------------------
    def baton(self) -> dict:
        data = read_json(self.paths.baton, default=None)
        if not isinstance(data, dict):
            return {"holder": "none", "role": None, "round": 0, "since": None}
        return data

    def set_baton(self, holder: str, role: str | None, round_no: int) -> dict:
        baton = {
            "holder": holder,
            "role": role,
            "round": round_no,
            "since": utc_iso(self.clock),
        }
        with self.lock:
            atomic_write_json(self.paths.baton, baton)
        return baton

    def clear_baton(self) -> None:
        self.set_baton("none", None, self.baton().get("round", 0))

    # --- flags ------------------------------------------------------------
    def flag_path(self, name: str) -> Path:
        mapping = {"pause": self.paths.pause_flag, "stop": self.paths.stop_flag, "halt": self.paths.halt_flag}
        if name not in mapping:
            raise StateError(f"unknown flag: {name}")
        return mapping[name]

    def set_flag(self, name: str, payload: dict | None = None) -> None:
        path = self.flag_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload or {}, indent=2) + "\n")

    def read_flag(self, name: str) -> dict | None:
        path = self.flag_path(name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            return data if isinstance(data, dict) else {"text": str(data)}
        except json.JSONDecodeError:
            return {"text": path.read_text(encoding="utf-8", errors="replace")}

    def clear_flag(self, name: str) -> None:
        try:
            self.flag_path(name).unlink()
        except FileNotFoundError:
            pass

    # --- heartbeats -------------------------------------------------------
    def heartbeat(self, path: Path | None = None, **extra: Any) -> dict:
        payload = {"ts": utc_iso(self.clock), "epoch": self.clock.epoch(), "pid": os.getpid(), **extra}
        atomic_write_json(path or self.paths.heartbeat, payload)
        return payload

    def heartbeat_age(self, path: Path | None = None) -> float | None:
        target = path or self.paths.heartbeat
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            return max(0.0, self.clock.epoch() - float(data["epoch"]))
        except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
            return None
