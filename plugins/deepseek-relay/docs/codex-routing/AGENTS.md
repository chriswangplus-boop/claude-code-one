# Codex Operating Policy — GPT Routing v3.0

Version: 3.0.0 · Released: 2026-10-21

## Authority and scope

This is the user-defined routing policy for Codex main sessions, subagents, nested delegations, plugins, skills, workflows, CLI launches and their templates. It operates within platform/system/developer constraints and later explicit user instructions. On Codex GPT sessions, it supersedes older GLM/Claude model and effort instructions embedded in components. Domain procedures, quality thresholds, independent verification and explicitly selected external evaluation backends remain in force. Claude and ZCode retain their own platform policies.

Machine-readable model/effort and agent defaults: `~/.codex/routing-policy.json`. This document owns task eligibility and delegation rules. `~/.codex/model-routing-strategy-v3.0.md` explains execution and workflow mapping. A disagreement is configuration drift to report and correct, not permission to silently choose another model.

## Model and effort

| Policy tier | Literal model | Literal effort | Use |
|---|---|---|---|
| Apex | `gpt-6.1-sol` | `high` | Restricted qualifying assignment only |
| Opus — default | `gpt-6.1-sol` | `medium` | Substantive implementation, analysis, synthesis, review and ordinary coordination |
| Sonnet | `gpt-6.1-sol` | `low` | Bounded mechanical work against clear specifications with meaningful checks |

Historical agent names are compatibility identifiers, not model families. Unknown or ambiguous work defaults to Medium. Only `low`, `medium`, and `high` are permitted by this policy; `xhigh`, `max`, `ultra` and omitted effective settings are not permitted. Do not silently fall back to GLM, another GPT model, or an inherited effort.

## Apex gate — five categories only

Apex is never a default. A High assignment must be BOTH difficult/high-impact AND within exactly one declared category:

1. hardest problem solving;
2. strategy;
3. decision making;
4. bug fixing;
5. master-plan writing.

Before dispatch, record `apex_category` and `apex_justification`: the concrete uncertainty, interacting constraints, unresolved cause, consequential tradeoff or dependent workstreams that make Medium insufficient. A routine choice is not Apex merely because it is a decision. Tags, role names, file counts, repeated workflow rounds and premium workflow labels do not authorize High. Missing justification means use Medium or surface the unresolved classification, never silently dispatch High.

Qualifying examples: hard architecture strategy; a consequential master plan spanning dependent workstreams; difficult unresolved root-cause diagnosis after a substantive attempt. Routine fixes, sprint breakdowns, reviews, security/financial analysis and coordination stay Medium unless their actual assignment independently satisfies the gate.

Return to the normal tier when the qualifying assignment ends. Classify children separately: an Apex parent may delegate checked extraction at Low and independent review at Medium. Never inherit Apex permission.

## Role routing

Classify the task first; choose the agent and decide whether delegation is useful second.

| Responsibility | Default | Conditional routing |
|---|---|---|
| `strategist-opus`, `planner`, `pge-planner` | Medium | High only for qualifying strategy, decisions or master-plan work |
| `pge-orchestrator`, research/experiment orchestration | Medium | High only for a separately bounded qualifying task; ordinary coordination stays Medium |
| Consequential implementers, `explorer-opus` | Medium | Difficult qualifying problem/bug tasks may use High |
| Reviewers, verifiers, evaluators, security auditors | Medium | Independent checks remain required where consequential; role alone never selects High |
| Specification/type analysis, accessibility, performance/test engineering, release engineering | Medium | Fixed, checked execution subtasks may use Low |
| `explorer-sonnet`, `implementer-sonnet`, fixed-spec generators | Low | Escalate to Medium when ambiguous, consequential or judgment-heavy |
| Simplification, routine documentation/comment work | Low | Substantive interpretation, design or judgment uses Medium |
| Self-audit (all correctness, consistency and cleanliness rounds) | Medium | Isolate any discovered qualifying difficult bug/problem/decision as a separate High assignment |

The `apex` agent is the dedicated High lane, not a general-purpose default. Agent files carry explicit model/effort; names and registry defaults do not authorize an ineligible assignment. Never assume an agent-type override works when the runtime fixes that role's model. Use a supported general-purpose override or the installed Apex configuration. If the required lane is unavailable, report the limitation and remain within a valid lane; do not claim a requested tier ran.

## Low gate and safe work

Low requires bounded scope, clear inputs/outputs, an established approach and meaningful checks. Examples: enumerable reads, fixed-schema extraction, straightforward fixed-spec edits, routine docs and tests implementing already-defined behavior. Test strategy, substantive code review and ambiguous synthesis use Medium.

Before mechanical implementation, evaluate: S1 reversible; S2 script-verifiable; S3 no downstream consumption before verification; S4 fails loudly. A failure routes implementation to Medium and adds an appropriate independent verifier. Risk changes verification requirements; it does not automatically authorize High. Preserve security-sensitive independent review and verifier independence (the grader must not be the writer).

## Delegation and efficiency

Stay in-session for small sequential work and known-path reads. Delegate when context isolation, independent verification, substantial exploration, specialist responsibility or useful parallelism justifies overhead. Batch independent reads; do not duplicate an explorer's completed survey. No hedge agents solely to increase perceived confidence. Prefer measured account usage over old provider quota assumptions; do not invent call/token costs.

Substantial unfamiliar exploration (15+ files), usage maps and pattern surveys normally use a bounded explorer. Failed coverage can escalate to Medium exploration; High still requires the Apex gate. A clear fixed-spec implementation may use Low; consequential work requires Medium and independent verification. Keep ownership explicit and preserve concurrent edits.

## Dispatch and evidence contract

Each dispatch identifies its task, expected effort, literal model, verification/backstop and, for High, Apex category and justification. Pin `model="gpt-6.1-sol"` plus literal `reasoning_effort` where the API supports it. In APIs where overrides require a fresh fork, use the supported fresh-context mechanism; do not invent parameters. Record the effective configuration when a fixed role controls execution.

Audit task eligibility, model and effort separately. A configured value is not observed execution. Explicit dispatch values override global subagent defaults and parent inheritance. Fixed custom-agent TOML model/effort settings take precedence over spawn values, independently per setting; use a different valid lane when needed. Tool results/session evidence determine what actually ran. Missing classification, unknown overrides, unavailable logs or unavailable execution confirmation remain unverified. Never claim a clean audit from recollection or from zero visible calls in an incomplete transcript.

If three or more subagent invocations were used, append a brief routing audit listing purposes and any misses. One or two calls do not need a prose routing appendix. The routing-audit skill uses the Codex registry/ledger branch on Codex.

## Workflow stages

- Portfolio: checked gathers, media digests and event extraction Low; valuation, macro synthesis, substantive audit and fresh verification Medium. Difficult contested decisions can qualify for Apex; preserve all data/domain gates.
- PGE: ordinary planning and coordination Medium; fixed-spec generation Low; evaluation Medium. Master-plan/strategy stages can use High only through the gate. Keep thresholds, number of evaluators and explicitly selected external backends unchanged. Names such as strict/god/ultra never select effort.
- Self-audit: all three rounds Medium; optional purely mechanical script cleanup is Low when bounded and verified. Do not relabel ordinary auditing as decision making to select Apex.
- All other skills/plugins: translate work by responsibility through this policy, including nested agents and CLI launch flags. Their non-Codex vendor-specific model examples are not Codex routing instructions.

## Updates and compatibility

Main session and GPT profile default: `gpt-6.1-sol` / `medium`. Preserve Claude/ZCode branches and explicit external backend choices. Update canonical local sources, then refresh Codex installations through supported sync/release mechanisms; never rely on edited managed caches. Keep backups, per-file hashes, release notes and rollback instructions. Validate installed behavior in a fresh chat because already-loaded role definitions can remain stale.
