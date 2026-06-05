# The `/loop` Function & Designing Self-Running Skills and Agents

> A detailed guide to Claude Code's recurring **`/loop`** command, the
> **`ralph-loop`** pattern, and how to design **self-running** skills and
> agents that keep working on an interval — safely and with a clear stop
> condition.
>
> Part of the Claude Code guide set. Last updated: 2026-06-04.

---

## Table of Contents

1. [What `/loop` Is](#1-what-loop-is)
2. [Syntax & Behavior](#2-syntax--behavior)
3. [The `ralph-loop` Pattern](#3-the-ralph-loop-pattern)
4. [The Self-Running Toolbox](#4-the-self-running-toolbox)
5. [Designing a Self-Running Skill](#5-designing-a-self-running-skill)
6. [Designing a Self-Running Agent](#6-designing-a-self-running-agent)
7. [Stop Conditions & Safety](#7-stop-conditions--safety)
8. [Worked Examples](#8-worked-examples)
9. [Quick Reference](#9-quick-reference)

---

## 1. What `/loop` Is

`/loop` runs a **prompt or slash command on a recurring interval**. Instead
of you re-typing the same instruction, Claude Code re-invokes it on a
cadence until you stop it. It turns a one-shot action into a **standing
task**.

Typical uses:

- **Polling for status** — "check the deploy every 5 minutes."
- **Recurring quality passes** — re-run `/code-review` periodically.
- **Babysitting** — keep running a PR-watch or autofix command.
- **Long-horizon grind** — repeatedly attempt a task until it's done.

> From the command palette tooltip: *"Run a prompt or slash command on a
> recurring interval (e.g. `/loop 5m /foo`). Omit the interval to let the
> model self-pace."*

---

## 2. Syntax & Behavior

```text
/loop [interval] [prompt-or-/command]
```

| Form | Meaning |
|------|---------|
| `/loop 5m /code-review` | Run `/code-review` every 5 minutes |
| `/loop 30m check the deploy and report` | Run a natural-language prompt every 30 min |
| `/loop /babysit-prs` | Run a command on the default cadence (~10m) |
| `/loop <prompt>` (no interval) | **Self-paced** — the model decides when to run again |

**Interval units:** use `s`, `m`, `h` (e.g. `90s`, `5m`, `1h`).

**Two cadence modes:**

- **Fixed interval** — give an explicit time (`5m`); it fires like a
  timer regardless of progress.
- **Self-paced** — omit the interval and the model chooses when to run
  next based on the work (e.g. wait longer when nothing changed).

**Stopping a loop:** tell Claude to stop, or cancel it from the session
UI. A loop should always have a **terminal condition** (see §7) so it
doesn't run forever.

> ⚠️ **Use a loop, not `sleep`-polling.** Never block the session with
> Bash `sleep` to wait for the next run — that's exactly what `/loop`
> (and background jobs / Monitor) exist to avoid.

---

## 3. The `ralph-loop` Pattern

The `ralph-loop` plugin (commands: `ralph-loop:ralph-loop`,
`ralph-loop:cancel-ralph`, `ralph-loop:help`) packages a well-known
agentic technique: **re-run the same task prompt over and over against a
fresh-but-persistent working state until the task is complete.**

The idea ("Ralph") is brutally simple and surprisingly effective:

```
while not done:
    run the same prompt
    let the agent make a little more progress
    commit/persist the progress
    re-evaluate: are we done?
```

| Command | Purpose |
|---------|---------|
| `ralph-loop:ralph-loop` | Start the loop on a task prompt |
| `ralph-loop:cancel-ralph` | Stop the running loop |
| `ralph-loop:help` | Show usage |

### Why it works

- Each iteration starts with **clear context** but **durable state** (the
  repo, a checklist, committed progress), so errors don't compound.
- The agent **chips away** at a big task one pass at a time.
- It pairs naturally with the **durable-state principle**: progress lives
  in files/commits, not in the conversation.

### When to reach for it

- Large mechanical migrations ("convert every file to the new API").
- "Make CI green" grinds where each pass fixes the next failure.
- Tasks too big for a single context window but decomposable into
  repeatable increments.

### When *not* to

- Tasks needing human judgment each step (use a single agent + check-ins).
- Anything without a checkable **done** signal (it'll run forever).
- Destructive or outward-facing actions that shouldn't repeat unattended.

---

## 4. The Self-Running Toolbox

`/loop` is one of several mechanisms for autonomy. Pick by *what triggers
the next run*:

| Mechanism | Trigger | Best for |
|-----------|---------|----------|
| **`/loop`** | A timer (or self-pace) | Recurring prompts/commands; polling; grind-to-done |
| **`ralph-loop`** | Loop until task complete | Big decomposable tasks driven to a terminal state |
| **Bash `run_in_background`** | A command exits | "Tell me when the build/test finishes" (one notification) |
| **Monitor** | Each stdout line from a script | Stream events: log errors, file changes, CI step results |
| **Hooks** (in `settings.json`/plugins) | A lifecycle event | Run automatically on `SessionStart`, `Stop`, `UserPromptSubmit`, `PreCompact` |
| **PR activity subscription** | A GitHub webhook | React to CI failures & review comments on a PR |
| **`send_later` / scheduled check-in** | A future timestamp | One-off "re-check in an hour" (when available) |
| **Agent `run_in_background`** | Agent completes | Long autonomous investigation off the main thread |

> **Event-driven beats time-driven.** Prefer a mechanism that fires on
> the *actual event* (a hook, a webhook, a process exit) over a blind
> timer. Use `/loop` when no such event exists, or when you genuinely
> want a periodic cadence.

### Hooks: the most "self-running" layer

Your repo already demonstrates this. `plugins/context-window-monitor`
wires scripts to lifecycle events via `hooks.json`:

```json
{
  "hooks": {
    "Stop":              [{ "hooks": [{ "type": "command",
        "command": "${CLAUDE_PLUGIN_ROOT}/scripts/check-context.sh" }] }],
    "UserPromptSubmit":  [{ "hooks": [{ "type": "command",
        "command": "${CLAUDE_PLUGIN_ROOT}/scripts/check-context-manual.sh" }] }],
    "PreCompact":        [{ "hooks": [{ "type": "command",
        "command": "${CLAUDE_PLUGIN_ROOT}/scripts/on-compact.sh" }] }]
  }
}
```

These run **without anyone asking** — the harness executes them on the
event. That is the cleanest form of "self-running": no loop, no polling,
just a reaction to a real trigger. Use the **`update-config`** skill to
add hooks to `settings.json`, or ship them in a plugin like the one above.

---

## 5. Designing a Self-Running Skill

A self-running skill = a normal skill whose body is written to run **one
increment** and be safely **re-invoked** by `/loop` (or a hook).

### Design principles

1. **One increment per run.** Each invocation should make a *bounded*
   amount of progress and return — not try to finish everything.
2. **Idempotent & re-runnable.** Re-running must be safe; never corrupt
   state on a repeat pass.
3. **Externalize state.** Read/write progress to files, a checklist, or
   commits — never rely on chat memory surviving between runs.
4. **Self-check for done.** Begin each run by checking the terminal
   condition; if met, announce completion and stop.
5. **Report deltas, not noise.** Surface what changed this pass; stay
   quiet when nothing did.

### Skeleton `SKILL.md`

```markdown
---
name: nightly-fixer
description: >
  Run one increment of the lint-fix backlog. Use with /loop to grind the
  backlog to zero. Re-runnable and idempotent.
---

# Nightly Fixer (one increment)

1. Read `BACKLOG.md`. If empty → print "DONE" and stop the loop.
2. Pick the top item.
3. Apply the fix with Edit; run the relevant test (Bash).
4. If green: check the item off in `BACKLOG.md` and `git commit`.
   If red: note the failure under the item and move on.
5. Report only this item's result. End the run.
```

Drive it with:
```text
/loop 10m /nightly-fixer        # fixed cadence
/loop /nightly-fixer            # self-paced
```

### Wiring a skill to a hook instead of a loop

If the trigger is an **event** (e.g. every time you submit a prompt, or
on session start), register a hook (see §4) pointing at a script that
does the increment — no `/loop` needed.

---

## 6. Designing a Self-Running Agent

Agents can be made self-running two ways: **looped** or **backgrounded**.

### 6.1 Looped agent (orchestrator-driven)

The main session is the loop controller; each iteration dispatches an
agent for one increment, then evaluates the result.

```
loop (via /loop or ralph-loop):
  Agent(general-purpose): "Do the next increment of <task>.
     Read CHECKLIST.md for state. Make bounded progress.
     Commit. Report what you did and whether the task is complete."
  if agent reports complete → stop the loop
```

Keep the **state in the repo** (`CHECKLIST.md`, commits) so each fresh
agent picks up where the last left off — this is the Ralph pattern with
a sub-agent doing the work.

### 6.2 Backgrounded agent (fire-and-monitor)

For a long autonomous job, launch a single agent with
`run_in_background: true` and keep doing other work; you're notified on
completion.

```
Agent(general-purpose, run_in_background:true,
      isolation:"worktree"):
  "Audit the whole repo for N+1 queries, fix the clear ones,
   open a draft PR, and report."
```

`isolation:"worktree"` lets it work on an isolated copy so a long,
risky pass never disturbs your working tree.

### 6.3 Choosing between them

| Want… | Use |
|-------|-----|
| Repeated bounded passes with re-evaluation between each | **Looped agent** (`/loop` / ralph-loop) |
| One long autonomous run, notify on finish | **Backgrounded agent** |
| Many independent passes at once | **Parallel agents** in one turn |

### 6.4 Prompt template for a self-running agent

Because each agent starts **without your context**, every iteration's
prompt must be self-contained:

```
GOAL: <the overall objective>
STATE: read <CHECKLIST.md / file> for what's done.
THIS RUN: do exactly one increment toward the goal.
CONSTRAINTS: <don't touch X; keep changes small; tests must pass>.
PERSIST: commit progress; update the checklist.
DONE CHECK: if the goal is fully met, say "TASK COMPLETE" so the loop ends.
REPORT: summarize only what changed this run.
```

---

## 7. Stop Conditions & Safety

A self-running construct **without a terminal state is a runaway.** Always
design the exit first.

### Mandatory: a checkable "done" signal

- A sentinel the loop watches for (`TASK COMPLETE`, empty backlog, CI
  green, PR merged/closed).
- The loop/agent checks it **at the start of each pass** and stops when
  met.

### Guardrails

- **Max iterations / time budget.** Cap passes so a stuck task halts and
  reports instead of grinding forever.
- **No-progress detection.** If N passes change nothing, stop and surface
  the blocker rather than looping uselessly.
- **Idempotency.** Re-running must never duplicate or corrupt work.
- **Don't auto-repeat irreversible/outward-facing actions.** Pushing,
  posting, emailing, deleting — gate these behind a confirmed,
  one-time step, not a blind loop.
- **Honest reporting.** On each pass, report real status; if it failed,
  say so with output. On success, state the terminal status plainly —
  that's the deliverable that closes the loop.
- **Easy cancel.** Provide/know the stop path (`ralph-loop:cancel-ralph`,
  "stop the loop", UI cancel).

### The runaway-prevention checklist

```
[ ] There is a concrete, checkable DONE signal
[ ] DONE is checked at the START of every pass
[ ] Max-iterations / time budget is set
[ ] No-progress passes trigger a stop-and-report
[ ] Each pass is idempotent and re-runnable
[ ] State persists in files/commits, not chat
[ ] Irreversible actions are gated, not looped
[ ] A clear cancel path exists
```

---

## 8. Worked Examples

### 8.1 Grind CI to green
```text
/loop 5m re-check PR #1 CI; if failing, diagnose, push a fix;
        if green, say "CI GREEN" and stop.
```
Terminal state: "CI GREEN". Each pass re-diagnoses rather than repeating
the same fix.

### 8.2 Burn down a refactor backlog (Ralph)
```text
ralph-loop:ralph-loop  "Convert one more file from REST to gRPC.
   State in MIGRATION.md. Commit. Say 'MIGRATION COMPLETE' when none left."
# stop early with: ralph-loop:cancel-ralph
```

### 8.3 Periodic quality pass
```text
/loop 1h /code-review        # hourly review of the working diff
```

### 8.4 Event-driven (no loop) — react on prompt submit
Add a `UserPromptSubmit` hook (via `update-config` or a plugin) that runs
a script each time you submit a prompt — like the repo's
`check-context-manual.sh`. Self-running, zero polling.

### 8.5 Background autonomous audit
```text
Agent(general-purpose, run_in_background:true):
   "Find & fix unhandled promise rejections; open a draft PR; report."
# keep working; get notified when it finishes.
```

---

## 9. Quick Reference

```text
/loop SYNTAX
  /loop 5m /code-review        every 5 minutes
  /loop 30m <prompt>           every 30 minutes
  /loop /command               default cadence (~10m)
  /loop <prompt>               self-paced (model decides)
  units: s | m | h             e.g. 90s, 5m, 1h
  stop: say "stop the loop" / UI cancel

RALPH-LOOP (plugin)
  ralph-loop:ralph-loop        start loop-until-done on a task
  ralph-loop:cancel-ralph      stop it
  ralph-loop:help              usage

SELF-RUNNING MECHANISMS (pick by trigger)
  /loop            timer / self-pace      recurring prompt or command
  ralph-loop       loop-until-done        big decomposable task
  Bash bg          command exits          "tell me when build finishes"
  Monitor          each stdout line       stream log/CI/file events
  Hooks            lifecycle event        SessionStart/Stop/PreCompact/...
  PR subscription  GitHub webhook         CI failures & review comments
  Agent bg         agent completes        long autonomous job
  send_later       future timestamp       one-off re-check (if available)

DESIGN RULES
  one bounded increment per run
  idempotent & re-runnable
  state in files/commits, not chat
  check DONE at the start of every pass
  cap iterations + detect no-progress
  gate irreversible actions; never blind-loop them
  report deltas; state terminal status plainly
```

---

*Generated by Claude Code (Opus 4.8). Grounded in this repo's plugin
layout (`plugins/context-window-monitor`) and the `/loop` + `ralph-loop`
commands. Pair with the agents, orchestration, and techniques guides.*
