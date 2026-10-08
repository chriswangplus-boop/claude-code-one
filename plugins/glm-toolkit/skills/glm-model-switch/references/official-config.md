# GLM ↔ Claude Code — Official Config & Recovery Reference

Source of truth: **https://docs.z.ai/devpack/tool/claude** (Z.AI official Claude Code guide).
Updated for GLM-5.3: 2026-10-08. **Model IDs below come from the Z.AI Claude Code page as quoted in search results — docs.z.ai itself was unreachable when this was written. Run `glm_switch.py check` and `/status` to confirm before relying on them.**

## 1. Official GLM env block (what `to-glm` writes)

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "your_zai_api_key",
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
    "API_TIMEOUT_MS": "3000000"
  }
}
```

Notes:
- The key goes in `ANTHROPIC_AUTH_TOKEN` (not `ANTHROPIC_API_KEY`) per the official guide.
- **Haiku slot**: `glm-5.3-flash[1m]` (the Coding Plan serves GLM-5.3 and GLM-5.3-Flash). One guide reports Z.AI changed the Claude Code *default* Sonnet slot to Flash on 2026-08-28; this skill deliberately pins Sonnet and Opus to the flagship `glm-5.3[1m]`. Older IDs (`glm-5.2`, `glm-5.1`) are reported to be routed to GLM-5.3 by the plan, so stale configs keep working but should be upgraded with `to-glm`.
- `https://api.z.ai/api/anthropic` is the **Anthropic-compatible** endpoint for Claude Code. The OpenAI-compatible `api/paas/v4/...` endpoint is for other tools (Cursor etc.) — swapping them breaks the connection.
- `glm-5.3[1m]` = GLM-5.3 with the 1M context window; `CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000` stops premature context compaction.
- The official guide also sets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: 1` (optional; this skill omits it to keep the managed surface minimal — add manually if desired).

## 2. Session-scoped alternative (CLI only — zero global config changes)

Safest possible pattern when working in a terminal: plain `claude` always stays Claude; GLM runs only when explicitly invoked. Nothing in `~/.claude/settings.json` is ever modified — "rollback" is simply not typing the function name.

```bash
# ~/.zshrc — GLM-only launcher; reads the same key file this skill manages
ccglm() {
  source ~/.config/glm-switch/key.env
  ANTHROPIC_AUTH_TOKEN="$ZAI_API_KEY" \
  ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic" \
  ANTHROPIC_DEFAULT_HAIKU_MODEL="glm-5.3-flash[1m]" \
  ANTHROPIC_DEFAULT_SONNET_MODEL="glm-5.3[1m]" \
  ANTHROPIC_DEFAULT_OPUS_MODEL="glm-5.3[1m]" \
  CLAUDE_CODE_AUTO_COMPACT_WINDOW="1000000" \
  API_TIMEOUT_MS="3000000" \
  claude "$@"
}
```

Limitations: not usable from inside the Claude Code Mac app (the app launches sessions itself, so it reads `settings.json` — that is why the skill's default path is the managed settings edit). Note that a shell-exported `ANTHROPIC_BASE_URL` overrides the settings file for sessions launched from that shell — keep provider config in one place only.

## 2b. Official verification & effort control (from the switch-models guide)

Verify a switch: run `claude`, type `/status`, and confirm **Settings source** = `~/.claude/settings.json` and **Model** = `glm-5.3` or `glm-5.3[1m]`.

Thinking intensity on GLM-5.3 via Claude Code's `/effort` command:

| Claude Code effort | GLM-5.3 actual effort |
|---|---|
| low, medium, high (default) | high |
| xhigh, max, ultracode | max |

Z.AI recommends **max** effort for coding tasks (deeper reasoning, more stable on complex work). If a `[1m]`-suffixed model is reported as not existing, upgrade Claude Code (`claude update`).

## 3. Troubleshooting (from Z.AI's guide + verified community reports)

| Symptom | Likely cause | Fix |
|---|---|---|
| "model doesn't exist" error | Outdated Claude Code binary doesn't recognize GLM model IDs | `claude --version`, then `claude update`; retry |
| Config seems ignored | Invalid JSON (stray comma) — Claude Code silently ignores the file | `python3 -m json.tool ~/.claude/settings.json`; or `glm_switch.py restore --latest` |
| Still hitting Anthropic on GLM | Shell env override or old session | New terminal/app restart; `echo $ANTHROPIC_BASE_URL` should print the Z.AI URL (or nothing) |
| Connection errors on GLM | Wrong endpoint (OpenAI-compatible URL used) or Z.AI outage | Confirm `https://api.z.ai/api/anthropic`; switching back to Claude always works regardless of Z.AI status |
| Context compacts too early on GLM | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` missing | Re-run `to-glm` (it sets 1000000) |

Z.AI's own FAQ escalation if config won't take: fresh terminal → validate JSON → as a last resort delete `~/.claude/settings.json` and reconfigure (Claude Code regenerates it). **With this skill, prefer `restore --latest` or `restore baseline-first-run.json` over deletion — they preserve your permissions/hooks.**

## 4. Emergency recovery (works even if the skill is gone)

Restore Claude defaults in two commands — this only removes provider overrides; your Anthropic login (keychain / `~/.claude.json`) is separate and untouched:

```bash
# Option A — restore the newest backup taken by the skill
cp "$(ls -t ~/.claude/glm-switch-backups/settings-*.json | head -1)" ~/.claude/settings.json

# Option B — restore the very first pre-skill state
cp ~/.claude/glm-switch-backups/baseline-first-run.json ~/.claude/settings.json

# Option C — primary backups gone too (~/.claude was wiped)? Use the mirror:
mkdir -p ~/.claude
cp "$(ls -t ~/.config/glm-switch/backups/settings-*.json | head -1)" ~/.claude/settings.json
```

If the baseline file contains the literal text `__FILE_DID_NOT_EXIST__`, your original state was "no settings file": `rm ~/.claude/settings.json` instead. Then restart Claude Code.

## 5. Cost/quota context

GLM Coding Plan is flat-rate. GLM-5.3 launched around mid-August 2026 (sources disagree on the exact day), initially through the Coding Plan rather than a public pay-per-token API, with open weights promised later. Quota multipliers and prices change often — check z.ai/subscribe before relying on any figure.

## 6. Using GLM in the desktop app (unverified)

Anthropic's docs conflict on whether the desktop app honours a `settings.json` provider switch:

- Desktop docs: `~/.claude/settings.json` is shared with the app, and variables can be set in the app's **Local → gear icon** environment editor (local sessions only).
- Gateway docs: the app reads gateway routing from *Developer → Configure Third-Party Inference* (enable via Help → Troubleshooting → Enable Developer Mode), "not from `ANTHROPIC_BASE_URL` or `settings.json`". With that form active the app loses Anthropic-hosted cloud environments and Remote Control.
- Anthropic also states it "doesn't support routing Claude Code to non-Claude models through any gateway", so this is an unsupported setup either way.

Test order: (1) `to-glm`, restart the app, start a **Local** session, `/status`. (2) If it still shows a Claude model, put the same variables in the Local environment editor. (3) Only then try the Third-Party Inference form.
