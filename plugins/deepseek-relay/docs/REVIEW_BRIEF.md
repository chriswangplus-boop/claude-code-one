# Independent review brief — Relay

You have the actual repository, not a bundle. Review the implementation in this folder.

## Ground truth

- `BUILD_PLAN.md` in this folder is the approved build plan.
- The implementation is `relay/`, `tests/`, `prompts/`, `config/`, `pyproject.toml`.
- Do not trust the README's claims; verify against code.
- Real desktop apps, a live DeepSeek key, and phone delivery are unavailable in this
  environment. Do not report that as a defect. Review what the code does and what it fails to
  guard against.

## Allowed

- Read any file.
- Run the test suite from this folder: `python3 -m pytest -q` (tests use temporary directories).
- Inspect git history and line numbers.
- Do **not** modify source, commit, or call external APIs/desktop apps.

## What to review, in priority order

1. Baton and file-ownership checks; sentinel round/sha256/mtime checks; stale/premature sentinels.
2. Crash/resume: can a round be skipped, double-counted, or replayed with stale state?
3. Role switching and context rollover: planner/reviewer chats, checkpoints, session counters.
4. Popup approval: confirm the decision is deterministic code, never the LLM.
5. Redactor and secret scan: can source or credentials reach DeepSeek, a commit, or a push?
6. 70% DeepSeek context guard, budget, and the monitor's own guard.
7. Monitor independence and Layer 1 invariants: false halts and missed violations; heartbeat at startup/loss.
8. Journal hash chain: day rollover, concurrent appends, long lines, corrupt tails, atomic writes.
9. Prompt injection fencing, untrusted agent text, human decision authorship.
10. Host control: `--detach`, monitor subprocess, locks, zombie processes, `instruct`/`decide` races.
11. Plan compliance: check each requirement in `BUILD_PLAN.md` item by item.

## Output

Return:
- Verdict: `BLOCK`, `FIX-FIRST`, or `SHIP-WITH-CAVEATS`.
- Findings ordered P0–P3, each with `file:line`, evidence, concrete failure, smallest fix, and a
  regression test.
- Test gaps that could produce false confidence.
- Anything you could not verify.

Maximum 25 findings. Omit anything you cannot substantiate from the code or tests.
