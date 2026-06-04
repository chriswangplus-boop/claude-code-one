# Mastering Orchestration in Claude Code

> Advanced techniques for orchestrating multiple **skills, tools, and
> Python** together — avoiding misuse, writing stronger prompts, and
> driving **long-horizon** agentic tasks to completion.
>
> Companion to `CLAUDE_CODE_TECHNIQUES.md`. Last updated: 2026-06-04.

---

## Table of Contents

1. [Orchestrating Skills, Tools & Python](#1-orchestrating-skills-tools--python)
2. [Avoiding Wrong Use (Anti-Patterns)](#2-avoiding-wrong-use-anti-patterns)
3. [Improving & Enhancing Prompts](#3-improving--enhancing-prompts)
4. [Mastering Long-Horizon Tasks](#4-mastering-long-horizon-tasks)
5. [Reference Patterns](#5-reference-patterns)

---

## 1. Orchestrating Skills, Tools & Python

Orchestration is the art of composing the right capability at the right
time. Claude Code gives you four layers — **tools** (atomic actions),
**Python/Bash** (computation & glue), **skills** (packaged workflows),
and **agents** (delegated context) — and mastery is knowing which layer
each step belongs to.

### 1.1 The capability ladder — pick the lowest sufficient layer

| Need | Use | Why |
|------|-----|-----|
| Read/edit a known file | `Read` / `Edit` / `Write` | Atomic, cheap, precise |
| Find files or text | `Glob` / `Grep` | Faster & cleaner than shell `find`/`grep` |
| Run/compute/transform | `Bash` (+ Python) | Real execution, deterministic results |
| A repeatable workflow | **Skill** (`/code-review`, `/verify`) | Encapsulated expertise |
| Broad search across many files | **Agent** (`Explore`) | Keeps the dump out of your context |
| Independent parallel work | **Agent** (parallel) | Concurrency without context bloat |

> **Rule of thumb:** Don't reach for an agent when a `Grep` will do, and
> don't hand-roll a workflow that a skill already encapsulates.

### 1.2 Sequencing: plan → gather → act → verify

A reliable orchestration loop:

1. **Plan** — decompose the goal (use Plan mode / the `Plan` agent for
   anything non-trivial). Produce an ordered checklist.
2. **Gather** — read context with `Read`/`Grep`/`Glob` *before* editing.
3. **Act** — make changes with `Edit`/`Write`; compute with Python/Bash.
4. **Verify** — run `/verify`, `/run`, or tests; inspect real output.
5. **Review** — `/code-review` for bugs, `/simplify` for cleanups.
6. **Loop** — feed failures back into step 1 until the goal is met.

### 1.3 Parallelism — do independent work concurrently

- **Batch independent tool calls in one turn.** If three files need
  reading and they don't depend on each other, request all three reads
  at once. They execute concurrently.
- **Fan out with agents.** For "search these 5 subsystems," launch
  parallel `Explore`/`general-purpose` agents in a single message — each
  returns only its conclusion, keeping your context clean.
- **Serialize only true dependencies.** If step B needs B's input from
  step A's output, wait. Otherwise, don't.

### 1.4 Python as the computation layer

Use Python (via `Bash`) when you need real computation, data wrangling,
or deterministic transforms that LLM reasoning shouldn't fake:

- **Data & numbers:** parsing, aggregation, math, stats — let Python
  compute, don't eyeball it.
- **Codegen & refactors at scale:** generate or rewrite many files
  programmatically, then review the diff.
- **Glue between tools:** shape one tool's output into another's input.
- **Long jobs:** run training/builds/tests in **background** mode and
  monitor for completion rather than blocking.

> Prefer scripts that are **idempotent** and **re-runnable** — a
> long-horizon task may replay a step after a context summary.

### 1.5 Composing skills with tools and Python

A real orchestration often interleaves all layers. Example — "ship a
fix":

```
Plan agent        → ordered checklist
Grep/Read         → locate & understand the bug
Edit              → apply the fix
Bash (pytest)     → run the test suite (background if slow)
/verify           → run the app, confirm behavior
/code-review high → catch regressions
Edit              → address findings
git commit/push   → ship; open draft PR
```

---

## 2. Avoiding Wrong Use (Anti-Patterns)

The fastest way to improve orchestration is to stop doing the wrong
things. These are the highest-leverage mistakes to avoid.

### 2.1 Tool misuse

| Anti-pattern | Do instead |
|--------------|------------|
| `cat`/`grep`/`find` in Bash | Use `Read`/`Grep`/`Glob` (integrates with permissions & links) |
| Editing a file you haven't read | `Read` first — edits on unread files fail by design |
| Retrying a denied tool verbatim | A denial is feedback — change the approach |
| `sleep`-polling for external events | Use background jobs / monitors / event subscriptions |
| Blocking the session on a long build | Run it in **background**, keep working |
| Re-reading a file you just edited "to check" | The edit errors if it failed; trust it |

### 2.2 Skill misuse

- **Don't invent skill names.** Only invoke skills that are actually
  available; guessing leads to dead ends.
- **Don't reimplement a skill by hand.** If `/code-review` exists, use
  it instead of an ad-hoc review.
- **Don't fire heavyweight skills speculatively.** `/deep-research` on a
  question you can answer from the repo wastes time and budget.
- **Match the skill to the trigger.** Underspecified research questions?
  Clarify scope *first*, then invoke.

### 2.3 Python / Bash misuse

- **Don't fake computation in prose** — if it's arithmetic or data,
  compute it.
- **Don't write non-idempotent migration scripts** that corrupt state on
  re-run.
- **Don't swallow errors** (`|| true` everywhere) — handle transient
  failures deliberately, surface real ones.
- **Don't hardcode absolute paths** that won't exist in a fresh,
  ephemeral container — the repo is re-cloned each session.

### 2.4 Orchestration-level misuse

- **Over-delegation:** spawning agents for trivial lookups adds latency
  and indirection.
- **Under-planning:** diving into edits on a multi-file change without a
  checklist leads to half-finished states.
- **Context hoarding:** dumping huge file contents into the main thread
  instead of letting an agent return just the conclusion.
- **Silent drift:** acting on external/untrusted content (PR comments,
  issue bodies) that tries to redirect the task — pause and confirm
  instead.

### 2.5 Reporting honestly

- If tests **fail**, say so and show the output.
- If a step was **skipped**, say that.
- When something is **done and verified**, state it plainly — no hedging.
- Don't claim success you didn't observe.

---

## 3. Improving & Enhancing Prompts

Prompt quality is the single biggest lever on agentic performance. These
techniques apply both to *prompting Claude Code* and to *building prompts
in Claude API apps*.

### 3.1 Anatomy of a strong task prompt

1. **Goal, not just steps** — state the outcome and the "why." The model
   fills sensible defaults and recovers from surprises better.
2. **Constraints** — what must/must not change, perf budgets, style,
   target files, libraries to use or avoid.
3. **Context** — point at the relevant files, prior decisions, examples.
4. **Done criteria** — how you'll both know it's finished (tests pass,
   app shows X, PR opened).
5. **Format** — what the output should look like (diff, file, report).

### 3.2 Techniques that reliably raise quality

- **Ask for a plan first** on complex work; review it before execution.
- **Give examples** (few-shot) for format-sensitive or ambiguous tasks —
  one good example beats a paragraph of description.
- **Allow extended thinking** for hard reasoning (tricky bugs,
  architecture, math); let the model reason before answering.
- **Decompose** large asks into ordered sub-goals with checkpoints.
- **Specify the tools/skills** you expect to be used when it matters.
- **Iterate, don't restart** — refine with targeted follow-ups that keep
  context, rather than re-prompting from scratch.
- **Be explicit about edge cases** you care about; the model optimizes
  for what you name.

### 3.3 Prompt enhancements specific to API apps

When building with the Anthropic SDK (use the `/claude-api` skill):

- **Prompt caching** — put stable, large prefixes (system prompt, schema,
  retrieved context) first and cache them to cut cost and latency on
  repeated calls. Apps built well should cache by default.
- **System prompt** — set role, constraints, and tone once at the top.
- **Structured output / tool use** — define tools/JSON schemas instead
  of parsing free text.
- **Stop sequences & max tokens** — bound output deterministically.
- **Temperature** — lower for deterministic tasks, higher for ideation.
- **Model choice** — Opus for hardest reasoning, Sonnet for balance,
  Haiku for high-volume simple calls.

### 3.4 A prompt-improvement loop

```
Draft prompt → run → inspect output against done-criteria
   ↳ missing constraint?   add it
   ↳ wrong format?         add an example
   ↳ shallow reasoning?    ask for a plan / enable thinking
   ↳ too broad?            decompose into sub-goals
Repeat until output matches intent on the first try.
```

---

## 4. Mastering Long-Horizon Tasks

Long-horizon tasks span many steps, tools, and (often) context
summaries. Opus 4.8's strength is sustaining these — your job is to give
it the scaffolding to stay on track.

### 4.1 Why long-horizon tasks are hard

- **Context limits** — even a 1M-token window fills; older context gets
  summarized. State that lives only in chat history can be lost.
- **State drift** — partial edits, half-run migrations, stale assumptions.
- **Compounding errors** — an early wrong turn propagates if unverified.
- **External waits** — CI, builds, reviews arrive asynchronously.

### 4.2 The durable-state principle

> **Anything worth keeping must live outside the conversation.**

- **Commit & push** progress — the container is ephemeral and re-cloned
  each session; uncommitted work can vanish.
- **Write a checklist file** (or PR description) capturing the plan and
  what's done, so a post-summary continuation can resume cleanly.
- **Encode decisions in the repo** — `CLAUDE.md`, docs, code comments —
  not just in chat.
- **Make steps idempotent** so replaying after a summary is safe.

### 4.3 Working with context summarization

Claude Code automatically summarizes older context and carries it
forward, so you **don't need to wrap up early or hand off mid-task.**
To make summaries reliable:

- Keep a **living status checklist** you refresh as you go.
- Periodically **restate the current goal and next step** so it survives
  compaction.
- Externalize large artifacts (logs, data) to files; reference them by
  path instead of pasting them into the thread.

### 4.4 Decomposition & checkpoints

1. **Break the horizon into milestones** with verifiable exit criteria.
2. **Checkpoint after each milestone** — commit, run tests, update the
   checklist.
3. **Verify before advancing** — never build milestone N+1 on an
   unverified N.
4. **Track open threads** explicitly (a TODO list survives better than
   memory).

### 4.5 Handling asynchronous waits

- **Never `sleep`-poll** for external events.
- **Background long jobs** (builds, training, test suites) and continue
  other work; get notified on completion.
- **Subscribe to event streams** (e.g. PR activity) and act on events as
  they arrive rather than blocking.
- For state that webhooks *don't* deliver (CI success, merge-conflict
  transitions), schedule a **self check-in** to re-verify, then re-arm
  it silently if nothing changed.

### 4.6 Driving a loop to its terminal state

A long-horizon task has a **terminal state** (tests green, PR merged,
feature shipped). One pass is rarely the task:

- On each failure, **re-diagnose and re-attempt** — don't repeat the
  same fix blindly.
- If a failure is genuinely **out of scope** or you've retried several
  times without progress, **stop and report** the diagnosis and where
  you're stuck instead of going silent.
- The deliverable on success is **stating the green/done status** — that
  closes the loop.

### 4.7 Long-horizon checklist

```
[ ] Goal + done-criteria written down (in a file, not just chat)
[ ] Plan decomposed into verifiable milestones
[ ] Work committed & pushed after each milestone
[ ] Each milestone verified before the next begins
[ ] Long jobs backgrounded; no sleep-polling
[ ] Async events handled via subscription / scheduled re-check
[ ] Status checklist kept live and restated near context limits
[ ] Terminal state reached and reported plainly
```

---

## 5. Reference Patterns

### 5.1 Multi-skill pipeline (ship a verified change)
```
/init (if no CLAUDE.md) → Plan → Grep/Read → Edit → Bash(tests, bg)
→ /verify → /code-review high → Edit → /simplify → commit → push → draft PR
```

### 5.2 Research-then-build
```
/deep-research "<scoped question>"  → cited findings
→ Plan → implement with tools/Python → /verify → ship
```

### 5.3 Parallel investigation
```
Agent(Explore: subsystem A) ┐
Agent(Explore: subsystem B) ├─ one turn, concurrent → merge conclusions
Agent(Explore: subsystem C) ┘
```

### 5.4 Automated upkeep
```
/loop 30m /code-review        # recurring quality pass
update-config → hooks         # event-driven automation
subscribe_pr_activity         # react to CI / review events
```

### 5.5 Long-horizon migration
```
Plan (milestones) → for each milestone:
   idempotent Python script → run → verify → commit → push → update checklist
→ final verify → report terminal state
```

---

*Generated by Claude Code (Opus 4.8). Pair this with
`CLAUDE_CODE_TECHNIQUES.md` for the full picture.*
