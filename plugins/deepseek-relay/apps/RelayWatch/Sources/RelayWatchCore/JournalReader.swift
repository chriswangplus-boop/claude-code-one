import Foundation

public enum JournalReaderError: Error, LocalizedError {
    case invalidLine(file: String, line: Int, underlying: String)

    public var errorDescription: String? {
        switch self {
        case let .invalidLine(file, line, underlying):
            return "\(file):\(line) is not valid JSON: \(underlying)"
        }
    }
}

/// Incremental reader: byte offsets per day file, and only complete lines are delivered.
///
/// A partial last line is normal while Relay is writing; it stays buffered until its `\n`
/// arrives. Older days are only read when `loadOlder()` is called (the timeline scrolls back).
public final class JournalReader {
    private let folder: RelayFolder
    private var offsets: [String: UInt64] = [:]
    private var buffers: [String: Data] = [:]
    private var knownFiles: [String] = []
    private var olderFiles: [String] = []
    private var initialized = false

    public init(folder: RelayFolder) {
        self.folder = folder
    }

    /// Read the newest `dayCount` files in full (today and yesterday by default).
    @discardableResult
    public func readInitial(dayCount: Int = 2) throws -> [JournalEvent] {
        let names = folder.dayFiles().map(\.lastPathComponent)
        knownFiles = Array(names.suffix(max(1, dayCount)))
        olderFiles = Array(names.dropLast(max(1, dayCount)))
        initialized = true
        var events: [JournalEvent] = []
        for name in knownFiles {
            events.append(contentsOf: try read(file: name))
        }
        return events
    }

    /// Read whatever is new since the last call, plus a newly created day file.
    public func readNewEvents() throws -> [JournalEvent] {
        guard initialized else { return try readInitial() }
        let names = folder.dayFiles().map(\.lastPathComponent)
        var events: [JournalEvent] = []
        for name in names where !olderFiles.contains(name) {
            if !knownFiles.contains(name) { knownFiles.append(name) }
            events.append(contentsOf: try read(file: name))
        }
        return events
    }

    /// Load one older day file (the timeline's "load older" control). Returns [] when exhausted.
    public func loadOlder() throws -> [JournalEvent] {
        guard let name = olderFiles.popLast() else { return [] }
        knownFiles.insert(name, at: 0)
        return try read(file: name)
    }

    public var hasOlderDays: Bool { !olderFiles.isEmpty }

    /// True when something was read but its line is still incomplete.
    public func hasPartialLine(_ name: String) -> Bool {
        !(buffers[name]?.isEmpty ?? true)
    }

    private func read(file name: String) throws -> [JournalEvent] {
        let url = folder.url(".relay/journal/\(name)")
        let size = folder.fileSize(".relay/journal/\(name)") ?? 0
        let offset = offsets[name] ?? 0
        guard size > offset else { return [] }

        // Read only the new bytes: the offset is the read cursor for this day file.
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        try handle.seek(toOffset: offset)
        let data = (try handle.readToEnd()) ?? Data()
        offsets[name] = offset + UInt64(data.count)

        var buffer = buffers[name] ?? Data()
        buffer.append(data)
        var events: [JournalEvent] = []
        var lineNumber = 0
        while let newline = buffer.firstIndex(of: 0x0A) {
            let lineData = buffer.subdata(in: 0..<newline)
            buffer.removeSubrange(0...newline)
            lineNumber += 1
            let line = String(data: lineData, encoding: .utf8)?.trimmingCharacters(in: .whitespaces)
            guard let line, !line.isEmpty else { continue }
            do {
                events.append(try JSONDecoder().decode(JournalEvent.self, from: Data(line.utf8)))
            } catch {
                throw JournalReaderError.invalidLine(file: name, line: lineNumber, underlying: "\(error)")
            }
        }
        buffers[name] = buffer
        return events
    }
}

/// Keeps at most `capacity` events in memory; older ones remain on disk for `loadOlder()`.
public final class JournalStore {
    public private(set) var events: [JournalEvent] = []
    public private(set) var totalReceived = 0
    public let capacity: Int

    public init(capacity: Int = 5_000) {
        self.capacity = max(1, capacity)
    }

    public var droppedCount: Int { max(0, totalReceived - events.count) }

    /// Appends new events and returns how many were added.
    @discardableResult
    public func append(_ newEvents: [JournalEvent]) -> Int {
        guard !newEvents.isEmpty else { return 0 }
        totalReceived += newEvents.count
        events.append(contentsOf: newEvents)
        if events.count > capacity {
            events.removeFirst(events.count - capacity)
        }
        return newEvents.count
    }

    public func latest(_ type: String) -> JournalEvent? {
        events.last { $0.type == type }
    }
}
