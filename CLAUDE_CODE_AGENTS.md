# Agents & Sub-Agents in Claude Code

> How to use agents and sub-agents effectively — what each agent type is
> for, how delegation works, and concrete strategies for handling
> different kinds of work.
>
> Part of the Claude Code guide set (see `CLAUDE_CODE_TECHNIQUES.md` and
> `CLAUDE_CODE_ORCHESTRATION.md`). Last updated: 2026-06-04.

---

## Table of Contents

1. [What Agents Are & Why They Matter](#1-what-agents-are--why-they-matter)
2. [The Built-in Agent Types](#2-the-built-in-agent-types)
3. [How Delegation Works](#3-how-delegation-works)
4. [When to Use an Agent (and When Not To)](#4-when-to-use-an-agent-and-when-not-to)
5. [Strategies by Type of Work](#5-strategies-by-type-of-work)
6. [Advanced Orchestration Patterns](#6-advanced-orchestration-patterns)
7. [Pitfalls & Best Practices](#7-pitfalls--best-practices)
8. [Quick Reference](#8-quick-reference)

---

## 1. What Agents Are & Why They Matter

An **agent** (or **sub-agent**) is a separate Claude instance that the
main session launches to handle a focused task. It runs with its own
context window, does the work, and returns **only its final message** to
the parent — not the intermediate file dumps, search output, or tool
chatter.

### Why this is powerful

- **Context isolation** — a broad search can read dozens of files; the
  agent absorbs all of that and hands back a one-paragraph conclusion.
  Your main context stays clean and focused.
- **Parallelism** — multiple agents run concurrently, so independent
  work happens at once instead of serially.
- **Specialization** — each agent type has a tailored toolset and
  instructions for its job.
- **Scale** — long, multi-step investigations don't bloat the main
  thread.

> **Mental model:** the main session is the *orchestrator*; agents are
> *workers* you dispatch. You keep the conclusions, not the raw work.

---

## 2. The Built-in Agent Types

| Agent | Role | Tools | Best for |
|-------|------|-------|----------|
| **Explore** | Read-only, fast fan-out search | All except edit/write/agent | Sweeping many files/dirs to locate code or confirm a fact — returns the conclusion, not file dumps |
| **Plan** | Software architect | All except edit/write/agent | Designing an implementation strategy: step-by-step plans, critical files, trade-offs |
| **general-purpose** | Catch-all researcher & multi-step executor | All tools (`*`) | Complex multi-step tasks, uncertain searches, autonomous execution |
| **claude** | Default catch-all | All tools (`*`) | Anything that doesn't fit a more specific agent |
| **claude-code-guide** | Claude Code / SDK / API expert | Glob, Grep, Read, WebFetch, WebSearch | Questions about Claude Code features, the Agent SDK, or the Claude API |
| **statusline-setup** | Config helper | Read, Edit | Configuring the status line |
| **keybindings / others** | Narrow helpers | Varies | Specific setup tasks |

### Read-only vs. read-write

- **Explore** and **Plan** are *read-only* — they locate, analyze, and
  advise but never modify files. Safe for reconnaissance.
- **general-purpose** / **claude** can *edit and execute* — use them when
  the delegated task includes making changes.

### Search breadth (for Explore)

Specify how wide to search:
- **"medium"** — moderate exploration.
- **"very thorough"** — multiple locations and naming conventions.

---

## 3. How Delegation Works

### Launching an agent

The orchestrator launches an agent with a **task type** and a **prompt**.
Key mechanics:

- The agent's **final message** is returned as the tool result — it is
  **not shown to the user**, so the orchestrator must relay what matters.
- **A fresh `Agent` call starts a new agent** with no prior context.
- **Continuing an agent** (via its ID/name) resumes it with its context
  intact — use this for follow-ups instead of re-explaining.

### Useful options

| Option | Effect |
|--------|--------|
| `run_in_background: true` | Runs the agent asynchronously; you're notified on completion and can keep working |
| `isolation: "worktree"` | Gives the agent its own git worktree (auto-cleaned if unchanged) — ideal for isolated/parallel edits |
| `model` override | Run a given agent on Opus / Sonnet / Haiku to balance cost vs. capability |

### Concurrency

When you launch **multiple agents for independent work, send them in a
single message** with multiple tool calls so they run concurrently.
Serialize only when one agent's output feeds another's input.

---

## 4. When to Use an Agent (and When Not To)

### Use an agent when…

- The answer requires **sweeping many files/dirs** and you only need the
  conclusion (→ **Explore**).
- You're **not confident** the first search will hit (→ delegate the
  search rather than burning main-context turns).
- You have **independent chunks of work** that can run in parallel.
- You need a **plan** for a non-trivial change before touching code
  (→ **Plan**).
- A task is **long and multi-step** and would otherwise flood the main
  thread.

### Don't use an agent when…

- You **already know the file/symbol/value** — just `Read`/`Grep`
  directly. Spawning an agent for a single-fact lookup adds latency.
- The task is **trivial** — the indirection costs more than it saves.
- You'd need to **stream raw output back** — agents return conclusions,
  not live file contents.

> **Golden rule:** Once you delegate a search, **don't also run it
> yourself** — wait for the agent's result. Pick one path.

---

## 5. Strategies by Type of Work

### 5.1 Codebase exploration & "where is X?"
**Strategy:** Single **Explore** agent, breadth tuned to uncertainty.
```
Explore("very thorough"): "Find where auth tokens are validated and
list the files + functions involved."
→ returns a concise map; you act on it directly.
```
Use this instead of multiple manual `Grep`s when naming is inconsistent.

### 5.2 Understanding a large, unfamiliar system
**Strategy:** **Parallel Explore** agents, one per subsystem, in a single
turn.
```
Explore: data layer  ┐
Explore: API layer    ├─ concurrent → merge the three conclusions
Explore: auth layer  ┘
```
Each returns only its summary; you assemble the big picture cheaply.

### 5.3 Planning a non-trivial feature or refactor
**Strategy:** **Plan** agent first, then execute.
```
Plan: "Design the migration from REST to gRPC for the orders service —
steps, critical files, trade-offs, risks."
→ review the plan → implement (yourself or via general-purpose).
```
Never start a multi-file change without a plan you've reviewed.

### 5.4 Complex, autonomous multi-step task
**Strategy:** **general-purpose** agent with a clear goal + done-criteria.
```
general-purpose: "Find all deprecated API calls, replace with the new
SDK, run tests, report what changed." (run_in_background if long)
```
Give it the outcome and constraints; let it search, edit, and verify.

### 5.5 Independent parallel edits
**Strategy:** Multiple agents with **`isolation: "worktree"`**, launched
together.
```
Agent A (worktree): refactor module X
Agent B (worktree): refactor module Y
→ each works on an isolated copy; merge results.
```
Avoids edit collisions when changes don't overlap.

### 5.6 Long-running / background investigation
**Strategy:** `run_in_background: true`; keep doing foreground work.
```
general-purpose (bg): "Audit the whole repo for N+1 queries."
→ you continue other tasks; get notified when it finishes.
```

### 5.7 Questions about Claude Code / SDK / API
**Strategy:** **claude-code-guide** agent (reuse an existing one via
follow-up if available, rather than spawning fresh).

### 5.8 Iterative deep-dive on one thread
**Strategy:** **Continue the same agent** instead of starting over, so it
keeps its accumulated context.
```
Agent #1 maps the auth flow → follow-up to Agent #1:
"now trace how refresh tokens are rotated" (context intact).
```

---

## 6. Advanced Orchestration Patterns

### 6.1 Orchestrator–worker (fan-out / fan-in)
The main session decomposes, dispatches N agents concurrently, then
synthesizes their conclusions. Best for breadth (multi-subsystem search,
multi-file audits).

### 6.2 Pipeline (sequential hand-off)
`Plan agent → general-purpose executor → review`. Each stage's output is
the next stage's input. Use only where there's a true dependency.

### 6.3 Map-reduce over the codebase
Split the repo into chunks → one agent per chunk (map) → orchestrator
merges findings (reduce). Great for "find every instance of X across a
large monorepo."

### 6.4 Background + foreground split
Dispatch a slow audit in the background while you make foreground
progress on an independent change; reconcile when the background agent
reports.

### 6.5 Isolated experimentation
Use `worktree` isolation to let an agent try a risky refactor without
touching your working tree — keep it only if it pans out.

---

## 7. Pitfalls & Best Practices

**Do**
- Write **specific, outcome-oriented prompts** for agents — they don't
  share your full context, so state the goal, constraints, and what to
  return.
- **Relay** the agent's conclusion to the user — its message isn't shown
  to them automatically.
- **Batch independent agents** in one turn for concurrency.
- **Continue** an agent for follow-ups; **start fresh** only for new,
  unrelated work.
- Choose **read-only agents** (Explore/Plan) for reconnaissance and
  planning to avoid accidental edits.
- Match the **model** to the job (Haiku for cheap breadth, Opus for hard
  reasoning).

**Don't**
- Spawn an agent for a **single known lookup** — do it directly.
- **Double-run** a search you've already delegated.
- Expect **raw file output** back — agents summarize.
- **Over-decompose** trivial tasks into many tiny agents (indirection
  overhead).
- Forget that agents **start without your context** — under-specified
  prompts produce vague results.

---

## 8. Quick Reference

```text
AGENT TYPES
  Explore           read-only fan-out search → returns conclusions
  Plan              read-only architect → implementation plans
  general-purpose   full tools → autonomous multi-step execution
  claude            default catch-all (full tools)
  claude-code-guide Claude Code / SDK / API questions (read + web)

LAUNCH OPTIONS
  run_in_background:true   async; notified on completion
  isolation:"worktree"     isolated git worktree (auto-clean)
  model: opus|sonnet|haiku per-agent capability/cost
  (continue an agent)      resume with context intact
  (new Agent call)         fresh, no prior context

DECISION RULE
  Known file/fact?            → Read/Grep directly, no agent
  Broad search, want answer?  → Explore
  Need a strategy?            → Plan
  Multi-step autonomous job?  → general-purpose
  Independent chunks?         → multiple agents, one turn (parallel)
  Delegated a search?         → don't also run it yourself

STRATEGY MAP
  locate code            → Explore (breadth = uncertainty)
  understand big system  → parallel Explore (one per subsystem)
  design a change        → Plan, then execute
  autonomous task        → general-purpose (+ bg if long)
  parallel edits         → worktree-isolated agents
  follow-up deep-dive    → continue the same agent
```

---

*Generated by Claude Code (Opus 4.8). Pair with the techniques and
orchestration guides for the complete workflow picture.*
