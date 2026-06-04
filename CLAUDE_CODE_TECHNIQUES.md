# Claude Code Techniques Guide — Opus 4.8, Skills, Plugins & Tools

> A practical reference for getting the most out of Claude Code with the
> Opus 4.8 model, the Skills system, Plugins, and the built-in tool set.
>
> Last updated: 2026-06-04

---

## Table of Contents

1. [Using Opus 4.8](#1-using-opus-48)
2. [Skills](#2-skills)
3. [Plugins](#3-plugins)
4. [Tools](#4-tools)
5. [Putting It Together — Workflows](#5-putting-it-together--workflows)
6. [Quick Reference Cheatsheet](#6-quick-reference-cheatsheet)

---

## 1. Using Opus 4.8

Opus 4.8 (`claude-opus-4-8`) is the most capable model in the Claude 4.X
family and the default high-end model for Claude Code. It is built for
deep reasoning, long-horizon agentic work, and large-context tasks.

### 1.1 Model IDs

| Model        | ID                          | Best for |
|--------------|-----------------------------|----------|
| Opus 4.8     | `claude-opus-4-8`           | Hardest reasoning, agentic coding, planning |
| Sonnet 4.6   | `claude-sonnet-4-6`         | Balanced speed/quality, everyday coding |
| Haiku 4.5    | `claude-haiku-4-5-20251001` | Fast, cheap, high-volume simple tasks |

When building AI applications, default to the latest and most capable
Claude models unless cost or latency dictates otherwise.

### 1.2 Selecting and switching the model

- **In the CLI:** run `/model` to switch the active model interactively.
- **In settings:** set the `model` field in `settings.json`
  (see [`/config`](#33-managing-configuration)).
- **Per API call:** pass the model ID in the request body.

### 1.3 Fast mode

Claude Code offers a **Fast mode** for Opus that produces output faster
*without* downgrading to a smaller model — it stays on Opus.

- Toggle it with `/fast`.
- Available on Opus 4.8, 4.7, and 4.6.
- Use it when you want Opus-level quality with snappier responses.

### 1.4 Large context window

Opus 4.8 supports a **1M-token context window** (the `[1m]` variant).
This means you can:

- Load entire large codebases or many files at once.
- Keep long multi-step agentic sessions without losing earlier context.

**Context management:** When a conversation grows long, Claude Code
automatically summarizes older context and carries the summary forward,
so you don't need to wrap up work early or hand off mid-task.

### 1.5 Getting the best results from Opus 4.8

- **Be explicit about the goal**, not just the steps. Opus reasons well
  about intent and will fill in sensible defaults.
- **Let it plan.** For complex tasks, ask it to plan first (or use Plan
  mode / the Plan agent) before editing.
- **Give it room to use tools.** Opus is strong at multi-tool,
  multi-step agentic workflows — point it at the problem and let it
  search, read, edit, and verify.
- **Prefer extended thinking for hard problems.** When a task needs
  careful reasoning (tricky bugs, architecture, math), Opus benefits
  from thinking before answering.
- **Use prompt caching** in API apps to cut cost/latency on repeated
  large prefixes (system prompts, file context).

---

## 2. Skills

**Skills** are specialized, self-contained capabilities that package
domain knowledge and instructions Claude can invoke on demand. When a
user types `/<skill-name>`, Claude runs that skill.

### 2.1 What a Skill is

A skill is a folder with a `SKILL.md` file containing:

- **Frontmatter** — `name`, `description`, and trigger guidance.
- **Body** — the instructions, workflow, and domain knowledge Claude
  follows when the skill runs.

The `description` tells Claude *when* to use the skill. Good descriptions
include concrete trigger phrases ("Use when the user wants to…").

### 2.2 Invoking a Skill

- **Explicitly:** type `/skill-name` (with optional arguments after it).
- **Automatically:** Claude matches your request against available skill
  descriptions and invokes the best match before responding.

> Tip: Skills are surfaced to Claude in the session. Only listed skills
> can be invoked — Claude won't guess at names that aren't available.

### 2.3 Examples of built-in / common skills

| Skill | Purpose |
|-------|---------|
| `deep-research` | Multi-source, fact-checked research report with citations |
| `code-review` | Review the current diff for bugs and cleanups (effort levels low→max) |
| `simplify` | Apply reuse/simplification/efficiency cleanups to changed code |
| `verify` | Run the app and observe behavior to confirm a change works |
| `run` | Launch and drive the project's app to see a change live |
| `security-review` | Security review of pending changes on the branch |
| `init` | Generate a `CLAUDE.md` documenting the codebase |
| `claude-api` | Build, debug, optimize Claude API / Anthropic SDK apps |
| `update-config` | Configure the harness via `settings.json` (hooks, permissions, env) |
| `session-start-hook` | Set up a `SessionStart` hook for Claude Code on the web |
| `keybindings-help` | Customize keyboard shortcuts in `~/.claude/keybindings.json` |
| `loop` | Run a prompt or slash command on a recurring interval |
| `fewer-permission-prompts` | Build an allowlist to reduce permission prompts |

### 2.4 Authoring your own Skill

1. Create a folder named after the skill.
2. Add a `SKILL.md` with frontmatter:
   ```markdown
   ---
   name: my-skill
   description: >
     One-line summary. Use when the user wants to <trigger>.
   ---

   # My Skill

   Step-by-step instructions Claude should follow…
   ```
3. Place it where Claude Code discovers skills (project `.claude/skills/`
   or your user skills directory).
4. Keep the **description trigger-rich** so auto-invocation works.
5. Keep the body **action-oriented** — concrete steps beat prose.

---

## 3. Plugins

**Plugins** extend Claude Code with bundled capabilities — they can ship
skills, slash commands, hooks, agents, and MCP servers as a single
installable unit.

### 3.1 What plugins bundle

- **Slash commands** — custom `/commands`.
- **Skills** — packaged as described above.
- **Hooks** — automation that runs on lifecycle events (e.g. on session
  start, before/after a tool runs).
- **Agents** — specialized sub-agents.
- **MCP servers** — external tool integrations (see [Tools](#4-tools)).

### 3.2 Plugin-namespaced skills

When a skill comes from a plugin, invoke it with the fully qualified
`plugin:skill` form, e.g. `/myplugin:deploy`.

### 3.3 Managing configuration

Plugin and harness behavior is controlled through `settings.json`:

- Use the **`update-config`** skill (or edit `settings.json`) to add
  hooks, permissions, and environment variables.
- For simple settings (theme, model), use the **`/config`** command.

> Automated "whenever X happens, do Y" behaviors must be implemented as
> **hooks** in `settings.json` — the harness executes hooks, not Claude's
> memory. That's why such requests route through `update-config`.

---

## 4. Tools

**Tools** are the actions Claude can take in your environment — reading
and editing files, running commands, searching, fetching the web, and
calling external services via MCP.

### 4.1 Built-in tools

| Tool | What it does |
|------|--------------|
| **Read** | Read a file (code, images, PDFs, notebooks) |
| **Write** | Create or fully overwrite a file |
| **Edit** | Exact string replacement in a file |
| **Glob** | Fast filename pattern matching (`**/*.ts`) |
| **Grep** | Content search built on ripgrep (regex, globs, types) |
| **Bash** | Run shell commands (foreground or background) |
| **Agent** | Launch a sub-agent for complex multi-step or parallel work |
| **AskUserQuestion** | Ask the user to decide between options |
| **Skill** | Invoke a skill in the conversation |
| **ToolSearch** | Load deferred tool schemas before calling them |
| **SendUserFile** | Surface a generated file to the user for download |

### 4.2 Deferred tools & ToolSearch

Some tools (e.g. `WebFetch`, `WebSearch`, `NotebookEdit`, `Monitor`) and
all MCP tools are **deferred** — their names are known but schemas aren't
loaded. Before calling one:

```
ToolSearch  query="select:WebSearch,WebFetch"   # load by exact name
ToolSearch  query="github pull request"          # keyword search
```

Once a tool's schema appears, call it like any other tool.

### 4.3 MCP servers (external tools)

MCP (Model Context Protocol) servers expose tools named
`mcp__<server>__*`. Examples seen in cloud sessions: `github`, `Gmail`,
`Google_Drive`, `Google_Calendar`, `Box`. Use **ToolSearch** with a
keyword to discover and load them, then call them directly.

> GitHub integration in the cloud uses the `mcp__github__*` tools for all
> PR/issue/CI/review operations (not the `gh` CLI).

### 4.4 Tool-use best practices

- **Prefer dedicated tools over shell.** Use `Read`/`Grep`/`Glob`
  instead of `cat`/`grep`/`find` for a better experience.
- **Parallelize independent calls.** Issue independent tool calls in a
  single turn so they run concurrently.
- **Edit safely.** You must `Read` a file before `Edit`/`Write` over it;
  match indentation exactly, or use `replace_all` for repeated strings.
- **Delegate broad searches.** Use the `Explore` or `general-purpose`
  agent when a search needs to fan out across many files.
- **Background long jobs.** Run long-running commands with Bash's
  background mode rather than blocking the session.
- **Respect permissions.** A denied tool call means the user declined —
  adapt rather than retrying verbatim.

---

## 5. Putting It Together — Workflows

### 5.1 Implement a feature on Opus 4.8

1. `/model` → select Opus 4.8 (optionally `/fast`).
2. Describe the goal; let Opus plan.
3. Opus uses `Grep`/`Glob`/`Read` to understand the code.
4. It edits with `Edit`/`Write`.
5. Run `/verify` or `/run` to confirm behavior.
6. `/code-review` for bugs, `/simplify` for cleanups.
7. Commit & push; open a draft PR.

### 5.2 Research with citations

- `/deep-research <question>` → fan-out web searches, adversarial
  verification, and a synthesized cited report.

### 5.3 Automate repetitive checks

- `/loop 5m /code-review` → run a command on a recurring interval.
- Use **hooks** (via `update-config`) for event-driven automation.

### 5.4 Reduce friction

- `/fewer-permission-prompts` → build a read-only allowlist.
- `/config` → quick settings like theme and model.

---

## 6. Quick Reference Cheatsheet

```text
MODEL
  /model            switch active model
  /fast             toggle Opus fast output (no downgrade)
  claude-opus-4-8   most capable; 1M-token context

SKILLS  (type /name or let Claude auto-invoke)
  /deep-research    cited research report
  /code-review      find bugs in the diff
  /simplify         clean up changed code
  /verify  /run     prove a change works
  /security-review  security pass on the branch
  /init             generate CLAUDE.md
  /claude-api       build/debug Anthropic SDK apps
  /update-config    hooks, permissions, env in settings.json
  /loop             recurring task on an interval

PLUGINS
  plugin:skill      invoke a plugin's skill
  settings.json     hooks / permissions / env (via /update-config)
  /config           simple settings (theme, model)

TOOLS
  Read Write Edit   file I/O
  Glob Grep         find files / search content (ripgrep)
  Bash              run commands (fg/bg)
  Agent             delegate multi-step / parallel work
  ToolSearch        load deferred & MCP tool schemas
  mcp__<server>__*  external integrations (github, gmail, drive…)
```

---

*Generated by Claude Code (Opus 4.8). Save or commit this file to keep it
with your project.*
