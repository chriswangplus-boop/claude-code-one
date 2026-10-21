"""Tests for the routing hook. Run: python3 -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)
import routing_lib  # noqa: E402

CFG = routing_lib.load_config()


def run_hook(payload, raw=None):
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "audit.jsonl")
        env = dict(os.environ, ROUTING_AUDIT_LOG=log)
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "enforce-routing.py")],
            input=raw if raw is not None else json.dumps(payload),
            capture_output=True, text=True, env=env, timeout=10,
        )
        with open(log) as f:
            audit = [json.loads(line) for line in f]
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, out, audit


class ResolveTests(unittest.TestCase):
    def check(self, tool_input, tier, reason_prefix):
        updated, d = routing_lib.resolve(CFG, tool_input)
        spec = CFG["tiers"][tier]
        self.assertEqual(d["tier"], tier)
        self.assertEqual(updated["model"], spec["alias"])
        self.assertEqual(updated["effort"], spec["effort"])
        self.assertTrue(d["reason"].startswith(reason_prefix), d["reason"])
        return updated, d

    def test_planner_is_apex(self):
        self.check({"subagent_type": "pge-orchestrator:planner", "prompt": "x"}, "apex", "mapped")

    def test_bare_planner_is_apex(self):
        self.check({"subagent_type": "planner", "prompt": "x"}, "apex", "mapped")

    def test_generator_is_sonnet_tier_and_overrides_caller(self):
        u, d = self.check({"subagent_type": "generator", "prompt": "x", "model": "opus", "effort": "max"},
                          "sonnet", "apex_denied")
        self.assertEqual(d["model_id"], "claude-haiku-5-5")
        self.assertEqual(u["effort"], "xhigh")

    def test_apex_tag_granted_on_general_purpose(self):
        self.check({"subagent_type": "general-purpose", "prompt": "[apex: bugfix] fix the crash"},
                   "apex", "apex-tag:bugfix")

    def test_apex_tag_unknown_case_denied(self):
        self.check({"subagent_type": "general-purpose", "prompt": "[apex: refactor] tidy"},
                   "opus", "apex_denied:refactor")

    def test_apex_tag_ineligible_agent_denied(self):
        self.check({"subagent_type": "Explore", "prompt": "[apex: strategy] look"},
                   "sonnet", "apex_denied:strategy")

    def test_untagged_opus_request_downgraded(self):
        self.check({"subagent_type": "general-purpose", "prompt": "x", "model": "opus"},
                   "opus", "apex_denied:untagged-opus")

    def test_unknown_agent_gets_default(self):
        self.check({"subagent_type": "someplugin:mystery", "prompt": "x"}, "opus", "unmapped-default")

    def test_missing_subagent_type_is_general_purpose(self):
        _, d = self.check({"prompt": "x"}, "opus", "mapped")
        self.assertEqual(d["subagent_type"], "general-purpose")

    def test_other_fields_preserved(self):
        u, _ = routing_lib.resolve(CFG, {"subagent_type": "Explore", "prompt": "p",
                                         "description": "d", "run_in_background": True})
        self.assertEqual((u["prompt"], u["description"], u["run_in_background"]), ("p", "d", True))


class HookProcessTests(unittest.TestCase):
    def test_hook_emits_updated_input_and_audits(self):
        code, out, audit = run_hook({"tool_name": "Agent",
                                     "tool_input": {"subagent_type": "Explore", "prompt": "find x"}})
        self.assertEqual(code, 0)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["updatedInput"]["model"], "haiku")
        self.assertEqual(hso["updatedInput"]["effort"], "xhigh")
        self.assertEqual(audit[0]["tier"], "sonnet")

    def test_malformed_json_fails_open(self):
        code, out, audit = run_hook(None, raw="{not json")
        self.assertEqual(code, 0)
        self.assertIsNone(out)
        self.assertIn("error", audit[0])


class ConfigTests(unittest.TestCase):
    def test_every_agent_maps_to_a_real_tier(self):
        for agent, tier in CFG["agents"].items():
            self.assertIn(tier, CFG["tiers"], agent)

    def test_effort_values_are_valid(self):
        for spec in CFG["tiers"].values():
            self.assertIn(spec["effort"], {"low", "medium", "high", "xhigh", "max"})


if __name__ == "__main__":
    unittest.main()
