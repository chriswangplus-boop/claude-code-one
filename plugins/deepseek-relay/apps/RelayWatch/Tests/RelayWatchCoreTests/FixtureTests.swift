import Foundation
import XCTest
@testable import RelayWatchCore

enum Fixtures {
    static let documentedTypes: Set<String> = [
        "init", "run.start", "run.resume", "run.stopped", "project.done", "state.transition",
        "state.paused", "state.baton_recovered", "turn.start", "turn.end", "turn.resume",
        "turn.resume_accepted", "turn.nudge", "turn.timeout", "sentinel.seen",
        "sentinel.rejected", "integrity.ok", "integrity.violation", "git.commit",
        "session.open", "session.checkpoint.start", "session.checkpoint", "rollover.start",
        "rollover", "checkpoint.oversize", "model.escalated", "model.limit_reached",
        "model.decision_applied", "model.requested", "model.declined", "fix.plan_requested",
        "limit.cap_reached", "limit.decision_applied", "blocked.cleared", "decision.asked",
        "decision.answered", "decision.waiting", "decision.reminder", "decision.timeout",
        "decision.rejected", "decision.tamper", "route.judgment", "route.decided", "route.after_decision",
        "stall.detected", "gui.focus", "gui.click", "gui.type", "gui.ring", "gui.ring.retry",
        "gui.model", "gui.new_session", "gui.screenshot", "vision.check", "popup.seen",
        "popup.approved", "popup.escalated", "recovery.start", "recovery.action", "recovery.end",
        "recovery.retry", "monitor.spawned", "monitor.check", "monitor.note", "monitor.correct",
        "monitor.correct_applied", "monitor.halt", "monitor.error", "deepseek.call",
        "guard.tripped", "human.pause", "human.stop", "human.halt_cleared", "journal.repaired",
        "notify.sent", "notify.error", "error", "calibration.ok",
    ]

    static func url() throws -> URL {
        guard let url = Bundle.module.url(
            forResource: "journal-sample", withExtension: "jsonl", subdirectory: "Fixtures"
        ) else {
            throw XCTSkip("journal-sample.jsonl is not bundled; run tools/make_event_fixtures.py")
        }
        return url
    }

    static func events() throws -> [JournalEvent] {
        let text = try String(contentsOf: url(), encoding: .utf8)
        return try text
            .split(separator: "\n")
            .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            .map { try JSONDecoder().decode(JournalEvent.self, from: Data($0.utf8)) }
    }

    static func makeProject() throws -> URL {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("RelayWatchTests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent(".relay/journal", isDirectory: true),
            withIntermediateDirectories: true
        )
        return root
    }

    static func eventLine(
        id: String, type: String, round: Int = 1, prevHash: String = "", hash: String,
        detail: [String: Any] = ["role": "coder"]
    ) -> Data {
        let object: [String: Any] = [
            "id": id,
            "ts": "2026-10-21T09:00:00.000+00:00",
            "round": round,
            "state": "CODE",
            "actor": "orchestrator",
            "type": type,
            "detail": detail,
            "result": "ok",
            "error": NSNull(),
            "prev_hash": prevHash,
            "hash": hash,
        ]
        return try! JSONSerialization.data(withJSONObject: object)
    }
}

final class FixtureTests: XCTestCase {
    func testDecodesEveryDocumentedEventTypeAndAnUnknownOne() throws {
        let events = try Fixtures.events()
        XCTAssertFalse(events.isEmpty)
        let types = Set(events.map(\.type))
        XCTAssertTrue(Fixtures.documentedTypes.isSubset(of: types),
                      "missing: \(Fixtures.documentedTypes.subtracting(types).sorted())")
        // Unknown types are kept, not dropped or guessed at.
        XCTAssertTrue(types.contains("vision.experiment"))
        let unknown = try XCTUnwrap(events.first { $0.type == "vision.experiment" })
        XCTAssertEqual(unknown.detail["future_field"]?.objectValue?["nested"]?.arrayValue?.count, 3)
    }

    func testPartBFieldsDecode() throws {
        let events = try Fixtures.events()
        let turn = try XCTUnwrap(events.last { $0.type == "turn.end" && $0.model != nil })
        XCTAssertNotNil(turn.model)
        XCTAssertNotNil(turn.effort)
        XCTAssertNotNil(turn.detail["model_verified"])
        XCTAssertNotNil(turn.detail["changed"]?.arrayValue)

        let escalation = try XCTUnwrap(events.first { $0.type == "model.escalated" })
        let today = try XCTUnwrap(escalation.detail["opus_turns_today"]?.objectValue)
        XCTAssertEqual(today["limit"]?.intValue, 3)
        XCTAssertNotNil(escalation.detail["opus_turns_milestone"]?.objectValue?["limit"]?.intValue)
        XCTAssertNotNil(escalation.detail["issue_ids"]?.arrayValue)
        XCTAssertNotNil(escalation.trigger)

        // "unverifiable" is a string value, not a boolean.
        let coderEnds = events.filter { $0.type == "turn.end" && $0.role == "coder" }
        XCTAssertTrue(coderEnds.contains { $0.detail["model_verified"] == .string("unverifiable") })
    }

    func testChainLinksHoldInTheFixture() throws {
        let events = try Fixtures.events()
        let status = ChainChecker.check(events)
        XCTAssertTrue(status.ok, status.reason ?? "")
        XCTAssertGreaterThan(status.checkedCount, 100)
    }

    func testTimelineAndInsightsReadOnlyRealValues() throws {
        let events = try Fixtures.events()
        let rows = Insights.timeline(events)
        XCTAssertFalse(rows.isEmpty)
        XCTAssertTrue(rows.contains { $0.modelLabel.contains("Opus") })
        XCTAssertTrue(rows.contains { $0.commit != nil && !$0.commit!.isEmpty })
        XCTAssertTrue(rows.contains { $0.duration != nil })

        let usage = Insights.usage(events: events, state: nil, config: nil)
        XCTAssertFalse(usage.ratio.isEmpty)

        let alerts = Insights.alerts(events)
        XCTAssertTrue(alerts.contains { $0.type == "integrity.violation" })
        XCTAssertTrue(alerts.contains { $0.type == "monitor.halt" })
    }

    func testPendingDecisionCommandNeverContainsACode() {
        let decision = PendingDecision(
            id: "D-007", question: "Which storage?", options: [], round: 4, kind: nil, returnTo: nil
        )
        XCTAssertEqual(decision.command, "relay decide D-007 --code <code> \"your answer\"")
    }
}


final class GlyphAndWakeTests: XCTestCase {
    func testActivePhaseWithAStaleHeartbeatIsRedWithoutANewEvent() {
        let symbol = MenuGlyph.symbol(
            phase: "CODE", pendingDecision: false, halt: false, paused: false, stopped: false,
            chainOK: true, error: false, heartbeatAge: 600
        )
        XCTAssertEqual(symbol, "exclamationmark.octagon.fill")
    }

    func testFreshActivePhaseIsGreen() {
        XCTAssertEqual(
            MenuGlyph.symbol(
                phase: "REVIEW", pendingDecision: false, halt: false, paused: false, stopped: false,
                chainOK: true, error: false, heartbeatAge: 20
            ),
            "circle.fill"
        )
    }

    func testPendingDecisionPausedAndDoneGlyphs() {
        func symbol(_ phase: String, pending: Bool = false, paused: Bool = false) -> String {
            MenuGlyph.symbol(
                phase: phase, pendingDecision: pending, halt: false, paused: paused, stopped: false,
                chainOK: true, error: false, heartbeatAge: 10
            )
        }
        XCTAssertEqual(symbol("WAIT_HUMAN", pending: true), "questionmark.circle.fill")
        XCTAssertEqual(symbol("PAUSED", paused: true), "pause.circle")
        XCTAssertEqual(symbol("DONE"), "checkmark.circle")
        XCTAssertEqual(
            MenuGlyph.symbol(
                phase: "CODE", pendingDecision: false, halt: false, paused: false, stopped: false,
                chainOK: false, error: false, heartbeatAge: 10
            ),
            "exclamationmark.octagon.fill"
        )
    }

    func testHeartbeatWritesNeverWakeTheViewer() {
        XCTAssertFalse(WatchPaths.isRelevant("/p/.relay/HEARTBEAT.json"))
        XCTAssertFalse(WatchPaths.isRelevant("/p/.relay/monitor/HEARTBEAT.json"))
        XCTAssertFalse(WatchPaths.isRelevant("/p/.relay/BATON.json"))
        XCTAssertTrue(WatchPaths.isRelevant("/p/.relay/journal/2026-10-21.jsonl"))
        XCTAssertTrue(WatchPaths.isRelevant("/p/.relay/STATE.json"))
        XCTAssertFalse(WatchPaths.isRelevant("/p/.relay/journal/screens/2026-10-21/claude.png"))
        XCTAssertFalse(WatchPaths.isRelevant("/p/.relay/journal/calls/call-abc.json"))
        XCTAssertFalse(WatchPaths.hasRelevant(["/p/.relay/HEARTBEAT.json"]))
        XCTAssertTrue(
            WatchPaths.hasRelevant(["/p/.relay/HEARTBEAT.json", "/p/.relay/journal/2026-10-21.jsonl"])
        )
    }
}
