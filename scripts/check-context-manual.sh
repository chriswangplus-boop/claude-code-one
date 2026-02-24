#!/usr/bin/env bash
#
# Context Window Monitor - Manual Report Trigger
#
# Runs as a UserPromptSubmit hook. Checks if the user's prompt
# contains trigger keywords like "@context" or "context status".
# If matched, generates a full usage report.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INPUT=$(cat)

# Extract the user's prompt text
USER_PROMPT=""
if command -v python3 &>/dev/null; then
    USER_PROMPT=$(echo "$INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('user_prompt', data.get('prompt', '')).lower())
" 2>/dev/null || echo "")
fi

# Check for trigger keywords
case "$USER_PROMPT" in
    *"@context"*|*"context status"*|*"context usage"*|*"context check"*|*"check context"*)
        # Trigger manual report
        CONTEXT_MONITOR_MODE=report exec python3 "$SCRIPT_DIR/check-context.py" --report <<< "$INPUT"
        ;;
    *)
        # Not a trigger, pass through
        exit 0
        ;;
esac
