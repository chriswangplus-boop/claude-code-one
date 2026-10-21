# Revision status — Part A

## Session checkpoint (2026-10-21)

- No Relay loop is running; there is nothing to pause.
- Part A work is committed locally at `deepseek-relay-part-a` (first commit `23b7f0e`, remainder after it).
- Patch: `reviews/deepseek-relay-part-a.patch` (applies to PR #5 `85072b9`).
- Tests: **195 passed** (Python) and **24 passed** (`swift test`).
- Resume from the workspace root file `CONTINUE_HERE.md`.
- Remaining: push the branch (no credential on this Mac), the real-app calibration checks, and the
  full 30-minute Activity Monitor energy reading.

Base: `plugins/deepseek-relay` at PR #5 head `85072b9` (code + `CLAUDE_REVIEW.md` + `REVISION_PLAN.md`).

`pytest`: **195 passed** on macOS. `swift test`: **24 passed** in `apps/RelayWatch`. This file is
a handoff, not a substitute for the review tests.

## Fixed with regression tests

- **P1-1** one-time decision codes: generated per decision, stored only as a SHA-256 in `STATE.json`,
  shown only in the desktop notification/terminal, required by `relay decide --code` and the
  `ANSWER:` line. `relay instruct` requires a human TTY/dialog confirmation. Agent-forged answers
  are rejected; tests cover wrong/empty codes.
- **P1-2** control files are now in the turn snapshot. `STATE.json`, `BATON.json`, flags,
  `monitor/`, `answers/`, `calibration/`, `MONITOR_NOTES.md` need matching journal evidence;
  otherwise they are ownership violations. Tests cover each control file.
- **P1-3** popup policy uses anchored whole-command patterns, rejects shell metacharacters, and
  clicks buttons by Accessibility label. Vision-supplied coordinates are ignored. Tests include
  the three review exploits plus redirection/newlines; the driver test proves no coordinate click.
- **P1-4** `relay resume --clear-halt` journals `human.halt_cleared`; monitor history checks start
  after that watermark. Halt dedupe is event-ID based. Test: violation → halt → clear → no halt →
  new violation → halt.
- **P1-5 / P2-15** screenshots use `CGWindowListCopyWindowInfo` + `screencapture -l <windowID>`,
  never focus or raise. Screen checks are skipped while the user is active. Tests prove capture
  uses `-l` (not `-R`) and `_screen_check` makes zero focus calls.
- **P2-6** the phase transition is in the same `STATE.json` save that clears `pending`; crash test
  asserts the next phase is kept and the round is not duplicated.
- **P2-7** resume without a sentinel always calls `_ensure_session`; test crashes in REVIEW and
  verifies a new reviewer chat is opened.
- **P2-8** switching Claude roles checkpoints the outgoing role first; the e2e run now has three
  role-switch checkpoints.
- **P2-9** aborted turns record blocked paths/hashes and do not advance the round; resume refuses
  until the unsafe write is resolved. Test asserts no commit of the violating file.
- **P2-10** every state writer reloads under the lock: `_route`, `_ensure_session`,
  `_rollover`, `_process_turn`, `escalate_human`, `_prepare`, `_apply_correction`, `_abort_turn`
  and `_pause` all use `store.mutate`. Decision ids are allocated inside the lock, so two
  contexts cannot reuse a `D-xxx` id. Tests interleave a concurrent `relay instruct` into each
  writer (including a stale-read simulation for the two that have no slow work) and assert the
  instruction and decision counter survive.
- **P2-11** `relay journal repair` quarantines a corrupt tail and appends `journal.repaired`; test
  covers a partial line and a subsequent append.
- **P2-12** monitor tolerates the pending↔baton startup window.
- **P2-13** model label region is mandatory; the picker must visibly open before typing; vision
  unavailable or an unverified label raises `HumanRequired` and types nothing.
- **P2-14** startup requires both calibration proof files and both resolve to `project_dir`.
- **P3-17/18** `replans` and the new `stalls_in_milestone` reset on milestone completion; only
  `MILESTONE_DONE` advances the milestone (`APPROVED` now returns to PLAN without advancing).
  A regression test drives a stuck route and asserts the reset.
- **P3-19** untrusted fence is case-insensitive and handles whitespace.
- **P3-20** failed DeepSeek JSON/shape responses are recorded and charged before raising.
- **P3-21** secret placeholder filter is anchored; a real secret containing “none” is detected.
- **P3-22** runtime wiring test injects a fake backend, so it can run off macOS.

- **P3-16** a reached round cap no longer pauses forever. The loop asks a decision with three
  options (raise the cap, re-plan, stop for now); the answer is applied exactly once from
  `STATE.json` (`limit_overrides`), so no manual config edit is needed after a crash. Tests cover
  raise, stop, and the no-pause path. Caps gate new turns only, so a finished cycle is not
  interrupted before routing.

## Part B — model policy (done)

- **B1 roles:** `relay/model_policy.py` owns the role→app→model table; `planner`, `reviewer`,
  `strategist`, `fixer` (Claude) and `coder` (GLM). Ownership is role-based: `FIX_PLAN.md`
  (reviewer), `STRATEGY.md` (strategist), `FIX_REPORT.md` (fixer) and the new checkpoints were
  added; source files belong to the baton holder, so the fixer may edit them only on its turn
  and a coder write during a fixer turn is a violation. A fixer turn is capped at
  `fixer_max_files` (10) source files and may not touch `PLAN.md`/`REVIEW.md`.
- **B2 ladder:** `blocking_issue_ids` (slug-validated, ≤10) drive streaks. Streak 2 requests
  `FIX_PLAN.md` (journal `fix.plan_requested`); streak 4 or a double coder block runs the Opus
  fixer; a growing streak after that asks the human instead. The re-plan path is Sonnet planner →
  Opus strategist at the second stall → human at the third (`stall_cap`). Limits are 1 Opus turn
  per milestone (first plan exempt) and 3/day; exceeding one raises a three-option decision, and
  the answer (`opus_override`/`opus_declined`) is applied once and journaled. DeepSeek
  `suggest_escalation` is journaled, never a trigger.
- **B3 switching:** the model is selected and vision-verified before anything is typed; an
  unverified label raises `HumanRequired` and types nothing. Opus turns always get a fresh chat
  (checkpoint first). The reviewer's pointer after a fixer turn reads `FIX_REPORT.md`.
- **B4 journal/config:** `turn.start`, `turn.end` and `gui.model` carry `model`, `effort`, `role`,
  `model_verified` and `effort_verified`; `model.escalated` carries trigger, issue ids, streak and both counters
  with limits. `model_policy` replaced `claude_app.planner_model`/`reviewer_model`. `relay status`
  (text and `--json`) shows `Opus today: x/3, this milestone: y/1`.
- **B5 tests 1–11** are in `tests/test_model_policy.py` and `tests/test_integrity.py`, including
  the two-milestone acceptance run: Opus only for the first plan and one forced streak-4 fixer.

## Review #2 (done) — base `897eee3`

Review #2's verdict was FIX-FIRST with three P1s. All findings are fixed, in its suggested order.

- **N2 (git hooks/config).** Every git command runs with `core.hooksPath=/dev/null`,
  `core.fsmonitor=false`, `core.pager=cat`, `commit --no-verify` and `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_TERMINAL_PROMPT=0`, empty `GIT_EXTERNAL_DIFF`. `git_control_snapshot` hashes
  `.git/config`, `.git/info/attributes`, `.gitmodules` and `.git/hooks/*` across each turn; a
  change is an `integrity.violation` (`kind: git_control`) and pauses. Tests plant a pre-commit
  hook and a `core.fsmonitor` command and assert neither runs, plus a mid-turn hook pause.
- **N3 (coder never sees the review).** After `CHANGES_REQUESTED`, the CODE pointer names
  `.relay/REVIEW.md` first with its sha256; `RULES_CODER.md` says to address every requested
  change. The test coder parses the pointer, reads only the named files, and its report quotes the
  review.
- **N1 (human-only actions).** A `trust` store outside the project
  (`~/Library/Application Support/Relay/<project-id>/`, 0700) holds the journal HMAC key, the
  trusted tail anchor and the decision-code hashes. Every journal event is signed; `verify()`
  checks chain, hash, signature and anchor; ownership evidence and the monitor use only
  `trusted_events()`. `STATE.json` keeps only the decision id. `relay instruct` only accepts the
  native macOS dialog (stdin is never read). During `WAIT_HUMAN`, a fingerprint of `STATE.json`
  and the question text (the human's ANSWER line excluded) detects tampering and pauses. Tests
  reproduce the three review paths (rewriting `STATE.json` + ANSWER, a piped "y", a forged chained
  `decision.answered`/`human.halt_cleared`) and assert each fails.
- **N4 (stale FIX_PLAN).** The plan is followed only while its issues still block; on resolution
  or at a milestone boundary it is moved to `.relay/archive/FIX_PLAN-<round>.md` (journal
  `fix.plan_archived`, committed), and the pointer stops naming it. Test drives it to resolution.
- **N5 (uncounted Opus checkpoint turns).** `strategist`/`fixer` chats are never checkpointed or
  rolled over; they always get a fresh chat and their outcome is in `STRATEGY.md`/`FIX_REPORT.md`.
  Test asserts the Opus turn count equals `model.escalated` and no fixer/strategist checkpoint
  exists.
- **N6 (popup text/arguments).** The dialog text and buttons come from Accessibility
  (`read_dialog`); an unreadable dialog raises `HumanRequired` instead of approving. Command
  arguments reject `--output`/`-o`, `--ext-diff`, `-c`, `--no-verify`, `-p`, `--rootdir`,
  absolute paths and `.git/…`; `git add`/`git commit` are no longer auto-approved. Tests cover the
  five review strings and the AX path (no screenshot/vision).
- **N7 (Relay Watch green after a stop / heartbeat wakes).** The watcher covers only
  `.relay/journal` day files plus `STATE.json`/`PENDING_DECISION.md`; heartbeat, screenshot and
  call-record paths are filtered. A one-shot timer at heartbeat + 5 min recomputes the glyph so a
  dead orchestrator turns it red with no new event. Swift tests cover both.
- **N8 (slug reuse).** The reviewer pointer lists the previous blocking ids and the previous
  review, so slugs do not drift. Test checks the pointer.
- **N9** the Opus counter and `pending` now land in one mutate; a crash+resume does not count
  twice (test). **N10** the first turn also checks uncommitted user edits (test). **N11** the
  Automation permission is probed with a real System Events call, separately from Accessibility,
  which uses `AXIsProcessTrusted` (tests).

Journal migration: an old (unsigned) journal is anchored once on the next start; events after the
anchor must be signed. See `docs/OPERATIONS.md` (trust folder) and `docs/SECURITY.md`.

## Part C — Relay Watch (done)

- **C1 schema:** `docs/EVENTS.md` lists every event type, the `detail` fields Relay Watch reads and
  which types redraw the UI. `tests/test_events_schema.py` regenerates a real fake-driver journal
  and fails if a documented type or field is missing; it also checks the committed fixture and
  that a plain fake run emits the Part B fields.
  `apps/RelayWatch/Tests/Fixtures/journal-sample.jsonl` is generated by
  `tools/make_event_fixtures.py` from a real run (decision, violation, monitor halt/notes,
  rollover, fix plan, Opus escalation, pause/stop, guard, repair) plus documented rare events.
- **C2 reader/model:** Swift Package `apps/RelayWatch` targets macOS 14, arm64, no third-party
  packages. `RelayWatchCore` has `JSONValue`, `JournalEvent`, `RelayFolder` (read-only),
  `JournalReader` (offsets, partial lines, day switch, older days on demand), `JournalStore`
  (5,000-event cap), `ChainChecker`, `EventFilter`, `RelaySnapshot` and `Insights`
  (timeline/alerts/usage/health).
- **C3 UI:** `MenuBarExtra` (LSUIElement, no Dock icon) with the six glyph states and round number;
  a `Window` with header/model badge, health lights with sources, round timeline, Needs-you card
  (command, never the code), usage panel, alerts and raw event inspector. The watcher is an
  `FSEventStream` (latency 3 s, file events, ignoreSelf); the 30 s heartbeat timer exists only
  while the UI is open; there is no polling, animation or network.
- **C4 energy:** method and a CPU-time sample are recorded in `apps/RelayWatch/README.md`; the
  Activity Monitor Energy Impact/Idle Wake Ups fields are UI-only and are listed as unverified.
- **Tests:** `swift test` covers C9 1-5 (including the read-only mtime test); C9 6 is documented.
- **Unverified:** the sandboxed bookmark picker needs a human; there is no live multi-day run. See
  the app README.

## Dialog note

The `Relay human confirmation` dialog belongs to `relay instruct` only. It blocks that command,
not the Relay loop, and can steal focus. It is the P1-1 anti-injection gate. Tests no longer open
it (`PYTEST_CURRENT_TEST` guard + monkeypatch). It can be disabled explicitly with
`security.require_instruction_confirmation=false`; that lets any shell-capable agent record a
human-labelled instruction, so it is a deliberate security downgrade.
