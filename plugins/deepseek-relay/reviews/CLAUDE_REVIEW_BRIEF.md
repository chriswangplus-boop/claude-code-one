# Independent review — Relay

You are an independent, adversarial code reviewer. Relay was written by a different AI (DeepSeek). Do not assume its README or tests are correct: verify claims against the code.

## Rules for this review

- READ-ONLY. Do not edit, create, delete, or move files. Do not run commands that modify the repository or call external APIs. You may read any file under the current directory.
- Do not trust the README's claims. Read the implementation.
- Do not report "not tested against the real desktop apps" as a defect; that is known and unavoidable here. Report what the code itself gets wrong or leaves dangerously unverified.
- Be concrete: file + line + code path + failure scenario + minimal fix. Prioritise P0/P1/P2/P3. Maximum 25 findings.
- If you cannot substantiate a finding from the code, omit it.

## What Relay is supposed to be

A Python 3.11+ macOS harness that drives the Claude Code desktop app and the Zcode desktop app through their GUIs to relay work between a planner, a reviewer and a coder. DeepSeek is the stateless judgment brain. The shared project folder holds `.relay/`:

- `BATON.json` — only one actor may write at a time.
- `STATE.json` — crash-safe state, pending turn, counters, sessions.
- `CLAUDE_DONE.json` / `CODER_DONE.json` — sentinels written last by agents, with round + output file + sha256 + verdict + context estimate.
- `PLAN.md`, `HANDOFF.md`, `REVIEW.md`, `CODER_REPORT.md`, `DECISIONS.md`, `PENDING_DECISION.md`, `MONITOR_NOTES.md`, `CHECKPOINT_<role>.md`, `RULES_*.md`.
- `journal/` — append-only hash-chained JSONL events; `journal/calls/` stores redacted DeepSeek prompts/responses; `journal/screens/` stores cropped screenshots.
- `monitor/` — independent monitor heartbeat, notes, correction file, HALT flag.
- `calibration/` — per-app GUI coordinates; `.relay/CALIBRATION_<app>.json` proves the shared working folder.

Core requirements to check against:
1. Pointer-only relay: the harness types one short ASCII line naming files; it never pastes file contents and never uses the clipboard.
2. One baton: before a turn snapshot hashes; after the sentinel, any file written by a non-owner is an ownership violation; all harness writes are atomic under a cross-process lock.
3. Sentinel validation: round, role, expected file, sha256, mtime freshness; stale/premature sentinels must not be accepted.
4. Turn policy: one git commit per turn, never push; pre-commit secret scan; uncommitted user edits must not be overwritten.
5. Context: rollover at ~70% using max(vision %, sentinel %, round cap), plus fresh chat per role; DeepSeek per-call context guard at 70%; host-guard for the hosting chat.
6. Independent monitor process: deterministic Layer 1 invariants (baton/wrong app, journal chain, decisions authorship, rollover, guard, budget, heartbeat, repeated feedback) plus an optional LLM Layer 2. It must be able to note/correct/halt but never click or type.
7. Security: minimal DeepSeek payloads, redaction before sending, window-only crops, keychain secrets, phone push only question+ID, local-only answers, prompt-injection fencing, deterministic popup allow-list, never auto-approve unknown dialogs.
8. Human flow: pending decision file, desktop + ntfy notification, local answer via CLI or ANSWER line, resume; new mid-run human instructions via `relay instruct` appended to DECISIONS with a real human journal event.
9. Host control: `relay start --detach` must outlive a host chat; `status --json`, `decide`, `instruct`, `pause`, `resume`, `stop`, `trace`, `why`, `replay`.
10. Google Drive is explicitly allowed; other sync folders are refused unless overridden.

## Where to look first

Read these, then follow the code paths:
- relay/orchestrator.py
- relay/state.py, relay/journal.py, relay/integrity.py
- relay/monitor/watchdog.py
- relay/brain/client.py, relay/brain/judgments.py, relay/redact.py
- relay/gui/app_driver.py, relay/gui/screen.py, relay/gui/calibrate.py, relay/gui/recovery.py
- relay/cli.py, relay/setup.py, relay/decisions.py, relay/policy.py
- prompts/RULES_CLAUDE.md, prompts/RULES_CODER.md, prompts/judgments/*.md
- tests/*.py

## Questions to answer

1. Does the orchestrator state machine handle resume, crash, timeout, needs_decision, blocked, ownership violation, secret detection, rollover and role switches without skipping/double-counting a round or sending a checkpoint to the wrong chat?
2. Can the baton/ownership/sentinel checks be bypassed accidentally by a legitimate code path (false positive) or by a malicious agent (false negative)?
3. Is the journal hash chain correct across day rollover, concurrent monitor/orchestrator writes, long lines, and corrupt tails? Are atomic writes actually crash-safe?
4. Does the monitor produce both false halts and missed violations? Is it truly independent (no GUI code, no shared memory)? Does the orchestrator's monitor heartbeat logic behave at startup and after a crash?
5. Are DeepSeek prompts/context guard/budget/cost accounting correct? Can secrets or raw source reach the API? Can an agent escape the untrusted block?
6. Does the GUI driver verify window/model/pointer-sent, crop only the target window, respect idle/corner failsafes, and handle vision unavailability safely?
7. Is the host-control path (`--detach`, monitor child, locks, `instruct`, `decide`, pause/resume) actually usable, or are there races/deadlocks/zombie processes?
8. Are the tests giving false confidence? Name the highest-value missing tests.
9. Any other correctness, security, or operability defect.

## Output format

# Independent review — Relay

Verdict: BLOCK | FIX-FIRST | SHIP-WITH-CAVEATS

## Findings
### P0/P1/P2/P3 — title
- Location: `file:line`
- Evidence: code path/quote
- Impact: concrete failure
- Fix: minimal change
- Test: how to catch it

## Test gaps
...

## What I could not verify
...

Be specific. Prefer fewer, substantiated findings over speculation.
