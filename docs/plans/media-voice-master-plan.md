# Master Plan: `media-voice`, a skill that makes LLM output read like a pro newsroom (Japanese and English)

> This plan is written to be run in a **local Mac session**, which can reach Google Drive files and the GLM API directly.
> This cloud session only commits the plan to the repo (Phase 0), so that the local session can `git pull` it.
> Self-audited on 2026-10-13 (`anthropic-skills:self-audit`, Light tier). The fixes are folded into this version.

## Context
**Problem.** The user runs media summaries, content digests and paper digests on **GLM 5.3 Flash (high)**. The Japanese output often reads stiff or machine-like: long stacked clauses, a lot of kanji, unclear subjects. English output has the matching problems: hedging, filler, and buried leads.

**Assets.** The user has two hand-picked corpora of professional writing aimed at a mass audience:

| Language | Outlet | Local path (Mac) | Drive folder ID (fallback) |
|---|---|---|---|
| Japanese | NewsPicks (`plainTXT_np_*.md`) | `/Users/16macbpro/My Drive/Medias/Newspicks_GPT_summary/full_plain_TXT` | `1bismlFfm2dbGQ5IjkUISqnI_5S6mwYSF` |
| English | The Information (`plainTXT_ti_*.md`) | `/Users/16macbpro/My Drive/Medias/THE INFORMATION/full_plain_TXT` | `1xgrWS1Bu5HEiNzEgNb6XMWltqci2gEim` |

The file format was checked on one sample from each folder:
- a markitdown header
- then the article body inside one ```` ``` ```` block
- then site boilerplate that must be stripped. In English this is the author bio, "Most Popular" and the footer. In Japanese it is the ⭐️バックナンバー block and the 執筆/編集 credits.

The number of files in each folder has not been counted yet. That is the first step of Phase 1.

**Goal.**
1. Learn these patterns and turn them into one skill for Japanese and English prose deliverables.
2. **Quantify** how much has been learned.
3. **Prove**, with a blind before/after test on GLM 5.3 Flash, that the skill meaningfully improves the user's three tasks without hurting accuracy.

**Decisions already made by the user:**
- one bilingual skill
- a new plugin in this repo (`chriswangplus-boop/claude-code-one`)
- standalone, so no other skill is edited
- the test covers media summary, content digest and paper digest

**Trigger scope (default chosen by the plan; the user can change it).** The skill loads for *prose meant for readers* in Japanese or English: summaries, digests, briefings, reports, articles, and explanations of a paragraph or more. It does **not** load for code, commit messages, logs, or short chat replies. Firing on every Japanese or English output would make it fire on almost everything.

## Related assets to respect (do not duplicate or conflict)
- `digest-paper-pipeline` already has `references/plain-language-playbook.md`, which covers term triage, plain anchors, human-scale numbers and a 高校2〜3年生 target. `media-voice` handles **voice and sentence craft**; the playbook handles **concept density**. Read the playbook first, and state in SKILL.md that its rules win when the two overlap.
- `summary-monitor` and the user's Summary Pipeline instructions set compression targets and the output schema. `media-voice` must not change length targets or schema. It only changes how sentences read.
  - `summary-monitor` Rule 1 says one main session writes every summary, one file at a time. That rule governs production runs. The A/B harness in Phase 5 is evaluation, not production, so batch API calls are allowed there. Say this in the harness README.
- `writing-skills:journalism-ap` and `writing-skills:asd-ste100`: borrow only the parts below.

### Borrowed rules (and what was dropped)
From **journalism-ap**, keep:
- put the news first (inverted pyramid)
- a who/what/when/where lead
- neutral attribution: "said" / 〜と述べた / 〜によると
- no opinion words in the narration; quotes carry the opinion
- short paragraphs

Drop the AP-only mechanics: the "Aug." abbreviation, title capitalization, the rule to spell out 1–9. For English, the corpus's own conventions replace them.

From **asd-ste100**, keep:
- sentence-length caps, as a soft target based on the corpus
- one idea per sentence
- conditions placed first
- no noun stack of more than 3 nouns (in Japanese: no long の-chains or kanji compounds)
- no ambiguous pronouns
- one term per concept

Drop the controlled dictionary, imperative-only voice, the no-semicolon rule, and WARNING/CAUTION blocks.

---

## Repo layout (what ships vs. what stays dev-only)
```
plugins/media-voice/                      # ships: installed by users of the marketplace
  .claude-plugin/plugin.json              # same shape as plugins/context-window-monitor/.claude-plugin/plugin.json
  skills/media-voice/
    SKILL.md                              # trigger, shared core rules, workflow, verify checklist (≤ ~150 lines)
    references/japanese.md                # NewsPicks-derived rules + numeric targets + 4–6 before→after pairs
    references/english.md                 # The Information-derived rules + numeric targets + 4–6 before→after pairs
    references/corpus-stats.md            # fingerprint, sample sizes, saturation results, date
    prompt-pack/glm-system-ja.md          # compact (~800–1,200 token) rule list for GLM API calls
    prompt-pack/glm-system-en.md
dev/media-voice/                          # does not ship: dev tooling and evaluation
  README.md  requirements.txt             # fugashi, unidic-lite, textstat, numpy, scipy
  tools/ prep_corpus.py  style_metrics.py  run_ab.py  judge.py  report.py
  eval/ tasks.yaml  results/              # scores and charts only
  eval/corpus/  eval/outputs/             # never committed
.gitignore                                # new file; ignores dev/media-voice/eval/corpus/ and eval/outputs/
```
**Copyright rule.**
- No article text, and no model output derived from articles, is committed. This holds whether the repo is public or private.
- Skill examples are written in our own words; quote at most short phrases.
- Full example pairs appear only in the private review artifact.

## Phase 0: Hand-off (cloud session, right after approval)
1. Copy this plan to `docs/plans/media-voice-master-plan.md`.
2. Commit it on `claude/ecstatic-lamport-alhmos`, push, and open a draft PR.
3. The local session then runs `git fetch && git checkout claude/ecstatic-lamport-alhmos` and continues from Phase 1.

## Phase 1: Corpus prep and split (local)
1. **Count and clean.** `prep_corpus.py` counts the files, strips boilerplate, removes duplicates (by content hash), and writes clean bodies to `eval/corpus/{ja,en}/`.
2. **Register split (Japanese).** Tag each NewsPicks file as a *written article* or a *dialogue/video transcript*.
   - A file counts as a transcript if ≥ 20% of its lines start with a speaker label (e.g. `名前：`) or quote marks.
   - Spot-check 10 tagged files by hand.
   - Rules come from written articles only. Transcripts are measured separately and reported.
3. **Split by absolute counts, with a fixed seed.**
   - The held-out test needs **26–30 articles per language**: 10 single articles for the summary test plus 4 digest bundles × 4–5 articles (16–20).
   - Calibration gets 15% of what remains, at least 15 articles.
   - Everything else goes to the learn set, which needs at least 40 articles for the saturation curve.
   - Minimum total per language: 26 + 15 + 40 = 81 clean articles. Below that, shrink the content-digest bundles to 3 articles (test needs 22) and record the reduced counts.
   - Test articles are never read while rules are being written.

## Phase 2: Style fingerprint (what "learning" means in numbers)
`style_metrics.py` computes **automatic** measures only. Anything that needs judgment goes to the Phase 5 judge.

**Japanese measures:**
- sentence length in characters (mean, p90)
- share of kanji, hiragana and katakana
- です/ます vs だ/である sentence endings
- 「」 marks per 100 sentences
- longest の-chain per sentence
- how often numbers are human-scaled, e.g. 2880億ドル（約45兆円）
- share of question subheads (なぜ…のか)
- density of linking words (しかし／つまり／では)
- density of opinion and hedge words

Morphology uses `fugashi` + `unidic-lite`.

**English measures:**
- words per sentence (mean, p90)
- sentences per paragraph
- Flesch–Kincaid grade (`textstat`)
- share of passive voice (regex on be + past participle)
- share of each attribution verb
- hedge words (may/might/could/potentially) per 1,000 words
- banned filler ("notably", "importantly", "it's worth noting")
- number and money formats ($100 million, Oct. 5)

**Two metric groups.** A summary is a different kind of text from a full article, so paragraph and article structure differ by nature.
- **Sentence-level metrics** (length, script mix, hedges, attribution, の-chains, number format) are scored for any text.
- **Structure metrics** (paragraph length, subheads) are scored only against the target pipeline's own output schema, never against article norms.

**Style score (0–100), per metric:**
- 100 inside the corpus's 10th–90th percentile band.
- Outside the band, the score drops by 25 points per band-width of distance, with a floor of 0. The band-width is the gap between the 10th and 90th percentiles.

The overall score is the equal-weight mean of the applicable metrics. Weights stay fixed once Phase 3 starts, so later scores stay comparable.

## Phase 3: Quantify learning progress (the user's explicit ask)
Measure progress in three ways and draw each as a chart in the final report.

1. **Saturation curve (how much of the corpus we need).**
   - Compute each metric's target using n = 5, 10, 20, 40, … learn-set articles, with 1,000 bootstrap resamples.
   - A metric counts as **learned** when its 95% confidence interval is narrower than 10% of the corpus band-width and moves less than 5% of the band-width between steps. Using band-width avoids the division-by-near-zero problem for rare features such as hedge words.
   - Report: "X of Y metrics saturated at n = N articles."
2. **Rule coverage (how much the rules explain).**
   - Every rule in the skill is tied to at least one metric, or to a pattern count from the learn set.
   - Report the share of rules backed by evidence, and the share of measured patterns the rules cover. The goal is 100% / ≥ 90%.
3. **Discrimination check (does the score see "pro" style).** This needs GLM, so it runs once API access is set up (see Phase 5).
   - Calibration articles should score high.
   - Baseline GLM summaries of the same articles should score lower. Use sentence-level metrics only.
   - Report the AUC: the chance that a randomly picked calibration article outscores a randomly picked baseline summary. It should be ≥ 0.85; if not, revise the metrics before Phase 4.

The **skill-version curve** (score per skill draft) is built in Phase 4, because that is when skill drafts exist.

## Phase 4: Write the skill
Follow `superpowers:writing-skills` (RED → GREEN → REFACTOR) and check with `anthropic-skills:skill-creator`.

- **SKILL.md** contains:
  - the trigger scope (see Context)
  - the shared core rules (the borrowed subset above)
  - the workflow: detect the output language → load the matching reference → draft → run the verify checklist
  - a short "do not" list
- **Reference files** contain:
  - numeric targets from Phase 2
  - a pattern catalog: headline, lead, subhead, jargon gloss, number formatting
  - before→after pairs
- **Prompt pack.** GLM Flash follows short, concrete rule lists better than long prose. The prompt pack is a condensed checklist plus 2 before→after pairs, and is what gets injected into GLM. The full SKILL.md is for Claude Code sessions, which may also run on GLM through `glm-model-switch`.
- **Skill-version curve.**
  - Run each prompt-pack draft (v0.1, v0.2, …) through GLM on **calibration** articles only.
  - Log the style score and the fact-check result in `eval/results/progress.csv`.
  - Stop iterating when one version improves the score by less than 3 points.
- Register the plugin in `.claude-plugin/marketplace.json`, following the existing `context-window-monitor` entry.

## Phase 5: Before/after test on GLM 5.3 Flash (local)
**Model access:**
- Read the key from `~/.config/glm-switch/key.env`, the file `glm-model-switch` already uses. Never print it.
- Look up the exact model ID for "GLM 5.3 Flash" and how to set "high" thinking in Z.AI's current docs. **Not yet verified.**
- Use the user's **production settings** (temperature and thinking level), so the test matches real use.
- Make 3 runs per item. The item's score is the mean of the 3 runs, and the item's winner is the majority of the 3 runs.

**Judge access.** The judge must be a Claude model, not GLM, so GLM is not grading itself. If the local session runs on GLM, switch back with `glm-model-switch to-claude`, or run `judge.py` through a Claude subagent.

**Baseline prompts (condition A).** Locate the user's current prompts in the local project files:
- the Summary Pipeline instructions (media summary)
- the user's content-digest prompt
- the `digest-paper-pipeline` writing-stage prompt (paper digest)

If a prompt cannot be found, stop and ask the user. **Never invent a baseline.**

**Test set (held-out only, 34 items):**

| Task | Items | Source |
|---|---|---|
| Media summary | 10 JA + 10 EN | held-out single articles |
| Content digest | 4 JA + 4 EN | each item bundles 4–5 held-out articles into one briefing |
| Paper digest | 3 papers × JA + EN | papers from the `digest-paper-pipeline` input folder (path to locate locally) |

**Conditions:**
- **A, before:** the baseline prompt.
- **B, after:** the same prompt plus the `media-voice` prompt pack.

Both conditions use the same model, the same settings and the same inputs.

**What is measured:**
1. **Style score** (Phase 2, sentence-level metrics) and readability: Japanese sentence length and kanji share; English FK grade.
2. **Accuracy guard:**
   - automatic check that every number, name and date in the output appears in the source
   - Claude-judge count of factual errors and omissions against the source
   - B must not have more errors than A
3. **Blind pairwise preference.** The Claude judge sees A and B in random order with labels hidden, and answers two questions: "which is clearer for a general reader?" and "which is more faithful?" This also covers the judgment-based patterns, such as whether the first sentence of a paragraph states its main point.
4. **Human blind review.**
   - A private artifact page shows 12 random A/B pairs with labels hidden, and saves the user's picks in its database.
   - The skill-writing session reads the picks back with `ArtifactData`.
   - The user's picks are the final word.
5. **Compression unchanged:** the length ratio of B to A stays within ±10%, so the skill does not wreck pipeline targets.

**Statistics:**
- Wilcoxon signed-rank test on the paired style scores.
- One-sided binomial test on the win count, plus a 95% Wilson interval for the win rate.
- With 34 pairs, B must win at least 23 (about 68%) to beat a coin flip at p < 0.05 (computed: P(X ≥ 23) = 0.029; P(X ≥ 22) = 0.061). If items drop out, recompute the threshold.

**"Meaningful improvement" bar (set before running):**
- style score +15 points or more
- blind win rate ≥ 70%, which also clears the p < 0.05 threshold
- B has no more factual errors than A
- length within ±10%

**If the bar is missed:**
1. Revise the prompt pack using the calibration set only.
2. Re-test once on the held-out set.
3. Report both runs. The second run has seen one round of feedback, so it is a weaker test.

## Phase 6: Report and ship
- Build `dev/media-voice/eval/results/REPORT.md` and a private HTML artifact covering:
  - saturation curves
  - the AUC
  - the skill-version curve
  - before/after scores for each task and language
  - known limits and anything unverified
- Example pairs go in the artifact only, not in the repo.
- Run `python -m json.tool` on `plugin.json` and `marketplace.json`, check that SKILL.md frontmatter has `name` and `description`, and run `git status` to confirm no corpus or output files are staged.
- Commit, push to `claude/ecstatic-lamport-alhmos`, and update the draft PR.

## Risks and open items
- **[UNVERIFIED]** The GLM 5.3 Flash model ID and thinking parameter. Check them in Phase 5.
- **[UNVERIFIED]** The paths to the baseline prompts and the paper input folder. Locate them locally, and ask the user if missing.
- **[UNVERIFIED]** The corpus size. If it is small, the Phase 1 split shrinks.
- An LLM judge can be biased. The user's own blind review is the final check.
- 34 items means wide confidence intervals. Report them honestly instead of overstating.
