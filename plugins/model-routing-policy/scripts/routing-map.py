#!/usr/bin/env python3
"""Print the modify map: each installed agent's declared vs enforced model and effort."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routing_lib  # noqa: E402


def main():
    cfg = routing_lib.load_config()
    rows = []
    for a in routing_lib.installed_agents():
        tier, mapped = routing_lib.lookup_tier(cfg, a["name"])
        spec = cfg["tiers"][tier]
        changed = a["model"] not in (spec["model_id"], spec["alias"]) or a["effort"] != spec["effort"]
        rows.append((a["name"], a["source"], a["model"], a["effort"],
                     f"{tier}{'' if mapped else ' (default)'}",
                     spec["model_id"], spec["effort"], "yes" if changed else "no"))

    print(f"# Routing map - {cfg['policy']} (revised {cfg['revised']})\n")
    print("| Agent | Source | Declared model | Declared effort | Tier | Enforced model | Enforced effort | Changed |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print("| " + " | ".join(r) + " |")
    unmapped = [r[0] for r in rows if r[4].endswith("(default)")]
    print(f"\n{len(rows)} agents, {sum(r[7] == 'yes' for r in rows)} changed by policy, "
          f"{len(unmapped)} unmapped{': ' + ', '.join(unmapped) if unmapped else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
