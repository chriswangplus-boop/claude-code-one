# Independent review #4: Relay after the review #3 fixes (Part 1)

**Reviewer:** Claude, independent of the author.

**What I reviewed:** `deepseek-relay-review-3.patch`, applied with `git apply` on PR head `dd3bd84`.
It touches 35 files, adding 1,994 lines and removing 483.

The patch's sha256 is `5185d5928fc5c405b1be65f305f66a021e92bae62cbc7cb9c3699c99169e73cf`. That does
not match the `92d785e9…9bf4c` DeepSeek reported. The patch applies cleanly and its contents match
the described work, so the reported hash was probably taken from an earlier copy. In future,
report the hash of the exact file you hand over.

**Verdict: FIX-FIRST (small).** Part 1 is almost done.
- Every review #3 finding I re-tested is fixed.
- Two problems remain. One is a P1: two "human" actions can still be run by an agent. The other is
  a P2: a git-lfs loophole. Both have small, local fixes.
- After those, start Part 2 of `REVISION_PLAN_3.md` (the Codex supervisor).

## How I checked
- **Python tests:** `python3 -m pytest` on Linux with Python 3.13 gives **220 passed, 3 skipped**.
  That is 223 in total, which matches DeepSeek's 223 on macOS. The 3 skipped tests are macOS-only.
- **Code check:** `pyflakes relay` reports **no errors**.
- **Swift:** `swift test` was **not run**, because this environment has no macOS.
- **Probes:** I re-ran the review #3 probes and wrote new ones. Their output is quoted below.

## Review #3 findings: status

| Finding | Status | Evidence |
|---|---|---|
| R3-1 replayed or altered journal events | **Fixed** | Same probe: now `violations: ['.relay/STATE.json']` |
| R3-2 decision not bound to its question | **Fixed** | Probe: editing `return_to`, `data` or `code_hash` in `STATE.json` → the answer is refused with both the attacker's code and the real code. A real `relay instruct`, or Relay's own write, during the wait does *not* block the real answer |
| R3-3 `NameError` on "Use Opus" | **Fixed** | `pyflakes` is clean and is now part of the test run |
| R3-4 baselines only in memory | Fixed (by reading the code) | Per-attempt baselines are stored in the trust folder (`store_attempt`). This wasn't probed end to end |
| R3-5 git filters run | **Fixed, except one loophole** | Probe: a `filter.x` filter is refused (`filter ran: False`). `commit.gpgsign` is forced off. Remaining gap: R4-2 |
| R3-6 traversal in option values | **Fixed** | `pytest --confcutdir=../..` and `--rootdir ../..` now go to the human |
| R3-7 answer code on screen | **Fixed** | The code goes only to ntfy with a token, or to an interactive terminal. It is never in a desktop notification or in `osascript` arguments |
| R3-8 every model request counted | Fixed (by reading the code) | `app.request` events at `relay/orchestrator.py:1582` and `:1613` |

## Findings

### R4-1 (P1): Two "human" actions need no human proof
- **Where:** `relay/cli.py:309-336` (`cmd_resume`) and `:339-365` (`_adopt_baseline`).
- **Evidence (from reading the code):**
  - `relay resume --clear-halt` checks the journal, clears the monitor's HALT flag, and writes a
    `human.halt_cleared` event with `actor: human`.
  - `relay resume --adopt-baseline` accepts the current files as a pending turn's baseline.

  Neither one asks for a one-time code or a native dialog. Both run before the run lock is
  acquired. So even when the loop is already running and the second process exits with "another
  loop is running", the halt is already cleared.
- **Why signing doesn't help here:** the events are correctly signed, because the agent ran
  Relay's own code, and that code can read the HMAC key. **A signature proves "written by Relay",
  not "written by a human".**
- **Impact:** an agent with shell access can do two things:
  - silence the monitor's safety stop (`--clear-halt`);
  - launder the files it wrote outside the rules (`--adopt-baseline`).

  The app's command-approval dialog is the only remaining barrier, and it disappears if the user
  runs Claude Code or Zcode in auto-approve mode.
- **Fix:**
  1. Gate both options with the same one-time code as decisions: sent to the phone or the
     terminal, checked against a hash in the trust folder, used once.
     `relay resume --clear-halt --code <code>` and the same for `--adopt-baseline`.
  2. Clear the flag only after the code checks out.
  3. Add a test that finds every `journal.append(..., actor="human")` call site. Each one must be
     gated by human proof: a one-time code or the native dialog. `relay pause` and `relay stop`
     are the exceptions, because they only make Relay safer.
- **Test:** run `relay resume --clear-halt` without a code. Assert HALT is still present and no
  `human.halt_cleared` event was written. Do the same for `--adopt-baseline`.

### R4-2 (P2): The git-lfs allowlist checks the name, not the command
- **Where:** `relay/integrity.py:336`. `GIT_DRIVER_ALLOWLIST = {"lfs"}` is applied to the driver
  *name* only.
- **Reproduced:** an agent can define `filter.lfs.clean = sh -c "…"` in `.git/config` and add
  `*.txt filter=lfs` to `.gitattributes`. Relay's `git_commit` then runs the agent's command:
  ```
  lfs: commit dea3c087…  | command ran: True
  ```
- **Exposure:** during a turn, the per-turn git-control comparison catches the `.git/config` edit
  before the commit runs. The gap is any write made *between* turns, for example while Relay waits
  in `WAIT_HUMAN` or after a popup escalation. The next turn takes its baseline after the edit,
  and its first `git status` can already run the filter.
- **Fix:**
  - Allow `lfs` only when its values exactly match the standard git-lfs commands:
    - `clean`: `git-lfs clean -- %f`
    - `smudge`: `git-lfs smudge -- %f`
    - `process`: `git-lfs filter-process`
    - `required`: `true`
  - Keep a Relay-signed "last known good" hash of `.git/config`, `.gitattributes` (all of them)
    and `.git/info/attributes` in the trust folder. Update it only after a turn passes its
    checks. Compare against it before **every** git call, so changes made between turns are caught.
- **Test:**
  - The probe above must be refused.
  - A `.git/config` edit made while in `WAIT_HUMAN` must pause the next turn before any git
    command runs.

### R4-3 (P3): `.gitattributes` files in subfolders are not scanned
- **Where:** `relay/integrity.py`, `git_attribute_risks`. It reads only the root `.gitattributes`
  and `.git/info/attributes`.
- **Why it matters:** git also honours `.gitattributes` files in subfolders. This only matters
  together with a driver defined somewhere Relay doesn't check, such as R4-2 or a filter in the
  user's own `~/.gitconfig`.
- **Fix:** scan every `.gitattributes` in the working tree, skipping the ignored folders.

### R4-4 (P3): No recovery path after a refused, tampered decision
- **What happens:** when someone tampers with a pending decision, Relay correctly refuses even the
  real code ("refusing the answer"). The message doesn't say how to continue. I didn't test
  whether `relay resume` asks the question again with a new code.
- **Fix:**
  - The refusal message names the next step.
  - `relay resume` re-asks the question with a new decision ID, a new code and a new signed record.
  - The tamper event is kept for the monitor.
- **Test:** tamper → refuse → `relay resume` → a new decision → the new code is accepted.

### Note: phone notifications shown on the Mac
The code now goes only to the phone, which is correct. But macOS can show iPhone notifications on
the Mac (iPhone Mirroring and notification sharing). That would put the code back on the Mac
screen. Add a setup step to `docs/OPERATIONS.md`: turn off notification mirroring for the ntfy app
on this Mac. Also list it in `relay calibrate`'s checklist.

## What I could not verify
- Swift tests.
- Anything on real macOS 27.2: the TCC probe for supervisor bundle IDs, ntfy delivery with a
  token, and Accessibility dialog reading.
- The full crash/resume flow using persisted baselines. I read the code but didn't probe it end
  to end.

## Next steps for DeepSeek
1. Fix R4-1 through R4-4 and the OPERATIONS note, each with its test. This completes Part 1.
2. Then start **Part 2 of `REVISION_PLAN_3.md`** (the Codex desktop supervisor). The human has
   already answered the four questions in that plan's Part 4.
3. Part 2 needs `docs/codex-routing/` filled from the human's `~/.codex/` files on the Mac. If those
   files are missing, stop and report it; don't substitute anything.
