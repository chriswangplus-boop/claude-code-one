#!/usr/bin/env bash
#
# Context Window Monitor Plugin - Installer
#
# Installs the plugin for global use with Claude Code.
#
# Usage:
#   ./install.sh          # Install plugin
#   ./install.sh --remove # Remove plugin
#

set -euo pipefail

PLUGIN_NAME="context-window-monitor"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Context Window Monitor Plugin Installer"
echo "========================================"
echo ""

# Make scripts executable
chmod +x "$SCRIPT_DIR/scripts/check-context.sh"
chmod +x "$SCRIPT_DIR/scripts/check-context.py"
chmod +x "$SCRIPT_DIR/scripts/on-compact.sh"

if [ "${1:-}" = "--remove" ]; then
    echo "To remove this plugin, run in Claude Code:"
    echo "  /plugin uninstall $PLUGIN_NAME"
    echo ""
    echo "To also clean up monitoring state files:"
    echo "  rm -rf ~/.claude/context-monitor/"
    exit 0
fi

echo "This plugin monitors context window usage across all Claude Code sessions."
echo ""
echo "To install, run in Claude Code:"
echo "  /plugin install $SCRIPT_DIR"
echo ""
echo "Or manually add to ~/.claude/settings.json:"
echo ""
echo '  {'
echo '    "plugins": ['
echo "      \"$SCRIPT_DIR\""
echo '    ]'
echo '  }'
echo ""
echo "Configuration (config/defaults.json):"
echo "  threshold_percent: 80       # Alert when usage exceeds this %"
echo "  warning_cooldown_minutes: 5 # Minutes between repeated warnings"
echo "  max_context_tokens: 200000  # Model's context window size"
echo "  chars_per_token: 4          # Estimation ratio"
echo ""
echo "Environment variable overrides:"
echo "  CONTEXT_MONITOR_THRESHOLD=80"
echo "  CONTEXT_MONITOR_COOLDOWN=5"
echo "  CONTEXT_MONITOR_MAX_TOKENS=200000"
echo ""
echo "Done! Scripts are ready."
