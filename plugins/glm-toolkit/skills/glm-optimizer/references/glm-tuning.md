# GLM-5.3 Tuning, Effort & Setup Reference

Reference for the `glm-optimizer` skill. Source: Z.AI GLM Coding Plan docs. Read when you need
exact parameter behavior, the effort mapping, 1M-context setup, MCP install commands, or error
decoding.

---

## 1. Reasoning effort (the lever that matters most for correctness)

`reasoning_effort` controls how hard GLM-5.3 thinks. In Claude Code you set it with the `/effort`
command. Claude Code's effort levels are **remapped** onto GLM-5.3:

```
Claude Code /effort        GLM-5.3 actual effort
──────────────────────     ─────────────────────
low / medium / high   ──▶  high
xhigh / max / ultracode ─▶ max          ◀── use this for hard coding
```

Guidance:
- **Default to `max`** for multi-file changes, tricky debugging, architecture, or anything where a
  wrong answer is expensive. Deeper reasoning is the cheapest correctness insurance you have.
- Lower effort (`high`) is fine for small, well-scoped edits and quick Q&A where latency matters.
- API-level values `none`/`minimal` skip thinking entirely; `low`/`medium` collapse to `high`;
  `xhigh` collapses to `max`. So on the API there are really three tiers: skip, `high`, `max`.
- `reasoning_effort` only takes effect when `thinking.type = enabled` (the default).

---

## 2. 1M context window

GLM-5.3 supports a 1,000,000-token context with the `[1m]` model suffix. To enable it in Claude
Code, `~/.claude/settings.json` should contain:

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "your_zai_api_key",
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash[1m]",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
    "API_TIMEOUT_MS": "3000000"
  }
}
```

(This is the complete env block — auth, endpoint, and models — so it also serves as the target
when reconfiguring from scratch, per §7. If the file already has other fields, only add/update
these keys; don't replace the whole file.)

Practical consequences for how you work:
- You can hold large codebases, long transcripts, and big files in context — **prefer keeping
  related work in one thread** rather than fragmenting across subagents (see SKILL.md §4).
- If `[1m]` is rejected as "model does not exist", Claude Code is out of date — `claude update`.
- Confirm with `/status`: the model line should read `glm-5.3[1m]` and settings source should be
  your `~/.claude/settings.json`.

---

## 3. Output length / truncation

`max_tokens` caps a single response's output (not input):

```
Model           default max_tokens    hard cap
──────────────  ──────────────────    ────────
glm-5.3               65536            131072   (carried over from GLM-5.2 docs — unverified for 5.3)
glm-5.3-flash         not verified     not verified
```

GLM-5.3 is described by Z.AI as a post-training-only upgrade of GLM-5.2 (same base model), so the
GLM-5.2 limits are the best available guess. Check docs.z.ai for the current numbers before relying on them.

If a long file or answer stops mid-stream, the cause is usually `finish_reason: length`. Don't
leave a half-written file — continue the output explicitly, or split the work into smaller writes.

---

## 4. Sampling parameters (when the user asks you to tune behavior)

- `temperature` (GLM-5.3 default `1.0`, range `0.0–1.0`): lower = more deterministic. For precise,
  factual, or reproducible code work, lower it (e.g. `0.2–0.6`).
- `top_p` (default `0.95`): nucleus sampling. Tune `temperature` **or** `top_p`, not both.
- `do_sample: false`: greedy, fully deterministic output — good for reproducible generation.

These are API-level knobs. In a plain Claude Code session you usually leave them at defaults and
control depth via `/effort` instead.

---

## 5. GLM Coding Plan MCP servers

Install commands (one-time, replace `your_api_key`):

```bash
# Vision (image/screenshot understanding, GLM-4.6V) — local server
claude mcp add -s user zai-mcp-server \
  --env Z_AI_API_KEY=your_api_key --env Z_AI_MODE=ZAI -- npx -y "@z_ai/mcp-server"

# Web Search (current info)
claude mcp add -s user -t http web-search-prime \
  https://api.z.ai/api/mcp/web_search_prime/mcp \
  --header "Authorization: Bearer your_api_key"

# Web Reader (fetch + parse a URL)
claude mcp add -s user -t http web-reader \
  https://api.z.ai/api/mcp/web_reader/mcp \
  --header "Authorization: Bearer your_api_key"

# Zread (GitHub repo docs/structure/code)
claude mcp add -s user -t http zread \
  https://api.z.ai/api/mcp/zread/mcp \
  --header "Authorization: Bearer your_api_key"
```

When to use which:
- **Vision** — the user references an image/screenshot on disk. Put the file in the working dir and
  name it in the prompt (`what does build-error.png show?`). Vision needs Node ≥ 22, and version
  ≥ 0.1.2 — if npx serves a cached older version, install as `@z_ai/mcp-server@latest`.
- **Web Search** — you need something newer than your knowledge or a current fact.
- **Web Reader** — you have a specific doc/URL to extract.
- **Zread** — understanding an open-source dependency before using or debugging it.

---

## 6. Error code quick-decode

If an API/tool call fails, these Z.AI business codes explain why (HTTP status in parens):

```
1000/1001/1003  (401)  auth failed / missing / expired token  → re-check API key
1113            (429)  insufficient balance / no package       → recharge
1211            (400)  unknown model code                      → fix model name
1210/1214       (400)  invalid parameter                       → check the field
1261            (400)  prompt too long                         → trim context
1301            (400)  flagged sensitive content
1302/1305       (429)  rate limited / overloaded               → back off, retry
1308/1310/1316-1321 (429) usage/plan limit reached             → wait for reset
1309            (429)  plan expired                            → renew subscription
1311            (429)  plan lacks access to this model
```

Note: in streaming (SSE) mode, mid-inference failures don't return these codes — the reason shows
up in `finish_reason` instead. That's an API-level field; inside Claude Code you can't see it, so
use SKILL.md §7's proxy instead (re-read the file tail after large writes).

---

## 7. Config sanity checklist

If GLM behaves wrong or a setting won't take:
1. `claude --version` — old versions don't know `[1m]`; `claude update`.
2. Close **all** Claude Code windows, open a fresh one — env changes need a new session.
3. Validate `~/.claude/settings.json` is well-formed JSON (no trailing/missing commas).
4. `/status` — confirm the model line and that the settings source is your user settings file.
5. Still broken? Delete `~/.claude/settings.json`, let Claude Code regenerate it, reconfigure.
