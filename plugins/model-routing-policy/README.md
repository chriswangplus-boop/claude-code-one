# model-routing-policy

Third-party plugins and skills launch subagents with whatever `model` they declare (often an alias like `opus`, or nothing, so they inherit the parent model) and never set `effort`. This plugin makes **your** routing policy win.

## How it works

| Piece | What it does |
|---|---|
| `config/routing-v2.5.json` | Single source of truth: tiers (model + effort) and the agent → tier map |
| `scripts/enforce-routing.py` (PreToolUse on `Agent\|Task`) | Overwrites `model` and `effort` on every subagent dispatch from the map, and logs each decision to `~/.claude/state/routing-audit.jsonl`. Fail-open: an error leaves the call unchanged. |
| `scripts/session-policy.py` (SessionStart) | Tells the session the policy and the Apex tag syntax, and warns about installed agents missing from the map |
| `/routing-map` | Prints every installed agent's declared vs enforced model and effort |

## Tiers

| Tier | Model | Effort | Use |
|---|---|---|---|
| `opus` | `claude-sonnet-5-5` | `high` | judgment, verification, default |
| `sonnet` | `claude-haiku-5-5` | `xhigh` | bounded read / write / build |
| `apex` | `claude-opus-5-5` | `medium` | only: hardest, strategy, decision, bugfix, master-plan |

Apex is granted only to agents mapped to it (`planner`, `Plan`), or to a `general-purpose`/`claude` dispatch whose prompt starts with `[apex: <case>]`, e.g. `[apex: bugfix]`. Any other `model: opus` request is logged as `apex_denied`.

## Known limits

- **Aliases, not full IDs.** The Agent tool accepts only `sonnet|opus|haiku|fable`. A full model ID in `updatedInput` fails schema validation and blocks the dispatch (tested 2026-10-21), so the hook emits the alias and logs the pinned ID. If an alias ever moves to a new model generation, update the map.
- **Skill-level `model:`** (e.g. skill-doctor's `rollback`/`treat`) is not a subagent dispatch and is not touched.
- Nested dispatches (a subagent that launches a subagent) and `Workflow`-tool agents were not verified: in the cloud session used for testing, subagents had no Agent tool.

## Changing the policy

Edit `config/routing-v2.5.json`, then run the tests:

```bash
python3 -m unittest discover -s plugins/model-routing-policy/tests
```
