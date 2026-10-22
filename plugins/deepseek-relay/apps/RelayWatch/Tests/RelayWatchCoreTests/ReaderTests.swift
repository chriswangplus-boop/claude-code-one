import Foundation
import XCTest
@testable import RelayWatchCore

final class ReaderTests: XCTestCase {
    private var roots: [URL] = []

    override func tearDown() {
        for root in roots { try? FileManager.default.removeItem(at: root) }
        roots = []
        super.tearDown()
    }

    private func project() throws -> URL {
        let root = try Fixtures.makeProject()
        roots.append(root)
        return root
    }

    private func journal(_ root: URL, _ day: String) -> URL {
        root.appendingPathComponent(".relay/journal/\(day).jsonl")
    }

    private func append(_ data: Data, to url: URL) throws {
        if !FileManager.default.fileExists(atPath: url.path) {
            try data.write(to: url)
            return
        }
        let handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        try handle.write(contentsOf: data)
        try handle.close()
    }

    func testPartialLineIsNotProcessedUntilItIsComplete() throws {
        let root = try project()
        let folder = RelayFolder(root: root)
        let file = journal(root, "2026-10-21")
        try Fixtures.eventLine(id: "evt-1", type: "turn.start", hash: "h1").write(to: file)

        let reader = JournalReader(folder: folder)
        XCTAssertEqual(try reader.readInitial(dayCount: 1).count, 0, "half a line must not be processed")
        XCTAssertEqual(try reader.readNewEvents().count, 0)

        try append(Data("\n".utf8), to: file)
        let first = try reader.readNewEvents()
        XCTAssertEqual(first.count, 1)
        XCTAssertEqual(first.first?.id, "evt-1")
        XCTAssertEqual(try reader.readNewEvents().count, 0, "a completed line is processed exactly once")
    }

    func testDayFileSwitchAtMidnight() throws {
        let root = try project()
        let folder = RelayFolder(root: root)
        try append(Fixtures.eventLine(id: "evt-1", type: "turn.start", hash: "h1") + Data("\n".utf8),
                   to: journal(root, "2026-10-21"))
        let reader = JournalReader(folder: folder)
        XCTAssertEqual(try reader.readInitial(dayCount: 1).count, 1)

        try append(Fixtures.eventLine(id: "evt-2", type: "turn.end", hash: "h2") + Data("\n".utf8),
                   to: journal(root, "2026-10-22"))
        let afterMidnight = try reader.readNewEvents()
        XCTAssertEqual(afterMidnight.map(\.id), ["evt-2"])
        XCTAssertEqual(try reader.readNewEvents().count, 0)
    }

    func testOlderDaysLoadOnDemand() throws {
        let root = try project()
        let folder = RelayFolder(root: root)
        for (index, day) in ["2026-10-19", "2026-10-20", "2026-10-21"].enumerated() {
            try append(
                Fixtures.eventLine(id: "evt-\(index)", type: "turn.start", hash: "h\(index)") + Data("\n".utf8),
                to: journal(root, day)
            )
        }
        let reader = JournalReader(folder: folder)
        let today = try reader.readInitial(dayCount: 1)
        XCTAssertEqual(today.map(\.id), ["evt-2"])
        XCTAssertTrue(reader.hasOlderDays)

        let older = try reader.loadOlder()
        XCTAssertEqual(older.map(\.id), ["evt-1"])
        XCTAssertTrue(reader.hasOlderDays)
        XCTAssertEqual(try reader.loadOlder().map(\.id), ["evt-0"])
        XCTAssertFalse(reader.hasOlderDays)
        XCTAssertEqual(try reader.loadOlder().count, 0)
    }

    func testRamCapKeepsNewestEvents() {
        let store = JournalStore(capacity: 3)
        let events = (0..<5).map { index in
            JournalEvent(
                id: "evt-\(index)", ts: "", round: index, state: nil, actor: "harness",
                type: "turn.start", prevHash: "", hash: "h\(index)"
            )
        }
        store.append(Array(events.prefix(2)))
        store.append(Array(events.dropFirst(2)))
        XCTAssertEqual(store.events.map(\.id), ["evt-2", "evt-3", "evt-4"])
        XCTAssertEqual(store.droppedCount, 2)
    }
}

final class ChainTests: XCTestCase {
    func testDeletedMiddleLineFailsAndNamesTheEvent() throws {
        var events = try Fixtures.events()
        XCTAssertTrue(ChainChecker.check(events).ok)
        let removed = events.remove(at: 5)
        let status = ChainChecker.check(events)
        XCTAssertFalse(status.ok)
        XCTAssertEqual(status.failingEventID, events[5].id)
        XCTAssertNotNil(status.reason)
        XCTAssertNotEqual(status.failingEventID, removed.id)
    }

    func testInsertedLineFails() throws {
        var events = try Fixtures.events()
        let duplicate = events[10]
        events.insert(duplicate, at: 20)
        XCTAssertFalse(ChainChecker.check(events).ok)
    }

    func testEmptyAndSingleEventAreTriviallyOK() {
        XCTAssertTrue(ChainChecker.check([]).ok)
        let single = JournalEvent(
            id: "evt-1", ts: "", round: 0, state: nil, actor: "harness", type: "init",
            prevHash: "", hash: "h"
        )
        XCTAssertTrue(ChainChecker.check([single]).ok)
    }

    func testMonitorVerificationComesFromTheJournal() throws {
        let events = try Fixtures.events()
        let verification = try XCTUnwrap(ChainChecker.monitorVerification(events))
        XCTAssertNotNil(verification.at)
        XCTAssertNotNil(verification.ok)
    }
}

final class FilterTests: XCTestCase {
    private func event(_ type: String, _ result: String = "ok", journalOK: Bool = true) -> JournalEvent {
        JournalEvent(
            id: UUID().uuidString, ts: "", round: 1, state: nil, actor: "orchestrator",
            type: type, detail: ["journal_ok": .bool(journalOK)], result: result,
            prevHash: "", hash: ""
        )
    }

    func testNoiseCausesZeroRefreshesAndOneTurnEndCausesOne() {
        var filter = EventFilter()
        var refreshes = 0
        for _ in 0..<100 {
            if filter.shouldRefresh(event("gui.ring")) { refreshes += 1 }
            if filter.shouldRefresh(event("vision.check")) { refreshes += 1 }
            if filter.shouldRefresh(event("gui.screenshot")) { refreshes += 1 }
        }
        XCTAssertEqual(refreshes, 0)
        XCTAssertTrue(filter.shouldRefresh(event("turn.end")))
        XCTAssertEqual(filter.refreshCount(for: [event("turn.end")]), 1)
    }

    func testMonitorCheckOnlyRefreshesWhenTheResultChanges() {
        var filter = EventFilter()
        XCTAssertTrue(filter.shouldRefresh(event("monitor.check", "ok", journalOK: true)))
        XCTAssertFalse(filter.shouldRefresh(event("monitor.check", "ok", journalOK: true)))
        XCTAssertTrue(filter.shouldRefresh(event("monitor.check", "halt", journalOK: true)))
        XCTAssertTrue(filter.shouldRefresh(event("monitor.check", "halt", journalOK: false)))
    }

    func testEveryMeaningfulTypeInTheSchemaIsInTheFilter() {
        let documented: Set<String> = [
            "turn.start", "turn.end", "turn.timeout", "sentinel.rejected", "route.decided",
            "stall.detected", "model.escalated", "rollover", "session.open", "decision.asked",
            "decision.answered", "state.paused", "run.start", "run.resume", "project.done",
            "integrity.violation", "guard.tripped", "monitor.halt", "monitor.correct",
            "monitor.note", "monitor.check", "git.commit", "error",
        ]
        XCTAssertTrue(documented.isSubset(of: EventFilter.meaningful))
    }
}

final class ReadOnlyTests: XCTestCase {
    func testAFullSessionLeavesTheProjectUntouched() throws {
        let root = try Fixtures.makeProject()
        defer { try? FileManager.default.removeItem(at: root) }
        let fixture = try String(contentsOf: Fixtures.url(), encoding: .utf8)
        try fixture.write(
            to: root.appendingPathComponent(".relay/journal/2026-10-21.jsonl"), atomically: true, encoding: .utf8
        )
        let relay = root.appendingPathComponent(".relay")
        let stateJSON = """
        {"phase": "CODE", "round": 3, "milestone": 1, "role": "coder", "pending_decision": null,
         "opus": {"date": "2026-10-21", "day": 1, "milestone": 1, "milestone_turns": 1}}
        """
        let batonJSON = #"{"holder": "coder", "role": "coder", "round": 3}"#
        let heartbeatJSON = #"{"epoch": 1792000000, "pid": 1, "phase": "CODE", "round": 3}"#
        let configJSON = #"{"daily_budget_usd": 20, "model_policy": {"max_opus_turns_per_day": 3, "max_opus_turns_per_milestone": 1}}"#
        try stateJSON.write(to: relay.appendingPathComponent("STATE.json"), atomically: true, encoding: .utf8)
        try batonJSON.write(to: relay.appendingPathComponent("BATON.json"), atomically: true, encoding: .utf8)
        try heartbeatJSON.write(to: relay.appendingPathComponent("HEARTBEAT.json"), atomically: true, encoding: .utf8)
        try configJSON.write(to: relay.appendingPathComponent("config.json"), atomically: true, encoding: .utf8)

        let before = try fingerprint(root)

        let folder = RelayFolder(root: root)
        let reader = JournalReader(folder: folder)
        let initial = try reader.readInitial(dayCount: 2)
        _ = try reader.readNewEvents()
        let snapshot = RelaySnapshot.load(folder: folder)
        _ = Insights.timeline(initial)
        _ = Insights.alerts(initial)
        // Pin "today" to the fixture's day: the fixture must not depend on when the test runs.
        let day = ISO8601DateFormatter().date(from: "2026-10-21T12:00:00Z")!
        let usage = Insights.usage(
            events: initial, state: snapshot.state, config: snapshot.config, now: day
        )
        _ = Insights.health(snapshot: snapshot, chain: ChainChecker.check(initial), events: initial)

        XCTAssertFalse(initial.isEmpty)
        XCTAssertEqual(snapshot.state?.round, 3)
        XCTAssertEqual(usage.opusToday, 1)
        XCTAssertEqual(usage.opusMilestone, 1)

        let after = try fingerprint(root)
        XCTAssertEqual(before, after, "reading must not create, modify or delete anything")
    }

    /// Path → (size, modification time) plus the directory entry list.
    private func fingerprint(_ root: URL) throws -> [String: String] {
        var result: [String: String] = [:]
        let manager = FileManager.default
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey]
        guard let walker = manager.enumerator(at: root, includingPropertiesForKeys: keys) else { return result }
        for case let url as URL in walker {
            let values = try url.resourceValues(forKeys: Set(keys))
            let size = values.fileSize ?? -1
            let modified = values.contentModificationDate?.timeIntervalSince1970 ?? 0
            result[url.path.replacingOccurrences(of: root.path, with: "")] = "\(size):\(modified)"
        }
        return result
    }
}

final class HealthTests: XCTestCase {
    func testChainFailureTurnsTheBadgeRedAndNamesTheEvent() throws {
        var events = try Fixtures.events()
        events.remove(at: 5)
        let health = Insights.health(
            snapshot: RelaySnapshot(), chain: ChainChecker.check(events), events: events
        )
        let chain = try XCTUnwrap(health.first { $0.id == "chain" })
        XCTAssertEqual(chain.status, .red)
        XCTAssertTrue(chain.source.contains(events[5].id), chain.source)
    }

    func testMonitorReportingBrokenChainTurnsTheBadgeRed() {
        let timestamp = ISO8601DateFormatter().string(from: Date())
        let event = JournalEvent(
            id: "evt-monitor-1", ts: timestamp, round: 4, state: "REVIEW", actor: "monitor",
            type: "monitor.check", detail: ["journal_ok": .bool(false)], result: "halt",
            prevHash: "", hash: "h"
        )
        let health = Insights.health(
            snapshot: RelaySnapshot(), chain: ChainStatus(ok: true), events: [event]
        )
        let chain = try? XCTUnwrap(health.first { $0.id == "chain" })
        XCTAssertEqual(chain?.status, .red)
        XCTAssertTrue(chain?.source.contains("evt-monitor-1") ?? false)
    }

    func testStaleVerificationIsAmberNotGreen() {
        let old = ISO8601DateFormatter().string(from: Date().addingTimeInterval(-3600))
        let event = JournalEvent(
            id: "evt-monitor-2", ts: old, round: 4, state: "REVIEW", actor: "monitor",
            type: "monitor.check", detail: ["journal_ok": .bool(true)], result: "ok",
            prevHash: "", hash: "h"
        )
        let health = Insights.health(
            snapshot: RelaySnapshot(), chain: ChainStatus(ok: true), events: [event]
        )
        XCTAssertEqual(health.first { $0.id == "chain" }?.status, .amber)
    }
}
