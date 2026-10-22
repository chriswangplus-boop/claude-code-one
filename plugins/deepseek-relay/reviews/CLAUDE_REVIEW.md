# Independent review — Relay

Reviewer: Claude (independent of the author). Reviewed commit `1e24781` on branch
`claude/lucid-bell-70tepa`, which is `fd35d84` from `deepseek-relay-code.patch` applied with
`git am -3` to `7bae720`. The commit has 63 files.

Verdict: **FIX-FIRST**

The design is sound and largely follows `BUILD_PLAN.md`. The parts are all present: pointer-only
typing, sentinel round/sha256/mtime checks, the hash-chained journal, a separate monitor process,
a stateless brain and the context guard. The sentinel validation and the code that commits
without ever pushing are careful work.

Five P1 findings break the plan's core promises. Two let an agent defeat a promise: agents can
answer human decisions, and agents can write harness control files without detection. One
weakens a third promise: the approval-dialog decision ("popup policy") can be steered by the
dialog text and by where the language model says to click. One makes a project unrecoverable:
the monitor re-halts forever after any past violation. One fights the user: screen checks pull
the agent apps to the front every minute. All five can be fixed locally. None needs a redesign.

## How this was checked

- `python3 -m pytest` on Linux with Python 3.13: **118 passed, 1 failed**. The failure is
  `tests/test_imports.py::test_runtime_wiring_without_touching_the_gui`. It only passes on
  macOS (see P3-22).
- I read every module under `relay/` that the brief prioritises.
- I confirmed the P1 findings by running small scripts against the real code. P1-2 and P1-3 used
  `ownership_violations` and `decide_popup`. P1-4 used `Monitor.check_once`. Their outputs are
  quoted below.

---

## Findings

### P1-1 — An agent can answer a human decision, and the journal records it as `actor: human`
- Location: `relay/decisions.py:64-74` (`local_answer`), `relay/orchestrator.py:1190-1196`,
  `relay/decisions.py:124-141`
- Evidence: While in `WAIT_HUMAN`, the orchestrator polls `.relay/PENDING_DECISION.md` and accepts
  any non-empty `ANSWER:` line. `_accept_locked` then journals `decision.answered` with
  `actor="human"` and appends it to `DECISIONS.md` with evidence. `relay decide` is an equally open
  path: it is an ordinary shell command, and both agents can run shell commands.
- Impact: The planner, reviewer or coder can answer its own question. This includes a
  prompt-injected agent and an agent that is still running when a timeout or popup escalates.
  The journal and `DECISIONS.md` then show a human-authored decision, and the monitor's
  `decision_authorship` check passes. This defeats plan requirement 3 ("never does job 3") and
  the §7 invariant "No AI-authored entry in DECISIONS.md".
- Fix: Bind each answer to a one-time code that is never written inside the project. Generate it
  in `escalate_human`, show it only in the desktop notification and in the terminal, and store
  only its hash in `STATE.json`. Require `ANSWER: <code> <text>` or
  `relay decide D-007 --code <code> "..."`. For `relay instruct`, require an interactive TTY
  confirmation, or apply the same code mechanism.
- Test: With a pending decision, write `ANSWER: A` into `PENDING_DECISION.md` and run
  `wait_for_decision`. Assert the phase stays `WAIT_HUMAN` and no `decision.answered` event
  exists. Then repeat with the correct code and assert the answer is accepted.

### P1-2 — Harness and monitor control files are outside the ownership check
- Location: `relay/integrity.py:26-34` (`IGNORED_PREFIXES`/`IGNORED_FILES`) and
  `relay/integrity.py:109-110` (the monitor-owned skip)
- Evidence: The snapshot ignores these files: `STATE.json`, `BATON.json`, `HALT`/`PAUSE`/`STOP`,
  `.relay/monitor/` (including `CORRECTION.json`), `.relay/answers/`, `.relay/journal/` and
  `.relay/calibration/`. `ownership_violations` also skips every `MONITOR_NOTES.md` change,
  whoever wrote it. Running the code shows a coder writing all of these produces no violations:
  ```
  coder writes control files -> []
  ignored from snapshot: ['.relay/monitor/CORRECTION.json', '.relay/STATE.json', '.relay/HALT', '.relay/answers/D-001.txt']
  ```
- Impact:
  - An agent can rewrite `STATE.json`, for example to set `phase` to `DONE` or clear
    `pending_decision`.
  - It can force a re-plan through `monitor/CORRECTION.json`.
  - It can edit calibration files, which control where clicks land.
  - It can write instructions into `MONITOR_NOTES.md`. Every agent is told to read that file
    each turn, and `escalate_human` copies it into decision context (`orchestrator.py:1128-1130`).
    That makes it an undetected prompt-injection channel between agents.
- Fix: Hash the control files in the turn snapshot. The harness should record each of its own
  writes during a turn (path and new hash) so `ownership_violations` can tell them apart from
  agent writes. Treat any `MONITOR_NOTES.md` change without a matching `monitor.note` journal
  event as a violation.
- Test: A fake coder turn writes each control file. Assert an `integrity.violation` event and a
  pause for each one.

### P1-3 — Popup approval passes chained shell commands, and the click point comes from the language model
- Location: `relay/policy.py:45` (substring allow-list) and `relay/gui/app_driver.py:265-280`
  (button coordinates from vision)
- Evidence: An allow-list entry matches if it appears **anywhere** in the dialog text. The
  dangerous-word list is a fixed denylist. Running the code shows all three of these are approved:
  ```
  approve | Allow Bash: git diff && bash -c "$(echo cm0gLXIgfg== | base64 -d)"?
  approve | Run tests? pytest; rm -r ~/Documents
  approve | Allow Bash: git commit -m wip && python3 exfil.py
  ```
  `choose_button` picks the button label deterministically. But `click_popup` clicks
  `popup.button_points[label]`, and those coordinates come from the DeepSeek vision response. A
  misread, or a prompt injection on screen, can map "Allow once" onto the "Always allow" button.
- Impact: The plan's central safety rule ("decided by a deterministic allow-list in code") does
  not hold. Arbitrary commands can be auto-approved as long as they mention an allowed phrase.
- Fix:
  1. Extract the command and match the **whole command** against anchored patterns, such as
     `^git (status|diff|add|commit)( [^;&|`$<>\n]*)?$` and `^(pytest|python3? -m pytest)\b[^;&|`$<>\n]*$`.
  2. Reject any shell metacharacters (`; & | ` $( > <` and newlines).
  3. Click by button label through Accessibility, using
     `tell process ... to click button "Allow once" of window 1` (System Events). Never use
     coordinates from the model. If the button can't be found, escalate.
- Test: Run the three strings above, plus redirection and newline cases, through `decide_popup`.
  Each must return `human`. A second test should prove `click_popup` never uses
  vision-supplied points.

### P1-4 — The monitor re-halts forever on past events; HALT can't be cleared
- Location: `relay/monitor/watchdog.py:60-65`. The same pattern appears at `:106-152`
  (missed rollover, context guard) and `:353-357` (`_last_halt_key` dedupe).
- Evidence: `check_invariants` re-scans **every event since `relay init`** on every check. A
  single historical `integrity.violation` event is therefore a permanent halt finding. I
  reproduced it:
  ```
  first monitor: {'halted': True, 'findings': ['ownership']}
  same monitor after clear: {'halted': True, 'findings': ['ownership']} HALT exists: False
  restarted monitor: {'halted': True, 'findings': ['ownership']} HALT exists: True
  ```
  The scenario: the user reviews the problem and runs `relay resume --clear-halt`. That spawns a
  fresh monitor, which halts again immediately. The same-process dedupe makes it worse: the
  monitor reports `halted: True` but does not set the flag.
- Impact: After any ownership violation, secret hit, context-guard trip or missed rollover, the
  project cannot run again. The only workaround is editing the journal. That breaks the hash
  chain, which is itself a permanent `journal_chain` halt.
- Fix: Have `cmd_resume --clear-halt` journal a `human.halt_cleared` event. In
  `check_invariants`, evaluate only the history-based checks (violations, push, missed rollover,
  guard, unhandled guard) on events after the latest `human.halt_cleared`. Base the dedupe on
  event IDs, not message text.
- Test: Violation → halt → `resume --clear-halt` → new `Monitor.check_once()`. Assert no halt.
  Then inject a new violation and assert it halts again.

### P1-5 — Screen checks bring the agent apps to the front while the user is working
- Location: `relay/gui/screen.py:124-142` (`window()` sets `frontmost` and runs `AXRaise`),
  `relay/gui/screen.py:189-196` (`capture` calls `window`), `relay/orchestrator.py:930-933`
  (screen check every `screen_check_seconds` during waits)
- Evidence: `read_popup`/`is_busy` → `_capture` → `backend.capture` → `window()`, which runs
  `set frontmost to true` and `AXRaise`. This runs every 60 s for the whole 30–60-minute wait, and
  it never checks whether the user is idle. That idle check exists only in `wait_for_idle`/`ring`.
- Impact: While the user types in another app, Claude or Zcode jumps to the front every minute.
  The user's keystrokes, and Return, go into the agent's chat. Content can leak, junk
  instructions can reach the agent, or a half-typed message can be submitted. This breaks the
  §3/§6 rule "don't fight the user".
- Fix: Use a read-only window lookup for screenshots: no `frontmost`, no `AXRaise`. Capture with
  `screencapture -l <CGWindowID>`, which captures that window even when it is covered (this also
  fixes P2-15). Skip screen checks when `idle_seconds() < user_idle_seconds`.
- Test: A fake backend counts `focus` calls. Assert `_screen_check` performs zero focus or raise
  operations.

### P2-6 — A crash between two state saves runs the same turn twice
- Location: `relay/orchestrator.py:537-574` and `:607-610`
- Evidence: `_process_turn` saves `pending=None` while keeping the old `phase`. The
  `phase → NEXT_PHASE` transition is a separate, later save.
- Impact: Suppose the process crashes, or is killed by `relay stop -9` or a power loss, between
  the two saves. `relay resume` then sees `phase=CODE, pending=None` and starts round N+1 of the
  **same** phase. The coder runs again, and `session_rounds` and `rounds_in_milestone` count it
  twice.
- Fix: Set `state["phase"] = target` (or `WAIT_HUMAN` with `pending_decision`) in the **same**
  save that clears `pending`.
- Test: Patch `self.journal.append` so it raises right after the `turn.end` save. Resume, and
  assert the next phase is `REVIEW` and the round number is unchanged.

### P2-7 — On resume, the pointer can be typed into the wrong role's Claude chat
- Location: `relay/orchestrator.py:235-238`, `:399-421`, `:439`
- Evidence: `_prepare` clears `active_session` on every start. On the resume path,
  `_ensure_session` is only called when a sentinel exists but is invalid. When there's no
  sentinel, the reviewer pointer is typed into whatever Claude chat is in front, which may be the
  planner's chat. The model is switched in that same chat.
- Impact: Roles get mixed: reviewer instructions land in the planner's chat history after any
  crash or restart.
- Fix: Always call `_ensure_session(role, round_no)` before ringing on the resume path.
- Test: Use a FakeAppDriver that tracks which session receives each pointer. Crash during REVIEW
  with no sentinel, resume, and assert a new reviewer session was opened before the ring.

### P2-8 — Switching Claude roles throws away the outgoing chat without a checkpoint
- Location: `relay/orchestrator.py:454-475` (`_ensure_session`), `:379-380`
- Evidence: Each planner↔reviewer switch opens a new chat. The outgoing role never writes
  `CHECKPOINT_<role>.md`. `_maybe_rollover` runs afterwards and sees a fresh session. Because
  `session_rounds` resets on every switch, the round-cap rollover (which is what writes
  checkpoints) almost never fires for Claude roles.
- Impact: Whatever the planner or reviewer knew but hadn't written down is lost at every switch.
  The plan's §6a procedure (checkpoint first, then a new session) is skipped for Claude. The new
  chat is seeded with a missing or stale `CHECKPOINT_<role>.md`.
- Fix: If the outgoing role's session has done at least one round, run the checkpoint step for it
  before opening the new role's chat. The alternative is one Claude window per role.
- Test: Run PLAN → CODE → REVIEW → (APPROVED) → PLAN. Assert `CHECKPOINT_reviewer.md` was
  requested before the planner chat opened.

### P2-9 — An aborted turn skips a round, and the bad write becomes the next baseline
- Location: `relay/orchestrator.py:1241-1253` (`_abort_turn`), `:336-342`, `:424`
- Evidence: On an ownership violation, `_abort_turn` clears `pending`, but `state["round"]` is
  already N. On resume, `before = snapshot(...)` includes the violating file, so it is never
  flagged again. `_user_edits_pending()` returns `False` when `_turns_completed == 0` (first turn)
  or when git is disabled. In those cases the next turn's `git add -A` commits the violating
  write.
- Impact: The violation is laundered into history, and the round numbering jumps.
- Fix: On abort, record the violating paths in `STATE.json`. On resume, refuse to continue while
  they differ from HEAD unless the human resolves them. Drop the `_turns_completed == 0` exemption.
- Test: The first turn writes `.relay/PLAN.md` as the coder. Resume. Assert it pauses again and
  that no commit contains the file.

### P2-10 — `STATE.json` updates race with `relay decide` and `relay instruct`
- Location: `relay/state.py:199-204` (the lock covers only the write), `relay/orchestrator.py:953-1090`
  (`_route` loads, waits on a DeepSeek network call, then saves), `:1185-1206`
- Evidence: The orchestrator does load → slow work → save without holding the lock.
  `relay decide` and `relay instruct` update `phase`, `pending_decision` and `decisions` under the
  lock in between, and the orchestrator's stale save then overwrites them.
- Impact:
  - A decision gets recorded in `DECISIONS.md` while the state returns to `WAIT_HUMAN` with
    `PENDING_DECISION.md` already deleted, so Relay waits forever.
  - Two different decisions can get the same `D-xxx` ID.
- Fix: Route every state change through `store.mutate()`, which reloads under the lock, and pass
  in only the fields being changed.
- Test: Two contexts on one project. Pause the orchestrator inside `brain.route` with a stub,
  run `record_instruction` from the other context, and assert the `decisions` counter survives.

### P2-11 — A partial last journal line bricks the project
- Location: `relay/journal.py:137-147`, `relay/orchestrator.py:255` (outside the `run` try block)
- Evidence: After a crash in the middle of a write, `_tail_event` raises `JournalError` on every
  append. `_prepare` appends `run.start`, so `relay start` and `relay resume` fail forever. There
  is no repair command. Editing the file by hand breaks the chain, which triggers P1-4.
- Fix: Add `relay journal repair`. It should move the partial tail into
  `journal/quarantine-<ts>.txt` and append a `journal.repaired` event that chains to the last
  valid line. `verify()` should accept that event.
- Test: Append half a JSON line, run repair, then assert `verify()` passes and `start` works.

### P2-12 — The monitor can falsely halt while a turn is starting
- Location: `relay/orchestrator.py:388-396` saves `pending` → `:424-426` (a full project
  `snapshot()`, then `set_baton`). The monitor's check is `relay/monitor/watchdog.py:193-197`.
- Evidence: During the full-repository hash, `pending.role` is set but the baton holder is still
  `none`. The monitor halts with "baton is 'none' but the pending turn belongs to 'coder'".
- Impact: Intermittent false halts. The bigger the repository, the more often they happen.
  (I'm confident about the timing window but haven't reproduced it under load.)
- Fix: Set the baton before saving `pending`. Alternatively, have the monitor tolerate
  `holder == none` when no `turn.start` has been journaled for the pending round.
- Test: A monitor check between those two saves must not halt.

### P2-13 — Choosing a model can type into the chat, and checking the model is optional
- Location: `relay/gui/app_driver.py:152-191`, `relay/orchestrator.py:814-816`
- Evidence: `select_model` clicks the model picker, presses ⌘A, types the model name and presses
  Return. If the click misses (for example after a UI change), this selects the chat input,
  replaces it with "Opus 5.5" and sends that as a message. With no `label_region`, or with vision
  unavailable, it returns without verifying the model. This runs before every ring.
- Impact: Stray messages go to the agent. The run can continue on the wrong model with nothing
  but an `unverified` journal entry. The plan (§4 and self-audit #11) requires the model to be
  verified before each ring.
- Fix: Make `label_region` mandatory when calibrating the Claude app. Confirm the picker actually
  opened before typing (with a vision check, or the picker's AX role). Treat `unverified` as
  `HumanRequired`.
- Test: A fake backend whose picker click does nothing. Assert no Return is pressed and that
  `HumanRequired` is raised.

### P2-14 — The shared-folder proof fails open
- Location: `relay/cli.py:131`
- Evidence: Startup is refused only if **both** proofs exist and disagree. If either proof is
  missing, Relay starts anyway. Nothing checks that the proven folder equals `project_dir`.
- Impact: Relay can run with one app working in a different folder. Sentinels never appear, or
  worse, the work lands in another copy of the repository.
- Fix: Require both `CALIBRATION_<app>.json` files, and require both to resolve to `project_dir`.
- Test: Remove one proof and assert `_runtime` raises `ConfigError`.

### P2-15 — Screenshots grab a screen region, not the window
- Location: `relay/gui/screen.py:170-187` (`screencapture -R x,y,w,h`)
- Evidence: A region grab captures whatever is drawn on top of that rectangle: notification
  banners (including Relay's own phone-mirrored notifications), other apps' windows and the
  user's content. These crops are sent to DeepSeek.
- Impact: Private screen content leaves the machine. This breaks §3's "window-only crops;
  notifications excluded".
- Fix: Use `screencapture -l <windowid>`. Get the window ID with `CGWindowListCopyWindowInfo`,
  then crop that image.
- Test: Unit-test that the capture command uses `-l` and never `-R`.

### P3-16 — Hitting a round cap leaves a pause that can't be resumed
- Location: `relay/orchestrator.py:282-287`
- Impact: `resume` re-pauses immediately until the user edits the config. Instead, escalate a
  decision with options: raise the cap, re-plan, or stop.

### P3-17 — `replans` never resets
- Location: `relay/orchestrator.py:323`, `:989`, `:1050`, `:1064`
- Impact: After 3 re-plans over a project's whole life, every later stall escalates straight to
  the human. Reset the counter when a milestone is approved.

### P3-18 — `APPROVED` is treated like `MILESTONE_DONE`
- Location: `relay/orchestrator.py:44`, `:961`, `:1073-1075`
- Impact: Any approval increments `milestone` and resets `rounds_in_milestone`. That defeats the
  per-milestone round cap and mislabels progress. Only `MILESTONE_DONE` should advance the
  milestone.

### P3-19 — The untrusted-text fence is case-sensitive
- Location: `relay/brain/prompts.py:38`
- Impact: `</UNTRUSTED>` or `</untrusted >` gets through unescaped. Replace with a
  case-insensitive regex such as `re.sub(r"(?i)<\s*/?\s*untrusted", "[untrusted", text)`.

### P3-20 — Failed DeepSeek responses aren't recorded or charged
- Location: `relay/brain/client.py:183-192`
- Impact: The code raises before `record_call` and the `deepseek.call` journal entry. The spend is
  undercounted against the budget, and `relay why` has no record. Record the call, with its
  usage and cost, before parsing.

### P3-21 — The secret scan's placeholder filter can match inside real secrets
- Location: `relay/integrity.py:322-324`
- Impact: Any value containing "none", "null", "true", "false" or "example" (case-insensitive) is
  skipped as a placeholder. Anchor the check to the whole value.

### P3-22 — The test suite isn't green outside macOS
- Location: `tests/test_imports.py:22-35`
- Impact: It calls the real `backend_for`, which raises `ScreenError` on Linux. Mark the test
  macOS-only, or inject the fake backend, so Linux CI can run the suite.

---

## Test gaps that give false confidence
- **Agent-written decision answers (P1-1):** no test covers this. The authorship tests only check
  that a journal event exists, not who could have created it.
- **Control files (P1-2):** no adversarial test has an agent write `STATE.json`,
  `monitor/CORRECTION.json`, `MONITOR_NOTES.md` or calibration files.
- **Popup policy (P1-3):** the tests use benign dialog text only. None try chained commands,
  redirection, or vision-supplied click points that disagree with the chosen label.
- **Monitor lifecycle (P1-4):** the tests run one `check_once`. None cover clear → restart →
  check.
- **Crash injection (P2-6):** nothing crashes between the multi-step saves in `_process_turn`,
  `_rollover` or `accept_decision`.
- **Concurrency (P2-10):** no two-process test of the orchestrator alongside
  `relay decide`/`relay instruct`.
- **GUI focus (P1-5, P2-13):** the GUI tests don't assert that observing the screen never
  focuses a window, or that a missed picker click never sends a message.

## Plan compliance notes
- Matches the plan:
  - pointer-only ASCII typing with no clipboard
  - sentinel round/role/file/sha256/mtime validation
  - one local commit per turn, never pushing, with a staged-file secret scan
  - stateless DeepSeek calls with a per-call context guard and daily budget
  - separate monitor process with Layer 1 and Layer 2 checks
  - hash-chained journal with `trace`, `why` and `replay`
  - desktop and ntfy notifications carrying only the question and ID
  - `host-guard`
- Deviations:
  - **Google Drive folders are allowed by default** (`security.allow_google_drive=true`). The
    plan said to refuse every cloud-synced folder. The brief says this was the user's choice, so
    it isn't listed as a defect. The risk the plan warned about (sync conflict copies of
    `.relay/` files) remains, and the code only prints a warning.
  - **There's no Windows backend.** The plan's phase 8 isn't done.
  - **A bad DeepSeek JSON reply pauses the loop with no retry.** The plan said to retry once,
    then ask the human. Pausing is the safer half of that.

## What I could not verify
- Real macOS behaviour of the AppleScript, CoreGraphics and `screencapture` calls, the
  Accessibility and Screen Recording permission checks, and the CoreGraphics key and mouse
  events.
- What the Claude Code and Zcode apps actually show on screen: dialog layout, model picker and
  context indicator.
- DeepSeek V4.1 Flash's real model ID, image-input support, prices and the `usage` field shape.
  The cost defaults (`0.3`/`1.2` USD per million tokens) are unverified.
- ntfy delivery and Keychain storage.
