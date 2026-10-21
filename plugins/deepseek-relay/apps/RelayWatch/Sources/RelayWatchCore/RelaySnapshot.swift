import Foundation

public struct DecisionOption: Decodable, Equatable, Sendable {
    public let label: String
    public let consequence: String
}

public struct PendingDecision: Decodable, Equatable, Sendable {
    public let id: String
    public let question: String
    public let options: [DecisionOption]
    public let round: Int?
    public let kind: String?
    public let returnTo: String?

    enum CodingKeys: String, CodingKey {
        case id, question, options, round, kind
        case returnTo = "return_to"
    }

    /// The exact command, with a placeholder for the one-time code. The code is never shown (P1-1).
    public var command: String {
        "relay decide \(id) --code <code> \"your answer\""
    }
}

public struct PendingTurn: Decodable, Equatable, Sendable {
    public let phase: String?
    public let role: String?
    public let round: Int?
    public let model: String?
    public let effort: String?
}

public struct OpusBook: Decodable, Equatable, Sendable {
    public let date: String?
    public let day: Int?
    public let milestone: Int?
    public let milestoneTurns: Int?

    enum CodingKeys: String, CodingKey {
        case date, day, milestone
        case milestoneTurns = "milestone_turns"
    }
}

public struct RelayState: Decodable, Equatable, Sendable {
    public let phase: String?
    public let round: Int?
    public let milestone: Int?
    public let role: String?
    public let pendingDecision: PendingDecision?
    public let pending: PendingTurn?
    public let pausedReason: String?
    public let updatedAt: String?
    public let opus: OpusBook?
    public let counters: [String: JSONValue]?

    enum CodingKeys: String, CodingKey {
        case phase, round, milestone, role, pending, counters, opus
        case pendingDecision = "pending_decision"
        case pausedReason = "paused_reason"
        case updatedAt = "updated_at"
    }
}

public struct Baton: Decodable, Equatable, Sendable {
    public let holder: String?
    public let role: String?
    public let round: Int?
}

public struct Heartbeat: Decodable, Equatable, Sendable {
    public let epoch: Double?
    public let pid: Int?
    public let phase: String?
    public let round: Int?

    public var timestamp: Date? { epoch.map { Date(timeIntervalSince1970: $0) } }

    public func age(now: Date = Date()) -> TimeInterval? {
        guard let timestamp else { return nil }
        return max(0, now.timeIntervalSince(timestamp))
    }
}

public struct ModelSlot: Decodable, Equatable, Sendable {
    public let model: String?
    public let effort: String?
}

public struct ModelPolicy: Decodable, Equatable, Sendable {
    public let defaultSlot: ModelSlot?
    public let escalated: ModelSlot?
    public let maxOpusTurnsPerMilestone: Int?
    public let maxOpusTurnsPerDay: Int?

    enum CodingKeys: String, CodingKey {
        case `default`, escalated
        case maxOpusTurnsPerMilestone = "max_opus_turns_per_milestone"
        case maxOpusTurnsPerDay = "max_opus_turns_per_day"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        defaultSlot = try? container.decodeIfPresent(ModelSlot.self, forKey: .default)
        escalated = try? container.decodeIfPresent(ModelSlot.self, forKey: .escalated)
        maxOpusTurnsPerMilestone = try? container.decodeIfPresent(Int.self, forKey: .maxOpusTurnsPerMilestone)
        maxOpusTurnsPerDay = try? container.decodeIfPresent(Int.self, forKey: .maxOpusTurnsPerDay)
    }
}

public struct RelayConfig: Decodable, Equatable, Sendable {
    public let dailyBudgetUSD: Double?
    public let modelPolicy: ModelPolicy?

    enum CodingKeys: String, CodingKey {
        case dailyBudgetUSD = "daily_budget_usd"
        case modelPolicy = "model_policy"
    }
}

/// Everything Relay Watch shows that is not derived from journal events. Every field is optional:
/// a missing or unreadable file becomes a visible error, never a guess.
public struct RelaySnapshot: Sendable {
    public var state: RelayState?
    public var baton: Baton?
    public var orchestratorHeartbeat: Heartbeat?
    public var monitorHeartbeat: Heartbeat?
    public var halt: String?
    public var pause: String?
    public var stop: String?
    public var pendingDecisionText: String?
    public var config: RelayConfig?
    public var loadedAt: Date
    public var loadErrors: [String]

    public init(
        state: RelayState? = nil, baton: Baton? = nil, orchestratorHeartbeat: Heartbeat? = nil,
        monitorHeartbeat: Heartbeat? = nil, halt: String? = nil, pause: String? = nil,
        stop: String? = nil, pendingDecisionText: String? = nil, config: RelayConfig? = nil,
        loadedAt: Date = Date(), loadErrors: [String] = []
    ) {
        self.state = state
        self.baton = baton
        self.orchestratorHeartbeat = orchestratorHeartbeat
        self.monitorHeartbeat = monitorHeartbeat
        self.halt = halt
        self.pause = pause
        self.stop = stop
        self.pendingDecisionText = pendingDecisionText
        self.config = config
        self.loadedAt = loadedAt
        self.loadErrors = loadErrors
    }

    public static func load(folder: RelayFolder, now: Date = Date()) -> RelaySnapshot {
        var errors: [String] = []
        func decode<T: Decodable>(_ type: T.Type, _ relative: String) -> T? {
            do {
                return try folder.decode(type, relative)
            } catch {
                if folder.exists(relative) {
                    errors.append("\(relative): \(error.localizedDescription)")
                }
                return nil
            }
        }
        let state = decode(RelayState.self, ".relay/STATE.json")
        if state == nil && folder.exists(".relay/STATE.json") && !errors.contains(where: { $0.hasPrefix(".relay/STATE.json") }) {
            errors.append(".relay/STATE.json could not be decoded")
        }
        return RelaySnapshot(
            state: state,
            baton: decode(Baton.self, ".relay/BATON.json"),
            orchestratorHeartbeat: decode(Heartbeat.self, ".relay/HEARTBEAT.json"),
            monitorHeartbeat: decode(Heartbeat.self, ".relay/monitor/HEARTBEAT.json"),
            halt: flagReason(folder, ".relay/HALT"),
            pause: flagReason(folder, ".relay/PAUSE"),
            stop: flagReason(folder, ".relay/STOP"),
            pendingDecisionText: folder.textIfPresent(".relay/PENDING_DECISION.md"),
            config: decode(RelayConfig.self, ".relay/config.json"),
            loadedAt: now,
            loadErrors: errors
        )
    }

    private static func flagReason(_ folder: RelayFolder, _ relative: String) -> String? {
        guard let text = folder.textIfPresent(relative) else { return nil }
        if let data = text.data(using: .utf8),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let reason = object["reason"] as? String, !reason.isEmpty {
            return reason
        }
        return text.isEmpty ? "present" : text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
