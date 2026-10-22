# Model Routing Strategy — v3.0

Version: 3.0.0 · Released: 2026-10-21 · Platform: Codex GPT

The active task/delegation policy is `~/.codex/AGENTS.md`; model, effort and role defaults are `~/.codex/routing-policy.json`. These form the single v3.0 policy. Historical v2.x rules are archived backups and do not govern GPT sessions.

## Resolve each assignment

1. Determine scope, ambiguity, consequence and verification. Bounded mechanical and checked work may use Low; all other ordinary work uses Medium.
2. High is allowed only for difficult/high-impact hardest problem solving, strategy, decision making, bug fixing or master-plan writing. Record the category and concrete justification. Unknown classification is Medium.
3. Use literal `gpt-6.1-sol`; Low=`low`, Medium=`medium`, High=`high`. No model-family aliases or silent fallback. No `max`, `xhigh`, `ultra`.
4. Delegate only for meaningful context isolation, specialist ownership, independent verification or parallelism. Pin supported dispatch settings and classify children independently.
5. For a qualifying High task use a supported general-purpose explicit override or `apex`. The old `*-opus` names default to Medium; planner/orchestrator names are not High authorization.
6. Independently verify consequential work. Keep task eligibility, configured settings and execution evidence distinct in reports.

## Role and stage defaults

The complete agent map lives in routing-policy.json. Strategist/planner/PGE-planner/PGE-orchestrator/self-audit use Medium unless a separately scoped assignment passes the Apex gate. Routine coordination and all normal self-audit rounds remain Medium. Fixed-spec implementation/exploration/generation uses Low; reviewers, evaluators, substantial analysts, security/accessibility/performance/test engineering and release judgment use Medium.

Portfolio checked gathers/digests/extraction Low; valuation/macro synthesis/fresh verification Medium. PGE fixed-spec generation Low; routine planning, coordination and evaluation Medium. Preserve each workflow's domain checks, evaluator counts, thresholds and explicitly selected external backends. Premium workflow names do not select effort.

## Execution interfaces

Agent TOML keys: `model` and `model_reasoning_effort`. Dispatch API keys where supported: `model` and `reasoning_effort`. CLI: `codex exec --model gpt-6.1-sol -c 'model_reasoning_effort="medium"' ...` (substitute only the classified permitted effort). GPT profile: `codex --profile gpt` reads `~/.codex/gpt.config.toml`.

Do not pass unsupported overrides to a fixed-model role. If a runtime cannot honor the requested lane, report it as unavailable/unverified. Existing sessions may retain pre-update role definitions: start a fresh chat for installed validation.

## Audit

Use the Codex branch of model-routing-audit. Registry defaults are configuration, not observed execution. Resolve global defaults → spawn values → fixed custom-role settings, then join available execution evidence; require explicit task classifications for eligibility checks. Missing evidence is unverified. A High call without the five-case category and concrete justification is not eligible. Do not infer eligibility from the agent's name or a tag.
