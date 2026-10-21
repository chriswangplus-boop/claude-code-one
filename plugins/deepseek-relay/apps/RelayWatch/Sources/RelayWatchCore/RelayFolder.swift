import Foundation

/// The only way Relay Watch touches the project folder.
///
/// It deliberately exposes read APIs only: there is no write, create, delete, rename or
/// truncate method anywhere in this type, so no view can accidentally modify `.relay/`.
public struct RelayFolder: Sendable {
    public let root: URL
    private let scoped: Bool

    /// Tests and the CLI use a plain folder URL.
    public init(root: URL) {
        self.root = root.standardizedFileURL
        self.scoped = false
    }

    /// The app resolves the read-only security-scoped bookmark the user picked once.
    public init(bookmark: Data) throws {
        var stale = false
        let url = try URL(
            resolvingBookmarkData: bookmark,
            options: [.withSecurityScope],
            relativeTo: nil,
            bookmarkDataIsStale: &stale
        )
        guard url.startAccessingSecurityScopedResource() else {
            throw RelayFolderError.bookmarkAccessDenied
        }
        self.root = url.standardizedFileURL
        self.scoped = true
    }

    public func release() {
        if scoped { root.stopAccessingSecurityScopedResource() }
    }

    public func makeBookmark() -> Data? {
        guard scoped else { return nil }
        return try? root.bookmarkData(options: [.withSecurityScope], includingResourceValuesForKeys: nil, relativeTo: nil)
    }

    public func url(_ relative: String) -> URL {
        root.appendingPathComponent(relative, isDirectory: false)
    }

    public func exists(_ relative: String) -> Bool {
        FileManager.default.fileExists(atPath: url(relative).path)
    }

    public func data(_ relative: String) throws -> Data {
        try Data(contentsOf: url(relative), options: [.uncached])
    }

    public func text(_ relative: String) throws -> String {
        let data = try data(relative)
        return String(data: data, encoding: .utf8) ?? ""
    }

    public func textIfPresent(_ relative: String) -> String? {
        guard exists(relative) else { return nil }
        return try? text(relative)
    }

    public func modificationDate(_ relative: String) -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: url(relative).path)[.modificationDate]) as? Date
    }

    public func fileSize(_ relative: String) -> UInt64? {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url(relative).path) else {
            return nil
        }
        return (attributes[.size] as? NSNumber)?.uint64Value
    }

    public func decode<T: Decodable>(_ type: T.Type, _ relative: String) throws -> T {
        try JSONDecoder().decode(type, from: data(relative))
    }

    public func decodeIfPresent<T: Decodable>(_ type: T.Type, _ relative: String) -> T? {
        guard exists(relative) else { return nil }
        return try? decode(type, relative)
    }

    /// `.relay/journal/YYYY-MM-DD.jsonl`, sorted oldest first.
    public func dayFiles() -> [URL] {
        let directory = url(".relay/journal")
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: [.fileSizeKey], options: [.skipsHiddenFiles]
        ) else { return [] }
        return entries
            .filter { isDayFile($0.lastPathComponent) }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    /// One day file's raw bytes; used only when the user scrolls back through older days.
    public func dayData(_ url: URL) throws -> Data {
        try Data(contentsOf: url, options: [.uncached])
    }

    private func isDayFile(_ name: String) -> Bool {
        name.count == "YYYY-MM-DD.jsonl".count
            && name.hasSuffix(".jsonl")
            && name.prefix(4).allSatisfy(\.isNumber)
            && name.dropFirst(4).first == "-"
    }
}

public enum RelayFolderError: Error, LocalizedError {
    case bookmarkAccessDenied

    public var errorDescription: String? {
        switch self {
        case .bookmarkAccessDenied:
            return "macOS denied access to the bookmarked project folder. Pick the folder again."
        }
    }
}
