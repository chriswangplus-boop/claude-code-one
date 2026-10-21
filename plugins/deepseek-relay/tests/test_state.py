import json
import os
import subprocess
from pathlib import Path
import sys
import textwrap
import time

import pytest

from relay.errors import StateError
from relay.paths import Paths
from relay.state import FileLock, StateStore, atomic_write_json, atomic_write_text, new_state


def test_state_roundtrip(ctx):
    state = new_state(ctx.clock)
    assert state["phase"] == "INIT"
    assert ctx.store.save(state)["updated_at"]
    loaded = ctx.store.load()
    assert loaded["schema"] == 1
    assert loaded["session_rounds"] == {"planner": 0, "reviewer": 0, "coder": 0}


def test_atomic_write_leaves_no_temp_and_survives_stale_temp(ctx, paths):
    atomic_write_text(paths.state, '{"schema": 1, "phase": "INIT"}')
    stale = paths.relay_dir / ".relay-tmp-stale.json"
    stale.write_text("{ this is a half-written file")
    loaded = ctx.store.load()
    assert loaded["schema"] == 1
    assert stale.exists()  # ignored, not treated as state
    assert not [p for p in paths.relay_dir.glob(".relay-tmp-*") if p != stale]


def test_state_schema_is_checked(ctx, paths):
    atomic_write_json(paths.state, {"schema": 99})
    with pytest.raises(StateError, match="schema"):
        ctx.store.load()


def test_mutate_and_baton(ctx):
    ctx.store.mutate(lambda s: s["counters"].__setitem__("total_rounds", 3))
    assert ctx.store.load()["counters"]["total_rounds"] == 3
    ctx.store.set_baton("planner", "planner", 1)
    baton = ctx.store.baton()
    assert baton["holder"] == "planner"
    ctx.store.clear_baton()
    assert ctx.store.baton()["holder"] == "none"


def test_flags(ctx):
    assert ctx.store.read_flag("pause") is None
    ctx.store.set_flag("halt", {"reason": "drift"})
    assert ctx.store.read_flag("halt")["reason"] == "drift"
    ctx.store.clear_flag("halt")
    assert ctx.store.read_flag("halt") is None


def test_reentrant_lock_does_not_deadlock(tmp_path):
    lock = FileLock(tmp_path / ".lock")
    with lock:
        with lock:
            pass


def test_lock_is_cross_process(tmp_path):
    lock_path = tmp_path / ".lock"
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from relay.state import FileLock
        lock = FileLock({str(lock_path)!r})
        with lock:
            print("held", flush=True)
            time.sleep(2)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(StateError, match="timed out"):
            FileLock(lock_path, timeout=0.2).acquire()
    finally:
        child.kill()
        child.wait(timeout=5)


def test_heartbeat_age(ctx, paths):
    ctx.store.heartbeat(extra="x")
    assert ctx.store.heartbeat_age() == pytest.approx(0, abs=1)
    ctx.clock.advance(seconds=90)
    assert ctx.store.heartbeat_age() == pytest.approx(90, abs=1)
    paths.heartbeat.unlink()
    assert ctx.store.heartbeat_age() is None
