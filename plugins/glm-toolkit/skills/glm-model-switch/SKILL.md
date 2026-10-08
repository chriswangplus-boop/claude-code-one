---
name: glm-model-switch
description: Safely switch Claude Code (Mac app or CLI) between Anthropic Claude models and Z.AI GLM (GLM-5.3 / GLM Coding Plan) by prompt — no manual config editing. Trigger whenever the user says "switch to GLM", "switch to Claude", "use GLM 5.3", "back to Claude model", "which model/provider am I on", "GLMに切り替え", "Claudeに戻して", "model switch status", "restore my model config", or mentions changing the model provider, ANTHROPIC_BASE_URL, or Z.AI endpoint in Claude Code. Every switch is reversible by design — timestamped backups, recorded prior state, atomic JSON writes, and a one-command restore. Never touches login credentials, permissions, hooks, or MCP config. NOT for switching between Claude tiers (Opus/Sonnet/Haiku on Anthropic) — use the built-in /model command for that.
metadata:
  version: 2.0.0
  released: 2026-10-08
  author: Chris
---

# GLM Model Switch

Prompt-driven, fully reversible switching of Claude Code's backend between **Claude (Anthropic)** and **GLM-5.3 via Z.AI's Anthropic-compatible endpoint**. One deterministic script does all writes; the model never hand-edits `settings.json`.

## Safety contract (read first, never violate)

1. **All config writes go through `scripts/glm_switch.py`.** Never edit `~/.claude/settings.json` by hand or with ad-hoc bash/sed/jq. The script is the only writer.
2. **Managed-keys-only.** The script touches exactly seven `env` keys (`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_DEFAULT_{HAIKU,SONNET,OPUS}_MODEL`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `API_TIMEOUT_MS`). Permissions, hooks, MCP servers, statusLine, model, login credentials (`~/.claude.json`, keychain) are never modified — this is verified after every write, and the write is auto-rolled-back if the check fails.
3. **Always a way back — in two independent places.** Every switch takes a timestamped full-file backup written to **both** `~/.claude/glm-switch-backups/` (primary) and `~/.config/glm-switch/backups/` (mirror, survives loss of `~/.claude`), plus a one-time `baseline-first-run.json` in each, and records each managed key's exact prior value. `to-claude` restores those exact values; `restore --latest` searches primary then mirror; the emergency recovery in `references/official-config.md` works even if this skill is broken.
4. **Never store or echo the API key.** The Z.AI key lives only in `~/.config/glm-switch/key.env` (chmod 600), written via hidden-input `setup-key`. Do not print it, log it, or place it in the skill, a repo, or chat.
5. **A switch applies to NEW sessions only.** Always tell the user to restart Claude Code (quit and reopen the Mac app, or start a new CLI session). Never claim the current session changed model.

## Locating the script

The script ships inside this skill at `scripts/glm_switch.py`. Resolve its path from this SKILL.md's own directory (e.g. `<skill_dir>/scripts/glm_switch.py`) and run with `python3`. On first use, copy it to a stable local path so it survives skill updates:

```bash
mkdir -p ~/.local/bin && cp <skill_dir>/scripts/glm_switch.py ~/.local/bin/glm_switch.py
```

Prefer the `~/.local/bin` copy if present.

## Commands → user intents

| User says | Run | Then tell the user |
|---|---|---|
| "which model / provider am I on?" | `python3 glm_switch.py status` | Report provider + note it reflects new-session config |
| "switch to GLM / use GLM 5.3 / upgrade my GLM config" | `status` first, then `to-glm` | Backup path, prior-state recorded, **restart required**, way back = "switch to Claude" |
| "switch back to Claude" | `to-claude` | Exact prior values restored, **restart required** |
| "set up my GLM key" | **Do not run this yourself.** Give the user the one-liner to run in their own Terminal: `python3 ~/.local/bin/glm_switch.py setup-key` | Key is typed hidden in their terminal, stored locally chmod 600 — never pasted into chat, never seen by the model |
| "does my GLM config work?" | `check` (needs the saved key) | Per-model OK/FAIL; a FAIL on a model ID means the ID changed — compare with docs.z.ai |
| "something broke / restore my config" | `backups` then `restore --latest` (or a chosen file) | What was restored; restart required |

First-time flow: user runs `setup-key` in their own terminal → `status` → `to-glm` → restart. If `to-glm` reports a missing key, hand the user the `setup-key` one-liner — never accept the key in chat and never pass it on a command line (chat transcript / shell history exposure).

## What the switch actually does

Per the official Z.AI guide (docs.z.ai/devpack/tool/claude), `to-glm` merges this into the `env` block — nothing else changes:

```json
"ANTHROPIC_AUTH_TOKEN": "<key from key.env>",
"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
"ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash[1m]",
"ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
"ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
"API_TIMEOUT_MS": "3000000"
```

`[1m]` enables GLM-5.3's 1M-token context. While on GLM, Claude Code's model aliases (opus/sonnet/haiku) route to GLM models; the Anthropic login stays intact and untouched, so switching back needs no re-authentication.

## Verifying a switch took effect

After the user restarts and asks "did it work" — the official Z.AI verification method:
1. In a Claude Code session, type `/status` and verify **Settings source** shows `~/.claude/settings.json` and **Model** shows `glm-5.3` or `glm-5.3[1m]` (on Claude, it shows a Claude model instead).
2. `python3 glm_switch.py status` — confirms what new sessions will use, from the file itself.
3. Fallback cross-check: ask the session "which model are you?" (GLM self-identifies as GLM).
On GLM, suggest `/effort max` for complex coding — Z.AI maps low/medium/high → GLM high, and xhigh/max/ultracode → GLM max (see references).
3. If the session still hits Anthropic after a restart on GLM: the settings file was likely overridden by a shell-exported `ANTHROPIC_BASE_URL` — check `echo $ANTHROPIC_BASE_URL` in the launching shell.

## Edge cases

- **Mac/desktop app vs CLI.** This skill edits `~/.claude/settings.json`, which the CLI always reads. Anthropic's desktop docs say the app shares that file and also accepts variables in its **Local → gear icon** editor, but its gateway docs say the app reads gateway routing from its own *third-party inference* form, "not from `ANTHROPIC_BASE_URL` or `settings.json`". These conflict and have not been tested end to end here: after a switch, restart the app and check `/status` in an app session — if it still shows a Claude model, the app is ignoring the file (see `references/official-config.md` §6). Cloud sessions (Anthropic-hosted) can never use GLM.

- **Corrupt settings.json** → script refuses to write, saves a `-corrupt` backup (both locations), and instructs `restore --latest`. Relay that; do not "fix" the JSON by rewriting the file yourself — restore from backup instead. Note: Claude Code itself ignores an invalid settings.json (some versions surface a warning) and falls back to defaults, so a broken file degrades config, it does not brick the app.
- **`~/.claude` deleted or lost** (including its primary backups) → `restore --latest` recovers from the mirror at `~/.config/glm-switch/backups/` automatically. Verified in tests.
- **Custom provider already configured** (Bedrock/Vertex/corp proxy in managed keys) → `to-glm` still records prior values and `to-claude` restores them exactly. If no prior state exists and the base URL isn't Z.AI, `to-claude` refuses to guess — use `restore`.
- **User asks to switch "for this session only"** → the settings-file switch is global for new sessions. Offer the isolated alternative in `references/official-config.md` (dedicated `--settings` file / shell function) which never touches global config — CLI only, not available inside the Mac app.
- **GLM endpoint down / model-not-found errors** → usually a stale Claude Code binary; see troubleshooting in `references/official-config.md`. Switching back to Claude is always safe and independent of Z.AI availability.
- **Skill unavailable but user in trouble** → the emergency recovery block in `references/official-config.md` restores Claude defaults with two shell commands.

## References

- `references/official-config.md` — verified official config, session-scoped alternative, troubleshooting, emergency recovery. Read when the user hits errors or wants per-session switching.

## Changelog

| Version | Date | Author | Notes |
|---|---|---|---|
| 2.0.0 | 2026-10-08 | Chris | **Moved to GLM-5.3.** Env block now Sonnet/Opus → `glm-5.3[1m]`, Haiku → `glm-5.3-flash[1m]` (per the Z.AI Claude Code guide as quoted in search results; Z.AI docs were not directly reachable when this was written — run `check` and `/status` to confirm). `to-glm` now **upgrades an existing GLM-5.2 config in place** (keeps the recorded pre-GLM state, so `to-claude` still restores the original values). Added `check` (1-token request per model ID). Retired: `glm-5.2`, `glm-4.7`, `glm-4.5-air` references. |
| 1.2.0 | 2026-07-07 | Chris | Changed: verification section now follows the official Z.AI `/status` procedure (Settings source + Model fields). Added: `/effort` intensity mapping guidance for GLM-5.2. Documented: official-docs conflict on the Haiku slot (`glm-4.7` on the Claude Code page vs `glm-4.5-air` on the switch-models page) — skill keeps `glm-4.7`; alternative noted in references. Source: user-provided official Z.AI docs (2026-07-07 snapshot). No script changes. |
| 1.1.0 | 2026-07-07 | Chris | Added: **mirror backups** — every backup and the baseline are duplicated to `~/.config/glm-switch/backups/`, an independent location outside `~/.claude`; `restore --latest` searches primary then mirror; `backups`/`status` report both. Mirror write failures warn but never block a switch. Added: broken-config behavior documented (Claude Code silently ignores invalid settings.json; script refuses + quarantines). Verified: full recovery after deleting the entire `~/.claude` directory. |
| 1.0.0 | 2026-07-07 | Chris | Initial release. Prompt-driven Claude↔GLM-5.2 switch for Claude Code via managed-keys-only surgical edits of `~/.claude/settings.json`: timestamped + baseline backups, recorded prior-state exact restore, atomic verified writes with auto-rollback, chmod-600 local key storage, idempotent commands, corrupt-file refusal, full-file `restore`. Config verified against the official Z.AI guide. |
