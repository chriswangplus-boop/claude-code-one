#!/usr/bin/env python3
"""
Context Window Monitor for Claude Code.

Runs as a Stop hook to estimate context window usage from the session transcript.
When usage exceeds a configurable threshold, blocks the stop with a warning
so Claude can inform the user and offer to compact/save memory.

Tracks per-session state to avoid repeated warnings within a cooldown period.
"""

import sys
import json
import os
import time


def load_config():
    """Load configuration with defaults."""
    defaults = {
        "threshold_percent": 80,
        "warning_cooldown_minutes": 5,
        "max_context_tokens": 200000,
        "chars_per_token": 4,
    }

    # Check for plugin config
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    config_path = os.path.join(plugin_root, "config", "defaults.json") if plugin_root else ""

    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                user_config = json.load(f)
                defaults.update(user_config)
        except (json.JSONDecodeError, IOError):
            pass

    # Environment variable overrides
    if os.environ.get("CONTEXT_MONITOR_THRESHOLD"):
        defaults["threshold_percent"] = int(os.environ["CONTEXT_MONITOR_THRESHOLD"])
    if os.environ.get("CONTEXT_MONITOR_COOLDOWN"):
        defaults["warning_cooldown_minutes"] = int(os.environ["CONTEXT_MONITOR_COOLDOWN"])
    if os.environ.get("CONTEXT_MONITOR_MAX_TOKENS"):
        defaults["max_context_tokens"] = int(os.environ["CONTEXT_MONITOR_MAX_TOKENS"])

    return defaults


def get_state_path(session_id):
    """Get the path to the per-session state file."""
    state_dir = os.path.join(os.path.expanduser("~"), ".claude", "context-monitor")
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, f"{session_id}.json")


def load_state(session_id):
    """Load per-session monitoring state."""
    state_path = get_state_path(session_id)
    if os.path.exists(state_path):
        try:
            with open(state_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"last_warning_time": 0, "warning_count": 0, "last_usage_pct": 0}


def save_state(session_id, state):
    """Save per-session monitoring state."""
    state_path = get_state_path(session_id)
    try:
        with open(state_path, "w") as f:
            json.dump(state, f)
    except IOError:
        pass


def cleanup_old_states(max_age_hours=24):
    """Remove state files older than max_age_hours."""
    state_dir = os.path.join(os.path.expanduser("~"), ".claude", "context-monitor")
    if not os.path.exists(state_dir):
        return
    now = time.time()
    cutoff = now - (max_age_hours * 3600)
    try:
        for f in os.listdir(state_dir):
            fpath = os.path.join(state_dir, f)
            if os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
    except (IOError, OSError):
        pass


def estimate_tokens_from_transcript(transcript_path, chars_per_token):
    """
    Parse the transcript JSONL file and estimate token count from text content.
    Returns (estimated_tokens, message_count).
    """
    total_chars = 0
    message_count = 0

    try:
        with open(transcript_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    total_chars += len(line)
                    continue

                # Count message content
                if "message" in entry:
                    message_count += 1
                    msg = entry["message"]
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            total_chars += len(content)
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict):
                                    if block.get("type") == "text":
                                        total_chars += len(block.get("text", ""))
                                    elif block.get("type") == "tool_use":
                                        total_chars += len(
                                            json.dumps(block.get("input", {}))
                                        )
                                    elif block.get("type") == "tool_result":
                                        result_content = block.get("content", "")
                                        if isinstance(result_content, str):
                                            total_chars += len(result_content)
                                        elif isinstance(result_content, list):
                                            for rc in result_content:
                                                if (
                                                    isinstance(rc, dict)
                                                    and rc.get("type") == "text"
                                                ):
                                                    total_chars += len(
                                                        rc.get("text", "")
                                                    )
                                elif isinstance(block, str):
                                    total_chars += len(block)

                # Count tool inputs/outputs at top level
                if "tool_input" in entry:
                    ti = entry["tool_input"]
                    if isinstance(ti, str):
                        total_chars += len(ti)
                    else:
                        total_chars += len(json.dumps(ti))

                if "tool_result" in entry:
                    tr = entry["tool_result"]
                    if isinstance(tr, str):
                        total_chars += len(tr)
                    elif isinstance(tr, dict):
                        total_chars += len(json.dumps(tr))
                    elif isinstance(tr, list):
                        total_chars += len(json.dumps(tr))

    except (IOError, OSError):
        return 0, 0

    estimated_tokens = total_chars // chars_per_token
    return estimated_tokens, message_count


def format_tokens(n):
    """Format token count for display."""
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def generate_usage_bar(usage_pct):
    """Generate a visual progress bar for context usage."""
    bar_width = 30
    filled = int(bar_width * min(usage_pct, 100) / 100)
    empty = bar_width - filled
    bar = "#" * filled + "-" * empty
    return f"[{bar}] {usage_pct}%"


def generate_report(estimated_tokens, max_tokens, usage_pct, message_count, config):
    """Generate a full context usage report (always printed, regardless of threshold)."""
    lines = []
    lines.append("[Context Window Monitor] Status Report")
    lines.append("=" * 50)
    lines.append("")
    lines.append(f"  Estimated tokens:  {format_tokens(estimated_tokens)} / {format_tokens(max_tokens)}")
    lines.append(f"  Messages:          {message_count}")
    lines.append(f"  Usage:             {generate_usage_bar(usage_pct)}")
    lines.append("")

    remaining_tokens = max(0, max_tokens - estimated_tokens)
    lines.append(f"  Remaining:         ~{format_tokens(remaining_tokens)} tokens")
    lines.append(f"  Alert threshold:   {config['threshold_percent']}%")
    lines.append("")

    if usage_pct >= 95:
        lines.append("  Status: CRITICAL - Compact immediately!")
    elif usage_pct >= 90:
        lines.append("  Status: HIGH - Consider compacting soon")
    elif usage_pct >= config["threshold_percent"]:
        lines.append("  Status: WARNING - Approaching limit")
    elif usage_pct >= 50:
        lines.append("  Status: OK - Moderate usage")
    else:
        lines.append("  Status: OK - Plenty of room")

    lines.append("")
    lines.append("  Actions: /memory (save context) | /compact (compress)")
    lines.append("=" * 50)
    return "\n".join(lines)


def run_report_mode(hook_input):
    """
    Manual report mode: always prints a full status report.
    Triggered by UserPromptSubmit hook when user types a trigger keyword.
    """
    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path or not os.path.exists(transcript_path):
        print("[Context Window Monitor] No transcript found for this session.", file=sys.stderr)
        sys.exit(2)

    config = load_config()
    estimated_tokens, message_count = estimate_tokens_from_transcript(
        transcript_path, config["chars_per_token"]
    )
    max_tokens = config["max_context_tokens"]
    usage_pct = (estimated_tokens * 100) // max_tokens if max_tokens > 0 else 0

    report = generate_report(estimated_tokens, max_tokens, usage_pct, message_count, config)
    print(report, file=sys.stderr)
    print("", file=sys.stderr)
    print("Please share this report with the user.", file=sys.stderr)
    # Exit 2 to inject the report into the conversation
    sys.exit(2)


def run_auto_mode(hook_input):
    """
    Automatic mode: runs on Stop hook, only alerts if usage exceeds threshold.
    Respects cooldown to avoid repeated warnings.
    """
    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path", "")

    if not transcript_path or not os.path.exists(transcript_path):
        sys.exit(0)

    config = load_config()

    # Periodically clean up old state files
    cleanup_old_states()

    # Estimate context window usage
    estimated_tokens, message_count = estimate_tokens_from_transcript(
        transcript_path, config["chars_per_token"]
    )
    max_tokens = config["max_context_tokens"]
    usage_pct = (estimated_tokens * 100) // max_tokens if max_tokens > 0 else 0

    # Load session state
    state = load_state(session_id)

    # Check cooldown
    now = time.time()
    cooldown_seconds = config["warning_cooldown_minutes"] * 60
    time_since_last = now - state.get("last_warning_time", 0)

    # Update state with current usage
    state["last_usage_pct"] = usage_pct

    threshold = config["threshold_percent"]

    if usage_pct >= threshold:
        # Check if we're within cooldown
        if time_since_last < cooldown_seconds and state.get("warning_count", 0) > 0:
            # Within cooldown, just update state silently
            save_state(session_id, state)
            sys.exit(0)

        # Issue warning
        state["last_warning_time"] = now
        state["warning_count"] = state.get("warning_count", 0) + 1
        save_state(session_id, state)

        severity = "CRITICAL" if usage_pct >= 95 else "HIGH" if usage_pct >= 90 else "WARNING"

        print(f"[Context Window Monitor] {severity}: ~{usage_pct}% used ({format_tokens(estimated_tokens)} / {format_tokens(max_tokens)} tokens, {message_count} messages)", file=sys.stderr)
        print("", file=sys.stderr)

        if usage_pct >= 95:
            print("The context window is nearly full. Auto-compaction may happen soon and could lose important context.", file=sys.stderr)
            print("", file=sys.stderr)
            print("STRONGLY RECOMMENDED: Inform the user immediately and suggest:", file=sys.stderr)
            print("  1. Run /memory to save key decisions and progress before compacting", file=sys.stderr)
            print("  2. Run /compact to compress the conversation", file=sys.stderr)
            print("  3. Or start a new session if the current task is complete", file=sys.stderr)
        elif usage_pct >= 90:
            print("The context window is getting very full.", file=sys.stderr)
            print("", file=sys.stderr)
            print("Please inform the user and recommend:", file=sys.stderr)
            print("  1. Run /memory to save important context", file=sys.stderr)
            print("  2. Run /compact to compress the conversation", file=sys.stderr)
        else:
            print("The context window is approaching its limit.", file=sys.stderr)
            print("", file=sys.stderr)
            print("Please let the user know and offer these options:", file=sys.stderr)
            print("  1. Run /memory to save important context, then /compact", file=sys.stderr)
            print("  2. Continue working (next check in {} minutes)".format(config["warning_cooldown_minutes"]), file=sys.stderr)

        # Exit code 2 blocks the stop and injects this message into conversation
        sys.exit(2)
    else:
        save_state(session_id, state)
        sys.exit(0)


def main():
    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # Determine mode: --report flag or CONTEXT_MONITOR_MODE env var
    mode = os.environ.get("CONTEXT_MONITOR_MODE", "auto")
    if "--report" in sys.argv:
        mode = "report"

    if mode == "report":
        run_report_mode(hook_input)
    else:
        run_auto_mode(hook_input)


if __name__ == "__main__":
    main()
