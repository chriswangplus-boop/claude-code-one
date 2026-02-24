#!/usr/bin/env bash
#
# Context Window Monitor - Entry Point
#
# Wrapper script that delegates to the Python monitor.
# Falls back to a simple file-size check if Python is unavailable.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INPUT=$(cat)

# Try Python first (more accurate transcript parsing)
if command -v python3 &>/dev/null; then
    echo "$INPUT" | python3 "$SCRIPT_DIR/check-context.py"
    exit $?
fi

# Fallback: simple file-size-based estimation using bash
TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")

# If we can't even extract the path, try with basic tools
if [ -z "$TRANSCRIPT_PATH" ]; then
    # Try to extract transcript_path with grep/sed
    TRANSCRIPT_PATH=$(echo "$INPUT" | grep -o '"transcript_path"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"transcript_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' 2>/dev/null || echo "")
fi

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
    echo "[Context Window Monitor] No transcript found — skipping." >&2
    exit 0
fi

# Simple file-size based check
# ~200K tokens * ~4 chars/token = ~800K chars
FILE_SIZE=$(wc -c < "$TRANSCRIPT_PATH" 2>/dev/null || echo "0")
MAX_CHARS=${CONTEXT_MONITOR_MAX_CHARS:-800000}
THRESHOLD=${CONTEXT_MONITOR_THRESHOLD:-80}

USAGE_PCT=$((FILE_SIZE * 100 / MAX_CHARS))

if [ "$USAGE_PCT" -ge "$THRESHOLD" ]; then
    echo "[Context Window Monitor] WARNING: ~${USAGE_PCT}% estimated usage (${FILE_SIZE} chars)" >&2
    echo "" >&2
    echo "The context window is approaching its limit." >&2
    echo "Please inform the user and suggest:" >&2
    echo "  1. Run /memory to save important context" >&2
    echo "  2. Run /compact to compress the conversation" >&2
    exit 2
fi

echo "[Context Window Monitor] OK: ~${USAGE_PCT}% estimated usage" >&2
exit 0
