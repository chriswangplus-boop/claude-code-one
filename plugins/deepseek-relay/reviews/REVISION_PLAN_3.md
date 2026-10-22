# Relay revision plan v0.4, for DeepSeek to build

**Sources:**
- Codex's `CODEX_PLAN.md`, kept verbatim in this folder.
- Claude's `CLAUDE_REVIEW_3.md`.

This plan **adopts Codex's direction**, Codex desktop as a switchable supervisor, with three
changes:
1. The order of work: repairs come first, then a simpler first version of switching.
2. Where the one-time code is shown, because an agent with computer use could read it on screen.
3. The human's answers of 2026-10-21 on the model, the routing policy, the High assessment and
   switching (Part 4).

**Ground rules (unchanged):**
- `BUILD_PLAN.md` holds unless this file changes it.
- Every finding gets a regression test that fails before the fix.
- Commit repairs and features separately.
- Never push.
- Ask the human; never decide product questions yourself.

**Branch base:** PR head after these commits. The repo holds `BUILD_PLAN.md` and
`docs/REVIEW_BRIEF.md`; there's no need to "recover" them.

---

## Part 1 — Repairs (finish all of these before Part 2)

Fix every `CLAUDE_REVIEW_3.md` finding in this order. Each fix's test is given in the review.

| Order | Finding | Fix in one line |
|---|---|---|
| 1 | R3-5 | Refuse any git call while risky git config or attributes (filters, textconv, drivers, fsmonitor, sshCommand, include) are present; run the git-control check before **every** commit |
| 2 | R3-1 | `trusted_events()` recomputes hashes, checks signatures, chain order and anchor; call `verify()` before every authorization decision |
| 3 | R3-7 | Send the answer code to the phone (ntfy with a token) or an interactive terminal only; never in a desktop notification or on a command line; refuse to start if a supervisor app has Screen Recording or Accessibility |
| 4 | R3-2 | Store a signed decision record in the trust folder; answers act only on that record; persist the `WAIT_HUMAN` fingerprint |
| 5 | R3-4 | Persist signed file and git-control baselines per attempt **before** ringing; reuse them on resume, checkpoint and rollover; a legacy turn with no baseline pauses |
| 6 | R3-3 | Fix the undefined `state`; add `pyflakes` (or `ruff`) to the test command and keep it at zero errors |
| 7 | R3-6 | Option allowlist per command, checking `--opt=value` and `--opt value` paths |
| 8 | R3-8 | Journal an `app.request` event for every message typed into any app (task, seed, nudge, retry, checkpoint), with backend, role, model and effort |

**Done when:**
- every R3 test passes;
- `pytest` passes on macOS and Linux;
- `swift test` passes;
- `pyflakes` reports no errors.

---

## Part 2 — Codex desktop as a second supervisor

Adopted from Codex's plan §2, with these clarifications.

1. **Reuse the existing `AppDriver` interface.** Add:
   - a Codex calibration profile;
   - `make_codex_driver`;
   - a `RULES_CODEX.md`, or a backend section in the rules;
   - a Codex sentinel, `.relay/CODEX_DONE.json`.

   Keep every Claude filename and behaviour unchanged.
2. **Settings and state:**
   - New setting `supervisor.backend` (`claude` or `codex`). Existing projects default to
     `claude`.
   - Session counters, context and round counts are tracked per **backend and role**.
3. **Every pending turn pins its backend and a unique `attempt_id`.** A sentinel must match both,
   or it is rejected.
4. **Calibration proofs:**
   - Required only for the selected supervisor and Zcode.
   - Both must resolve to the exact `project_dir`.
   - Codex must work in the shared folder, not a separate Codex worktree. Calibration proves this
     with the same `CALIBRATION_<app>.json` self-test.
5. **Model and effort are calibrated separately.**
   - Both must be visibly verified, through Accessibility or vision, before the task is typed.
   - A model that isn't available, or an unreadable control, pauses the loop.
6. **The answer code never appears in Codex's view (R3-7).**
   - Calibration checks that Codex's computer use and its Screen Recording and Accessibility
     permissions are off.
   - If any is on, it refuses.

---

## Part 3 — Manual switching between Claude and Codex

```text
relay switch --to codex
relay switch --to claude
```

**Version 1 (build this first). Switching happens only at safe points:**
- A switch request is authenticated with a one-time code, using the same mechanism as decisions
  (phone or terminal).
- It is stored as a signed, replay-safe record: project, from, to, a generation number and the
  attempt ID.
- **If a supervisor turn has finished:** validate and accept it exactly once, then switch.
- **If Zcode is coding:** let that turn finish and validate it, then switch before the next
  supervisor turn.
- **If the outgoing supervisor stopped on a verified usage-limit message:**
  - copy its partial outputs into `.relay/recovery/<attempt_id>/`, marked *unverified*;
  - never commit, discard or revert them automatically;
  - retry the same phase in a fresh session on the target backend, with a new `attempt_id` and
    the **original** signed baseline;
  - fixer file limits are counted across both attempts.
- **Before any switch, verify that the outgoing app has stopped writing.** Both of these must
  hold:
  1. No file in the project has changed for a quiet period (default 60 s, measured with
     snapshots).
  2. The app shows idle, read through Accessibility or vision.

  If either can't be established, the switch stays pending and Relay asks the human.
- **Incomplete changes are always preserved.** If their safety can't be established, the loop
  stays blocked until the human resolves them. Safety means passing the ownership check, the
  git-control check and the secret scan against the original signed baseline.
- **An active Zcode coding turn always finishes, and is validated, before the supervisor
  changes.**
- **The replacement session** is seeded from the plans, reviews, reports and decisions, plus a
  harness-written `HANDOVER.md`. It never needs the quota-limited app to write a checkpoint.
- **A usage-limit message** always pauses and notifies. There's no automatic fallback and no
  automatic switch back.

**Version 2 (only after v1 passes its pilot): switching while a supervisor turn is running.**
- It needs a calibrated "stop" action and a verified idle observation; a stop click alone isn't
  enough.
- If activity is unknown, the switch stays pending.

**Why split it:**
- The main real case is "Claude ran out of quota". That is already covered by the usage-limit
  path in v1, without the riskiest part: stopping an agent in the middle of its work.
- Version 2 adds a GUI action, and an "is it really idle?" check, that can't be verified
  outside the real apps.

---

## Part 4 — Choosing Codex's model and effort, and showing status

### Human decisions (2026-10-21)
1. **The model.** The installed Codex model metadata lists display name `GPT-6.1-Sol` and
   identifier `gpt-6.1-sol`, and the local Codex configuration selects that identifier.
   - Pin `gpt-6.1-sol`.
   - During calibration, read the label the Codex picker actually shows. If it isn't
     `GPT-6.1-Sol`, pause and ask the human. Nobody has inspected the live picker yet.
2. **The routing policy.**
   - Bundle the full **GPT Routing v3.0** instructions, plus `routing-policy.json` (on the
     human's Mac at `~/.codex/routing-policy.json`), into `docs/codex-routing/`. DeepSeek copies
     them on the Mac.
   - Record the policy version and the sha256 of each file in `docs/codex-routing/MANIFEST.json`.
     Every Codex turn's journal event also carries the version and those hashes.
   - **If the policy files are missing, or their hashes don't match the manifest, Relay refuses
     to run Codex as supervisor and reports the missing policy.** It never substitutes Claude's
     routing rules.
3. **Before every High turn, run a separate High qualification assessment.** The human chose
   this extra check.
4. **Version-1 switching** (between turns and at quota stops) is enough. Part 3 adds the human's
   extra requirements.

### Effort routing for Codex (as the bundled policy defines it)
- **Medium** is used for substantive planning, review, strategy, fixes and coordination. A first
  plan or a repeated failure does not authorize High by itself.
- **Low** is used only for bounded mechanical work that passes the policy's checks. Harness seeds,
  nudges and handover reading are candidates.
- **High** requires a qualification that passed, as defined below.

### High qualification assessment
1. **When a High trigger fires** (the same deterministic triggers as Opus: first master plan,
   failed Medium re-plan, hardest-bug streak of 4, or blocked twice), Relay first runs a
   **separate Medium assessment turn**.
   - It is counted and journaled as `model.assessment`.
   - It writes `.relay/HIGH_QUALIFICATION.json`.
2. **`HIGH_QUALIFICATION.json` contains:**
   - the verdict: `QUALIFIED` or `NOT_QUALIFIED`;
   - **exactly one** permitted category from the bundled policy;
   - concrete difficulty evidence;
   - the bounded task scope;
   - the sha256 of every input file it relied on;
   - the `assignment_id` it applies to.
3. **Relay validates the file deterministically:**
   - the schema;
   - the category must be in the policy;
   - the input hashes must still match the files on disk;
   - the `assignment_id` must equal the pending one.
4. **A passing qualification is bound to one assignment and used once.** Relay journals it as
   consumed, and any later work is classified again.
5. **The turn stays at Medium** if the qualification is missing, `NOT_QUALIFIED`, stale (input
   hashes changed), malformed, or for a different scope.
6. **High still counts toward its limits** (default 1 per milestone and 3 per day). When a limit
   is reached, Relay asks the human, as with Opus.
7. **Nested Codex agents follow the same rules.** Relay records the *requested* model and effort
   separately from what it *observed*. Anything Relay can't observe is journaled as `unverified`.

### Status and monitoring
- Claude's model policy and Zcode's configuration stay unchanged.
- The monitor covers every supervisor role on both backends.
- `relay status --json` and Relay Watch show the following, all read from the journal and
  state:
  - the active backend;
  - any pending switch;
  - usage-limit pauses;
  - the policy version;
  - Claude Opus counters and Codex High counters, plus assessment counts, kept separate.

---

## Part 5 — Acceptance

Adopted from Codex's plan §5:
1. Regression tests for every R3 finding.
2. A fake-driver run of **Claude → Codex → Claude** across 2 milestones, including:
   - review corrections, with the coder quoting the review;
   - stable issue IDs and context rollover;
   - partial outputs and late sentinels from the old attempt, which must be rejected;
   - crash recovery with persisted baselines.
3. Switch cases:
   - during coding;
   - on a quota stop before and after a sentinel;
   - wrong directory;
   - unverified model or effort;
   - the app isn't running;
   - a replayed switch record.
4. Effort-routing tests:
   - Low is allowed only where the policy allows it.
   - High needs a fresh `QUALIFIED` assessment that is used once.
   - The turn stays at Medium when the assessment is missing, `NOT_QUALIFIED`, stale (changed
     input hashes), malformed, or for a different scope.
   - The policy files are missing or their hashes don't match → Codex supervisor refused, and
     no Claude-rule fallback.
   - The live picker label differs from `GPT-6.1-Sol` → pause.
   - Switching waits for the quiet period, keeps incomplete changes, and blocks while their
     safety is unknown.
5. The full Python and Swift suites, plus `pyflakes`.
6. Human-assisted pilot on macOS 27.2:
   - calibrate Claude, Codex and Zcode;
   - run a toy project with one forced switch;
   - compare Relay Watch with `relay status --json`;
   - repeat the energy check.

**Deliverables:**
- separate repair and feature commits;
- one patch;
- sha256s of the changed files;
- updated `docs/REVISION_STATUS.md`;
- an updated handoff.

---

## Decisions recorded
The four questions were answered by the human on 2026-10-21. See Part 4, "Human decisions", and
the switching rules in Part 3. If a new question comes up while building, stop and ask.
