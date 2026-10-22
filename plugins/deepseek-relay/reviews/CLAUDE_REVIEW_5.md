# Independent review #5: Relay after review #4, plus the start of Part 2

**Reviewer:** Claude, independent of the author.

**What I reviewed:** `deepseek-relay-review-4.patch`, applied with `git apply` on PR head
`3643c6e`. It touches 30 files, adding 11,156 lines and removing 81. Of those, 9,503 lines are
`reviews/REVIEW_BUNDLE.md` (see R5-3).

**Patch hash:** sha256 `92f63b96acac6aa7f92c1197b949a7c92dca5bd8759a46de243449e29e38e303`. This
**matches** the hash DeepSeek reported.

**Verdict: FIX-FIRST (small).**
- Every review #4 finding is fixed. I re-ran each probe to confirm.
- One new P1: the new code gate can be bypassed by faking a terminal. It's the same trick as the
  earlier `relay instruct` finding.
- One P2: the guard that blocks screen-reading supervisors lets everything through.
- Both fixes are small. Part 2 is on track.

## How I checked
- **Python tests:** `python3 -m pytest` on Linux with Python 3.13 gives **243 passed and 3
  skipped**, 246 in total. That matches DeepSeek's 246 on macOS.
- **Code check:** `pyflakes` reports nothing.
- **Swift:** `swift test` was not run, because this environment has no macOS.
- **Routing policy:** the hashes in `docs/codex-routing/MANIFEST.json` match all three bundled
  files. A secret scan of the bundle found nothing.

## Review #4 findings: status

| Finding | Status | Evidence |
|---|---|---|
| R4-1 `--clear-halt` / `--adopt-baseline` need no proof | Fixed for a plain shell | Without a code: "Refusing to perform a human-only action without proof", and HALT stays. A wrong code is refused. **The terminal path can be bypassed (R5-1)** |
| R4-2 git-lfs matched by name | **Fixed** | A redefined `filter.lfs.clean` returns `not the standard git-lfs command`. The standard lfs config has no risks |
| R4-3 nested `.gitattributes` | **Fixed** | `sub/.gitattributes: filter=zz` is flagged |
| R4-4 decision re-ask | Fixed (by reading the code and DeepSeek's tests) | Not probed live |

## Findings

### R5-1 (P1): An agent can make Relay show it the one-time code
- **Where:** the human-proof flow for command-line actions: `relay resume --clear-halt`,
  `--adopt-baseline` and `--accept-git-control` (`relay/actions.py`, `relay/cli.py`). If there is
  no ntfy token, the code is printed to the terminal that ran the command, whenever that terminal
  looks interactive.
- **Reproduced:** an agent runs the command inside `script`, a standard tool that makes a
  command think it's in a real terminal. It reads the printed code, then runs the command again
  with it:
  ```
  One-time code for clear-halt: roxwkierjpjqmpzb
  captured code: roxwkierjpjqmpzb
  phase: CODE  round: 1        ← HALT gone, human.halt_cleared journaled
  ```
- **Why:** "the terminal is interactive" proves nothing about *who* is at the terminal. The code
  is shown to whoever started the command, which is exactly the party it's meant to keep out.
  This is the same weakness as the review #2 `relay instruct` finding.
- **Fix:**
  - For any human-only action **started from the command line**, deliver the code **only** to
    the phone (ntfy with an access token). Never print it to the terminal that asked for it.
  - If there's no phone channel, refuse.
  - Decision codes that the orchestrator prints in its *own* terminal can stay. That terminal
    isn't the one the agent started.
  - Apply the same rule to the future `relay switch` in Part 3.
- **Test:** run each gated command under `script -qc` with no token. The output must contain no
  16-letter code, and nothing changes.

### R5-2 (P2): The guard against screen-reading supervisors lets everything through
- **Where:** `relay/guard.py:44-62` (`supervisor_risks`). It returns "no risks", and so allows
  the run, in each of these cases:
  - `security.supervisor_bundle_ids` is empty. That's the default (`config/defaults.json:101`).
  - The permissions database (TCC) can't be read. macOS usually blocks reading it without Full
    Disk Access.
  - The bundle ID isn't listed in the database.

  It also reads only the *user* database
  (`~/Library/Application Support/com.apple.TCC/TCC.db`). On recent macOS versions,
  Accessibility grants are normally kept in the *system* database, so this check probably never
  sees them. **Please confirm this on macOS 27.2.**
- **Impact:** the R3-7 protection is effectively off, both for Claude and for the new Codex
  supervisor. A supervisor with Screen Recording or Accessibility could read the code or click a
  dialog.
- **Fix (fail closed):**
  - `relay start` refuses unless the selected supervisor's bundle ID is configured. Ship known
    defaults for Claude and Codex, to be confirmed at calibration.
  - When the database can't be read, require a **human attestation** during calibration: "this
    app has no Screen Recording, no Accessibility, and computer use is off". Gate it with a
    one-time code sent to the phone.
  - Store the attestation in the trust folder together with the macOS version, and ask again
    after an OS update.
  - Also probe the system database when it's readable.
- **Test:**
  - No bundle IDs → refused.
  - Unreadable database with no attestation → refused.
  - Valid attestation → allowed.
  - Attestation from a different macOS version → refused.

### R5-3 (P3): A 9,503-line generated bundle is committed
- **Where:** `reviews/REVIEW_BUNDLE.md` is a generated snapshot of the source code.
- **Impact:** it grows the repo by about 360 KB with every refresh, and reviewers may read stale
  code from it.
- **Fix:** remove it from the patch, add it to `.gitignore`, and generate it only on demand.

## Part 2 (Codex supervisor): what's in and what's left
What's in:
- `supervisor.backend` (default `claude`).
- `make_codex_driver`, `CODEX_DONE.json` and `RULES_CODEX.md`.
- A pending turn is pinned to its backend.
- Calibration proof is needed only for the selected supervisor plus Zcode.
- Routing-policy files and hashes are checked, with no Claude fallback.
- The model is pinned to `gpt-6.1-sol` at Medium effort.

All of that matches the plan. DeepSeek's list of open items is accurate. Do them in this order
before starting Part 3:
1. Put `attempt_id` into every sentinel and require it to match. Part 3's retry logic depends on
   this.
2. Track session counters and context by **backend and role**.
3. R5-2: the screen-reading check, wired into Codex calibration.
4. Real Codex-app calibration on macOS 27.2. The process name "ChatGPT" and window title "Codex"
   are guesses until then.

## What I could not verify
- Swift tests.
- Anything in the real Codex app, including its picker label `GPT-6.1-Sol` and its
  usage-limit messages.
- The TCC database location on macOS 27.2.
- ntfy delivery with a token.
