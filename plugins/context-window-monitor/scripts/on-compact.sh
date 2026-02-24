#!/usr/bin/env bash
#
# On-Compact Hook
#
# Runs before context compaction to reset the monitoring state
# for the current session, so the monitor starts fresh after compaction.
#

set -euo pipefail

INPUT=$(cat)

# Extract session_id
SESSION_ID=""
if command -v python3 &>/dev/null; then
    SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
else
    SESSION_ID=$(echo "$INPUT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' 2>/dev/null || echo "")
fi

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

# Reset state file so monitoring starts fresh after compaction
STATE_FILE="$HOME/.claude/context-monitor/${SESSION_ID}.json"
if [ -f "$STATE_FILE" ]; then
    rm -f "$STATE_FILE"
fi

# Log the compaction event
echo "[Context Window Monitor] Context compacted. Monitoring state reset."
exit 0
