# Independent review #3 — Relay after the review #2 fixes

**Reviewer:** Claude, independent of the author.

**What was reviewed:**
- `deepseek-relay-review-2.patch` (34 files; DeepSeek's tip `1bf9408`), applied with `git apply`
  on PR head `897eee3`.
- The "fresh review" claims in Codex's `PLAN.md`, kept in this folder as `CODEX_PLAN.md`.

**Verdict: FIX-FIRST.** All the review #2 fixes are real, but six new problems need fixing before
any Codex feature work (Part 1 of `REVISION_PLAN_3.md`). They are deeper versions of N1 and N2:
1. The way Relay checks that the journal is genuine can be fooled.
2. A decision's meaning can still be changed.
3. One code path crashes.
4. The integrity baselines are kept only in memory.
5. Git filters still run.
6. Adding Codex creates a new exposure: the answer code is shown on screen.

## How this was checked
- `python3 -m pytest` on Linux, Python 3.13: **192 passed, 3 skipped**. The skipped tests are
  macOS-only, which is consistent with DeepSeek's 195 passing on macOS.
- **`swift test` was not run.** There's no macOS here.
- I re-ran the review #2 probes and wrote new ones for each finding below. Their outputs are
  quoted.
- Codex's claims are marked as **reproduced** (probe output below), **confirmed** (checked in the
  code, sometimes with a static checker), or **not verified**.

## Review #2 findings — status

| Finding | Status |
|---|---|
| N1 human-only actions | Partly fixed. The trust folder, HMAC signing and native-dialog-only `instruct` are real. The new gaps are R3-1, R3-2 and R3-7 |
| N2 git hooks/config | Partly fixed. Hooks, `fsmonitor` and the pager are neutralised, but **clean/smudge filters still run** and three commit paths skip the check (R3-5) |
| N3 coder reads the review | **Fixed.** The pointer names `REVIEW.md` first, and the test coder models reading it |
| N4 stale `FIX_PLAN.md` | Fixed (archived) |
| N5 uncounted Opus checkpoints | Fixed (strategist/fixer are never checkpointed) |
| N6 popup text from vision | Fixed (Accessibility `read_dialog`). One small option-value gap remains (R3-6) |
| N7 Relay Watch stale green | Fixed (one-shot timer, filtered paths). The Swift code was read, not run |
| N8 slug reuse | Fixed |
| N9, N10, N11 | Fixed |

## Findings

### R3-1 (P1) — Trusted journal events don't recompute their hash
- **Location:** `relay/journal.py`, `trusted_events()`. It checks the HMAC over the event's
  *stored* `hash` field, but never recomputes that hash from the event body. Ownership checks
  (`relay/integrity.py` `_control_allowed`) take their evidence from `trusted_events()`.
- **Reproduced:** an agent turn edits `STATE.json`. It then copies an earlier genuine human
  event, changes `type` to `decision.answered`, keeps the old `hash` and `sig`, and appends it.
  Result:
  ```
  violations with forged/replayed signed event: []
  verify(): False
  ```
  The ownership check passes, so the forged state is committed. Only the monitor's later
  `verify()` notices, and only if the monitor is running.
- **Fix:**
  - Make `trusted_events()` recompute each event's hash from its body, verify the signature, and
    check chain continuity, with no duplicate IDs and monotonic IDs.
  - Count only events after the trusted anchor and in chain order.
  - The orchestrator also calls `verify()` before every authorization decision (ownership,
    decision acceptance, halt clear). It pauses on any failure.
- **Test:** the probe above must produce a violation and a pause, with no commit.

### R3-2 (P1) — A human answer isn't bound to the question that was asked
- **Location:**
  - `relay/decisions.py` `_accept_locked` reads `return_to`, `options`, `kind` and `data` from
    `STATE.json`, inside the project.
  - `relay/trust.py` `store_code` keeps only the code hash.
  - The `WAIT_HUMAN` fingerprint (`relay/orchestrator.py` `_decision_fingerprint`) lives only in
    memory, so a restart takes a fresh baseline from whatever is on disk.
- **Impact:** while Relay isn't running (or before a restart), an agent can change what a
  pending decision *does*: for example its `return_to`, or a `model_limit` decision's `data.role`.
  The human's genuine code then authorizes the altered action. *(Confirmed from the code; Codex
  found this too.)*
- **Fix:**
  - At `escalate_human`, store a decision record in the trust folder: id, question hash, options,
    kind, data, return_to, round, and an HMAC over all of it.
  - Accepting an answer uses **only** that record and verifies its signature. `STATE.json` is a
    display copy.
  - Persist the `WAIT_HUMAN` fingerprint in the trust folder too.
- **Test:** edit `STATE.json` `pending_decision.return_to` and `data` while the loop is stopped,
  restart, and answer with the right code. The original action must run, or Relay pauses for
  tampering.

### R3-3 (P2) — Choosing "Use Opus" crashes the loop
- **Location:** `relay/orchestrator.py:507`. `_apply_human_opus_request` reads `state` before it
  is defined. `pyflakes` reports: `undefined name 'state'`.
- **Impact:** a decision answer containing "use opus" raises `NameError`. That isn't a
  `RelayError`, so the run loop exits with "unexpected error". The tests pass only because the
  `or` short-circuits on other answers. *(Confirmed; Codex found this too.)*
- **Fix:** load the state first. Add `pyflakes` (or `ruff`) to the test run.
- **Test:** answer a decision with "Use Opus"; an Opus turn is armed and nothing crashes.

### R3-4 (P2) — Integrity baselines are kept only in memory
- **Location:** `relay/orchestrator.py`:
  - `git_before = git_control_snapshot(...)` (line 731)
  - the turn's `before = snapshot(...)`, which is also recomputed on resume
- **Impact:** if Relay crashes mid-turn, everything the agent wrote before the crash becomes the
  new baseline on resume, so ownership and git-control violations are laundered. Checkpoint and
  rollover turns don't carry the git-control baseline at all. *(Confirmed from the code; Codex
  found this too.)*
- **Fix:**
  - Before ringing, persist both baselines: the file hashes and the git-control hashes, plus an
    HMAC, in the trust folder, keyed by attempt ID.
  - Resume, checkpoint and rollover all reuse them.
  - A legacy pending turn with no baseline pauses for the human to reconcile.
- **Test:** the agent writes `.relay/PLAN.md` as the coder, Relay crashes, resume runs. Assert a
  violation is found.

### R3-5 (P1) — Git clean/smudge filters still run, and three commit paths skip the check
- **Location:** `relay/integrity.py` `git_commit`. Also `relay/orchestrator.py:919`
  (checkpoint-before-switch commit), `:1260` (rollover checkpoint commit) and `:1341`
  (FIX_PLAN archive commit). None of these run `git_control_changes` before committing.
- **Reproduced:** with `filter.x.clean` set in `.git/config` and `*.txt filter=x` in
  `.gitattributes`, Relay's `git_commit` runs the filter:
  ```
  commit: 4962bf6d…   filter ran: True
  ```
  Normal turns catch the `.git/config` change before committing, but the three commit paths
  above don't.
- **Fix:**
  1. Before **every** git call, refuse if:
     - `.git/config`, or any `include.path` it pulls in, defines `filter.*`, `diff.*.textconv`,
       `diff.*.command`, `merge.*.driver`, `core.fsmonitor`, `core.sshCommand`,
       `core.gitProxy` or `credential.*`, unless it's on an explicit allowlist (e.g. git-lfs);
     - or any `.gitattributes` or `.git/info/attributes` uses `filter=`, `diff=` or `merge=`
       with a driver that isn't allowlisted.
  2. Run the git-control comparison before every commit, including checkpoints, rollovers and
     archives.
- **Test:** the probe above, run through each of the four commit paths. The filter never runs,
  and the turn pauses.

### R3-6 (P3) — Popup option values can still traverse out of the project
- **Reproduced:** `pytest --confcutdir=../..` → approve. Other traversal cases are correctly sent
  to the human.
- **Fix:** keep a per-command allowlist of options. For `--opt=value` and `--opt value`, check the
  value with the same path rules (no `..`, no absolute paths, no `~`, no `.git/`). *(Codex found
  this too.)*

### R3-7 (P1, new and made worse by adding Codex) — The answer code is visible on screen
- **Location:** `relay/orchestrator.py` `escalate_human` puts the one-time code in the
  **desktop notification**. The code is also passed as an `osascript` command-line argument,
  which any process of the same user can briefly see with `ps`.
- **Impact:** the code is the only secret that separates "a human answered" from "an agent
  answered". Any agent that can see the screen can read the notification and answer its own
  question. Codex desktop and Claude can both have computer-use abilities, so this matters more
  once Codex supervises. The native `instruct` dialog has the same problem: an agent with
  Accessibility can click "Allow". DeepSeek's `SECURITY.md` already names that residual risk.
- **Fix:**
  - Deliver the code **only to the phone**. Use ntfy over HTTPS with an access token, or show it
    in the terminal only when Relay runs in an interactive TTY. Never put it in a desktop
    notification, and never put it on any process command line.
  - Make it a hard rule in the setup docs and in `relay start`'s checks: Claude and Codex must
    not have Screen Recording or Accessibility permission, and computer use must be off for this
    project. If either app has those permissions, refuse to start.
- **Test:**
  - No `notify.sent` desktop payload contains the code.
  - The `osascript` arguments never contain it.
  - `relay start` refuses when a supervisor app has Accessibility permission (macOS probe;
    unit-test it with a fake).

### R3-8 (P3, not verified) — Some model requests may go uncounted
Codex says seed prompts, nudges, retries and checkpoints are model requests that aren't all
counted. That's plausible: seed rings are separate submissions. I didn't trace every path. Part 1
of `REVISION_PLAN_3.md` asks for an `app.request` journal event per submission, so this becomes
measurable.

## Codex's other claims
- **Review #3 must come before fixes:** agreed. These findings come with the tests listed above.
- **"Recover the missing master plan and review brief":** this refers to files on the Mac. They
  are in git: `plugins/deepseek-relay/BUILD_PLAN.md` and `docs/REVIEW_BRIEF.md`.

## What I could not verify
- Swift tests and the app build.
- The 10-minute live energy sample.
- Accessibility `read_dialog` against the real Claude Code and Zcode dialogs on macOS 27.2.
- How the trust folder behaves with Codex's sandbox.
- The Codex app itself: its model names, effort controls and usage-limit messages.
