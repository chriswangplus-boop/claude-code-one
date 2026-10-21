"""docs/EVENTS.md is the contract Relay Watch decodes; this keeps it honest."""

from __future__ import annotations

import json
import re
from pathlib import Path

from journal_scenarios import build_journal

ROOT = Path(__file__).resolve().parents[1]
EVENTS_MD = ROOT / "docs" / "EVENTS.md"
FIXTURE = ROOT / "apps" / "RelayWatch" / "Tests" / "Fixtures" / "journal-sample.jsonl"
ROW = re.compile(r"^\|\s*`([a-z][a-z_.]+)`\s*\|\s*(yes|no|monitor\.check)\s*\|(.*)\|\s*$")


def documented() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in EVENTS_MD.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line.strip())
        if not match:
            continue
        event_type, _refresh, fields = match.groups()
        rows[event_type] = re.findall(r"`([a-z0-9_.]+)`", fields)
    assert rows, "EVENTS.md has no parseable event rows"
    return rows


def test_every_documented_event_is_emitted_with_its_fields(ctx):
    schema = documented()
    events = build_journal(ctx.cfg.project_dir)
    by_type: dict[str, list[dict]] = {}
    for event in events:
        by_type.setdefault(event["type"], []).append(event)

    missing = sorted(set(schema) - set(by_type))
    assert not missing, f"fixture does not exercise: {missing}"

    for event_type, fields in schema.items():
        for event in by_type[event_type]:
            detail = event.get("detail") or {}
            absent = [field for field in fields if field not in detail]
            assert not absent, f"{event_type} {event['id']} is missing {absent}"

    # Unknown types and unknown fields must be legal: Relay Watch keeps them as raw events.
    unknown = by_type["vision.experiment"][0]
    assert unknown["detail"]["future_field"] == {"nested": [1, 2, 3]}

    # The chain must verify: Relay Watch relies on prev_hash links being intact.
    previous = ""
    for event in events:
        assert event["prev_hash"] == previous, event["id"]
        previous = event["hash"]


def test_real_loop_emits_the_part_b_fields(ctx):
    """Emission proof on a plain fake-driver run, not just the fixture builder."""
    from relay.context import open_project
    from relay.demo import DemoApps
    from relay.notify import FakeNotifier
    from relay.orchestrator import Orchestrator
    from relay.setup import init_project

    project = ctx.cfg.project_dir
    init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal="emission")
    apps = DemoApps(project, verdicts=["CHANGES_REQUESTED", "MILESTONE_DONE", "PROJECT_DONE"])
    orch = Orchestrator(ctx, apps.drivers(), notifier=FakeNotifier())
    orch.run(max_steps=40)

    starts = ctx.journal.of_type("turn.start")
    ends = ctx.journal.of_type("turn.end")
    assert starts and ends
    for event in starts + ends:
        detail = event["detail"]
        assert {"role", "model", "effort", "model_verified", "effort_verified"} <= set(detail)
    escalated = ctx.journal.of_type("model.escalated")
    assert escalated and all(
        {"trigger", "role", "issue_ids", "streak", "opus_turns_today", "opus_turns_milestone"}
        <= set(event["detail"])
        for event in escalated
    )
    assert any(
        (event["detail"].get("opus_turns_today") or {}).get("limit") == 3 for event in escalated
    )


def test_committed_fixture_matches_the_documented_schema():
    assert FIXTURE.exists(), "run tools/make_event_fixtures.py"
    events = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert events
    types = {event["type"] for event in events}
    schema = documented()
    missing = sorted(set(schema) - types)
    assert not missing, f"committed fixture is stale; regenerate it (missing {missing})"
    assert "vision.experiment" in types
    previous = ""
    for event in events:
        assert event["prev_hash"] == previous, event["id"]
        previous = event["hash"]
    # The fixture is the same shape the Swift tests decode.
    sample = next(event for event in events if event["type"] == "turn.end")
    assert isinstance(sample["detail"]["changed"], list)
    assert sample["detail"]["model_verified"] in (True, False, "unverifiable")
