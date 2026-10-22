# Codex routing policy (GPT Routing v3.0)

These files are copied from the human's `~/.codex/` on 2026-10-22 and are the policy Relay checks
before it will run **Codex desktop** as supervisor. They are not used for Claude or Zcode.

| File | Role |
|---|---|
| `AGENTS.md` | the task-eligibility and delegation rules (GPT Routing v3.0) |
| `model-routing-strategy-v3.0.md` | the execution/workflow mapping |
| `routing-policy.json` | the machine-readable model, effort and agent defaults |
| `MANIFEST.json` | version, model, allowed efforts, and the sha256 of each file |

Rules Relay enforces:

- `relay start` refuses Codex when a file is missing or its hash differs from `MANIFEST.json`. It
  never substitutes Claude's routing rules.
- The model is pinned to `gpt-6.1-sol`; only `low`, `medium`, `high` are used.
- High is allowed only through the plan's High qualification assessment (Part 4; not built yet).

Verify the bundle:

```bash
python3 - <<'PY'
from relay.codex_policy import policy_status
print(policy_status())
PY
```

Refresh it after the human updates `~/.codex/`: replace the three files, regenerate
`MANIFEST.json`, then commit. Never edit these files by hand.
