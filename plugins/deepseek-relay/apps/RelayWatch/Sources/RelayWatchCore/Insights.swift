import Foundation

public struct RoundRow: Identifiable, Equatable, Sendable {
    public let round: Int
    public var role: String?
    public var model: String?
    public var effort: String?
    public var outcome: String?
    public var startedAt: String?
    public var endedAt: String?
    public var duration: TimeInterval?
    public var filesChanged: Int?
    public var commit: String?
    public var ctxPct: Double?
    public var markers: [String]

    public var id: Int { round }

    public init(round: Int, markers: [String] = []) {
        self.round = round
        self.markers = markers
    }

    public var modelLabel: String {
        guard let model else { return "—" }
        if let effort, !effort.isEmpty { return "\(model) · \(effort)" }
        return model
    }

    public var durationLabel: String {
        guard let duration else { return "—" }
        let seconds = Int(duration.rounded())
        if seconds < 60 { return "\(seconds)s" }
        return "\(seconds / 60)m \(seconds % 60)s"
    }
}

public struct AlertItem: Identifiable, Equatable, Sendable {
    public let id: String
    public let type: String
    public let message: String
    public let round: Int
    public let time: String
}

public struct HealthItem: Identifiable, Equatable, Sendable {
    public let id: String
    public let title: String
    public let status: Health
    public let source: String
}

public enum Health: String, Sendable {
    case green, amber, red, unknown
}

public struct CurrentTurn: Equatable, Sendable {
    public let role: String?
    public let model: String?
    public let effort: String?
    public let round: Int?
    public let eventID: String?
}

public struct UsageToday: Equatable, Sendable {
    public var spendUSD: Double = 0
    public var budgetUSD: Double?
    public var opusToday: Int = 0
    public var opusTodayLimit: Int = 3
    public var opusMilestone: Int = 0
    public var opusMilestoneLimit: Int = 1
    public var sonnetTurns: Int = 0
    public var opusTurns: Int = 0

    public var ratio: String {
        let total = sonnetTurns + opusTurns
        guard total > 0 else { return "no Claude turns today" }
        let percent = Int((Double(opusTurns) / Double(total) * 100).rounded())
        return "Sonnet \(sonnetTurns) : Opus \(opusTurns)  (\(percent)% Opus)"
    }
}

public enum Insights {
    public static func timeline(_ events: [JournalEvent]) -> [RoundRow] {
        var rows: [Int: RoundRow] = [:]
        var starts: [Int: Date] = [:]

        func row(_ round: Int) -> RoundRow {
            if let existing = rows[round] { return existing }
            let fresh = RoundRow(round: round)
            rows[round] = fresh
            return fresh
        }

        for event in events {
            switch event.type {
            case "turn.start":
                guard event.round > 0 else { continue }
                var item = row(event.round)
                item.role = event.role ?? item.role
                item.model = event.model ?? item.model
                item.effort = event.effort ?? item.effort
                item.startedAt = event.shortTime
                if let date = event.date { starts[event.round] = date }
                rows[event.round] = item
            case "turn.end":
                guard event.round > 0 else { continue }
                var item = row(event.round)
                item.role = event.role ?? item.role
                item.model = event.model ?? item.model
                item.effort = event.effort ?? item.effort
                item.outcome = event.detailString("verdict") ?? event.detailString("status")
                item.endedAt = event.shortTime
                if let start = starts[event.round], let end = event.date {
                    item.duration = end.timeIntervalSince(start)
                }
                item.filesChanged = event.detail["changed"]?.arrayValue?.count
                if let commit = event.detailString("commit") {
                    item.commit = String(commit.prefix(8))
                }
                item.ctxPct = event.detailDouble("ctx_pct")
                rows[event.round] = item
            case "sentinel.rejected":
                addMarker(&rows, event.round, "sentinel rejected")
            case "turn.nudge":
                addMarker(&rows, event.round, "nudge")
            case "turn.timeout":
                addMarker(&rows, event.round, "timeout")
            case "rollover":
                addMarker(&rows, event.round, "rollover")
            case "stall.detected":
                addMarker(&rows, event.round, "stall")
            case "model.escalated":
                let trigger = event.trigger ?? "escalated"
                addMarker(&rows, event.round, "Opus: \(trigger)")
            case "fix.plan_requested":
                addMarker(&rows, event.round, "fix plan")
            case "integrity.violation":
                addMarker(&rows, event.round, "integrity violation")
            default:
                continue
            }
        }
        return rows.values.sorted { $0.round < $1.round }
    }

    private static func addMarker(_ rows: inout [Int: RoundRow], _ round: Int, _ marker: String) {
        guard round > 0 else { return }
        var item = rows[round] ?? RoundRow(round: round)
        if !item.markers.contains(marker) { item.markers.append(marker) }
        rows[round] = item
    }

    public static func currentTurn(_ events: [JournalEvent]) -> CurrentTurn? {
        let relevant = events.last { $0.type == "turn.start" || $0.type == "turn.end" }
        guard let event = relevant, let model = event.model else { return nil }
        return CurrentTurn(
            role: event.role, model: model, effort: event.effort,
            round: event.round, eventID: event.id
        )
    }

    public static func alerts(_ events: [JournalEvent]) -> [AlertItem] {
        let types: Set<String> = [
            "integrity.violation", "monitor.halt", "monitor.correct", "monitor.note",
            "guard.tripped", "error", "monitor.error", "notify.error",
            "limit.cap_reached", "model.limit_reached",
        ]
        return events.reversed().compactMap { event in
            guard types.contains(event.type) else { return nil }
            let message = event.detailString("reason")
                ?? event.detailString("error")
                ?? event.detailString("text")
                ?? event.detailString("summary")
                ?? event.detailString("kind")
                ?? event.type
            return AlertItem(
                id: event.id, type: event.type, message: message,
                round: event.round, time: event.shortTime
            )
        }
    }

    public static func usage(
        events: [JournalEvent], state: RelayState?, config: RelayConfig?, now: Date = Date()
    ) -> UsageToday {
        var usage = UsageToday()
        let today = RelayDate.day.string(from: now)

        usage.spendUSD = events.reduce(0) { total, event in
            guard event.type == "deepseek.call",
                  let date = event.date,
                  RelayDate.day.string(from: date) == today
            else { return total }
            return total + (event.deepseek?.costUSD ?? 0)
        }
        usage.budgetUSD = config?.dailyBudgetUSD

        if let opus = state?.opus {
            usage.opusToday = opus.date == today ? (opus.day ?? 0) : 0
            let sameMilestone = opus.milestone == (state?.milestone ?? 1)
            usage.opusMilestone = sameMilestone ? (opus.milestoneTurns ?? 0) : 0
        }
        if let policy = config?.modelPolicy {
            usage.opusTodayLimit = policy.maxOpusTurnsPerDay ?? usage.opusTodayLimit
            usage.opusMilestoneLimit = policy.maxOpusTurnsPerMilestone ?? usage.opusMilestoneLimit
        }

        for event in events where event.type == "turn.start" {
            guard let date = event.date, RelayDate.day.string(from: date) == today,
                  let model = event.model?.lowercased()
            else { continue }
            if model.contains("opus") {
                usage.opusTurns += 1
            } else if model.contains("sonnet") {
                usage.sonnetTurns += 1
            }
        }
        return usage
    }

    public static func health(
        snapshot: RelaySnapshot, chain: ChainStatus, events: [JournalEvent], now: Date = Date()
    ) -> [HealthItem] {
        func status(for age: TimeInterval?) -> Health {
            guard let age else { return .unknown }
            if age < 120 { return .green }
            if age < 300 { return .amber }
            return .red
        }
        var items: [HealthItem] = []
        items.append(HealthItem(
            id: "orchestrator",
            title: "Orchestrator",
            status: status(for: snapshot.orchestratorHeartbeat?.age(now: now)),
            source: ".relay/HEARTBEAT.json"
        ))
        items.append(HealthItem(
            id: "monitor",
            title: "Monitor",
            status: status(for: snapshot.monitorHeartbeat?.age(now: now)),
            source: ".relay/monitor/HEARTBEAT.json"
        ))

        let monitorCheck = ChainChecker.monitorVerification(events)
        let chainStatus: Health
        let chainSource: String
        if !chain.ok {
            chainStatus = .red
            chainSource = chain.reason ?? "chain link failed"
        } else if let monitorCheck, monitorCheck.ok == false {
            chainStatus = .red
            chainSource = "monitor check \(monitorCheck.eventID ?? "?") reported journal_ok false at \(monitorCheck.at ?? "?")"
        } else if let monitorCheck, monitorCheck.ok == true {
            let age = monitorCheck.date.map { now.timeIntervalSince($0) } ?? .infinity
            if age > 600 {
                // Never show green on stale data: an old verification is amber, not green.
                chainStatus = .amber
                chainSource = "last verified by monitor at \(monitorCheck.at ?? "?") (older than 10 min)"
            } else {
                chainStatus = .green
                chainSource = "verified by monitor \(monitorCheck.eventID ?? "?") at \(monitorCheck.at ?? "?") · link check over \(chain.checkedCount) events"
            }
        } else {
            chainStatus = .amber
            chainSource = "link check OK over \(chain.checkedCount) events; monitor has not verified yet"
        }
        items.append(HealthItem(
            id: "chain", title: "Journal chain", status: chainStatus, source: chainSource
        ))

        let flagStatus: Health
        let flagSource: String
        if let halt = snapshot.halt {
            flagStatus = .red
            flagSource = "HALT: \(halt)"
        } else if let pause = snapshot.pause {
            flagStatus = .amber
            flagSource = "PAUSE: \(pause)"
        } else if let stop = snapshot.stop {
            flagStatus = .amber
            flagSource = "STOP: \(stop)"
        } else {
            flagStatus = .green
            flagSource = "no HALT, PAUSE or STOP flag"
        }
        items.append(HealthItem(id: "flags", title: "Flags", status: flagStatus, source: flagSource))
        return items
    }
}
