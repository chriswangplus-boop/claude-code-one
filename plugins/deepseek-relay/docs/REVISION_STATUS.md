# Revision status — Part A

## Session checkpoint (2026-10-21)

- No Relay loop is running; there is nothing to pause.
- Part A work is committed locally at `deepseek-relay-part-a` (first commit `23b7f0e`, remainder after it).
- Patch: `reviews/deepseek-relay-part-a.patch` (applies to PR #5 `85072b9`).
- Tests: **167 passed** (Python) and **20 passed** (`swift test`).
- Resume from the workspace root file `CONTINUE_HERE.md`.
- Remaining: push the branch (no credential on this Mac), the real-app calibration checks, and the
  full 30-minute Activity Monitor energy reading.

Base: `plugins/deepseek-relay` at PR #5 head `85072b9` (code + `CLAUDE_REVIEW.md` + `REVISION_PLAN.md`).

`pytest`: **167 passed** on macOS (Linux run pending; P3-22's fake backend keeps it portable).
`swift test`: **20 passed** in `apps/RelayWatch`. This file is a handoff, not a substitute for
the review tests.

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
