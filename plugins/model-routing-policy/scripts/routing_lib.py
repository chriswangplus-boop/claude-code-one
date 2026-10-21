"""Shared routing logic for the model-routing-policy plugin."""
import glob
import json
import os
import re
from datetime import datetime, timezone

PLUGIN_ROOT = os.environ.get(
    "CLAUDE_PLUGIN_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
CONFIG_PATH = os.environ.get(
    "ROUTING_POLICY_CONFIG", os.path.join(PLUGIN_ROOT, "config", "routing-v2.5.json")
)
AUDIT_LOG = os.environ.get(
    "ROUTING_AUDIT_LOG", os.path.expanduser("~/.claude/state/routing-audit.jsonl")
)
APEX_TAG = re.compile(r"^\s*\[apex:\s*([a-z-]+)\s*\]", re.IGNORECASE)


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return json.load(f)


def base_name(subagent_type):
    """'pge-orchestrator:planner' -> 'planner'."""
    return subagent_type.rsplit(":", 1)[-1]


def lookup_tier(cfg, subagent_type):
    """Return (tier, mapped) for a subagent type, trying the full then the bare name."""
    agents = cfg["agents"]
    for key in (subagent_type, base_name(subagent_type)):
        if key in agents:
            return agents[key], True
    return cfg["default_tier"], False


def resolve(cfg, tool_input):
    """Decide the enforced tier for one Agent tool call.

    Returns (updated_input, decision) where decision is the audit record.
    """
    subagent_type = tool_input.get("subagent_type") or "general-purpose"
    tier, mapped = lookup_tier(cfg, subagent_type)
    reason = "mapped" if mapped else "unmapped-default"

    match = APEX_TAG.match(tool_input.get("prompt") or "")
    if match:
        case = match.group(1).lower()
        eligible = base_name(subagent_type) in cfg["apex"]["tag_eligible"]
        if case in cfg["apex"]["cases"] and eligible:
            tier, reason = "apex", f"apex-tag:{case}"
        else:
            reason = f"apex_denied:{case}"
    elif tool_input.get("model") == "opus" and tier != "apex":
        reason = "apex_denied:untagged-opus"

    spec = cfg["tiers"][tier]
    # The Agent tool accepts only aliases (sonnet|opus|haiku|fable); a full model ID in
    # updatedInput fails schema validation and blocks the dispatch. The pinned ID is logged.
    model = spec["alias"]
    updated = dict(tool_input, model=model, effort=spec["effort"])
    decision = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "subagent_type": subagent_type,
        "tier": tier,
        "model": model,
        "model_id": spec["model_id"],
        "effort": spec["effort"],
        "was_model": tool_input.get("model"),
        "was_effort": tool_input.get("effort"),
        "reason": reason,
    }
    return updated, decision


def append_audit(record, path=AUDIT_LOG):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass  # auditing must never block a dispatch


def read_frontmatter(path):
    """Return the YAML frontmatter of a .md file as a flat dict of top-level keys."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fields = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            fields[m.group(1)] = m.group(2).strip().strip("\"'")
    return fields


def _plugin_name(agent_path):
    """Walk up from an agent file to its .claude-plugin/plugin.json and return the name."""
    d = os.path.dirname(os.path.dirname(agent_path))
    manifest = os.path.join(d, ".claude-plugin", "plugin.json")
    try:
        with open(manifest) as f:
            return json.load(f).get("name")
    except (OSError, ValueError):
        return None


def installed_agents(home=None, project=None):
    """List installed agent definitions as dicts: name, source, model, effort, path."""
    home = home or os.path.expanduser("~/.claude")
    project = project or os.getcwd()
    found = []
    for path in sorted(glob.glob(os.path.join(home, "agents", "*.md"))):
        found.append((path, "user"))
    for path in sorted(glob.glob(os.path.join(project, ".claude", "agents", "*.md"))):
        found.append((path, "project"))
    for path in sorted(glob.glob(os.path.join(home, "plugins", "**", "agents", "*.md"), recursive=True)):
        found.append((path, "plugin"))

    agents = []
    for path, source in found:
        fm = read_frontmatter(path)
        if not fm.get("name"):
            continue  # README.md or a non-agent file
        name = fm["name"]
        if source == "plugin":
            plugin = _plugin_name(path)
            if not plugin:
                continue  # e.g. a src/ copy that is not a loaded agent
            name = f"{plugin}:{name}"
        agents.append({
            "name": name,
            "source": source,
            "model": fm.get("model") or "(inherit)",
            "effort": fm.get("effort") or "-",
            "path": path,
        })
    # De-duplicate identical name+source pairs (synced plugins can appear twice).
    seen, unique = set(), []
    for a in agents:
        key = (a["name"], a["source"])
        if key not in seen:
            seen.add(key)
            unique.append(a)
    return unique
