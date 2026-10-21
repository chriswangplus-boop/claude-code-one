import json

import pytest

from relay.brain import Brain
from relay.notify import Notifier
from relay.tools import replay as replay_call


def test_notifier_redacts_body_and_reaches_both_channels(ctx):
    ctx.cfg.set("ntfy.topic", "relay-test-topic")
    desktop_calls = []
    ntfy_calls = []

    def desktop(title, body, urgent):
        desktop_calls.append((title, body, urgent))
        return True

    def ntfy(server, topic, title, body, token, urgent):
        ntfy_calls.append((server, topic, title, body, token, urgent))
        return True

    notifier = Notifier(
        ctx.cfg, ctx.journal, clock=ctx.clock, desktop_fn=desktop, ntfy_fn=ntfy,
        secrets=["sk-abcdefghijklmnopqrstuvwx"],
    )
    result = notifier.send(
        "Relay needs a decision (D-001)",
        "D-001: use JSON? key sk-abcdefghijklmnopqrstuvwx and mail dev@example.com",
        decision_id="D-001", urgent=True,
    )
    assert result["channels"] == {"desktop": True, "ntfy": True}
    for _, body, _ in desktop_calls:
        assert "sk-abcdefghijklmnopqrstuvwx" not in body
        assert "dev@example.com" not in body
        assert "[secret]" in body
    assert ntfy_calls[0][1] == "relay-test-topic"
    assert "sk-abcdefghijklmnopqrstuvwx" not in ntfy_calls[0][3]
    event = ctx.journal.latest("notify.sent")
    assert event["detail"]["decision_id"] == "D-001"


def test_notifier_survives_channel_failure(ctx):
    ctx.cfg.set("ntfy.topic", "relay-test-topic")

    def broken(*args):
        raise RuntimeError("boom")

    notifier = Notifier(ctx.cfg, ctx.journal, clock=ctx.clock, desktop_fn=broken, ntfy_fn=broken)
    result = notifier.send("t", "b")
    assert result["channels"]["desktop"] is False
    assert ctx.journal.latest("notify.sent")["result"] == "failed"


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def __call__(self, url, headers, payload, timeout):
        self.calls += 1
        return self.response


def _reply(content):
    return {
        "choices": [{"message": {"content": json.dumps(content)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def _make_call(ctx, monkeypatch):
    monkeypatch.setenv("RELAY_DEEPSEEK_API_KEY", "sk-test")
    brain = Brain(ctx.cfg, ctx.journal, api_key="sk-test", transport=FakeTransport(_reply({
        "route": "CODE", "reason": "changes requested", "round_summary": "fix it",
    })), clock=ctx.clock)
    decision = brain.route(
        round_no=1, milestone=1, phase="ROUTE",
        sentinel={"verdict": "CHANGES_REQUESTED"}, review="fix", handoff="do it",
        history="", monitor_notes="", decisions="",
    )
    return decision.call_id


def test_replay_compares_against_the_stored_call(ctx, monkeypatch):
    call_id = _make_call(ctx, monkeypatch)
    same_transport = FakeTransport(_reply({
        "route": "CODE", "reason": "changes requested", "round_summary": "fix it",
    }))
    result = replay_call(ctx, call_id, transport=same_transport)
    assert result["same"] is True
    assert same_transport.calls == 1
    assert ctx.journal.latest("deepseek.replay")["detail"]["same"] is True

    changed_transport = FakeTransport(_reply({"route": "PLAN", "reason": "different"}))
    result2 = replay_call(ctx, call_id, transport=changed_transport)
    assert result2["same"] is False
