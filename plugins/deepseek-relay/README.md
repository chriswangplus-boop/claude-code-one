# Relay

Relay is a long-running harness that sits between the **Claude Code** desktop app and the
**Zcode** desktop app and moves work between them like a careful human would: it looks at the
screen, clicks, types a one-line pointer, waits for a sentinel file, and goes back to sleep.

Its brain is **DeepSeek `deepseek-flash`** (served by DeepSeek-V4.1-Flash-1017, 1M context,
vision-capable). Every judgment call is stateless and rebuilt from files. The orchestrator has
no conversation of its own to drift.

> **Status.** The orchestrator, journal, integrity layer, DeepSeek client, monitor, CLI and the
> full offline end-to-end loop are tested. The macOS GUI driver and calibration are implemented
> but cannot be verified without the real apps, an unlocked desktop and macOS Accessibility /
> Screen Recording permissions. Windows is not implemented. See [Limitations](#limitations).

---

## What it does, in one turn

```
1. pick the role whose turn it is (planner / reviewer / strategist / fixer / coder)
   and the model, with a deterministic policy (Sonnet by default, Opus by rule)
2. write an intent record to STATE.json, snapshot file hashes
3. focus the right app, select/verify the model, type ONE ASCII pointer line
4. poll for .relay/CLAUDE_DONE.json or .relay/CODER_DONE.json
5. validate round + sha256 + mtime, check file ownership and secrets
6. commit the turn, clear the baton, route to the next state
```

The pointer never contains file contents. It says *where* the handoff is, for example:

```
RELAY round 12 role=reviewer: read .relay/HANDOFF.md (sha256 9f3a2b1c4d5e),
.relay/CODER_REPORT.md (sha256 71bb09ac33d1), .relay/DECISIONS.md,
.relay/MONITOR_NOTES.md and .relay/RULES_CLAUDE.md (reviewer section).
Write .relay/REVIEW.md, then .relay/CLAUDE_DONE.json last.
```

## Model policy: Sonnet by default, Opus by rule

Long loops burn Claude tokens, so the model is chosen by code, never by an agent. The decision is
`relay/model_policy.py::choose_turn(state, phase, cfg)` — a pure function of `STATE.json`, the
phase and `model_policy` in the config. A DeepSeek `suggest_escalation` is journaled and ignored.

| Role | Model | App | Writes | When |
|---|---|---|---|---|
| `planner` | Sonnet 5.5 high | Claude | `PLAN.md`, `HANDOFF.md` | normal planning and the first re-plan |
| `reviewer` | Sonnet 5.5 high | Claude | `REVIEW.md`, `FIX_PLAN.md` | every review, and a fix plan at streak 2 |
| `strategist` | Opus 5.5 medium | Claude | `PLAN.md`, `HANDOFF.md`, `STRATEGY.md` | the first master plan, or after a Sonnet re-plan failed |
| `fixer` | Opus 5.5 medium | Claude | source (max 10 files), `FIX_REPORT.md`, `HANDOFF.md` | the same issue survives 4 reviews, or the coder blocks twice |
| `coder` | GLM 5.3 flash high | Zcode | source, `CODER_REPORT.md` | everything else |

The reviewer lists stable `blocking_issue_ids`; Relay counts consecutive reviews that list the
same slug. Streak 2 adds `FIX_PLAN.md` for the coder. Streak 4 (or a coder `blocked` twice on one
issue) runs the Opus fixer, then a normal Sonnet review that reads `FIX_REPORT.md`. If the streak
still grows after that fix, only a human can approve another Opus fix.

Limits: `max_opus_turns_per_milestone` (default 1; the first master plan does not count) and
`max_opus_turns_per_day` (default 3). When a rule fires over a limit, Relay asks with three
options — allow this Opus turn, continue on Sonnet, or pause — and never silently falls back.
`relay instruct --opus "..."` (or a decision answer containing "Use Opus") arms one Opus turn.
Every Claude `turn.start`, `turn.end` and `gui.model` event carries `model`, `effort`, `role` and
`model_verified` (and `effort_verified`); `model.escalated` records the trigger and both counters.

## What it will not do

- It will not answer a product, UX, scope, cost, data or security question. It pauses, shows a
  decision on the desktop and phone, and waits for a human answer.
- It will not paste code into a chat, use the clipboard, or send source files to DeepSeek.
- It will not `git push`, install from the network, or approve a non-allowlisted popup.
- It will not fight you for the mouse: it waits for 30 s of idle, has a screen-corner failsafe,
  and a global `relay pause`.

## Architecture

```
                    one shared project folder
   Claude Code app            Zcode app                 Monitor (separate process)
   planner / reviewer         coder                     read-only on .relay, writes notes/HALT
   strategist / fixer (Opus by rule)
        ^  |                     ^  |                              ^
        |  |  pointer + files    |  |                              | journal
        |  v                     |  v                              |
   +---------------------------------------------------------------+---------+
   | Orchestrator: state machine, scripted GUI hands, DeepSeek judgment        |
   | memory = STATE.json + BATON.json + files + hash-chained journal           |
   +---------------------------------------------------------------------------+
                                  |
                                  v
                       .relay/ (baton, handoffs, sentinels, decisions, journal)
```

Three rules carry the design:

1. **Files are the baton; the GUI is only the doorbell.** Work moves through `.relay/`.
2. **The orchestrator remembers nothing in its head.** Restart it any time; state is on disk.
3. **Only one actor holds the baton at a time.** Ownership violations pause the loop.

## Requirements

- macOS (the GUI backend uses AppleScript, `screencapture`, and CoreGraphics via `ctypes`).
- Python 3.11+ (tested on 3.14).
- `git` on `PATH`.
- The two desktop apps installed and logged in.
- A DeepSeek API key.
- Optional: `keyring` package. Without it Relay stores secrets with the macOS `security`
  command. Environment variables always win.
- Optional: `Pillow` for screenshot scaling / permission probes. Relay works without it.

No Python dependency is required for the core loop.

## Install

```bash
cd plugins/deepseek-relay
python3 -m pip install -e .
relay --version
```

Or run without installing:

```bash
python3 -m relay.cli --project /path/to/project status
```

## Quick start

```bash
# 1. initialize the contract inside the project both apps will open
relay init --project ~/code/myproject \
  --goal "Build a todo CLI with JSON storage and tests" \
  --set-api-key            # prompts; stored in the OS keychain
# init generates a random ntfy topic and stores it in the keychain too.
# Google Drive is allowed by default (security.allow_google_drive=true) but
# pause Google Drive sync while Relay runs. iCloud/Dropbox/OneDrive are refused
# unless you pass --allow-sync-dir.

# 2. calibrate each app once (and again after a major app update)
relay calibrate --app claude --project ~/code/myproject
relay calibrate --app zcode  --project ~/code/myproject
# The calibration self-test asks each app to write .relay/CALIBRATION_<app>.json
# with its absolute working directory. That proves the GUI path works AND that
# both apps opened the same folder.

# 3. run
relay start --project ~/code/myproject            # foreground
relay start --project ~/code/myproject --detach   # background, logs in .relay/relay.log

# 4. operate
relay status --project ~/code/myproject
relay trace  --project ~/code/myproject --round 12 --verify
relay why    --project ~/code/myproject evt-20261020-00042
```

When Relay needs a person:

```bash
# the one-time code is shown in the desktop notification for D-003
relay decide --project ~/code/myproject D-003 --code "<code>" "B, keep the free tier"
# or edit the ANSWER line in .relay/PENDING_DECISION.md: <code> <answer>
```

Nothing is lost if the process dies. `relay resume` re-enters the pending turn, and if the
sentinel was already written it is consumed rather than re-requested.

## Try it without the apps

```bash
relay init  --project /tmp/relay-demo --goal "demo" --no-ntfy
relay start --project /tmp/relay-demo --fake
relay status --project /tmp/relay-demo
```

`--fake` swaps in a deterministic pair of scripted apps, so the whole loop runs offline with no
API key. It is also the fastest way to see the journal and routing.

## Using Relay from a host chat

Relay is not installed into the chat app. The chat is the **control room**; Relay runs as a
detached process and owns the GUI apps.

One-time setup (in Terminal, because calibration is interactive):

1. `relay init --goal "..." --set-api-key`
2. `relay calibrate --app claude` and `--app zcode`
3. Grant Accessibility and Screen Recording to the app that will run the loop. If that is the
   chat app, grant the chat app; keep Terminal granted too for calibration.

For each task, from the chat:

```bash
relay init --project "$PROJECT" --goal "$BRIEF" --set-api-key   # or write .relay/GOAL.md
relay start --project "$PROJECT" --detach
relay status --project "$PROJECT" --json
```

`--detach` is required: the loop must outlive the tool call. It logs to `.relay/relay.log`.
You can close the chat; `relay status` and `relay decide` work from any later session.

During the run:

```bash
relay status --project "$PROJECT" --json   # phase, round, pending_decision, paused_reason
relay instruct --project "$PROJECT" "New scope: keep JSON storage; add CSV export."
relay decide --project "$PROJECT" D-003 --code "<code from desktop>" "B, keep the free tier"
relay pause  --project "$PROJECT"
relay resume --project "$PROJECT"
relay stop   --project "$PROJECT"
```

`relay instruct` records a real human instruction in `DECISIONS.md` and the journal, so both
agents read it on their next turn. It does not answer a pending question; use `relay decide`
for that. For a mid-turn change, `relay pause`, instruct, then `relay resume`.

In a terminal it asks `y/N`; from a headless host it shows a macOS confirmation dialog. The
dialog blocks only the `instruct` command, not the Relay loop, but it can take focus, so do not
invoke it during a GUI turn. To make instructions automatic, set
`security.require_instruction_confirmation: false` in `.relay/config.json` (or
`RELAY_SECURITY_REQUIRE_INSTRUCTION_CONFIRMATION=false`). That is a security downgrade: any
agent or process that can run `relay` could then record a "human" instruction. Keep the default
unless you trust every process in the environment.

Relay sends desktop and ntfy notifications for decisions. The chat itself is woken only when you
ask it to check, or when you create a scheduled reminder that polls `relay status --json`.

Keep the host chat small: the loop's history is on disk, not in the chat. At ~70% context, start
a new chat and run `relay status`; no resume is needed unless the loop is paused.

## The contract in `.relay/`

| File | Written by | Purpose |
|---|---|---|
| `GOAL.md` | human / `init` | what the project is for |
| `PLAN.md`, `HANDOFF.md` | planner | milestones and the next unit of work |
| `REVIEW.md` | reviewer | verdict and requested changes |
| `FIX_PLAN.md` | reviewer | exact fix steps when an issue repeats |
| `STRATEGY.md` | strategist | why the approach changed |
| `FIX_REPORT.md` | fixer | root cause, files, test results |
| `CODER_REPORT.md` | coder | what changed, tests run, self-audit |
| `CLAUDE_DONE.json`, `CODER_DONE.json` | agent, **last** | sentinel: round, file, sha256, verdict |
| `STATE.json`, `BATON.json` | orchestrator | crash-safe state and whose turn it is |
| `DECISIONS.md` | harness, from human answers only | every entry links a `decision.answered` event |
| `PENDING_DECISION.md` | harness | question, options, `ANSWER:` line |
| `MONITOR_NOTES.md` | monitor | reminders/corrections agents must read |
| `CHECKPOINT_<role>.md` | outgoing session | compact memory for a fresh session |
| `RULES_CLAUDE.md`, `RULES_CODER.md` | harness (`init`) | the agents' standing contract |
| `journal/` | harness + monitor | JSONL, hash-chained; screenshots and DeepSeek calls |

Sentinel:

```json
{"round": 12, "role": "reviewer", "status": "done",
 "verdict": "CHANGES_REQUESTED", "file": ".relay/REVIEW.md",
 "sha256": "…", "summary": "one line", "context_estimate_pct": 61,
 "blocking_issue_ids": ["auth-token-expiry"], "question": null}
```

A sentinel is ignored when its round is stale, its `sha256` does not match, or its mtime
predates the round. The agent is told to write it last; the harness assumes it may lie.

## State machine

```
INIT -> PLAN -> CODE -> REVIEW -> ROUTE
                  ^                  |
                  +-- CHANGES_REQUESTED
        MILESTONE_DONE / APPROVED -> PLAN (next milestone)
        PROJECT_DONE -> DONE
        STUCK -> PLAN (re-plan)
any state -> NEEDS_HUMAN -> WAIT_HUMAN -> answer -> resume the same step
any state -> MONITOR_HALT / CONTEXT_GUARD / budget -> PAUSED
round cap -> decision (raise the cap / re-plan / stop), never a dead-end pause
```

Routing is decided deterministically from the reviewer's verdict. DeepSeek reads the round and
can **escalate** to a human or flag drift, but can never auto-approve work the reviewer rejected.
A human answer always wins.

## Context management

Three signals feed rollover, and the highest wins:

1. the app's on-screen context indicator, read by vision (if it has one),
2. `context_estimate_pct` from the sentinel,
3. rounds in this session (Claude 4, Zcode 3).

At 70% (or at a milestone boundary above 50%) Relay asks the outgoing session to write
`CHECKPOINT_<role>.md`, verifies it, opens a new session, selects the model, and seeds the new
session with pointers to the rules, plan, decisions, checkpoint and monitor notes. No old chat
history is needed.

Every DeepSeek call is checked before sending (estimated tokens) and after (API usage). At 70%
of the context window the loop pauses and notifies you with the call ID. The stateless design
keeps normal calls far below that; the guard is a backstop, not the main mechanism.

If you host `relay start` inside another chat session, run it with `--detach`; then use:

```bash
relay host-guard --pct 72     # or --turns 35, or RELAY_HOST_CONTEXT_PCT
```

There is no portable API for a host app's usage. `host-guard` needs one number from you (or the
environment), and is honest about that instead of guessing.

## Independent monitor

`relay start` launches `relay monitor` as a separate process with its own DeepSeek calls and
fresh context per check. It can read `.relay/` and write `MONITOR_NOTES.md`, `CORRECTION.json`
and `HALT`; it has no GUI code and cannot click or type.

**Layer 1 (deterministic, no model):**

- journal hash chain intact; orchestrator heartbeat fresh
- baton matches the pending/open turn and the app that received keystrokes
- no ownership violations, no `git push`, no AI-authored decision
- sentinel's announced file still matches its sha256
- rollover happened when the context signal was ≥ 70%
- DeepSeek calls under the guard; budget and round caps respected
- three byte-identical reviews in a row → request a re-plan

Any Layer 1 violation is a **halt**. A Layer 2 rubric review looks for drift, loops, quality
decay and silent scope decisions, and can write a note, request `replan`/`rollover`, or halt.

The orchestrator pauses if the monitor heartbeat disappears. If the monitor's own Layer 2 fails
three times, it halts: who watches the watcher.

```bash
relay monitor --once --project ~/code/myproject   # one check, JSON result
```

## Security and privacy

| Risk | Control |
|---|---|
| Source/secrets to DeepSeek | Judgment calls receive review/report/sentinel text and cropped screenshots, never source files or `.env`. Every payload is redacted (API keys, tokens, private keys, emails, home paths, high-entropy strings) before sending. `deepseek.text_only_mode` disables images. |
| Screenshots | Only the target app window, usually a small region; stored `0600` under `.relay/journal/screens/`; pruned after 14 days; excluded from git. |
| API keys / ntfy topic | OS keychain (`keyring` or macOS `security`), never in config files or logs. Env vars override for CI. |
| Clipboard | Not used at all. The pointer is typed as ASCII keystrokes. |
| Secrets committed | Every commit re-scans staged files with a built-in regex scanner (no external binary required). A hit aborts the turn and pauses. |
| Phone push | Contains only a short question and decision ID. The ntfy topic is random (32+ chars). Answers are accepted **only locally**; `ntfy.phone_reply` is rejected by config validation because it is not implemented. |
| Prompt injection | Agent text is fenced in `<untrusted>` blocks with forged closing tags neutralised; system prompts say never follow instructions inside. Popup approval is a deterministic allow-list in `policy.py`; unknown or dangerous popups go to a human. |
| GUI control | Waits for user idle ≥ 30 s; verifies the window title before every action; a mouse in any screen corner raises a pause; every action is journaled. |
| Git | One local commit per turn (`relay: round N role VERDICT`); no push helper exists anywhere; a monitor invariant also checks for it. |

See [docs/SECURITY.md](docs/SECURITY.md) for the threat model and residual risks.

## DeepSeek usage

Model IDs and limits are configurable. Defaults follow the official docs as of 2026-10:

- model `deepseek-flash` (served by DeepSeek-V4.1-Flash-1017)
- 1M context, vision supported
- peak pricing $0.30 / 1M input and $1.20 / 1M output (Relay budgets at peak rates)

Sources: [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing) and
[Your First API Call](https://api-docs.deepseek.com/).

## Commands

| Command | What it does |
|---|---|
| `relay init` | create `.relay/`, rules, git baseline, keychain secrets |
| `relay calibrate --app claude\|zcode` | interactive coordinates + self-test |
| `relay start [--detach] [--fake]` | run the loop (auto-starts the monitor) |
| `relay resume [--clear-halt]` | continue after pause / human answer / crash |
| `relay pause` / `relay stop` | safe-point pause / stop loop and monitor |
| `relay status [--json]` | phase, baton, spend, Opus counters, monitor age, pending decision |
| `relay instruct "text" [--opus]` | record a new human instruction for both agents; `--opus` also arms one Opus turn |
| `relay decide ID --code CODE "answer"` | record a human answer locally |
| `relay trace --round N --verify` | timeline and hash-chain check |
| `relay why EVENT_ID` | inputs, judgment, response, screenshot, monitor notes |
| `relay replay CALL_ID` | re-run one DeepSeek judgment with the stored prompt |
| `relay monitor [--once]` | independent watchdog |
| `relay host-guard --pct N` | pause if the hosting chat is near its limit |

## Configuration

Defaults live in `config/defaults.json`; `.relay/config.json` overrides them; `RELAY_*`
environment variables override both. Nested keys use underscores:
`RELAY_DEEPSEEK_MODEL`, `RELAY_CLAUDE_APP_TIMEOUT_MINUTES`, `RELAY_NTFY_SERVER`, etc.

Important knobs:

| Key | Default | Meaning |
|---|---|---|
| `model_policy.default` | Sonnet 5.5 / high | normal model for every Claude role |
| `model_policy.escalated` | Opus 5.5 / medium | strategist and fixer only |
| `model_policy.fix_plan_after` | 2 | reviews in a row that trigger `FIX_PLAN.md` |
| `model_policy.opus_fix_after` | 4 | streak that triggers the Opus fixer |
| `model_policy.fixer_max_files` | 10 | source files one fixer turn may change |
| `model_policy.max_opus_turns_per_milestone` / `_per_day` | 1 / 3 | Opus limits (the first plan is exempt from the milestone limit) |
| `claude_app.max_rounds_per_session` | 4 | rollover trigger |
| `zcode_app.model` / `_effort` | GLM 5.3 flash / high | coder session |
| `zcode_app.max_rounds_per_session` | 3 | rollover trigger |
| `rollover_threshold` | 0.70 | context rollover |
| `deepseek.model` | `deepseek-flash` | judgment model |
| `deepseek.context_guard` | 0.70 | pause threshold |
| `deepseek.text_only_mode` | false | never send images |
| `monitor.interval_minutes` | 10 | periodic check |
| `max_rounds_per_milestone` | 8 | asks: raise the cap, re-plan, or stop |
| `max_total_rounds` | 60 | same |
| `daily_budget_usd` | 20 | pause guard |
| `security.allow_google_drive` | true | permit projects inside Google Drive (pause sync) |
| `screenshot_retention_days` | 14 | local retention |
| `popup_allow_list` | read/edit/tests/git | deterministic approval |

## Testing

```bash
cd plugins/deepseek-relay
python3 -m pytest -q
```

The suite covers crash-safe state, the hash chain (including tamper detection), ownership,
sentinel staleness, redaction, the context guard, budget, the offline two-milestone loop, a
decision pause/resume, crash/resume, stale sentinels, rollovers for every role, monitor
fault injection, popup policy, CLI commands and GUI-driver logic with a fake backend. The Part B
suite (`tests/test_model_policy.py`) covers the Sonnet default, the first-plan and re-plan Opus
rules, the fix-plan and fixer ladder, all three fixer ownership limits, the Opus limits and their
three-option decision, `suggest_escalation` being journaled only, and an unverifiable model typing
nothing. **167 tests pass** on macOS 27.2 arm64 / Python 3.14.3. Relay Watch adds **20 Swift
tests** (`swift test` in `apps/RelayWatch`), covering C9 1-5.

## Relay Watch (read-only menu-bar viewer)

`apps/RelayWatch/` is a small SwiftUI app that shows the journal as a live progress view: status
header with the current model, health lights (heartbeats, journal chain, flags), a round timeline,
a "Needs you" card with the exact `relay decide` command (never the code), usage today (DeepSeek
spend, Opus counters, Sonnet-to-Opus ratio), alerts and a raw event inspector. It only reads:
one `RelayFolder` type with no write APIs, a read-only security-scoped bookmark, no network
entitlement, and it never answers a decision.

It redraws only for the meaningful events in `docs/EVENTS.md`, using an `FSEventStream` with 3 s
latency and incremental byte-offset reads — no polling. Build and test instructions, the sandbox
notes and the energy-check results are in
[apps/RelayWatch/README.md](apps/RelayWatch/README.md).

## Limitations

- **macOS only.** `relay/gui/screen.py` implements the macOS backend. The Windows backend from
  the plan is not implemented; `backend_for()` fails with a clear message. The `AppDriver`
  interface is unchanged, so a Windows backend can be added without touching the orchestrator.
- **Calibration is per app and per layout.** Coordinates are stored from calibration; there is no
  template matching. An app redesign breaks them. Re-run calibration; the window-title check and
  bounded recovery catch most drift, and every action is journaled.
- **Vision can misread.** Context percentages and popup text are checked conservatively; an
  unknown popup always goes to a human. The max-of-three rollover signals prevent one bad read
  from skipping a rollover.
- **DeepSeek sees redacted review text.** It is a third-party cloud API. `text_only_mode` and the
  redactor limit exposure, but do not eliminate it. Review DeepSeek's data policy before a
  long run.
- **Phone replies are not implemented.** Answers are local only by design.
- **No built-in global hotkey.** Bind a macOS Shortcut (or any launcher) to `relay pause`; the
  command is the global pause switch.
- **`host-guard` needs a number.** There is no portable host-usage API; pass `--pct`/`--turns`.
- **The first turn for an app opens a fresh chat.** Switching roles (planner, reviewer,
  strategist, fixer) also opens a new chat and re-seeds it from `.relay/`; this keeps role memory
  separate. Opus turns always get a fresh chat.
- **Monitor Layer 2 needs an API key.** Without one it still runs all deterministic checks.
- **Cloud-synced folders.** Google Drive is allowed by default for this setup
  (`security.allow_google_drive=true`); keep Drive sync paused while Relay runs. iCloud, Dropbox
  and OneDrive are refused unless `--allow-sync-dir` is passed. Atomic state writes, per-turn
  commits and the journal chain reduce risk, but an active sync client can still create conflict
  copies.

## Documentation

- [Design](docs/DESIGN.md) — state machine, turn protocol, invariants.
- [Security](docs/SECURITY.md) — threat model, data flows, residual risk.
- [Operations](docs/OPERATIONS.md) — first run, failures, recovery, audit workflow.
- [Events](docs/EVENTS.md) — every journal event and the `detail` fields Relay Watch relies on.
- [Relay Watch](apps/RelayWatch/README.md) — build, sandbox, tests and the energy check.
