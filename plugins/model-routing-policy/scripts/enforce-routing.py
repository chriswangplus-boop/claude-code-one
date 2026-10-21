#!/usr/bin/env python3
"""PreToolUse hook: stamp routing-policy model + effort onto every Agent/Task call.

Fail-open: any error leaves the call unchanged and is logged.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routing_lib  # noqa: E402


def main():
    try:
        event = json.load(sys.stdin)
        tool_input = event.get("tool_input") or {}
        cfg = routing_lib.load_config()
        updated, decision = routing_lib.resolve(cfg, tool_input)
    except Exception as exc:  # fail-open by design
        routing_lib.append_audit({"error": f"{type(exc).__name__}: {exc}"})
        return 0

    routing_lib.append_audit(decision)
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": (
                f"routing policy: {decision['subagent_type']} -> {decision['tier']} tier "
                f"({decision['model_id']} @ {decision['effort']}, {decision['reason']})"
            ),
            "updatedInput": updated,
        }
    }, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
