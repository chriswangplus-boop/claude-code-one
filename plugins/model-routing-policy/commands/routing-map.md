---
description: Show every subagent's declared vs policy-enforced model and effort
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/routing-map.py"`

Show the routing map above to the user as-is. After it, list any agents marked "(default)"; they are unmapped and need a row in `config/routing-v2.5.json`.
