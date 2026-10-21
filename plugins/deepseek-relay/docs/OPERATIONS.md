# Operations

## First run

1. Use a stable project folder. Google Drive is allowed by default
   (`security.allow_google_drive=true`) but pause Drive sync while Relay is running. iCloud,
   Dropbox and OneDrive are refused unless `--allow-sync-dir` is passed.
2. `relay init --project <dir> --goal "<one sentence>" --set-api-key`
3. Open the project folder in both apps. Leave each app on a normal chat screen.
4. `relay calibrate --app claude` and `relay calibrate --app zcode`.
   - Grant Accessibility (required) and Screen Recording (needed for vision).
   - Both self-tests must report the same `working_dir`.
5. Run a short pilot: `relay start --project <dir> --max-steps 6`.
6. Check `relay status`, `relay trace --verify`, and `git log --oneline`.

## During a run

- `relay status` is safe to run at any time; it only reads.
- `relay pause` stops at the next safe point. `relay resume` continues. Bind a macOS Shortcut
  to `relay pause` if you want a global hotkey; there is no built-in one.
- Planner and reviewer are separate chats in the same app. Relay opens a fresh chat and seeds it
  from files whenever the role changes, so a different chat is normal, not data loss.
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

Answer with:

```bash
relay decide D-003 "B, keep the free tier"
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

## Failure playbook

| Symptom | What to do |
|---|---|
| `PAUSED: monitor halt` / `HALT` file | Read `MONITOR_NOTES.md` and `relay why` the last monitor event. Fix the cause. `relay resume --clear-halt`. |
| `PAUSED: monitor heartbeat lost` | Check `.relay/monitor/monitor.log`; restart the monitor (`relay monitor`) or resume (it will spawn one). |
| `PAUSED: DeepSeek context guard` | The call ID is in the message. `relay why CALL_ID`; start a fresh host session if applicable; `relay resume`. |
| `PAUSED: daily budget` | Inspect `relay status`; raise `daily_budget_usd` deliberately or wait for the next day. |
| `PAUSED: ownership violation` | An agent wrote another role's file. Read the journal event; revert the file if needed; `relay resume`. The monitor will also halt on this. |
| `PAUSED: secret scan` | A key or token is in a changed file. Remove/rotate it, then resume. The commit was not made. |
| `WAIT_HUMAN: popup` | Approve/deny the dialog yourself, then answer with `relay decide` or resume after closing it. |
| `WAIT_HUMAN: uncommitted changes` | Commit or stash your edits, then `relay resume`. |
| App not responding / timeout | The pending turn is kept. Open the app, check the chat, then `relay resume`; it re-sends the same pointer and round. |
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
