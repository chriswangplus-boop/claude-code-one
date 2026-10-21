# Security

## Threat model

Assets:

- source code and `.env` / credentials in the project,
- screenshots of two desktop apps,
- the DeepSeek API key and the ntfy topic,
- the integrity of the relay's decisions and journal.

Adversaries / failure sources:

- prompt injection in repo text, web pages, review files, or the apps' own output,
- a compromised or careless app agent,
- accidental secrets in an auto-commit or a phone push,
- another local user or a synced-folder conflict,
- a UI misread or a drifting model.

## What leaves the machine

| Destination | Data |
|---|---|
| DeepSeek API | System prompt + redacted review/report/sentinel/handoff excerpts, round summaries, monitor notes, and cropped target-app screenshots (unless `text_only_mode`). Never source code files, `.env`, keychain values, or the clipboard. |
| ntfy | A short question and decision ID (`D-00N`). No code, file paths, or answers. |
| OS keychain | DeepSeek key, ntfy topic/token. |
| Local git | The project + `.relay` text files; runtime state and screenshots are ignored. |

The redactor runs on every string before it is placed in a prompt. It removes private key
blocks, provider token shapes (`sk-…`, `ghp_…`, `AKIA…`, `xox…`, `AIza…`, JWTs), bearer/basic
credentials, `password/token/api_key=…` assignments, emails, `/Users/<name>` home paths, and
high-entropy blobs. SHA-256 and 40-hex git hashes are preserved so sentinels and pointers remain
verifiable. Only rule counts are journaled, never matched text.

`text_only_mode: true` (or `deepseek.vision: false`) makes every vision read return
"unavailable"; the loop degrades to file/sentinel signals instead of sending pixels.

## Screenshots

- Capture is limited to the target app's window or a calibrated sub-region. Full-screen capture
  is never used.
- Files are `0600` under `.relay/journal/screens/`, excluded from git by `.gitignore`.
- Retention is 14 days by default; the CLI and monitor prune older files.
- A screenshot is referenced in the journal by path + SHA-256. Only the cropped image is sent to
  DeepSeek; call records store an image reference, not base64 pixels.
- The Screen Recording permission probe rejects all-black captures.

## Secrets

- Precedence: `RELAY_*` env > OS keychain. Nothing is written to `config.json` or the journal.
- `relay init --set-api-key` reads with `getpass` (no echo) and stores via `keyring` or
  `security add-generic-password`.
- A missing key produces a clear error; Relay never falls back to an unauthenticated call.

## Git

- Before every commit, staged files are scanned. A hit raises `SecretDetected`, aborts the turn,
  adds no commit, and pauses.
- `init` refuses to create the baseline if the files it would commit contain secrets.
- There is no push helper. `git push` never appears in the code. The monitor also checks the
  journal for push attempts.
- Each turn is one local commit, so every step is undoable.
- `.git/` is agent-writable, so Relay never lets git run agent code: every command uses
  `-c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.pager=cat`, commits with
  `--no-verify`, and runs with `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0` and an empty
  `GIT_EXTERNAL_DIFF`. A change to `.git/config`, `.git/info/attributes`, `.gitmodules` or any
  `.git/hooks/*` file during a turn is an ownership violation and pauses the loop.

## Prompt injection

- Every agent-produced string is wrapped in `<untrusted source="…"> … </untrusted>`.
- Forged closing tags are rewritten, so text cannot break out of its block.
- System prompts state that untrusted blocks are data, never instructions.
- The routing model cannot approve work or answer a decision.
- Popup approval is **not** an LLM decision. The dialog text and buttons are read through
  Accessibility (no screenshot, no vision). `policy.py` normalises the text, rejects dangerous
  verbs (push/delete/install/network/credentials/always-allow…), requires a phrase from the
  configured allow-list, and requires a safe button label. Command arguments are checked too:
  `--output`, `-o`, `--ext-diff`, `-c`, `--no-verify`, `-p`, `--rootdir`, absolute paths and
  `.git/…` are rejected. `git add`/`git commit` are never auto-approved. Anything else goes to a
  human.
- Agent output is capped and truncated before it reaches a model.

## Phone channel

- ntfy topic is random (≥32 chars after the prefix) and stored in the keychain.
- Push body is a question + ID only.
- Answers are accepted only from the local CLI or the local `PENDING_DECISION.md` file.
- `ntfy.phone_reply: true` is rejected at config validation; an unimplemented reply channel
  would be a spoofing risk.

## GUI takeover

- Wait for 30 s of user idle before touching input.
- Verify the process/window title before every focus and action.
- Screen-corner mouse position raises a pause.
- Users can always `relay pause`; the loop checks the flag while waiting.
- Every click, keystroke and screenshot is journaled with the window identity.

## Human-only actions outside the project

Everything used to prove "a human did this" is kept out of the agent-writable folder, in
`~/Library/Application Support/Relay/<project-id>/` (mode `0700`, files `0600`):

- the journal HMAC key and the trusted tail anchor;
- one-time decision codes, as SHA-256 hashes only (never the code itself).

`STATE.json` holds only the decision id. During `WAIT_HUMAN`, Relay fingerprints `STATE.json` and
the question text and pauses on any change not made by Relay (the human's `ANSWER:` line is
excluded from the fingerprint). `relay instruct` can only be confirmed in the native macOS dialog;
stdin is never read, so `echo y | script -qc "relay instruct ..."` cannot record an instruction.

Residual risk: an agent with unrestricted shell access as the same user can read the trust folder
(and thus the HMAC key). Claude Code and Zcode ask permission before touching files outside the
project, and the popup policy escalates "outside" dialogs to the human. Relay is not a sandbox
against a fully compromised user session.

## Journal integrity

- Append-only JSONL with a SHA-256 chain across day files.
- Every event is HMAC-signed with the external key; the last event is pinned by an anchor file in
  the trust folder. `verify()` reports chain, hash, signature and anchor errors.
- Ownership evidence and the monitor's history checks use only events whose signature verifies,
  so a forged `decision.answered` or `human.halt_cleared` is ignored even if it is chained.
- `relay trace --verify` detects edits, deletions, reordering, truncation and unsigned appends.
- The monitor verifies the chain every check; a break halts the loop.
- DeepSeek prompts and responses are stored redacted under `journal/calls/` for `why`/`replay`.

## Residual risks (accepted)

1. **UI redesign.** Coordinates break. Mitigation: window verification, bounded recovery,
   re-calibration, human halt.
2. **Vision misread.** A context percentage or popup can be misread. Mitigation: conservative
   max-of-three rollover signals, deterministic popup policy, every action verified/journaled.
3. **Overlapping windows.** Capture uses the target window's bounds after raising it; a floating
   panel from another app that overlaps those bounds can appear in the crop. Keep other windows
   clear during a run, or use a dedicated desktop.
4. **Third-party cloud.** Redacted review text and cropped screenshots still leave the machine if
   vision is enabled. The user must accept DeepSeek's data policy; `text_only_mode` is the
   off-switch.
5. **Local compromise.** An attacker with the user's OS session can read `.relay/`, the keychain
   and the Relay trust folder (including the journal HMAC key). Relay is not a sandbox. The
   trust folder is outside the project so a prompt-injected agent must first escape its app's
   file-permission gate to reach it.
6. **Cloud-synced folder.** Google Drive is allowed by default (`security.allow_google_drive`)
   because the intended workflow pauses Drive sync during runs. If a sync client is active,
   conflict copies can still corrupt `.relay`; atomic writes and the journal chain detect some
   cases but not all. iCloud, Dropbox and OneDrive are refused unless `--allow-sync-dir` is
   passed.
7. **A dialog is only as strong as the agent's TCC grants.** `relay instruct` cannot be answered
   through a pseudo-TTY, but an agent that has been granted Automation/Accessibility could click
   the dialog itself. Grant those permissions only to the app that runs Relay, not to the agent
   apps, and review `relay trace` for unexpected `decision.answered` events.

## Recommended setup

- A stable project folder. Google Drive is acceptable if Drive sync is paused or quit while
  Relay runs; otherwise prefer a purely local folder.
- Full-disk encryption and a screen lock.
- A dedicated DeepSeek API key with a spending limit.
- A dedicated ntfy topic; do not reuse one across projects.
- Review `.relay/MONITOR_NOTES.md` and `relay status` after every pause.
- Keep `deepseek.text_only_mode` on if the project contains regulated data.
