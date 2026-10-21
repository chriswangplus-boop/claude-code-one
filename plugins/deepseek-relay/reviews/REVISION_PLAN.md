# Relay revision plan (v0.2), handed to DeepSeek

This file has three parts, built in this order:

- **Part A:** fix the findings in `reviews/CLAUDE_REVIEW.md`.
- **Part B:** a new model policy for the Claude app. Sonnet 5.5 (high) is the default and
  Opus 5.5 (medium) is used only for critical work.
- **Part C:** a small read-only Swift macOS app, **Relay Watch**, that shows the journal as a
  live progress view.

Ground rules (same as v0.1):
- `BUILD_PLAN.md` still holds unless this file changes it.
- Every change comes with a test.
- Never push.
- Keep the commits for Parts A, B and C separate.
- Do not mark a finding fixed until its regression test passes.

---

## Part A — fix the review findings (do this first)

Fix in this order. Each fix's test is listed in `CLAUDE_REVIEW.md`.

| Order | Finding | Short description |
|---|---|---|
| 1 | P1-1 | Decision answers need a one-time code that is never written in the project |
| 2 | P1-2 | Control files (`STATE.json`, `BATON.json`, flags, `monitor/`, `answers/`, `calibration/`, `MONITOR_NOTES.md`) are covered by ownership checks |
| 3 | P1-3 | Popups: anchored whole-command allow-list, shell metacharacters rejected, buttons clicked by label through Accessibility (never at LLM-supplied coordinates) |
| 4 | P1-4 | Monitor: `human.halt_cleared` watermark; history checks only look at events after it |
| 5 | P1-5 + P2-15 | Screenshots: read-only window lookup and `screencapture -l <windowID>`; no focus or raise; skip checks when the user isn't idle |
| 6 | P2-6, P2-7, P2-8, P2-9 | Crash/resume and session switching (one save per transition, always `_ensure_session`, checkpoint before switching roles, aborted turns stay blocked) |
| 7 | P2-10, P2-11, P2-12 | Every state write through `store.mutate`; `relay journal repair`; set the baton before `pending` |
| 8 | P2-13, P2-14 | Model choice must be verified (see Part B); shared-folder proof required |
| 9 | P3-16 … P3-22 | Small fixes. **P3-18 is now required by Part B**, because milestone counting drives the model choice |

Done when: all of `CLAUDE_REVIEW.md` is fixed or explicitly deferred with a reason, and
`pytest` passes on macOS and on Linux (P3-22).

---

## Part B — model policy: Sonnet by default, Opus only when really necessary

### Goal and the human's decisions (2026-10-21)
Long relay loops use a lot of Claude tokens. The human decided:
1. **GLM does the fixing.** By default every fix is implemented by GLM in Zcode, following the
   reviewer's feedback or a written fix plan. **Only the hardest bugs** are fixed by Opus
   directly.
2. **Opus only when really necessary.** Everything else runs on **Sonnet 5.5 at high effort**.
   That covers planning, milestone handoffs, reviews and fix plans. **Opus 5.5 at medium effort**
   is the exception, and every use of it must be justified by a rule in B2.

Relay can't see Claude subscription token counts, so the saving is not measured in tokens.
Instead, the journal records which model ran every turn and why, so the Opus share is visible
(Part C shows it).

### B1. Roles

| Role | Model | App | May write | When it runs |
|---|---|---|---|---|
| `planner` | **Sonnet 5.5 (high)** | Claude | `PLAN.md`, `HANDOFF.md` | All normal planning, including milestone handoffs and the first re-plan after a stall |
| `reviewer` | **Sonnet 5.5 (high)** | Claude | `REVIEW.md`, `FIX_PLAN.md` | Every review; also writes a fix plan when an issue repeats (B2 ladder, step 2) |
| `strategist` | **Opus 5.5 (medium)** | Claude | `PLAN.md`, `HANDOFF.md`, `STRATEGY.md` | Only by a B2 rule: the first master plan, or after a Sonnet re-plan has also failed |
| `fixer` | **Opus 5.5 (medium)** | Claude | **source files** (within limits), `FIX_REPORT.md`, `HANDOFF.md` | Only the hardest bugs (B2 ladder, step 3) |
| `coder` | GLM 5.3 flash (high) | Zcode | source files, `CODER_REPORT.md` | All other code changes |

Ownership changes in `integrity.py`:
- `FIX_PLAN.md` is owned by Claude (the reviewer writes it). Add `STRATEGY.md`, `FIX_REPORT.md`,
  `CHECKPOINT_strategist.md` and `CHECKPOINT_fixer.md` to `CLAUDE_OWNED`.
- **Source files are owned by the role that holds the baton.** That is the `coder` normally, and
  the `fixer` only during a fixer turn. Pass the active role, not just the app, to
  `ownership_violations`.
- **Fixer limits:**
  - A fixer turn may change at most `model_policy.fixer_max_files` source files (default
    **10**). Exceeding that is an ownership violation and pauses the loop.
  - It may not touch `.relay/` files other than `FIX_REPORT.md`, `HANDOFF.md` and its sentinel.
  - The secret scan and the "never push" rule apply unchanged.

Prompts (`RULES_CLAUDE.md`):
- **`reviewer` section:**
  - Add `blocking_issue_ids` (see B2).
  - When the pointer says `write FIX_PLAN.md`, write the exact files, functions, steps and the
    test that proves the fix, for GLM to implement.
- **`strategist` section:** re-think the approach. Write `STRATEGY.md` with the reasoning, then
  update `PLAN.md` and `HANDOFF.md`. Do not edit source.
- **`fixer` section:**
  - Fix only the issues named in the pointer.
  - Make the smallest change that fixes the root cause, and add or adjust the test that proves
    it.
  - Run the tests.
  - Write `FIX_REPORT.md` covering the root cause, the files changed and the test results.
  - Do not refactor or start new work.

### B2. When Opus is used (decided by code, never by the LLM)

The decision is a **deterministic function**:
`relay/model_policy.py::choose_turn(state, phase, cfg) -> (role, model, effort, trigger)`.
- DeepSeek's routing judgment may include `suggest_escalation`. That suggestion is **journaled
  only and is never a trigger**.
- The monitor may request a re-plan. That request counts as a stall, which starts at step 1 of
  the re-plan path below.

**Fix ladder for a repeated problem.** The "same problem" test is deterministic:
- The reviewer lists `blocking_issue_ids` in `CLAUDE_DONE.json`: stable slugs such as
  `["auth-token-expiry"]`.
- `validate_sentinel` checks the list: each ID matches `^[a-z0-9-]{3,40}$`, at most 10 IDs.
- An issue's **streak** is the number of consecutive reviews that list it.

| Step | Condition | What happens | Model |
|---|---|---|---|
| 1 | Normal `CHANGES_REQUESTED` | GLM fixes from `REVIEW.md` | Sonnet reviews, GLM codes |
| 2 | Streak reaches `fix_plan_after` (default **2**) | The reviewer also writes `FIX_PLAN.md`; GLM implements it | Sonnet |
| 3 | **Hardest bug:** streak reaches `opus_fix_after` (default **4**, i.e. still failing after the fix plan), **or** the coder reports `blocked` twice in a row on the same issue | `fixer` turn: Opus fixes the code directly, then a normal Sonnet review | **Opus** |
| 4 | Streak still grows after an Opus fix | Human decision (no second Opus fix for the same issue without approval) | — |

**Re-plan path:**

| Step | Condition | Role | Model |
|---|---|---|---|
| — | First PLAN turn of the project (no `PLAN.md` yet) | `strategist` | **Opus** (`escalate_on_first_plan`, default on) |
| 1 | First stall or re-plan in a milestone | `planner` | Sonnet |
| 2 | A second stall in the same milestone, after the Sonnet re-plan | `strategist` | **Opus** |
| 3 | A stall after the Opus re-plan | Human decision | — |

**Human request:** `relay instruct --opus "…"`, or a decision answer that chooses "Use Opus",
runs the role that fits the phase (`strategist` when planning, `fixer` when fixing). It counts
toward the limits.

**Limits** (config, checked by `choose_turn`):
- `max_opus_turns_per_milestone`, default **1**. The first master plan does not count.
- `max_opus_turns_per_day`, default **3**.

If a rule fires but a limit is reached, ask the human with these options:
- "Allow this Opus turn"
- "Continue on Sonnet"
- "Pause"

Never silently fall back, and never silently go over a limit.

**After every Opus turn, the next Claude turn is Sonnet again.** Opus is never "sticky".

### B3. Switching models in the Claude app
- Pick the model at the start of every Claude turn, and verify it by reading the model label
  with vision. Fix P2-13 at the same time:
  - `label_region` is required when calibrating the Claude app.
  - Confirm the picker opened before typing.
  - An unverified model raises `HumanRequired`; nothing is typed.
- Pick the effort level the same way if the app shows an effort control. Otherwise, journal
  `effort: "unverifiable"` once per session.
- Each role (`planner`, `reviewer`, `strategist`, `fixer`) has its own seed section and
  checkpoint file.
- Switching roles follows the checkpoint-then-new-chat rule (P2-8). Opus turns always use a
  fresh chat seeded from files. That keeps Opus context, and therefore tokens, small.
- The reviewer's pointer after a fixer turn tells it to read `FIX_REPORT.md` instead of
  `CODER_REPORT.md`.

### B4. Journal and config changes (Part C depends on these)
- These events carry `model`, `effort`, `role` and `model_verified` (`true`, `false` or
  `"unverifiable"`) in `detail`:
  - every Claude `turn.start` and `turn.end`
  - every `gui.model` event
- New event `model.escalated`, with `detail`:
  - `trigger`: one of `first_plan`, `replan_after_sonnet`, `hardest_bug_streak`,
    `hardest_bug_blocked` or `human`
  - `role`
  - `issue_ids`
  - `streak`
  - `opus_turns_today` and `opus_turns_milestone`, each with its limit
- New event `fix.plan_requested` (ladder step 2), with `detail`: `issue_ids` and `streak`.
- Config. This replaces `planner_model`/`planner_effort`/`reviewer_model`/`reviewer_effort`:
  ```json
  "model_policy": {
    "default":   {"model": "Sonnet 5.5", "effort": "high"},
    "escalated": {"model": "Opus 5.5",   "effort": "medium"},
    "escalate_on_first_plan": true,
    "fix_plan_after": 2,
    "opus_fix_after": 4,
    "fixer_max_files": 10,
    "max_opus_turns_per_milestone": 1,
    "max_opus_turns_per_day": 3
  }
  ```
- `relay status` (text and `--json`) shows `Opus today: x/3, this milestone: y/1`.

### B5. Tests for Part B
1. A normal milestone runs every Claude turn on Sonnet/high, including planning and reviews.
2. The first PLAN turn is the `strategist` on Opus/medium. Every later PLAN turn is Sonnet
   unless the re-plan path applies.
3. Streak 2 → the reviewer writes `FIX_PLAN.md` on Sonnet, and the next CODE pointer names it.
   No Opus turn happens.
4. Streak 4, or the coder `blocked` twice on the same issue → a `fixer` turn on Opus. It may
   edit source; then a Sonnet review that reads `FIX_REPORT.md`.
5. Ownership checks for the fixer:
   - a fixer turn changing 11 source files → violation and pause
   - a fixer writing `PLAN.md` → violation
   - the coder writing source during a fixer turn → violation
6. The streak keeps growing after an Opus fix → a human decision, not a second Opus turn.
7. First stall → Sonnet re-plan; second stall in the same milestone → Opus `strategist`;
   third → human decision.
8. A rule fires with the limit reached → a decision with three options, and no Opus turn before
   the answer.
9. DeepSeek `suggest_escalation` with no rule satisfied → Sonnet (and the suggestion is
   journaled).
10. Model verification fails → `HumanRequired`, and no pointer is typed.
11. Every Claude `turn.start` and `turn.end` carries `model`, `effort`, `role` and
    `model_verified`.

### B6. Platform: the human's Mac
The human runs **macOS 27.2 "Golden Gate" on Apple Silicon**.
- Relay (Python):
  - Support **Apple Silicon (arm64)** with Python 3.11+ (python.org or Homebrew arm64).
  - Calibrate and run the live checks (phases 5–6 of `BUILD_PLAN.md`) **on macOS 27.2**.
- Recalibrate after OS updates:
  - Store the macOS version (`sw_vers -productVersion`) in each calibration file.
  - If the running version differs, `relay start` refuses until `relay calibrate` is re-run.
    Accessibility, Screen Recording and app layouts can change between OS releases.
- Check the permissions at every `relay start`, not only at calibration. They can be revoked or
  require re-approval after updates.

  Required permissions:
  - Accessibility (for keystrokes and clicks)
  - Screen Recording (for `screencapture -l`)
  - Automation of System Events (for window lookup and clicking buttons by label)

  If any is missing, pause with a clear message naming the System Settings pane.
- Do not assume undocumented behaviour of macOS 27. Anything the code relies on that cannot be
  confirmed on 27.2 must be listed under "could not verify" in the report back.

---

## Part C — Relay Watch: a read-only Swift progress viewer

### C1. Purpose and principles
A small macOS app that shows what Relay is doing, built from Relay's own records:
- **Faithful:**
  - Every number and status on screen comes directly from `.relay/journal/*.jsonl` or
    `.relay/STATE.json`, `BATON.json`, `HEARTBEAT.json`, `monitor/HEARTBEAT.json` or
    `PENDING_DECISION.md`.
  - Nothing is estimated or extrapolated: no progress percentage, no predicted finish time.
  - Each item can show the journal event ID it came from.
  - If a file can't be read, the app shows that error, never a guess.
- **Read-only:** the app never writes inside the project and never answers decisions. It shows
  the command to run instead, so it can't become a decision channel. This keeps P1-1 closed.
- **Low energy:** no polling loop, no animation, no network. It updates only when the journal
  changes, and only redraws for meaningful events.

### C2. Technology
- SwiftUI and the Swift toolchain from the current Xcode release on the human's Mac, which runs
  **macOS 27.2 on Apple Silicon**. No third-party packages.
- **Build arm64 only.** Set the deployment target to **macOS 14**, which is enough for
  `MenuBarExtra` and `@Observable`, and run all testing on macOS 27.2.
- Use only documented, stable SwiftUI/AppKit APIs. Do not adopt APIs newer than macOS 14 unless
  they're needed. If one is used, guard it with `if #available` and list it in the README.
- The source lives at `plugins/deepseek-relay/apps/RelayWatch/` as a Swift Package with an app
  target (or an Xcode project), with a `README.md` covering build and run steps.
- App form:
  - a **menu bar item** (`MenuBarExtra`) showing a one-glyph status and the round number
  - an optional main window (`Window`) with the full view
  - `LSUIElement` set, so there's no Dock icon by default
- App Sandbox on. The user picks the project folder once (`NSOpenPanel`), and it's kept as a
  **read-only security-scoped bookmark**. Entitlements:
  - `com.apple.security.files.user-selected.read-only`
  - `com.apple.security.files.bookmarks.app-scope`
  - No network entitlement.

### C3. Data model, which matches Relay's files exactly
- `JournalEvent` decodes the journal fields:
  - `id`, `ts`, `round`, `state`, `actor`, `type`
  - `detail` (a dictionary of JSON values)
  - `inputs`, `outputs`, `screenshot`, `deepseek`, `result`, `error`
  - `prev_hash`, `hash`

  Unknown fields are ignored. Unknown event `type`s are kept and shown as raw events.
- `RelayState` decodes the `STATE.json` fields it shows: `phase`, `round`, `milestone`, `role`,
  `pending`, `pending_decision`, `counters`, `session_ctx`, `paused_reason`, `updated_at`.
- **Agreed event schema:** add `docs/EVENTS.md` on the Python side, listing each event type and
  the `detail` fields the app relies on, including the Part B fields. Add a Python test that the
  orchestrator emits those fields, and a Swift test that decodes fixture lines copied from a
  real Python run. Commit the fixtures under
  `apps/RelayWatch/Tests/Fixtures/journal-sample.jsonl`.

### C4. Reading and updating, the low-energy part
1. **File change watching:** use an `FSEventStream` on `.relay/journal/` and `.relay/` with
   `latency = 3.0` seconds, so bursts of writes become one wake-up. Use the flags
   `kFSEventStreamCreateFlagFileEvents` and `kFSEventStreamCreateFlagIgnoreSelf`.
2. **Incremental reading:** keep the byte offset per day file and read only the new bytes. Only
   process a line once its closing `\n` has arrived; a partial last line is left for next time,
   which is normal while Relay is mid-write. Re-read `STATE.json` and the baton or heartbeat
   files only after a journal change.
3. **Meaningful-event filter:** only these types trigger a UI refresh. All other events are
   stored and counted but don't redraw:
   - `turn.start`, `turn.end`, `turn.timeout`, `sentinel.rejected`
   - `route.decided`, `stall.detected`, `model.escalated`
   - `rollover`, `session.open`
   - `decision.asked`, `decision.answered`
   - `state.paused`, `run.start`, `run.resume`, `project.done`
   - `integrity.violation`, `guard.tripped`
   - `monitor.halt`, `monitor.correct`, `monitor.note`, `monitor.check` (only when the result
     changes)
   - `git.commit`, `error`
4. **Heartbeat age:** compute it only when the window or menu is open, using one `Timer` every
   **30 s** with `tolerance = 10 s`. Stop the timer when the UI closes. While the UI is closed,
   the menu-bar glyph updates on file events only.
5. **No continuous animation.** No spinners: show the text "working since 14:02" instead. The
   UI must be able to sit idle with zero wake-ups between file events.
6. **Startup:** read today's and yesterday's day files in full, and older files only when the
   user scrolls back. Cap memory at the newest 5,000 events in RAM; older ones are re-read from
   disk on demand.

### C5. What it shows
1. **Status header:**
   - phase, round, milestone, role
   - which app holds the baton
   - the model and effort of the current turn, from the latest `turn.start`, shown as
     "Sonnet 5.5 · high" or "Opus 5.5 · medium" in a distinct colour
   - "last update 14:07:12 (evt-20261021-00412)"
2. **Health lights**, each with the exact source shown on hover:
   - orchestrator heartbeat: green under 2 min, amber under 5 min, red otherwise
   - monitor heartbeat: same thresholds
   - journal chain (see C6)
   - HALT, PAUSE or STOP flag present
3. **Round timeline:** one row per round, built from `turn.start` → `turn.end`, with:
   - role, model, verdict or status
   - duration (end ts minus start ts)
   - number of files changed
   - commit hash (short)
   - `ctx_pct`

   Rows tie together by round number and event ID. Rejected sentinels, nudges, rollovers and
   escalations appear as small markers on their row.
4. **Needs you:** shown when `pending_decision` is set. It shows the question, the options and the
   exact command to run (for example `relay decide D-007 --code <code> "…"`), with a "Copy
   command" button. The code itself is never shown (P1-1).
5. **Usage today:**
   - DeepSeek spend: the sum of `deepseek.cost_usd`, compared with the budget in config
   - Opus turns: today and in this milestone, compared with their limits
   - the Sonnet-to-Opus turn ratio
6. **Alerts list:** integrity violations, monitor halts, corrections and notes, guard trips and
   errors, each with its event ID.
7. **Event inspector:** click any row to see that event's raw JSON (pretty-printed, read-only).

Never shown: screenshots, file contents, or DeepSeek prompts and responses. This avoids putting
private content on screen. It just says "see `relay why <id>`".

### C6. Showing integrity honestly
- The app checks that each event's `prev_hash` equals the previous event's `hash`, in order,
  across day files. This is cheap and catches deleted, inserted or reordered lines.
- It does **not** recompute the hashes. Python's canonical JSON (`sort_keys`,
  `ensure_ascii=False`, its float formatting) is hard to reproduce exactly in Swift. Instead,
  the full check shows as "verified by monitor at HH:MM", taken from the latest `monitor.check`
  event with `journal_ok: true`.
- When the latest monitor check has `journal_ok: false`, or the link check fails, the badge
  turns red with the failing event ID. It never shows green on stale data.

### C7. Menu-bar glyph states, using SF Symbols
| Glyph | Meaning |
|---|---|
| `circle.fill` (green) | Running (an active phase with a fresh heartbeat) |
| `pause.circle` | Paused |
| `questionmark.circle.fill` (orange) | Waiting for a decision |
| `exclamationmark.octagon.fill` (red) | Monitor HALT, integrity violation, or a stale heartbeat while active |
| `checkmark.circle` | Done |

A plain-text round number appears next to the glyph. The app sends no notifications, because
Relay already does.

### C8. Interaction with the GUI harness
- Relay Watch must never be in the way of Relay's screen control:
  - It never comes to the front by itself.
  - It never opens windows on its own.
  - It never takes focus.
- Relay's window-only capture (P1-5/P2-15 fix) makes sure the viewer never appears in
  screenshots sent to DeepSeek.

### C9. Tests for Part C
1. Decoding tests on the committed fixtures, covering every event type in `EVENTS.md` plus one
   unknown type.
2. Incremental reader: append half a line and check it isn't processed, then complete it and
   check it's processed exactly once. Also cover the day-file switch at midnight.
3. Chain link check: a fixture with a deleted middle line turns the badge red and names the
   event ID.
4. Meaningful-event filter: 100 `gui.*` and `vision.check` events cause zero UI refreshes; one
   `turn.end` causes one.
5. Read-only guarantee: the app's file access goes through one `RelayFolder` type that has no
   write APIs, and a test asserts the project folder's modification time is unchanged after a
   full session.
6. Energy check (manual, documented in the app README):
   - Leave the app running for 30 minutes with Relay idle.
   - Activity Monitor → Energy tab: "Energy Impact" should average close to 0, with "Idle Wake
     Ups" near 0.
   - Record the observed numbers in the README.

---

## Build order and acceptance

| Phase | Deliverable | Gate |
|---|---|---|
| A | All P1 fixes, then the P2s, then the P3s | Every `CLAUDE_REVIEW.md` test passes; `pytest` passes on macOS and Linux |
| B | `model_policy.py`, the new roles and prompts, journal fields, config, status | B5 tests pass; a fake-driver end-to-end run of 2 milestones shows Opus only on the first plan and on one forced hardest-bug fixer turn (streak 4); all other Claude turns are Sonnet |
| C1 | `docs/EVENTS.md` plus the Python emission test plus fixtures from a real fake-driver run | Schema test passes |
| C2 | Relay Watch reader, model, chain link check and filter (no UI yet) | C9 tests 1–5 pass (`swift test`) |
| C3 | Menu bar plus window UI | Manual check on a live fake-driver run: every header value matches `relay status --json` at the same moment |
| C4 | Energy check | C9 test 6 recorded in the README |

When reporting back, include:
- the commit list
- `pytest` and `swift test` output
- a screenshot of Relay Watch next to `relay status --json` taken at the same time
- the Activity Monitor energy numbers
- any finding deferred, with its reason

## Decisions recorded (human, 2026-10-21)
1. **Who fixes code:** GLM implements fixes, following the reviewer's feedback or a Sonnet-written
   `FIX_PLAN.md`. Opus edits code only for the hardest bugs (B2 ladder, step 3).
2. **When to use Opus:** only when really necessary. Sonnet 5.5 (high) is the default for all
   planning and reviewing. Opus 5.5 (medium) is used only by the B2 rules, with limits of 1 per
   milestone (not counting the first master plan) and 3 per day. Going over a limit always asks
   the human.
3. **Platform:** macOS 27.2 "Golden Gate" on Apple Silicon (arm64). Relay Watch targets macOS 14
   and is tested on 27.2; Relay is calibrated and verified on 27.2 (B6).

## Open questions for the human (do not decide these yourself)
- None at present. If a new question comes up while building, stop and ask; do not guess.
