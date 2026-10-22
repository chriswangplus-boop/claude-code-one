# Relay journal events (schema for Relay Watch)

This is the contract between the Python harness and relay Watch (Part C). It lists the events
Relay writes to `.relay/journal/<day>.jsonl`, the `detail` fields the viewer relies on, and
whether the event should redraw the UI.

The journal is append-only and hash-chained. Every line has these fields; the viewer decodes all
of them and ignores unknown ones:

| Field | Meaning |
|---|---|
| `id` | `evt-YYYYMMDD-NNNNN`, unique and ordered |
| `ts` | ISO 8601 with timezone |
| `round` | round number (0 when not tied to a round) |
| `state` | phase at the time, or `null` |
| `actor` | `orchestrator`, `monitor`, `harness`, `human`, or a role |
| `type` | the event type in the table below |
| `detail` | object; the fields below are the ones Relay Watch reads |
| `inputs`, `outputs`, `screenshot`, `deepseek`, `duration_ms`, `result`, `error` | optional |
| `prev_hash`, `hash` | chain links |

`Refresh` is `yes` when the event type is in Relay Watch's meaningful-event filter (C4.3). Events
with `refresh: monitor.check` redraw only when `result` or `detail.journal_ok` changes.

Unknown event types are kept, counted, and shown by the raw-event inspector; they never crash
the decoder. Unknown `detail` fields are ignored.

## Events

| Type | Refresh | detail fields |
|---|---|---|
| `init` | yes | `created`, `goal`, `git_commit` |
| `run.start` | yes | `pid`, `run_id` |
| `run.resume` | yes | `pid` |
| `run.stopped` | no | `reason`, `steps` |
| `project.done` | yes | `summary` |
| `state.transition` | no | `from`, `to` |
| `state.paused` | yes | `reason`, `return_to` |
| `state.baton_recovered` | no | `note` |
| `turn.start` | yes | `role`, `app`, `model`, `effort`, `model_verified`, `effort_verified`, `expected`, `pointer`, `trigger` |
| `turn.end` | yes | `role`, `model`, `effort`, `model_verified`, `effort_verified`, `status`, `verdict`, `summary`, `changed`, `ctx_pct`, `commit`, `trigger` |
| `turn.resume` | no | `role`, `expected`, `resumed` |
| `turn.resume_accepted` | no | `role`, `note` |
| `turn.nudge` | no | `role` |
| `turn.timeout` | yes | `role`, `expected` |
| `sentinel.seen` | no | `role`, `status`, `verdict`, `file`, `sha256` |
| `sentinel.rejected` | yes | `role`, `reasons` |
| `integrity.ok` | no | `changed`, `snapshot_sha256` |
| `integrity.violation` | yes | `kind` |
| `git.commit` | yes | `hash`, `message` |
| `session.open` | yes | `app`, `role`, `seeded` |
| `session.checkpoint.start` | no | `role`, `reason`, `checkpoint` |
| `session.checkpoint` | no | `role`, `reason`, `sha256` |
| `rollover.start` | no | `role`, `reason`, `checkpoint` |
| `rollover` | yes | `role`, `reason`, `checkpoint_sha256` |
| `checkpoint.oversize` | no | `role`, `lines`, `limit` |
| `model.escalated` | yes | `trigger`, `role`, `model`, `effort`, `issue_ids`, `streak`, `opus_turns_today`, `opus_turns_milestone`, `limit_bypassed` |
| `model.limit_reached` | no | `trigger`, `kind`, `data` |
| `model.decision_applied` | no | `action`, `role` |
| `model.requested` | no | `source` |
| `model.declined` | no | `role`, `issue_ids` |
| `fix.plan_requested` | no | `issue_ids`, `streak` |
| `fix.plan_archived` | no | `reason`, `archived`, `sha256` |
| `limit.cap_reached` | no | `kind`, `current`, `limit` |
| `limit.decision_applied` | no | `action`, `cap` |
| `blocked.cleared` | no | `paths` |
| `decision.asked` | yes | `id`, `question`, `options`, `reason` |
| `decision.answered` | yes | `id`, `answer`, `source` |
| `decision.waiting` | no | `id` |
| `decision.reminder` | no | `id` |
| `decision.timeout` | no | `id` |
| `decision.rejected` | no | `id`, `reason` |
| `decision.tamper` | yes | `id`, `reason` |
| `route.judgment` | no | `route`, `reason`, `drift_flags`, `call_id`, `suggest_escalation` |
| `route.decided` | yes | `base`, `target`, `brain`, `stall`, `summary` |
| `route.after_decision` | no | `note` |
| `stall.detected` | yes | `reason`, `replans` |
| `gui.focus` | no | `app`, `title` |
| `gui.click` | no | `app`, `target` |
| `gui.type` | no | `app`, `chars`, `submit` |
| `app.request` | no | `app`, `backend`, `role`, `kind`, `model`, `effort`, `chars`, `attempt_id`, `sent` |
| `gui.ring` | no | `role`, `app`, `chars`, `sent` |
| `gui.ring.retry` | no | `role`, `sent` |
| `gui.model` | no | `app`, `role`, `model`, `effort`, `model_verified` |
| `gui.new_session` | no | `app`, `confirmed_empty` |
| `gui.screenshot` | no | `app`, `tag`, `region`, `sha256` |
| `vision.check` | no | `kind`, `role` |
| `popup.seen` | no | `role`, `text`, `buttons` |
| `popup.approved` | no | `button` |
| `popup.escalated` | no | `text` |
| `recovery.start` | no | `app`, `max_actions` |
| `recovery.action` | no | `n`, `action` |
| `recovery.end` | no | `actions`, `app` |
| `recovery.retry` | no | `app`, `role` |
| `monitor.spawned` | no | `pid`, `log` |
| `monitor.check` | monitor.check | `findings`, `layer2`, `journal_ok`, `heartbeat_age` |
| `monitor.note` | yes | `code`, `severity`, `text` |
| `monitor.correct` | yes | `action`, `codes`, `reason` |
| `monitor.correct_applied` | no | `action`, `reason` |
| `monitor.halt` | yes | `reason`, `codes` |
| `monitor.error` | no | `error` |
| `monitor.layer2` | no | `score`, `verdict`, `summary`, `findings` |
| `deepseek.call` | no | `job`, `model`, `prompt_version`, `error` |
| `guard.tripped` | yes | `kind`, `pct`, `turns`, `limit` |
| `human.pause` | no | `reason` |
| `human.stop` | no | `reason` |
| `human.halt_cleared` | no | `previous_reason` |
| `journal.repaired` | no | `file`, `quarantined` |
| `notify.sent` | no | `channels`, `decision_id`, `errors` |
| `notify.error` | no | `error` |
| `error` | yes | `error` |
| `calibration.ok` | no | `app`, `working_dir` |

## What Relay Watch reads outside the journal

| File | Fields used |
|---|---|
| `.relay/STATE.json` | `phase`, `round`, `milestone`, `role`, `pending`, `pending_decision`, `counters`, `session_ctx`, `paused_reason`, `updated_at`, `opus`, `history` |
| `.relay/BATON.json` | `holder`, `role`, `round`, `since` |
| `.relay/HEARTBEAT.json` | `ts`, `epoch`, `phase`, `round` |
| `.relay/monitor/HEARTBEAT.json` | `ts`, `epoch`, `pid` |
| `.relay/PENDING_DECISION.md` | `id`, `question`, `options`, `ANSWER:` line (the answer code is never shown) |
| `.relay/HALT`, `.relay/PAUSE`, `.relay/STOP` | presence and `reason` |
| `.relay/config.json` | `daily_budget_usd`, `model_policy` (limits), `deepseek.price_*` |

## Fixtures

`apps/RelayWatch/Tests/Fixtures/journal-sample.jsonl` is produced by
`tools/make_event_fixtures.py` from a fake-driver run plus real monitor, decision, guard and
pause paths; a few rare events are appended with their documented shape. `tests/test_events_schema.py`
checks that the fixture covers every type in this file and that each documented `detail` field is
present. The Swift tests decode the same file.

## Part B additions (why Relay Watch shows the Opus share)

`turn.start`/`turn.end` carry `model`, `effort`, `model_verified` and `effort_verified`
(`true`, `false` or `"unverifiable"`), so the header and timeline can show "Sonnet 5.5 · high" or
"Opus 5.5 · medium", and an app without an effort control is recorded as unverifiable instead of
guessed.
`model.escalated` carries the rule that fired and both counters with their limits, which is what
the usage panel shows. `gui.model` records each picker verification, including failures. Nothing
here is estimated: if a field is missing, the viewer shows that it is missing.
