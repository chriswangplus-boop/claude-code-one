# Continue Relay development and add switchable Codex supervision

## Summary

Take over Claude’s independent review role, repair the remaining defects, then add **manual switching between Claude and Codex desktop** for planning, reviewing, strategy, and exceptional fixes. **Zcode remains the normal coder.**

The current files match commit `1bf9408`. Baseline verification passes: **195 Python tests and 24 Swift tests**. Fresh review found additional defects, so passing those suites is not yet sufficient for release.

## 1. Repair and verify the existing implementation

- Work from `deepseek-relay-review-2` in an isolated checkout, preserving the unrelated staged changes in the current Git checkout. Recover the missing master plan and review brief from that branch.
- Produce Review #3 with reproducible findings and regression tests before fixing them.
- Authenticate journal evidence completely before authorization: recompute event hashes and verify signatures, chain continuity, and the trusted anchor.
- Bind human decisions to their original question, options, action data, and return phase in external trust storage. Require authenticated acceptance evidence for CLI answers, file answers, waiting, and restart.
- Fix the undefined state reference in “Use Opus.”
- Persist authenticated file and Git-control baselines **before submitting work**. Reuse them after crashes and during checkpoints and rollovers. Legacy unfinished turns without a recoverable baseline pause for reconciliation.
- Replace broad popup argument checks with command-specific option allowlists, including `--option=value` paths and traversal cases.
- Reproduce and close Git filter/configuration execution paths before staging. Apply the same checks to normal turns, checkpoints, rollovers, and resume.
- Combine session seeding with the actual task where possible; account for every submitted model request, including assessments, nudges, retries, and checkpoints.

## 2. Add the Codex desktop backend

Reuse the existing calibrated `AppDriver` interface.

- Add Codex calibration, driver construction, rules, and a Codex sentinel. Preserve existing Claude filenames and behavior.
- Add `supervisor.backend`, defaulting existing projects to `claude`.
- Route planner, reviewer, strategist, and fixer through the selected supervisor. Track sessions, context, and round counts by **backend and role**.
- Pin the backend and a unique attempt ID in each pending turn. New sentinels must match both.
- Require calibration proofs only for the selected supervisor and Zcode, with both resolving to the exact project directory.
- Calibrate model selection and effort selection separately. Codex must visibly verify both before receiving a task; unavailable models or unreadable controls pause execution.
- Require Codex to use the shared local directory. A separate Codex worktree would contain different working files. See [OpenAI’s worktree documentation](https://learn.chatgpt.com/docs/environments/git-worktrees).

## 3. Implement safe manual switching

Expose:

```text
relay switch --to codex
relay switch --to claude
```

- Authenticate each switch through the existing native human-confirmation mechanism.
- Persist a replay-safe switch record bound to the project, source backend, target backend, and switch generation.
- Add a calibrated stop action and verified idle observation. A stop click alone is insufficient; unknown activity keeps the switch pending.
- If Zcode is coding, finish and validate that turn before changing supervisors.
- If the outgoing supervisor completed its turn, validate and accept it exactly once before switching.
- Otherwise, preserve incomplete outputs in a uniquely named recovery snapshot, label them unverified, and retry the same logical phase in a fresh target session with a new attempt ID. Preserve the original integrity baseline and count fixer changes across attempts.
- Never automatically commit, discard, or revert incomplete changes. Unsafe changes or unresolved human decisions remain blocking.
- Seed the replacement session from the existing plans, reviews, reports, decisions, and a harness-generated handover brief. Switching must not depend on the quota-limited app producing another checkpoint.
- Verified usage-limit messages pause and notify. Neither fallback nor switching back happens automatically.

## 4. Enforce Codex routing and expose accurate status

Use your canonical GPT Routing v3.0 policy, recording its version and hash.

- Pin `gpt-6.1-sol`.
- Use **Medium** for substantive planning, reviews, strategy, fixes, and coordination. First plans and repeated failures do not automatically authorize High.
- Permit **Low** only for bounded mechanical work meeting the policy’s checks.
- Before **High**, run a separately counted Medium assessment returning `QUALIFIED` or `NOT_QUALIFIED`. Qualification must identify exactly one permitted category, concrete difficulty evidence, bounded task scope, and referenced input hashes.
- Bind qualification to one assignment and consume it once. Missing, negative, stale, or malformed assessments keep the assignment at Medium. Reclassify subsequent work.
- Apply the same routing requirements to nested Codex agents. Record requested settings separately from observed execution; unavailable execution evidence remains unverified.
- Preserve Claude’s existing model policy and Zcode’s configuration.
- Update the monitor for every supervisor role and backend. Extend status output and read-only Relay Watch to show backend, pending switch, usage-limit pauses, and separate Claude/Codex usage information.

## 5. Acceptance and delivery

- Add regressions for every Review #3 finding.
- Exercise Claude → Codex → Claude across two milestones, including review corrections, stable issue IDs, context rollover, partial outputs, late sentinels, and crash recovery.
- Verify switches during coding, quota interruption before/after completion, wrong directories, unverified model/effort, unavailable apps, and replayed switch records.
- Test Medium/Low eligibility and one-use High qualification, including stale inputs and changed task scope.
- Run the complete Python and Swift suites, followed by independent security and correctness review.
- Complete user-assisted calibration and a toy-project pilot, then compare Relay Watch with CLI status and repeat the energy checks. **Live Codex verification requires user participation because this session’s Computer Use tool refused access to that app.**
- Deliver separate repair and feature commits, a patch, file hashes, updated review/master-plan documents, and a refreshed handoff. Synchronize the supplied project folder after verification.

Dispatch configuration for planning: exploration used Low; independent review and plan checking used Medium; all were pinned to `gpt-6.1-sol`.
