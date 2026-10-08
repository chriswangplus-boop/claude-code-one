# Tiered, Commit-Focused `security-guidance` Plugin (rev. 2)

> Revision of the original draft. Every claim about plugin behavior below was
> checked against the upstream source: `security-guidance` **v2.0.11**,
> `anthropics/claude-plugins-official` @ `315c4e4` (2026-10-08). File:line
> references point into that version's `hooks/` directory.

## Summary

Keep the security reviews, but run them only when they're worth it:

| Lane | When it runs | Model | Effort |
|---|---|---|---|
| **Commit / push** | Every `git commit`, `git push`, `gt create/modify/submit` | `glm-5.3-flash` | `high` |
| **Stop / SubagentStop** | End of a turn, **only if** the turn's diff is big **or** touches a high-risk path | `glm-5.3-flash` | `medium` |
| Pattern rules (regex) | Off (`ENABLE_PATTERN_RULES=0`) | none | none |

"Effort" is the dial that controls how much thinking the model does before it answers. `high` costs more tokens and catches more.

The changes live in a **git-tracked fork inside this repo's marketplace**, so a plugin update can never quietly undo them.

**Phase 0 comes first:** a short probe of the z.ai endpoint decides how much of Patch C is needed (see below). The draft assumed that only `effort` might fail, but the plugin sends two other fields that z.ai may also reject.

---

## What changed from the draft, and why

| # | Draft said | Source shows | Revision |
|---|---|---|---|
| 1 | Fork into `~/.codex/plugins/local/…` | Claude Code caches plugins under `~/.claude/plugins/cache/`. A folder copied into a plugins directory is never loaded unless a **marketplace** declares it. | Host the fork in this repo (`chriswangplus-plugins` marketplace) as `security-guidance-tiered`. |
| 2 | Skip small diffs but "mark consumed so they don't pile up into one giant stale diff" | Nothing piles up. `UserPromptSubmit` resets the baseline every turn (`security_reminder_hook.py:519-596`), so each Stop sees **only that turn's edits**, and `consume_stop_state` clears the state before any skip. | Keep the skip, using plain `_skip(reason)` with **no** `restore=True`. The real blind spot is the reverse: small edits that never get committed are never reviewed. That's accepted, because the commit lane is the backstop. |
| 3 | Gate = lines < 300 **and** ≤ 5 files | Size alone lets a 10-line change to `auth/` through unreviewed | Add an **always-review path list**: a small diff touching auth, secrets, shell exec, SQL, CI workflows and similar paths is still reviewed. |
| 4 | Add `effort` to `_call_claude()` | `_call_claude` is reached through `analyze_code_security → _call_claude_dual_or`, and **three** lanes call it: Stop (`:2082`), commit race fallback (`:1402/1406`), push sweep (`:1800/1804`) | Thread `effort` through all three functions. Stop passes `medium`; commit and push pass `high`. |
| 5 | Commit review = child CLI only | Commit review is a **race** (`security_reminder_hook.py:959-1025`). The agentic child CLI starts at t=0, and the single-shot HTTP reviewer starts at t=180 s (`SG_AGENTIC_RACE_DELAY_S`). The first to finish wins. | Set `high` on **both** racers. |
| 6 | "Pin the child's effort setting" (mechanism not named) | Child runs with `setting_sources=[]` (`llm.py:1460`), so your `settings.json` effort is ignored. CLI 2.1.295 reads `CLAUDE_CODE_EFFORT_LEVEL` and accepts `--effort low\|medium\|high\|xhigh\|max`. | Set `CLAUDE_CODE_EFFORT_LEVEL=high` in `_agentic_spawn_env()`. This works with every SDK version. |
| 7 | Retry once without `effort` on rejection | GLM is an unknown model ID, so the plugin **defaults to adaptive thinking** (`llm.py:320-323`). Every request then sends `thinking:{type:"adaptive"}` + `output_config.effort` + `output_config.format` (JSON schema). A third-party GLM reference lists the Messages fields it accepts, and `output_config` is not among them. | A **400-fallback ladder** (Patch C), with steps enabled according to Phase 0's results. |
| 8 | (not covered) | Setting `SECURITY_REVIEW_MODEL` **turns off** the built-in Sonnet fallback (`llm.py:801`: `if r is None and not explicit`). Any 400 then means **no review at all**, and nothing tells you. | Patch C is mandatory, not "resilience". Turn on the debug log during rollout. |
| 9 | (not covered) | With `SG_AGENTIC_MODEL` set to a GLM ID, the child gets `fallback_model="opus"` (`llm.py:1475`) and `FALLBACK_FOR_ALL_PRIMARY_MODELS=1` (`llm.py:1279`). On overload, the child would send a **Claude Opus ID** to z.ai. | In `glm()`, map the aliases: `ANTHROPIC_DEFAULT_OPUS_MODEL` / `_SONNET_` / `_HAIKU_` → GLM IDs. |
| 10 | Gate covers "the Stop path" | `SubagentStop` runs the same `handle_stop_hook` | The gate lives inside `handle_stop_hook`, so it covers both events automatically. |

---

## Phase 0: probe the endpoint (≈10 min, before any patching)

Inside a `glm()` shell, send four requests to `$ANTHROPIC_BASE_URL/v1/messages` with model `glm-5.3-flash`. Each one adds one field:

| Probe | Payload adds | Tells you |
|---|---|---|
| P1 | nothing (plain prompt) | auth + model ID work |
| P2 | `thinking:{type:"adaptive"}` | adaptive thinking accepted? |
| P3 | P2 + `output_config:{effort:"medium"}` | effort accepted, ignored, or 400? |
| P4 | `output_config:{format:{type:"json_schema",schema:{…}}}` | structured output **enforced**, ignored, or 400? |

For P4, ask for a schema the model would not produce by accident, then check whether the reply actually matches it. If z.ai accepts the field but ignores it, the review breaks quietly: the plugin's `json.loads` fails and the call returns `None`.

**Output:** a 4-row pass/fail table. That table decides which steps of Patch C are needed. It also settles assumption A1 below.

---

## Key changes

### 1. Fork the plugin into this repo's marketplace

- Copy upstream `plugins/security-guidance/` (v2.0.11) → `plugins/security-guidance-tiered/` here. Leave out `__pycache__`, keep `LICENSE` (Apache-2.0), and add a `NOTICE`/README line saying it was modified from upstream `315c4e4`.
- Rename it in `.claude-plugin/plugin.json` (`"name": "security-guidance-tiered"`, `"version": "2.0.11-tiered.1"`) and add an entry to `.claude-plugin/marketplace.json`.
- Commit the untouched copy first, then each patch as its own commit. `git diff <copy-commit>..HEAD` is then exactly your delta, and that makes re-basing onto a new upstream release mechanical.
- `/plugin` → disable `security-guidance@claude-plugins-official`, install and enable `security-guidance-tiered@chriswangplus-plugins`. Both share the `~/.claude/security/` state dir, so only one may be active at a time.
- **Drift check:** each month, diff upstream `plugins/security-guidance` against `315c4e4`. Port any security-relevant upstream fixes.

### 2. Patch A: gate the Stop review (`hooks/security_reminder_hook.py`)

Insert the gate right after `diff_files = filter_preexisting_from_diff(...)` (`:2072`) and before `analyze_code_security` (`:2082`). At that point the diff holds only this turn's new content in reviewable source files, so the count is honest.

```python
SG_STOP_MIN_LINES      = int(os.environ.get("SG_STOP_MIN_LINES", "300"))   # 0 = gate off
SG_STOP_MAX_SKIP_FILES = int(os.environ.get("SG_STOP_MAX_SKIP_FILES", "5"))
_ALWAYS_REVIEW_TOKENS = (  # deliberately narrower than gitutil's _SECURITY_RISK_PATH_TOKENS
    "auth", "login", "session", "token", "secret", "credential", "passw",
    "perm", "acl", "rbac", "iam", "crypt", "exec", "subprocess", "shell",
    "pickle", "deser", "sql", ".env", "dockerfile", ".github/workflows",
)

added = sum(sum(1 for l in c.splitlines() if l.startswith("+") and not l.startswith("+++"))
            for _, c in diff_files)
risky = any(t in fp.lower() for fp, _ in diff_files for t in _ALWAYS_REVIEW_TOKENS)
if (SG_STOP_MIN_LINES > 0 and added < SG_STOP_MIN_LINES
        and len(diff_files) <= SG_STOP_MAX_SKIP_FILES and not risky):
    _skip(51, added_lines=added, diff_files_count=len(diff_files))  # no restore → consumed
```

- It counts **added** lines only. Deletions rarely introduce vulnerabilities, and counting them would let a big cleanup trigger a review.
- Skip code `51` follows the plugin's convention, where 50+ marks an opt-out skip (`:1983-1984`).
- Why not reuse `_SECURITY_RISK_PATH_TOKENS` (`gitutil.py:864`)? It includes broad tokens like `view`, `client`, `request` and `parse` that match a large share of ordinary files. The gate would almost never skip.

### 3. Patch B: effort tiering on every path (`hooks/llm.py`, `hooks/security_reminder_hook.py`)

**B1: direct-HTTP reviewer (Stop, commit fallback, push sweep)**
- `_call_claude(..., effort="high")`: the adaptive branch (`llm.py:686`) uses `effort` instead of the literal `"high"`.
- `_call_claude_dual_or(..., effort=...)` and `analyze_code_security(..., effort=...)` pass the value through.
- Callers:
  - Stop (`:2082`) → `os.environ.get("SG_STOP_EFFORT", "medium")`
  - commit (`:1402`, `:1406`) and push sweep (`:1800`, `:1804`) → `os.environ.get("SG_COMMIT_EFFORT", "high")`

**B2: agentic child CLI (commit race, t=0)**
- In `_agentic_spawn_env()` add `"CLAUDE_CODE_EFFORT_LEVEL": os.environ.get("SG_COMMIT_EFFORT", "high")`.
- Optional belt-and-braces: also pass `effort=` to `ClaudeAgentOptions` **only if** the installed SDK has the field (`"effort" in ClaudeAgentOptions.__dataclass_fields__`). SDK 0.2.159 has it. The plugin's venv installs an unpinned SDK, so an older venv may not.

### 4. Patch C: 400-fallback ladder (`hooks/llm.py::_call_claude`)

On an HTTP **400** (and only 400; 429/5xx keep the existing retry), retry with a smaller payload, one step at a time:

1. drop `output_config.effort`
2. drop `thinking` (GLM thinks by default anyway, per the third-party reference)
3. drop `output_config.format`, append the JSON schema to the prompt ("Reply with only a JSON object matching this schema: …"), and parse leniently: strip a ```` ```json ```` fence and take the first `{…}` block.

- **Remember the working shape** per `(sha256(base_url)[:16], model)` in the state dir, the same way `default_review_model()` caches (`llm.py:474-507`). Then only the first call ever pays for the 400s.
- If Phase 0 shows z.ai *accepts but ignores* `format`, make step 3 the default for GLM models instead of a fallback. A 400 never arrives in that case, so the ladder would never trigger.
- Apply the same prompt-schema idea to the child CLI only if Phase 0/live tests show `error_max_structured_output_retries`. The CLI handles structured output on its own side, so this may not be needed.

### 5. Environment configuration

Inside the `glm()` launcher (next to `ANTHROPIC_BASE_URL`). Plugin hooks inherit the session's environment, so this keeps GLM IDs out of non-GLM sessions:

```bash
export SECURITY_REVIEW_MODEL=glm-5.3-flash
export SG_AGENTIC_MODEL=glm-5.3-flash
export ANTHROPIC_DEFAULT_OPUS_MODEL=<GLM id>     # catches the child's fallback_model="opus"
export ANTHROPIC_DEFAULT_SONNET_MODEL=<GLM id>
export ANTHROPIC_DEFAULT_HAIKU_MODEL=<GLM id>
# optional during rollout:
export SECURITY_GUIDANCE_DEBUG_LOG="$HOME/.claude/security/glm-log.txt"
```

(If `glm()` already sets the three `ANTHROPIC_DEFAULT_*` vars, leave them as they are.)

Globally in `~/.zshrc`:

```bash
export ENABLE_PATTERN_RULES=0   # regex warnings on every edit; costs no tokens, so this is about noise, not money
```

Leave alone: `ENABLE_STOP_REVIEW=1`, `ENABLE_COMMIT_REVIEW=1` (defaults), and `SG_DUAL_OR` unset (default off; turning it on doubles every review call). `MAX_COMMIT_REVIEWS_PER_HOUR` defaults to 20, and `MAX_STOP_HOOK_FIRINGS` defaults to 3. Both are sane caps.

---

## Test plan

**Unit / fixture** (the bundled `tests/conftest.py` already runs a stub HTTP server at `ANTHROPIC_BASE_URL`, so payloads can be asserted):
1. `pytest tests/` passes unchanged after each patch commit.
2. `test_stop_gate.py`
   - 40 added lines, 2 files, no risky path → no POST to stub; state consumed (next Stop sees nothing).
   - 40 added lines in `src/auth/login.py` → POST made.
   - 400 added lines → POST made with `output_config.effort == "medium"`.
   - 40 lines across 6 files → POST made.
   - `SG_STOP_MIN_LINES=0` → POST made (gate disabled).
3. `test_effort_tiering.py`: commit fallback and push sweep send `effort == "high"`; `_agentic_spawn_env()["CLAUDE_CODE_EFFORT_LEVEL"] == "high"`.
4. `test_400_ladder.py`: stub returns 400 while a field is present. Assert the review still returns findings, and that a second call goes straight to the cached shape (exactly one POST).

**Live, under `glm()`** (watch the debug log):
5. Small edit + turn end → log shows `skip_reason 51`, no rewake.
6. Small edit to an auth file → review fires.
7. ≥300-line edit → review fires at `medium`, findings arrive as a rewake.
8. `git commit` → agentic review starts. Log shows the child launched with model `glm-5.3-flash`. If it runs past 180 s, the single-shot fallback starts at `high`.
9. Plant a known bug (e.g. `subprocess.run(user_input, shell=True)`) in a commit and confirm it gets flagged. This proves the reviews are *working*, not just *running*.
10. Duplicates: `/plugin` shows only the fork enabled, and each event produces one review entry in the log, not two.

---

## Assumptions and open questions

| | Assumption | Status | How it gets settled |
|---|---|---|---|
| A1 | z.ai's Anthropic-protocol endpoint accepts or safely ignores `thinking.adaptive`, `output_config.effort`, `output_config.format` | **Unverified.** A third-party GLM Messages reference lists accepted fields without `output_config`. | Phase 0 |
| A2 | Claude Code's CLI forwards `CLAUDE_CODE_EFFORT_LEVEL` for a non-Claude model ID | **Unverified.** The env var exists in CLI 2.1.295, but the CLI may only send effort to models it knows support it. | Live test 8 plus the proxy/debug log |
| A3 | `glm-5.3-flash` is good enough to catch real bugs at `high` | Untested | Live test 9; if recall is poor, move only the commit lane to a larger GLM model |
| A4 | 300 added lines / 5 files / risky-path list is the right "big diff" line | Judgment call | Tune `SG_STOP_MIN_LINES` and `SG_STOP_MAX_SKIP_FILES` after a week of logs |
| A5 | Small uncommitted work going unreviewed is acceptable | Design choice | Commit lane is the backstop; revisit if you often leave work uncommitted |

### Sources
- Upstream plugin: <https://github.com/anthropics/claude-plugins-official/tree/main/plugins/security-guidance> (v2.0.11 @ `315c4e4`)
- Third-party GLM Messages API reference (field list): <https://evolink.ai/docs/en/api-manual/language-series/glm/messages/messages-reference.md>
- Proxies dropping `output_config.effort`: <https://github.com/router-for-me/CLIProxyAPI/issues/1540>
- `glm-5.3-flash` listing: <https://openrouter.ai/z-ai/glm-5.3-flash>
