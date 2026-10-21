# Independent review #2 — Relay v0.2 (Parts A, B and C)

Reviewer: Claude (independent of the author). Reviewed `deepseek-relay-parts-abc.patch`. It is
one combined diff covering DeepSeek's 7 commits (`23b7f0e`…`dfd3041`), applied with `git apply`
to `85072b9`. It touches 62 files, adding 6,951 lines and removing 445.

Verdict: **FIX-FIRST**

Most of the first review is genuinely fixed:
- popup command chaining
- the monitor's permanent halt
- the screen grabbing focus
- window-only screenshots
- crash and resume handling
- locking
- journal repair
- model verification
- the shared-folder proof

The Part B model policy is a clean, deterministic function that matches the plan. Relay Watch is
read-only and well structured.

Three P1 problems remain or are new. **Two of them were already in v0.1 and I missed them in
review #1.**
1. **Human-only actions can still be done by an agent.** The P1-1 fix keeps its proof inside the
   agent-writable project folder. I show three bypass paths below.
2. **Relay's own `git commit` runs git hooks an agent plants.** That is arbitrary code
   execution outside every approval popup. Present since v0.1.
3. **GLM is never told to read the review.** After `CHANGES_REQUESTED`, the coder isn't pointed
   at `REVIEW.md`, so the feedback loop that is the point of Relay doesn't close. Present since
   v0.1.

## How this was checked
- `python3 -m pytest` on Linux with Python 3.13: **166 passed, 1 skipped**. The skipped test is
  the macOS-only GUI wiring test, now correctly marked. That is consistent with DeepSeek's 167
  passing on macOS.
- **`swift test` was not run.** This container has no macOS and no Swift toolchain. I reviewed
  Relay Watch by reading its code only.
- I re-ran the review #1 probe scripts against the new code and wrote new probe scripts for each
  P1 below. Their outputs are quoted.

---

## Status of review #1 findings

| Finding | Status | Note |
|---|---|---|
| P1-1 agent answers decisions | **Not fixed** | One-time code added, but its hash lives in `STATE.json` inside the project. See N1 |
| P1-2 control files unchecked | Mostly fixed | Control files are now checked against matching journal events, but the journal itself is unsigned and unchecked, so those events can be forged. See N1 |
| P1-3 popup chaining and LLM click points | Fixed for chaining and clicking | Anchored commands, shell metacharacters rejected, clicks by Accessibility label. Gaps remain at the argument level and in reading the dialog text. See N6 |
| P1-4 monitor re-halts forever | Fixed | `human.halt_cleared` watermark. It can be forged through N1 |
| P1-5 screen checks grab focus | Fixed | Read-only window lookup, `screencapture -l <id>`, and a skip when the user is active |
| P2-6 double turn after crash | Fixed | Phase and `pending` change in one locked save |
| P2-7 resume rings the wrong chat | Fixed | `_ensure_session` runs on resume |
| P2-8 role switch without checkpoint | Fixed | `_checkpoint_before_switch`. It causes the new problem N5 |
| P2-9 aborted turn laundered | Mostly fixed | `blocked` record with hashes. The first-turn exemption remains (N10) |
| P2-10 state races | Fixed (per code reading) | `store.mutate` everywhere I looked |
| P2-11 corrupt journal tail | Fixed | `relay journal repair` |
| P2-12 false baton halt | Fixed | |
| P2-13 model typed into chat / unverified | Fixed | An unverified model raises `HumanRequired` |
| P2-14 shared-folder proof | Fixed | |
| P2-15 region screenshots | Fixed | |
| P3-16 … P3-22 | Fixed | Fence regex, failed-call recording, anchored placeholder check, macOS test skipped on Linux |

---

## Findings

### N1 (P1) — Agents can still perform human-only actions
- **Root cause:** everything Relay uses to prove "a human did this" sits in the project folder,
  or is reachable from an agent's shell running as the same user:
  - the decision code hash (`STATE.json`, `relay/orchestrator.py:1748`)
  - the journal events that ownership checks and the monitor trust (`relay/integrity.py:112-151`,
    `relay/monitor/watchdog.py`). The journal is excluded from snapshots
    (`IGNORED_PREFIXES = (".relay/journal/",)`), and its hash chain uses no secret key, so anyone
    can append a correctly chained line.
  - the `relay instruct` confirmation, which trusts any TTY (`relay/decisions.py:40-52`).
- **Path A, reproduced: forging the decision code.** A test that acts as the agent replaces
  `pending_decision.code_hash` in `STATE.json` with the hash of its own code and writes
  `ANSWER: <its code> A, use JSON`. Relay accepted it:
  ```
  phase: PLAN | answered events: [('human', 'A, use JSON')]
  ```
  This happens during `WAIT_HUMAN`, when no turn snapshot is running, so nothing flags the
  `STATE.json` edit. The monitor's authorship check passes because the event says `actor: human`.
- **Path B, reproduced: a fake terminal for `relay instruct`.**
  `echo y | script -qc "relay instruct --opus '…'" /dev/null` gives `confirm_human` a pseudo-TTY
  that answers "y". It recorded `D-001` as a human instruction and armed an Opus turn:
  ```
  D-001 recorded as a human instruction ... also armed one Opus turn
  human_opus_request: {'at_round': 0, 'source': 'D-001'}
  ```
  If this runs during a turn, the ownership check also allows the `STATE.json` and
  `DECISIONS.md` edits, because a `decision.answered` event by `human` follows the turn start.
- **Path C, by code reading: forged journal events.** An agent can append a correctly chained
  `human.halt_cleared` event, which silences the monitor's history checks, or a
  `decision.answered` event, which covers its own `STATE.json` edit. The chain check can't tell
  these apart from real ones.
- **Impact:** the core promise ("never does job 3", the human makes decisions) still doesn't
  hold against a prompt-injected agent.
- **Fix:**
  1. Keep the trust material **outside the project**, in
     `~/Library/Application Support/Relay/<project-id>/` (mode 0700) or the Keychain:
     - the decision code hashes
     - an HMAC key
  2. **Sign every journal event** with that key, as an HMAC over the canonical event. `verify()`,
     `ownership_violations` and the monitor accept only signed events.
  3. `relay instruct` uses the same one-time-code mechanism as decisions, or always shows the
     native macOS dialog. Never accept a bare TTY "y".
  4. During `WAIT_HUMAN`, hash `STATE.json` and `PENDING_DECISION.md` when the question is asked,
     and pause on any change not made by Relay itself.
- **Residual risk to state honestly:** an agent with unrestricted shell access as the same user
  can still read the home folder. The mitigation is that Claude Code and Zcode ask permission
  before touching files outside the project, and the popup policy escalates anything containing
  "outside" to the human.
- **Test:** the three paths above, as regression tests. Each must leave the phase at
  `WAIT_HUMAN` and produce no `decision.answered` or `decision.instruction` event.

### N2 (P1, present since v0.1) — Relay's `git commit` runs agent-planted git hooks
- **Location:** `relay/integrity.py:312-316` (`_git`), and `git_commit`. `.git/` is in
  `IGNORED_DIRS`, so it is never snapshotted.
- **Reproduced:** a script plants `.git/hooks/pre-commit` that writes a file, then calls Relay's
  `git_commit`:
  ```
  commit: a6034d2f…   hook ran: True
  ```
- **Impact:** any agent can get arbitrary code run by Relay itself, with Relay's macOS
  permissions (Accessibility and Screen Recording). No approval popup ever appears.
  - Hooks fire on the commit at the end of every turn.
  - `core.fsmonitor` in `.git/config` runs a command on every `git status`. Relay runs
    `git status` in `git_snapshot`.
  - `core.pager`, `diff.external` and similar settings are reachable the same way.
- **Fix:**
  1. Run every git command with:
     `git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.pager=cat -c diff.external= …`
  2. Commit with `--no-verify`.
  3. Set `GIT_CONFIG_NOSYSTEM=1`, and `GIT_TERMINAL_PROMPT=0` in the environment.
  4. Hash `.git/config` and `.git/hooks/*` in the turn snapshot, and treat any change as an
     ownership violation.
- **Test:** plant a pre-commit hook and a `core.fsmonitor` command, run a full fake turn, and
  assert neither ran and the turn paused with a violation.

### N3 (P1, present since v0.1) — The coder is never pointed at the review
- **Location:** the CODE pointer at `relay/orchestrator.py:1305-1310`, which names
  `HANDOFF.md`, `FIX_PLAN.md`, `PLAN.md`, `DECISIONS.md` and `MONITOR_NOTES.md`, but not
  `REVIEW.md`.
  - `prompts/RULES_CODER.md:11` (the non-negotiable read list) also omits `REVIEW.md`.
  - The reviewer can't update `HANDOFF.md`, because `ROLE_FILES["reviewer"]` doesn't include it
    (`relay/integrity.py`).
- **Impact:** after `CHANGES_REQUESTED`, GLM re-reads an unchanged `HANDOFF.md` and never sees
  the requested changes. Sonnet's feedback is lost, the same review repeats, and the fix ladder
  then wrongly escalates to Opus. In other words, tokens are spent because the loop can't
  converge. The fake-driver tests don't catch this, because the scripted coder never reads files.
- **Fix:**
  - When the last route was `CHANGES_REQUESTED`, the CODE pointer names `.relay/REVIEW.md` with
    its sha256 as the first file to read.
  - `RULES_CODER.md` step 1 adds: "If `.relay/REVIEW.md` is newer than your last report, address
    every requested change in it."
- **Test:** after a `CHANGES_REQUESTED` route, assert the CODE pointer contains
  `.relay/REVIEW.md`. Also use a scripted coder that only reads the files named in the pointer
  and must quote the review.

### N4 (P2) — An old `FIX_PLAN.md` is followed forever
- **Location:** `prompts/RULES_CODER.md:11` says "If `.relay/FIX_PLAN.md` exists, follow it".
  Nothing ever removes or archives that file.
- **Impact:** once a fix plan is written, GLM keeps applying it in later rounds and milestones,
  even after that issue is fixed.
- **Fix:**
  - The harness archives the file to `.relay/archive/FIX_PLAN-<round>.md` when its issues leave
    `issue_streaks` or the milestone advances, and journals the move.
  - The pointer names `FIX_PLAN.md` only when it is current.
  - Change the rule to "follow it only when the pointer names it".
- **Test:** issue resolved → the next CODE pointer has no `FIX_PLAN.md`, and the file has been
  archived.

### N5 (P2) — Extra Opus turns that aren't counted
- **Location:** `relay/orchestrator.py:1357-1363` (`_model` returns Opus for `strategist` and
  `fixer`) and `:1385-1387` (`_ring` selects that model). `_checkpoint_before_switch` rings the
  **outgoing** role.
- **Impact:** after every strategist or fixer turn, the next role switch runs a checkpoint turn
  on **Opus**. It isn't counted toward the 1-per-milestone / 3-per-day limits and isn't journaled
  as `model.escalated`. This breaks "Opus only when really necessary" and makes Relay Watch's
  Opus counter wrong.
- **Fix:** skip the checkpoint for `strategist` and `fixer`. Their outcome is already written to
  `STRATEGY.md` and `FIX_REPORT.md`, and their chats are always fresh. Alternatively, select
  Sonnet before ringing the checkpoint. In either case, count anything that actually runs on
  Opus.
- **Test:** fixer turn → reviewer turn. Assert no Claude turn ran on Opus beyond the counted one.

### N6 (P2) — Popup approval: the text still comes from vision, and arguments are unchecked
- **Location:** `relay/orchestrator.py:1428-1447` (the dialog text comes from
  `driver.read_popup()`, i.e. DeepSeek vision) and `relay/policy.py:20-23`.
- **Reproduced:** each of these is approved:
  ```
  approve | Allow Bash: git diff --output=/Users/me/.zshrc        (writes a file anywhere)
  approve | Allow Bash: git add .relay/STATE.json
  approve | Allow Bash: git commit --no-verify -m x                (the coder rules say only Relay commits)
  approve | Allow Bash: python3 -m pytest -q -p evil_plugin
  approve | Allow Bash: pytest --rootdir=/tmp/evil
  ```
- **Fix:**
  - Read the dialog's text through Accessibility (the same AX tree now used to click), so no
    language model sits in the approval path.
  - Reject `--output`/`-o`, `--ext-diff`, `-c`, `--no-verify`, `-p`, `--rootdir`, and any path
    outside the project.
  - Drop `git add` and `git commit` from the auto-approve list entirely, because only Relay
    commits.
- **Test:** the five strings above must return `human`.

### N7 (P2) — Relay Watch can keep showing green after everything has stopped
- **Location:** `apps/RelayWatch/Sources/RelayWatch/RelayController.swift:120-138` (redraws only
  on meaningful journal events) and `:210-224` (`menuGlyph`). The 30 s heartbeat timer only runs
  while the window or menu is open (`:193-206`).
- **Impact:** if the orchestrator and monitor both stop (crash, sleep, `kill`), no new journal
  events arrive. With the menu closed, the glyph never recomputes, so it shows "Running" in
  green indefinitely. That is exactly the moment it must be right (requirement C1: faithful).
- **Second energy issue:** the watcher covers all of `.relay/`. The monitor writes its heartbeat
  every 10 s and the orchestrator every 25 s, so during a real run the app wakes several times
  a minute just to find no new events.
- **Why the energy sample doesn't settle this:** DeepSeek's "30-minute idle" sample was taken with
  Relay idle, so it doesn't show this.
- **Fix:**
  - Filter FSEvents callbacks to journal files and `STATE.json`. The flags already provide
    per-file paths.
  - Arm a single one-shot timer at "last heartbeat + 300 s". Re-arm it on each refresh, and
    when it fires, recompute the glyph. That gives at most one extra wake-up per staleness window,
    and an honest red glyph when things stop.
- **Test:** a fixture snapshot with a 10-minute-old heartbeat in an active phase must give the
  red glyph without any new event. Then measure energy again **during a live fake-driver run**.

### N8 (P2) — The fix ladder depends on the reviewer reusing slugs it isn't shown
- **Location:** `prompts/RULES_CLAUDE.md:88` asks the reviewer to reuse the previous slug. But
  the reviewer pointer (`relay/orchestrator.py:1318-1323`) doesn't list them, and the reviewer's
  read list doesn't include the previous `REVIEW.md`. The reviewer often starts in a fresh chat
  after a role switch or rollover.
- **Impact:** slugs drift ("token-expiry" vs "auth-expiry"), streaks reset, and the fix-plan and
  Opus steps rarely fire. That is the opposite of "deterministic".
- **Fix:** put `last_review_issues` from `STATE.json` into the reviewer pointer ("previous
  blocking ids: a, b; reuse them if it is the same problem").
- **Test:** the pointer contains the previous IDs.

### N9 (P3) — An Opus turn is counted before the turn is saved
- **Location:** `relay/orchestrator.py:733` (`_record_escalation`) runs before the `pending`
  mutate at `:745-762`.
- **Impact:** a crash between the two counts the Opus turn, and the restart then counts it again.
- **Fix:** count in the same `mutate` that saves `pending`.

### N10 (P3) — The first turn still skips the user-edit check
- **Location:** `relay/orchestrator.py:554-560` (`_turns_completed == 0`).
- **Fix:** compare against the baseline commit instead.

### N11 (P3) — The Automation permission is assumed, not checked
- **Location:** `relay/gui/screen.py`: `"system_events": accessibility`.
- **Impact:** the first System Events call can show a macOS permission prompt in the middle of a
  run.
- **Fix:** probe it with a harmless `tell application "System Events" to get name of first process`.

---

## Test gaps
- **Adversarial authorship tests (N1):** none.
- **Malicious git config or hooks (N2):** no tests.
- **Coder reading the review (N3):** the fake coder doesn't model what it reads, so the
  feedback-loop break is invisible to the e2e tests.
- **Relay Watch staleness (N7):** no test with an active phase and a stale heartbeat.
- **Swift tests:** they decode a fixture generated by the same Python code. That's good for
  schema drift, but no test covers the menu glyph or redraw policy.

## What I could not verify
- `swift test` (20 tests), the app bundle build, the entitlements, and the energy numbers. This
  needs macOS.
- Anything on real macOS 27.2:
  - Accessibility button clicking (`entire contents`, which may be slow on large windows)
  - `screencapture -l`
  - the CGWindowList ctypes code
  - permission checks
- DeepSeek's real model ID and image support, ntfy delivery, and Keychain storage.

## Suggested order for DeepSeek
1. N2 (small and contained).
2. N3 (a pointer and prompt change, plus a test).
3. N1: move trust material out of the project, sign the journal, harden `instruct`.
4. N4–N8.
5. The P3s.

After that, pilot it on a toy project on macOS 27.2.
