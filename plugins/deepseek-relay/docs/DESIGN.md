# Design

## Invariants

1. **One baton.** `.relay/BATON.json` names the only app allowed to write. The orchestrator and
   monitor both check it.
2. **The journal is the truth.** Every action is an event with a hash chain. If it is not in the
   journal, it did not happen.
3. **Every judgment is stateless.** No conversation is reused between DeepSeek calls. The inputs
   are rebuilt from `.relay/` and the event is stored under `journal/calls/`.
4. **The LLM cannot approve or decide.** It can route, summarize, escalate, and read pixels.
   Code decides whether a popup is safe; a human answers product questions.
5. **Every turn is restartable.** `STATE.json.pending` records the phase, round, pointer and
   start time before anything is typed. A crash resumes without skipping or double-counting a
   round (a sentinel that already exists is consumed, not re-requested).
6. **Every write is atomic.** Temp file + `fsync` + `rename`, under a cross-process `flock`.

## Turn protocol

```
snapshot hashes (before)  ->  delete old sentinel  ->  state.pending  ->  baton = role
  ->  focus + verify window  ->  select/verify model (Claude)  ->  type pointer  ->  submit
  ->  poll sentinel (25 s) + screenshot check (60 s) + one nudge
  ->  validate sentinel (round, role, sha256, mtime)
  ->  ownership check + secret scan  ->  git commit  ->  baton = none
  ->  state.history / last_sentinel  ->  route
```

A sentinel is a claim. Relay validates the claim independently:

- `round` must equal the turn being run;
- `role` must match the turn;
- `file` must be the expected relative path;
- `sha256` must equal the file's current hash;
- the file and the sentinel must not predate the turn;
- a reviewer sentinel must carry a verdict (checkpoint sentinels excepted);
- `needs_decision` must carry a question.

Rejected sentinels are journaled. Identical rejected content is not counted repeatedly; the
turn times out or is nudged instead of escalating on every poll.

## Ownership

| Path | Owner |
|---|---|
| source code (everything outside `.relay/`) | coder |
| `PLAN.md`, `HANDOFF.md`, `REVIEW.md`, `CLAUDE_DONE.json`, `CHECKPOINT_planner/reviewer.md` | claude |
| `CODER_REPORT.md`, `CODER_DONE.json`, `CHECKPOINT_coder.md` | coder |
| `STATE.json`, `BATON.json`, `DECISIONS.md`, `PENDING_DECISION.md`, `journal/`, `answers/`, `RULES_*` | harness |
| `MONITOR_NOTES.md`, `monitor/` | monitor |

Any other `.relay/` file is harness-owned, so an agent cannot invent a new coordination file.
Monitor-owned changes are always allowed because the monitor is independent; the monitor's own
Layer 1 checks decision authorship and the journal chain.

## Routing

The verdict gives the deterministic base route:

| Verdict | Base |
|---|---|
| `CHANGES_REQUESTED` | `CODE` |
| `APPROVED` / `MILESTONE_DONE` | `PLAN` (milestone + 1) |
| `PROJECT_DONE` | `DONE` |
| anything else | human |

DeepSeek receives the round, sentinel, review, handoff, last three summaries, monitor notes and
decisions. It returns a route, a three-line summary, drift flags and an optional human question.
Its `NEEDS_HUMAN` always wins. Its `STUCK` can force a re-plan when the base route was `CODE`.
It can never turn a `CHANGES_REQUESTED` into `CODE` approval, nor override `PROJECT_DONE`
without a human.

Deterministic stall detection runs first: three consecutive `CHANGES_REQUESTED` with no source
file changes → a re-plan. More than two re-plans → a human.

## Context rollover

Triggers (highest signal wins):

- on-screen context percentage (vision),
- sentinel `context_estimate_pct`,
- rounds in this session (Claude 4, Zcode 3),
- forced at a milestone when a role's session is above 50%.

Procedure: checkpoint turn (a sentinel announcing `CHECKPOINT_<role>.md`) → verify → new
session → select/verify model → seed pointer → reset counters. Checkpoint failures go to a
human.

## Monitor

Layer 1 is pure code. It deliberately does not trust `STATE.json` for facts it can recompute
from the journal and hashes. Findings are `halt`, `correct` or `note`.

- `halt` writes `.relay/HALT` and a notification; the orchestrator pauses at its next check.
- `correct` writes `.relay/monitor/CORRECTION.json`; the orchestrator applies it at a turn
  boundary (`replan`, `rollover`, `halt`).
- `note` appends a line to `MONITOR_NOTES.md`; every pointer tells agents to read that file.

Layer 2 sends a compact bundle to DeepSeek and applies the rubric. Three consecutive Layer 2
failures halt the loop.

## Journal schema

One JSON object per line. Fields: `id`, `ts`, `round`, `state`, `actor`, `type`, `detail`,
`inputs`, `outputs`, `screenshot`, `deepseek`, `duration_ms`, `result`, `error`, `prev_hash`,
`hash`.

`hash = sha256(canonical_json(event without hash))`; `prev_hash` is the previous event's `hash`,
including across day-file boundaries. `relay trace --verify` walks the chain.

Event types include: `turn.start/end/resume`, `gui.focus/ring/type/click/screenshot/model`,
`sentinel.seen/rejected`, `popup.seen/approved/escalated`, `integrity.violation`,
`deepseek.call`, `route.decided`, `rollover`, `decision.asked/answered`, `monitor.*`,
`guard.tripped`, `git.commit`, `error`, `run.start/resume/stopped`.

## Why the orchestrator is not an agent chat

A long chat accumulates unnoticed instructions and stale context. Relay instead:

- reconstructs every judgment from files,
- keeps the loop in a process with no model conversation,
- bounds every recovery and every DeepSeek call,
- pauses rather than guessing when a guard trips.

That is the main defence against attention drift over days.
