#!/usr/bin/env python3
"""SessionStart hook: inject the routing policy and warn about unmapped agents."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routing_lib  # noqa: E402


def policy_text(cfg):
    t = cfg["tiers"]
    cases = ", ".join(cfg["apex"]["cases"])
    eligible = ", ".join(cfg["apex"]["tag_eligible"])
    return "\n".join([
        f"Subagent model routing policy ({cfg['policy']}, revised {cfg['revised']}) is enforced by a PreToolUse hook.",
        f"- Opus tier: {t['opus']['model_id']} @ {t['opus']['effort']} (judgment, verification, default).",
        f"- Sonnet tier: {t['sonnet']['model_id']} @ {t['sonnet']['effort']} (bounded read/write/build).",
        f"- Apex tier: {t['apex']['model_id']} @ {t['apex']['effort']} - ONLY for: {cases}.",
        f"To request Apex for a {eligible} subagent, start its prompt with `[apex: <case>]`, e.g. `[apex: bugfix]`.",
        "Any other model/effort you pass to the Agent tool is overwritten by the policy map.",
        "Run /routing-map to see every agent's declared vs enforced model and effort.",
    ])


def main():
    try:
        cfg = routing_lib.load_config()
        text = policy_text(cfg)
        unmapped = [a["name"] for a in routing_lib.installed_agents()
                    if not routing_lib.lookup_tier(cfg, a["name"])[1]]
        if unmapped:
            text += (f"\nRouting drift: {len(unmapped)} installed agent(s) are not in the policy map "
                     f"and fall back to the {cfg['default_tier']} tier: {', '.join(unmapped)}.")
            print(f"[model-routing-policy] unmapped agents: {', '.join(unmapped)}", file=sys.stderr)
    except Exception as exc:  # fail-open
        print(f"[model-routing-policy] session hook error: {exc}", file=sys.stderr)
        return 0
    json.dump({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
              sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
