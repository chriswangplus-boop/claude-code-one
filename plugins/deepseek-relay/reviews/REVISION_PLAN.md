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

## Part B — model policy: Sonnet by default, Opus only when it earns it

### Goal
Long relay loops use a lot of Claude tokens. The planner and reviewer should run on
**Sonnet 5.5 at high effort** almost all the time. **Opus 5.5 at medium effort** is used only for:
1. **Critical strategy:** the first master plan and re-plans after the loop gets stuck.
2. **The hardest fixes:** when GLM has failed the same problem repeatedly, Opus diagnoses it and
   writes an exact fix plan, and GLM implements it.

Relay can't see Claude subscription token counts, so the saving is not measured in tokens.
Instead, the journal records which model ran every turn, so the share of Opus turns is visible
(Part C shows it).

### B1. Roles and default models

| Role | Default model | Writes | When it runs |
|---|---|---|---|
| `planner` | **Sonnet 5.5 (high)** | `PLAN.md`, `HANDOFF.md` | Normal milestone handoffs |
| `strategist` (new) | **Opus 5.5 (medium)** | `PLAN.md`, `HANDOFF.md`, `STRATEGY.md` | Escalation only (B2) |
| `reviewer` | **Sonnet 5.5 (high)** | `REVIEW.md` | Every review |
| `fixer` (new) | **Opus 5.5 (medium)** | `FIX_PLAN.md`, `HANDOFF.md` | Escalation only (B2) |
| `coder` | GLM 5.3 flash (high), in Zcode | Source code, `CODER_REPORT.md` | Unchanged |

- **The fixer does not edit source code.** Source stays coder-owned, so the ownership model in
  `integrity.py` doesn't change. The fixer writes a precise diagnosis and step-by-step fix in
  `FIX_PLAN.md`, then points `HANDOFF.md` at it. GLM implements it in the next CODE turn.
- **New ownership entries:**
  - `STRATEGY.md` and `FIX_PLAN.md` are owned by Claude; add them to `CLAUDE_OWNED`.
  - Add `CHECKPOINT_strategist.md` and `CHECKPOINT_fixer.md` the same way.
- **Prompts:** add `strategist` and `fixer` sections to `RULES_CLAUDE.md`.
  - The fixer's section says: "Diagnose the root cause from `CODER_REPORT.md`, `REVIEW.md`, the
    git diff and the failing tests. Write `FIX_PLAN.md` with exact files, functions and steps,
    plus the test that proves the fix. Do not edit source files."

### B2. When Opus is used (decided by code, never by the LLM)

The escalation decision is a **deterministic function**, `relay/model_policy.py::choose_model(state, phase, cfg)`.
DeepSeek may *suggest* an escalation by setting a `suggest_escalation` flag in its route
judgment. That suggestion only counts as one trigger among the rules below and is journaled. It
can never pick a model by itself.

| Trigger | Role used | Default |
|---|---|---|
| First PLAN turn of the project (no `PLAN.md` yet) | `strategist` (Opus) | on |
| A re-plan caused by stall detection (`stall.detected`), DeepSeek `STUCK`, or a monitor `replan` correction | `strategist` (Opus) | on |
| Same problem not fixed after **N** consecutive `CHANGES_REQUESTED` (default N=2), or the coder reports `blocked` twice in a row | `fixer` (Opus), then back to CODE | on |
| Human asks: `relay instruct --opus "…"` or a decision answer chooses "Use Opus" | Whichever of strategist/fixer fits the phase | on |
| Everything else (milestone handoffs after `MILESTONE_DONE`, all reviews) | `planner`/`reviewer` (Sonnet) | — |

The "same problem" test is deterministic:
- The reviewer writes a `blocking_issue_ids` list in `CLAUDE_DONE.json` (short stable slugs,
  e.g. `["auth-token-expiry"]`).
- An issue counts as repeated when it appears in N consecutive reviews.
- Add `blocking_issue_ids` to the reviewer section of `RULES_CLAUDE.md`, and validate it in
  `validate_sentinel` (list of `^[a-z0-9-]{3,40}$`, at most 10 entries).

**Limits that protect the budget** (config, checked by `choose_model`):
- `model_policy.max_opus_turns_per_milestone` (default **2**)
- `model_policy.max_opus_turns_per_day` (default **6**)

When a trigger fires but a limit is hit, escalate a **human decision** with these options:
- "Allow one more Opus turn"
- "Continue on Sonnet"
- "Pause"

Never silently fall back, and never silently go over the limit.

**After an Opus turn**, the next Claude turn goes back to Sonnet automatically. Opus is not
"sticky".

### B3. Switching models in the Claude app
- Pick the model at the start of every Claude turn (as now), and verify it by reading the model
  label with vision. Fix P2-13 at the same time:
  - `label_region` is required when calibrating the Claude app.
  - Confirm the picker opened before typing.
  - An unverified model raises `HumanRequired`; it never continues silently.
- Pick the effort level the same way when the app has an effort control. If it doesn't, journal
  `effort: "unverifiable"` once per session.
- Session handling: each role (`planner`, `strategist`, `reviewer`, `fixer`) keeps its own seed
  section and checkpoint file. Switching roles follows the checkpoint-then-new-chat rule (P2-8).
  Because Opus turns are rare, a new chat for each Opus turn is acceptable and keeps Opus context
  small, which also saves tokens.

### B4. Journal and config changes (Part C depends on these)
- `turn.start`, `turn.end` and `gui.model` events carry these `detail` fields:
  - `model` (e.g. `"Sonnet 5.5"`)
  - `effort` (e.g. `"high"`)
  - `model_verified` (`true`, `false` or `"unverifiable"`)
- Add a new event, `model.escalated`, with `detail`:
  - `trigger` (`first_plan`, `stall`, `stuck`, `monitor_replan`, `repeated_issue`, `blocked_twice`
    or `human`)
  - `role`
  - `issue_ids`
  - `opus_turns_today`
  - `opus_turns_milestone`
- New config, replacing `planner_model`/`planner_effort`:
  ```json
  "model_policy": {
    "default": {"model": "Sonnet 5.5", "effort": "high"},
    "escalated": {"model": "Opus 5.5", "effort": "medium"},
    "repeat_issue_threshold": 2,
    "max_opus_turns_per_milestone": 2,
    "max_opus_turns_per_day": 6,
    "escalate_on_first_plan": true,
    "escalate_on_replan": true
  }
  ```
- `relay status` (text and `--json`) shows the Opus turns used today and in this milestone, next
  to their limits.

### B5. Tests for Part B
1. A normal milestone runs every Claude turn on Sonnet/high.
2. The first PLAN turn uses `strategist` on Opus/medium. The next PLAN after `MILESTONE_DONE` is
   back on Sonnet.
3. Two reviews with the same `blocking_issue_ids` lead to a `fixer` turn on Opus, then CODE.
   The fixer touching a source file is an ownership violation.
4. A stall re-plan uses `strategist`, and a monitor `replan` correction does too.
5. Hitting `max_opus_turns_per_milestone` creates a decision with the three options, and no
   Opus turn runs before the answer.
6. A DeepSeek `suggest_escalation` alone, with no rule triggered, does **not** select Opus.
7. Model verification fails → `HumanRequired`, and no pointer is typed.
8. The journal carries `model`, `effort` and `model_verified` on every Claude `turn.start` and
   `turn.end`.

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
- Swift 5.9+, SwiftUI, **macOS 14+**, no third-party packages.
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
| B | `model_policy.py`, the new roles and prompts, journal fields, config, status | B5 tests pass; a fake-driver end-to-end run of 2 milestones shows Opus only on the first plan and one forced fixer turn |
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

## Open questions for the human (do not decide these yourself)
1. Should GLM still implement Opus's fix plan, or may the `fixer` edit source directly for the
   very hardest bugs? This plan assumes **GLM implements**, which keeps the ownership model
   unchanged.
2. Are the Opus limits right? Defaults are **2 per milestone and 6 per day**.
3. Is macOS 14 acceptable as the minimum for Relay Watch? `MenuBarExtra` needs macOS 13. macOS
   14 adds the `@Observable` macro, which keeps refreshes limited to the views whose data changed
   and so saves energy. Lowering the minimum to 13 means using `ObservableObject` instead.
