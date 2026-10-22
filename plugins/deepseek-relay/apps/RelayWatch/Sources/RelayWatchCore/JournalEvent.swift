import Foundation

public struct DeepSeekRecord: Codable, Equatable, Sendable {
    public let callID: String?
    public let costUSD: Double?
    public let promptTokens: Int?
    public let completionTokens: Int?
    public let ctxPct: Double?

    enum CodingKeys: String, CodingKey {
        case callID = "call_id"
        case costUSD = "cost_usd"
        case promptTokens = "prompt_tokens"
        case completionTokens = "completion_tokens"
        case ctxPct = "ctx_pct"
    }
}

/// One line of `.relay/journal/<day>.jsonl`. Unknown fields are ignored; unknown types are kept.
public struct JournalEvent: Decodable, Identifiable, Equatable, Sendable {
    public let id: String
    public let ts: String
    public let round: Int
    public let state: String?
    public let actor: String
    public let type: String
    public let detail: [String: JSONValue]
    public let result: String?
    public let error: String?
    public let deepseek: DeepSeekRecord?
    public let prevHash: String
    public let hash: String

    enum CodingKeys: String, CodingKey {
        case id, ts, round, state, actor, type, detail, result, error, deepseek
        case prevHash = "prev_hash"
        case hash
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? container.decode(String.self, forKey: .id)) ?? UUID().uuidString
        ts = (try? container.decode(String.self, forKey: .ts)) ?? ""
        round = (try? container.decode(Int.self, forKey: .round)) ?? 0
        state = try? container.decodeIfPresent(String.self, forKey: .state)
        actor = (try? container.decode(String.self, forKey: .actor)) ?? "unknown"
        type = (try? container.decode(String.self, forKey: .type)) ?? "unknown"
        detail = (try? container.decode([String: JSONValue].self, forKey: .detail)) ?? [:]
        result = try? container.decodeIfPresent(String.self, forKey: .result)
        error = try? container.decodeIfPresent(String.self, forKey: .error)
        deepseek = try? container.decodeIfPresent(DeepSeekRecord.self, forKey: .deepseek)
        prevHash = (try? container.decode(String.self, forKey: .prevHash)) ?? ""
        hash = (try? container.decode(String.self, forKey: .hash)) ?? ""
    }

    public init(
        id: String, ts: String, round: Int, state: String?, actor: String, type: String,
        detail: [String: JSONValue] = [:], result: String? = nil, error: String? = nil,
        deepseek: DeepSeekRecord? = nil, prevHash: String, hash: String
    ) {
        self.id = id
        self.ts = ts
        self.round = round
        self.state = state
        self.actor = actor
        self.type = type
        self.detail = detail
        self.result = result
        self.error = error
        self.deepseek = deepseek
        self.prevHash = prevHash
        self.hash = hash
    }

    public var date: Date? { RelayDate.parse(ts) }

    public var shortTime: String {
        guard let date else { return ts }
        return RelayDate.clock.string(from: date)
    }

    public var model: String? { detail["model"]?.stringValue }
    public var effort: String? { detail["effort"]?.stringValue }
    public var role: String? { detail["role"]?.stringValue }
    public var trigger: String? { detail["trigger"]?.stringValue }
    public var modelVerified: JSONValue? { detail["model_verified"] }

    public func detailString(_ key: String) -> String? { detail[key]?.stringValue }
    public func detailInt(_ key: String) -> Int? { detail[key]?.intValue }
    public func detailDouble(_ key: String) -> Double? { detail[key]?.doubleValue }
    public func detailStrings(_ key: String) -> [String] { detail[key]?.strings ?? [] }

    /// Full raw object for the event inspector (pretty-printed, read-only).
    public func rawJSON() -> String {
        var parts: [String] = [
            "\"id\": \(quoted(id))",
            "\"ts\": \(quoted(ts))",
            "\"round\": \(round)",
            "\"state\": \(state.map(quoted) ?? "null")",
            "\"actor\": \(quoted(actor))",
            "\"type\": \(quoted(type))",
            "\"detail\": \(JSONValue.object(detail).prettyJSON())",
            "\"result\": \(result.map(quoted) ?? "null")",
            "\"error\": \(error.map(quoted) ?? "null")",
            "\"prev_hash\": \(quoted(prevHash))",
            "\"hash\": \(quoted(hash))",
        ]
        if let deepseek, let data = try? JSONEncoder().encode(deepseek), let text = String(data: data, encoding: .utf8) {
            parts.append("\"deepseek\": \(text)")
        }
        return "{\n  " + parts.joined(separator: ",\n  ") + "\n}"
    }

    private func quoted(_ value: String) -> String {
        let escaped = value
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        return "\"" + escaped + "\""
    }
}

public enum RelayDate {
    public static let clock: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()

    public static let day: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()

    private static let fractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let plain: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    public static func parse(_ text: String) -> Date? {
        fractional.date(from: text) ?? plain.date(from: text)
    }

    public static func today(now: Date = Date()) -> String {
        day.string(from: now)
    }
}
