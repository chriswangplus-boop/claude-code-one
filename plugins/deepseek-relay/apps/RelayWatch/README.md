# Relay Watch

A read-only menu-bar view of a Relay project. It shows what the loop is doing from Relay's own
records — the hash-chained journal, `STATE.json`, the baton, heartbeats and the pending decision.
It never writes in the project, never answers a decision, and never touches the network.

- **Faithful:** every value comes from a file Relay wrote. Nothing is estimated: no percentage,
  no finish-time prediction. The event inspector shows the journal line each row came from. If a
  file is unreadable, the app shows the error instead of a guess.
- **Read-only:** all file access goes through one `RelayFolder` type that has no write APIs. The
  app shows the command to run for a decision, never the one-time code, so it cannot become a
  decision channel.
- **Low energy:** `FSEventStream` with 3 s latency, incremental byte-offset reads, a
  meaningful-event filter, no polling loop, no animation, no network. A 30 s heartbeat timer runs
  only while the window or menu is open.

## Requirements

- macOS 14 or later (tested on macOS 27.2, arm64). `MenuBarExtra` and `@Observable` need 14.
- Xcode 27 / Swift 6.4 (any Swift 5.9+ toolchain works).
- arm64 only (`swift build --arch arm64`, or the script below).

## Build and run

Development (no sandbox; reads the project through the path you pick or `RELAY_WATCH_PROJECT`):

```bash
cd apps/RelayWatch
swift build
swift run RelayWatch
```

For the documented energy test, point it at a fixture project:

```bash
RELAY_WATCH_PROJECT=/tmp/relaywatch-energy swift run RelayWatch
```

App bundle, sandboxed, ad-hoc signed with the entitlements in `RelayWatch.entitlements`:

```bash
cd apps/RelayWatch
./scripts/build-app.sh
open .build/RelayWatch.app
```

For distribution, sign with a Developer ID instead of `--sign -`; the sandbox entitlement is
applied by the script.

## First use

1. Click the menu-bar glyph and choose **Choose project folder…** (or **Open Relay Watch** first).
2. Pick the folder Relay runs in. The app stores a **read-only** security-scoped bookmark; it
   re-prompts if the bookmark goes stale.
3. Leave it running. Relay Watch redraws when the journal changes and only then.

The window shows:

- **Status header:** phase, round, milestone, role, baton holder, the current turn's model and
  effort ("Sonnet 5.5 · high" in blue, "Opus 5.5 · medium" in purple), and the last update with
  its event ID.
- **Health lights** (hover for the exact source): orchestrator heartbeat (<2 min green, <5 min
  amber, red after), monitor heartbeat (same), the journal `prev_hash` link check plus the latest
  `monitor.check` verification, and the HALT/PAUSE/STOP flags.
- **Needs you** when a decision is pending: the question, options and the exact `relay decide`
  command with a copy button. The one-time code is never shown.
- **Round timeline:** one row per round from `turn.start`/`turn.end` with role, model, outcome,
  duration, files changed, commit and context %, plus markers for rejected sentinels, nudges,
  rollovers, stalls, fix plans and Opus escalations. Click a row for its raw event JSON.
- **Usage today:** DeepSeek spend from `deepseek.call` costs against your config budget, the Opus
  counters with their limits, and the Sonnet-to-Opus turn ratio.
- **Alerts:** integrity violations, monitor halts/corrections/notes, guard trips and errors, each
  with its event ID.
- **Event inspector:** pretty-printed raw JSON, read-only. Screenshots, prompts and file contents
  are never shown.

Menu-bar glyphs: `circle.fill` green (running, fresh heartbeat), `pause.circle` (paused),
`questionmark.circle.fill` orange (waiting for you), `exclamationmark.octagon.fill` red (HALT,
chain failure or error), `checkmark.circle` (done). The round number sits next to the glyph.

The app never activates itself, never opens a window on its own, and sends no notifications.
Relay's window-only screenshots means the viewer can never appear in a crop sent to DeepSeek.

## Tests

```bash
cd apps/RelayWatch
swift test
```

20 tests cover Part C's C9 1–5:

1. every event type in `docs/EVENTS.md` decodes from the committed fixture, plus one unknown type
   and unknown fields;
2. the incremental reader holds half a line, delivers it exactly once when completed, switches
   day files at midnight and loads older days on demand;
3. a deleted or inserted line fails the chain check and names the failing event ID;
4. 100 `gui.*`/`vision.check` events cause zero refreshes; one `turn.end` causes one;
   `monitor.check` only refreshes when its result changes;
5. a full read session leaves every file size and modification time unchanged (all access goes
   through `RelayFolder`, which has no write APIs).

`tools/make_event_fixtures.py` (in the plugin root) regenerates the fixture from a real
fake-driver run plus real monitor/decision/guard paths.

## Energy check (C9 6)

Method (the numbers below were taken with the menu-bar item idle and the window closed):

1. Build with `swift build -c release`.
2. Run `RELAY_WATCH_PROJECT=<fixture project> <binary>` and leave it for 30 minutes without
   opening the window.
3. Read CPU time with `ps -o etime,time,%cpu -p <pid>` and, in Activity Monitor → Energy,
   "Energy Impact" and "Idle Wake Ups".

Observed on 2026-10-21, macOS 27.2 arm64, Apple Silicon, debug build (SwiftPM `swift build`),
menu-bar item only, window never opened, started 15:20:07 JST:

| Measurement | Value |
|---|---|
| Wall time | 30 min 10 s |
| CPU time (`TIME`) | 0.75 s |
| Average CPU % | ≈ 0.04 % (0.75 s / 1810 s) |
| Instantaneous `ps %CPU` at the sample | 2.7 % (the 1 s snapshot caught a wake; the lifetime average is the 0.04 % above) |
| Resident memory | 118 MB |
| Activity Monitor "Energy Impact" | not readable headlessly — see limitation |
| Activity Monitor "Idle Wake Ups" | not readable headlessly — see limitation |

The app read a fixture project (two day files, 297 events) and wrote nothing: the fixture folder
still contains only the files created before launch. A release build should be at or below these
numbers; re-run the 30-minute test with Activity Monitor on the Mac that will run it.

The FSEvents stream with `latency = 3.0` wakes the app only when Relay writes; the 30 s timer is
invalidated when the UI closes, so an idle app should have no timer wake-ups at all. Re-run the
30-minute test with the Activity Monitor numbers on the Mac that will run it, and record them
here.

## What I could not verify

- Activity Monitor's Energy Impact and Idle Wake Ups fields: they are UI-only. The CPU-time sample
  above is the headless proxy; the full 30-minute Activity Monitor reading should be repeated.
- The sandboxed app bundle reading a real Relay project end to end: the bookmark path is
  implemented and ad-hoc signed, but the bookmark picker flow needs a human click. The core was
  tested against real journal fixtures.
- Relay Watch against a live multi-day Relay run (the fixture is a single-day slice plus a
  synthesized previous day).
- Any API newer than macOS 14: none is used. `MenuBarExtra`, `Grid`, `Window(id:)` and
  `NSOpenPanel` are all macOS 14-safe.
- The C3 manual check ("every header value matches `relay status --json` at the same moment")
  against a live fake-driver run: the values come from the same files, but the side-by-side
  screenshot has not been taken.

## Files

```
Sources/RelayWatchCore/   decoding, read-only folder, incremental reader, chain check, filter,
                          snapshot, timeline/usage/alerts/health
Sources/RelayWatch/       menu bar app, FSEvents watcher, controller, views
Tests/RelayWatchCoreTests tests (C9 1–5)
Tests/Fixtures/           journal-sample.jsonl generated by tools/make_event_fixtures.py
Info.plist                LSUIElement, bundle identity
RelayWatch.entitlements   sandbox, read-only user-selected files, app-scope bookmarks; no network
scripts/build-app.sh      release build + .app bundle + ad-hoc codesign
```
