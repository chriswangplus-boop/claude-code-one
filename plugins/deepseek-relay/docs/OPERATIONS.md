# Operations

## First run

1. Use a stable project folder. Google Drive is allowed by default
   (`security.allow_google_drive=true`) but pause Drive sync while Relay is running. iCloud,
   Dropbox and OneDrive are refused unless `--allow-sync-dir` is passed.
2. `relay init --project <dir> --goal "<one sentence>" --set-api-key`
3. Open the project folder in both apps. Leave each app on a normal chat screen.
4. `relay calibrate --app claude` and `relay calibrate --app zcode`.
   - Grant Accessibility (required), Screen Recording (needed for vision) and Automation of
     System Events. **Quit and relaunch the app that runs Relay** after granting Screen Recording;
     macOS caches it per process, so a running harness will still see black captures.
   - Both self-tests must report the same `working_dir`.
5. Run a short pilot: `relay start --project <dir> --max-steps 6`.
6. Check `relay status`, `relay trace --verify`, and `git log --oneline`.

## During a run

- `relay status` is safe to run at any time; it only reads. It also prints the trust folder that
  holds the decision codes and journal key.
- `relay pause` stops at the next safe point. `relay resume` continues. Bind a macOS Shortcut
  to `relay pause` if you want a global hotkey; there is no built-in one.
- Planner, reviewer, strategist and fixer are separate chats in the same app. Relay opens a
  fresh chat and seeds it from files whenever the role changes, so a different chat is normal,
  not data loss. Opus turns always get a fresh chat, which keeps their context (and cost) small.
- `relay status` shows `Opus today: x/3, this milestone: y/1`. Read it before wondering why a
  fix ran on a cheaper model.
- `relay stop` also stops the monitor. `relay resume` clears the stop flag.
- Do not edit `.relay/` files while a turn is open. If you do, the ownership check pauses and
  asks; it never overwrites silently.
- You may edit source files between turns. Relay sees uncommitted changes when the baton is
  free, pauses, and asks you to commit or stash.

## Decision flow

When Relay needs a human it writes `.relay/PENDING_DECISION.md`, sends a desktop notification
and an ntfy push, sets the baton to `human`, and waits. Reminders repeat every 4 hours.

To subscribe on your phone, read the generated ntfy topic from the keychain and add it in the
ntfy app:

```bash
security find-generic-password -s deepseek-relay -a ntfy_topic -w
```

Treat that string as a secret: anyone with the topic can read the pushes (which contain only a
question and decision ID). Use `ntfy.server` for a self-hosted server and `ntfy.token` for an
access token.

R3-7: the one-time answer code is delivered **only** to the phone (ntfy with an access token) or,
when Relay runs in an interactive terminal, printed there. It is never in a desktop notification
and never on a process command line, because a computer-use agent can read both. `relay start`
refuses when a supervisor bundle id in `security.supervisor_bundle_ids` holds Screen Recording or
Accessibility; set that list to the apps Relay supervises.

Answer with:

```bash
relay decide D-003 --code "<code>" "B, keep the free tier"
```

or edit the `ANSWER:` line. The harness appends a `DECISIONS.md` entry with the
`decision.answered` event ID, clears the pending state, and resumes the phase that was waiting.

If the process is not running, `relay resume` picks the answer up.

For a new instruction that is not a pending question (a scope or priority change):

```bash
relay instruct "keep JSON; add CSV export" --project ~/code/myproject
```

It is appended to `DECISIONS.md` with a `decision.answered` journal event. Both agents read it on
their next turn. To make it take effect during a turn, pause first and resume afterwards.

`relay instruct` always asks for confirmation in the native macOS dialog. It never reads stdin,
because an agent can hand a command a pseudo-TTY (`echo y | script -qc "relay instruct ..."`); that
hole is closed. If you are driving Relay unattended and accept the risk, set
`security.require_instruction_confirmation=false`; otherwise a shell-capable agent could record its
own instruction. The dialog does not pause the loop, but it can steal focus, so avoid running it
mid-turn.

## Model policy and Opus limits

The model is chosen by code (`relay/model_policy.py`), never by an agent or by DeepSeek. Sonnet 5.5
at high effort is the default for planning and review. Opus 5.5 at medium effort runs only the
`strategist` (first master plan, or a second stall after a Sonnet re-plan) and the `fixer` (the
same issue at streak 4, or the coder blocked twice on it). `relay why EVENT_ID` on a
`model.escalated` event shows the trigger and both counters.

Limits come from `model_policy`: 1 Opus turn per milestone (the first master plan is exempt) and 3
per day. When a rule fires over a limit, Relay writes a three-option decision — **Allow this Opus
turn**, **Continue on Sonnet**, or **Pause** — and waits. Nothing runs before the answer. Choosing
"Continue on Sonnet" is remembered for that issue until its streak changes.

`relay instruct --opus "..."` arms one Opus turn for the next phase that has an Opus role; a
decision answer containing "Use Opus" does the same. Both count toward the limits.

If a round cap is reached, Relay also asks: **Raise the cap** (adds at least 3 rounds, stored in
`STATE.json`), **Re-plan** (starts a new cycle and restarts the milestone round count), or **Stop
for now**. There is no config edit to make and no unpickable pause.

## The trust folder

Everything that proves a human acted is stored outside the project, in
`~/Library/Application Support/Relay/<project-id>/` (folder `0700`, files `0600`):

| File | Holds |
|---|---|
| `journal.key` | the HMAC key for every journal event and every signed record |
| `journal-anchor.json` | the fixed start of trust (one-time journal migration point) |
| `journal-tip.json` | the moving tail, so deleting the end of the journal is detected |
| `decisions.json` | signed decision records: question hash, options, kind, data, return_to, round, code hash |
| `wait-fingerprint.json` | the decision-relevant `STATE.json` digest while a question is pending |
| `attempts.json` | signed file and git-control baselines for the in-flight turn |
| `decision-codes.json` | legacy code hashes (v0.2/v0.3); new decisions use `decisions.json` |

`relay status` prints the exact path, and `RELAY_TRUST_DIR` moves it (tests use a temp dir).

- Do not copy this folder into the project, a repo, or a cloud-synced backup.
- Deleting it invalidates the pending decision records and the journal anchor. Relay re-anchors an
  old journal once on the next start (older events stay chain-only); every new event is signed.
  A decision pending at that moment cannot be answered; re-ask it with `relay resume`.
- Losing `journal.key` means existing signatures can no longer be verified. Keep the project's
  `.relay/` and this folder together in a backup if you back up at all.
- A pending turn whose baseline is missing (an upgrade from v0.2/v0.3, or a deleted folder) pauses.
  Inspect the files, then run `relay resume --adopt-baseline` to accept them as the baseline.

## Failure playbook

| Symptom | What to do |
|---|---|
| `PAUSED: monitor halt` / `HALT` file | Read `MONITOR_NOTES.md` and `relay why` the last monitor event. Fix the cause. `relay resume --clear-halt`. |
| `PAUSED: monitor heartbeat lost` | Check `.relay/monitor/monitor.log`; restart the monitor (`relay monitor`) or resume (it will spawn one). |
| `PAUSED: DeepSeek context guard` | The call ID is in the message. `relay why CALL_ID`; start a fresh host session if applicable; `relay resume`. |
| `PAUSED: daily budget` | Inspect `relay status`; raise `daily_budget_usd` deliberately or wait for the next day. |
| `PAUSED: ownership violation` | An agent wrote another role's file. Read the journal event; revert the file if needed; `relay resume`. The monitor will also halt on this. |
| `PAUSED: git control file changed` | An agent planted a hook, fsmonitor or `.git/config` change during a turn. Nothing ran (hooks are disabled). Inspect `.git/hooks` and `.git/config`, remove the change, then `relay resume`. |
| `PAUSED: this pending turn has no signed baseline` | An upgrade or a removed trust folder. Inspect the changed files, then `relay resume --adopt-baseline` (it records the acceptance). |
| `PAUSED: decision tampering detected` | `STATE.json` or the question text changed while Relay waited for you. Inspect the project and the trust folder; the decision was not accepted. Restore `STATE.json` from git if needed, then `relay resume`. |
| `WAIT_HUMAN: dialog could not be read` | Accessibility/Automation failed while a dialog was open. Nothing was approved. Check the permissions, close the dialog yourself, then resume. |
| `relay instruct` refuses to confirm | The native dialog was cancelled or macOS could not show it. Relay never accepts a piped "y". Run it from an interactive session. |
| `PAUSED: secret scan` | A key or token is in a changed file. Remove/rotate it, then resume. The commit was not made. |
| `WAIT_HUMAN: popup` | Approve/deny the dialog yourself, then answer with `relay decide` or resume after closing it. |
| `WAIT_HUMAN: uncommitted changes` | Commit or stash your edits, then `relay resume`. |
| App not responding / timeout | The pending turn is kept. Open the app, check the chat, then `relay resume`; it re-sends the same pointer and round. |
| `WAIT_HUMAN: model label` | Nothing was typed. Re-run `relay calibrate --app claude`; the model picker or its label region has drifted. |
| `WAIT_HUMAN: Opus limit` | Answer the three-option decision: allow one Opus turn, continue on Sonnet, or pause. Raising the limits is an explicit `model_policy` edit. |
| Wrong model selected | `relay calibrate --app claude` again; the model label verification is what failed. |
| Coordinates drifted after an app update | Re-run calibration for that app. |
| Journal chain broken | Do not continue. Copy `.relay/` for investigation, inspect `relay trace --verify`, and restore from git if needed. |

## Auditing a past failure

```bash
relay trace --round 12            # timeline for the round
relay trace --verify              # chain integrity
relay why evt-20261020-00042      # event + nearby events + DeepSeek prompt/response + screenshot
relay why call-8f21a0c3d4e5       # the call record directly
relay replay call-8f21a0c3d4e5    # re-run the judgment with the stored (redacted) prompt
```

`why` shows the redacted user prompt, the model response, usage, context percentage, cost, the
image reference (not pixels), and relevant monitor notes.

`replay` costs one API call. It is the right tool before changing a judgment prompt: replay a
sample of past calls, compare, then edit the prompt file.

## Crash recovery

- `STATE.json.pending` is written before the pointer is typed.
- `relay resume`:
  - if a valid sentinel already exists, it is consumed without re-ringing;
  - otherwise the same round/pointer is re-sent;
  - if a human answer exists, it is accepted first.
- `BATON.json` left by a dead process is cleared on start when no pending turn exists.
- The per-turn git commit means the last completed step is recoverable.

## Monitor hygiene

- `relay monitor --once` prints a JSON result. A monitor lock rejects a second concurrent
  instance; use the running one, or stop it first.
- `MONITOR_NOTES.md` is read by every agent; keep it short.
- A `CORRECTION.json` is consumed at the next turn boundary. A `HALT` requires a human.
- If the monitor dies, the orchestrator pauses within its heartbeat threshold.

## Checking that nothing leaked

```bash
grep -R "sk-" .relay/journal/calls 2>/dev/null   # should be empty
git -C <project> log --oneline | head
git -C <project> status --porcelain              # should be empty between turns
```

Relay's own tests plant a fake key and assert it never appears in a DeepSeek payload, a call
record, or a commit.

## Stopping for the day

`relay pause` (keep the session for tomorrow) or `relay stop` (exit the loop and monitor).
Everything is on disk; `relay resume` continues, and `relay status` tells you where you are.
