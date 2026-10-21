# Relay revision plan v0.4, for DeepSeek to build

**Sources:**
- Codex's `CODEX_PLAN.md`, kept verbatim in this folder.
- Claude's `CLAUDE_REVIEW_3.md`.

This plan **adopts Codex's direction**, Codex desktop as a switchable supervisor, and changes it
in four places:
1. The order of work: repairs first, then a simpler first version of switching.
2. How Codex chooses its effort level.
3. Where the one-time code is shown, because an agent with computer use could read it on screen.
4. Two facts this plan can't confirm: the model name and the Codex routing policy. The human must
   supply them.

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

### Confirm with the human before writing code
1. **The model name.** Codex's plan pins `gpt-6.1-sol`. This plan couldn't verify that name. Use
   it only if the Codex app shows exactly that label during calibration. Otherwise pause and ask.
2. **The routing policy.** Codex's plan says "use your canonical GPT Routing v3.0 policy". That
   document isn't in this repo, so DeepSeek can't follow it. **The human must add it** as
   `docs/CODEX_ROUTING.md`. Relay records its version and sha256 in every Codex turn.

### Default rules if the human doesn't supply a policy (recommended)
These mirror the Claude policy the human already approved, so there's one mental model:
- **Medium** for all substantive planning, review, strategy and fix work. A first plan or a
  repeated failure does not authorize High by itself.
- **Low** only for harness-generated mechanical prompts: seeds, nudges and handover reading.
- **High** only by the same deterministic triggers as Opus:
  - first master plan;
  - Sonnet/Medium re-plan failed;
  - hardest-bug streak of 4, or blocked twice.

  High has its own limits (default 1 per milestone and 3 per day), and Relay asks the human when
  a limit is reached.

### Codex's alternative: a High qualification gate
Before every High turn, run a separately counted Medium assessment. It returns `QUALIFIED` or
`NOT_QUALIFIED`, together with:
- the category;
- evidence;
- the scope;
- hashes of its inputs.

Each assessment is used once.

This plan **does not** build that by default. It adds a model turn before every escalation, so
it costs tokens, and it adds a second judgment path. Build it only if the human chooses it
(question 3 below).

### Both options
- Record the *requested* model and effort separately from what was *observed* on screen. If the
  observation can't be made, journal it as `unverified`.
- Claude's model policy and Zcode's configuration stay unchanged.
- The monitor covers every supervisor role on both backends.
- `relay status --json` and Relay Watch show, all read from the journal and state:
  - the active backend;
  - any pending switch;
  - usage-limit pauses;
  - separate Claude Opus counters and Codex High counters.

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
4. Effort-routing tests for whichever option the human chooses.
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

## Questions for the human (answer before Part 4)
1. Is Codex's model really `gpt-6.1-sol`? What exact label does the Codex app show?
2. Will you add the "GPT Routing v3.0" policy text to the repo? Otherwise Relay uses the default
   rules above, which mirror the Claude policy.
3. Do you want Codex's separate "High qualification" assessment turn? It's stricter but costs an
   extra Medium turn before every High.
4. Is version-1 switching (at safe points and on quota stops) enough to start? Mid-turn stop
   would come later.
