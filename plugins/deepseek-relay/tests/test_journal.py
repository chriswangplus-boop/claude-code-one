import json
from pathlib import Path
import threading

import pytest

from relay.errors import JournalError
from relay.journal import Journal
from relay.state import FileLock


def _append_many(journal, n):
    for i in range(n):
        journal.append("test.event", detail={"i": i})


def test_chain_verifies(ctx):
    for i in range(3):
        ctx.journal.append("turn.start", round_no=i, detail={"i": i})
    ok, errors = ctx.journal.verify()
    assert ok, errors
    events = ctx.journal.all_events()
    assert [e["detail"]["i"] for e in events] == [0, 1, 2]
    assert events[1]["prev_hash"] == events[0]["hash"]
    assert events[1]["id"].endswith("00002")


def test_tamper_is_detected(ctx, paths):
    for i in range(3):
        ctx.journal.append("turn.start", round_no=i)
    files = sorted(paths.journal_dir.glob("*.jsonl"))
    lines = files[0].read_text().splitlines()
    lines[1] = lines[1].replace('"turn.start"', '"turn.started"')
    files[0].write_text("\n".join(lines) + "\n")
    ok, errors = ctx.journal.verify()
    assert not ok
    assert any("hash mismatch" in e or "prev_hash" in e for e in errors)


def test_day_rollover_keeps_chain(ctx, paths):
    ctx.journal.append("a", round_no=1)
    ctx.clock.advance(days=1)
    ctx.journal.append("b", round_no=2)
    files = sorted(paths.journal_dir.glob("*.jsonl"))
    assert len(files) == 2
    ok, errors = ctx.journal.verify()
    assert ok, errors
    events = ctx.journal.all_events()
    assert events[1]["prev_hash"] == events[0]["hash"]
    assert events[1]["id"].startswith("evt-20261021-")


def test_spend_for_day_uses_event_day(ctx):
    ctx.journal.append("deepseek.call", deepseek={"cost_usd": 0.25})
    ctx.journal.append("deepseek.call", deepseek={"cost_usd": 0.5})
    ctx.clock.advance(days=1)
    ctx.journal.append("deepseek.call", deepseek={"cost_usd": 9.0})
    assert ctx.journal.spend_for_day("2026-10-20") == pytest.approx(0.75)
    assert ctx.journal.spend_for_day("2026-10-21") == pytest.approx(9.0)


def test_call_records_are_private_and_validated(ctx):
    call_id = ctx.journal.new_call_id()
    path = ctx.journal.record_call(call_id, {"job": "route", "response": {"route": "CODE"}})
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert ctx.journal.read_call(call_id)["response"]["route"] == "CODE"
    with pytest.raises(JournalError, match="invalid call id"):
        ctx.journal.read_call("../../etc/passwd")


def test_concurrent_appends_stay_consistent(ctx):
    _append_many(ctx.journal, 1)
    threads = [threading.Thread(target=_append_many, args=(ctx.journal, 10)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    events = ctx.journal.all_events()
    assert len(events) == 41
    assert len({e["id"] for e in events}) == 41
    ok, errors = ctx.journal.verify()
    assert ok, errors


def test_append_fields(ctx):
    event = ctx.journal.append(
        "gui.type",
        actor="orchestrator",
        round_no=4,
        state="CODE",
        detail={"chars": 12},
        inputs=[{"path": ".relay/HANDOFF.md", "sha256": "abc"}],
        result="ok",
        duration_ms=7,
    )
    assert event["actor"] == "orchestrator"
    assert event["round"] == 4
    assert event["detail"]["chars"] == 12
    assert event["deepseek"] is None


def test_large_event_tail_is_read_completely(ctx):
    ctx.journal.append("big.event", detail={"text": "x" * 90000})
    ctx.journal.append("after.big", detail={"ok": True})
    events = ctx.journal.all_events()
    assert events[-1]["type"] == "after.big"
    assert events[-1]["prev_hash"] == events[-2]["hash"]
    ok, errors = ctx.journal.verify()
    assert ok, errors


def test_corrupt_tail_refuses_to_fork_the_chain(ctx, paths):
    from relay.errors import JournalError

    ctx.journal.append("good")
    files = sorted(paths.journal_dir.glob("*.jsonl"))
    with open(files[0], "a", encoding="utf-8") as fh:
        fh.write('{"broken":')
    with pytest.raises(JournalError, match="refusing to append"):
        ctx.journal.append("next")


def test_journal_repair_quarantines_a_partial_tail(ctx, paths):
    ctx.journal.append("good.event", detail={"ok": True})
    files = sorted(paths.journal_dir.glob("*.jsonl"))
    files[0].write_bytes(files[0].read_bytes() + b'{"partial":')
    from relay.errors import JournalError
    with pytest.raises(JournalError):
        ctx.journal.append("blocked")
    result = ctx.journal.repair()
    assert result["repaired"] is True
    assert result["quarantined"]
    assert Path(result["quarantined"]).exists()
    ok, errors = ctx.journal.verify()
    assert ok, errors
    ctx.journal.append("after.repair")
    ok, errors = ctx.journal.verify()
    assert ok, errors
