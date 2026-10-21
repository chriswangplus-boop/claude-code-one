import Foundation

public struct ChainStatus: Equatable, Sendable {
    public let ok: Bool
    public let failingEventID: String?
    public let reason: String?
    public let checkedCount: Int

    public init(ok: Bool, failingEventID: String? = nil, reason: String? = nil, checkedCount: Int = 0) {
        self.ok = ok
        self.failingEventID = failingEventID
        self.reason = reason
        self.checkedCount = checkedCount
    }

    public static let empty = ChainStatus(ok: true, reason: "no events yet")
}

/// The cheap link check from C6: each event's `prev_hash` must equal the previous event's `hash`.
///
/// This never recomputes Python's canonical JSON hashes; Relay's monitor does that and its latest
/// `monitor.check` is shown as "verified by monitor at HH:MM".
public enum ChainChecker {
    public static func check(_ events: [JournalEvent]) -> ChainStatus {
        guard events.count > 1 else { return ChainStatus(ok: true, checkedCount: events.count) }
        var previous = events[0]
        for index in 1..<events.count {
            let current = events[index]
            if current.prevHash != previous.hash {
                return ChainStatus(
                    ok: false,
                    failingEventID: current.id,
                    reason: "prev_hash of \(current.id) does not match \(previous.id)",
                    checkedCount: index + 1
                )
            }
            previous = current
        }
        return ChainStatus(ok: true, checkedCount: events.count)
    }

    /// Latest monitor result, used for the "verified by monitor" badge (never recomputed here).
    public static func monitorVerification(
        _ events: [JournalEvent]
    ) -> (at: String?, ok: Bool?, eventID: String?, date: Date?)? {
        guard let event = events.last(where: { $0.type == "monitor.check" }) else { return nil }
        let journalOK = event.detail["journal_ok"]?.boolValue
        return (event.shortTime, journalOK, event.id, event.date)
    }
}

/// C4.3 meaningful-event filter. Everything else is stored and counted but does not redraw.
public struct EventFilter {
    public static let meaningful: Set<String> = [
        "turn.start", "turn.end", "turn.timeout", "sentinel.rejected",
        "route.decided", "stall.detected", "model.escalated",
        "rollover", "session.open",
        "decision.asked", "decision.answered",
        "state.paused", "run.start", "run.resume", "project.done",
        "integrity.violation", "guard.tripped",
        "monitor.halt", "monitor.correct", "monitor.note", "monitor.check",
        "git.commit", "error",
        "init",
    ]

    private var lastMonitorSignature: String?

    public init() {}

    public mutating func shouldRefresh(_ event: JournalEvent) -> Bool {
        guard Self.meaningful.contains(event.type) else { return false }
        if event.type == "monitor.check" {
            let signature = "\(event.result ?? ""):\(event.detail["journal_ok"]?.displayText ?? "")"
            if signature == lastMonitorSignature { return false }
            lastMonitorSignature = signature
        }
        return true
    }

    public mutating func refreshCount(for events: [JournalEvent]) -> Int {
        var count = 0
        for event in events where shouldRefresh(event) { count += 1 }
        return count
    }
}
