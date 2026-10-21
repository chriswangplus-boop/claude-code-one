import AppKit
import Foundation
import RelayWatchCore
import SwiftUI

@MainActor
final class RelayController: ObservableObject {
    @Published private(set) var snapshot = RelaySnapshot()
    @Published private(set) var events: [JournalEvent] = []
    @Published private(set) var chain = ChainStatus.empty
    @Published private(set) var error: String?
    @Published private(set) var refreshCount = 0
    @Published private(set) var projectName: String?
    @Published private(set) var hasOlderDays = false
    @Published private(set) var lastUpdate = "waiting for a project folder"
    @Published private(set) var lastEventID: String?

    private var folder: RelayFolder?
    private var reader: JournalReader?
    private var store = JournalStore()
    private var filter = EventFilter()
    private var watcher: FSEventsWatcher?
    private var heartbeatTimer: Timer?
    private var stalenessTimer: Timer?
    private var visibleCount = 0
    private var bookmarkData: Data?

    private let bookmarkKey = "relayWatchBookmark"
    private let pathKey = "relayWatchProjectPath"

    var timeline: [RoundRow] { Insights.timeline(events) }
    var alerts: [AlertItem] { Insights.alerts(events) }
    var usage: UsageToday { Insights.usage(events: events, state: snapshot.state, config: snapshot.config) }
    var health: [HealthItem] { Insights.health(snapshot: snapshot, chain: chain, events: events) }
    var currentTurn: CurrentTurn? { Insights.currentTurn(events) }
    var pendingDecision: PendingDecision? { snapshot.state?.pendingDecision }

    init() {
        restoreBookmark()
    }

    // MARK: - Project folder

    func chooseFolder() {
        // Only ever activated by this explicit human click, never on its own.
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Watch this project"
        panel.message = "Pick the project folder Relay runs in. Relay Watch only reads it."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let bookmark = try? url.bookmarkData(
            options: [.withSecurityScope], includingResourceValuesForKeys: nil, relativeTo: nil
        )
        if let bookmark {
            UserDefaults.standard.set(bookmark, forKey: bookmarkKey)
            bookmarkData = bookmark
            UserDefaults.standard.set(url.path, forKey: pathKey)
            open(RelayFolder(root: url))
        } else {
            // Non-sandboxed development build: keep the path so `swift run` still works.
            UserDefaults.standard.set(url.path, forKey: pathKey)
            open(RelayFolder(root: url))
        }
    }

    private func restoreBookmark() {
        if let path = ProcessInfo.processInfo.environment["RELAY_WATCH_PROJECT"], !path.isEmpty {
            // Development and the documented energy test can point the app at a fixture project.
            open(RelayFolder(root: URL(fileURLWithPath: path)))
            return
        }
        if let data = UserDefaults.standard.data(forKey: bookmarkKey),
           let folder = try? RelayFolder(bookmark: data) {
            bookmarkData = data
            open(folder)
            return
        }
        if let path = UserDefaults.standard.string(forKey: pathKey) {
            open(RelayFolder(root: URL(fileURLWithPath: path)))
        }
    }

    private func open(_ folder: RelayFolder) {
        self.folder = folder
        projectName = folder.root.lastPathComponent
        reader = JournalReader(folder: folder)
        store = JournalStore(capacity: 5_000)
        filter = EventFilter()
        events = []
        chain = ChainStatus.empty
        error = nil
        hasOlderDays = false
        do {
            let initial = try reader?.readInitial(dayCount: 2) ?? []
            store.append(initial)
            events = store.events
            hasOlderDays = reader?.hasOlderDays ?? false
            reloadSnapshot()
        } catch {
            self.error = "\(error.localizedDescription)"
        }
        startWatcher(for: folder)
    }

    private func startWatcher(for folder: RelayFolder) {
        watcher?.stop()
        // Watch only the journal (plus the two files whose changes matter between events).
        // Watching `.relay/` recursively made the monitor/orchestrator heartbeats wake the
        // app every few seconds for nothing (N7).
        let journal = folder.url(".relay/journal")
        var paths = [journal]
        for relative in [".relay/STATE.json", ".relay/PENDING_DECISION.md"] {
            let url = folder.url(relative)
            if FileManager.default.fileExists(atPath: url.path) { paths.append(url) }
        }
        let watcher = FSEventsWatcher(paths: paths, latency: 3.0) { [weak self] paths in
            // Heartbeats are written every 10-25 s; they must not wake the UI (N7).
            guard WatchPaths.hasRelevant(paths) else { return }
            Task { @MainActor in self?.filesChanged() }
        }
        self.watcher = watcher
        watcher.start()
    }

    // MARK: - Reading (never writing)

    private func filesChanged() {
        guard let reader else { return }
        do {
            let newEvents = try reader.readNewEvents()
            guard !newEvents.isEmpty else { return }
            store.append(newEvents)
            hasOlderDays = reader.hasOlderDays
            var meaningful = false
            for event in newEvents where filter.shouldRefresh(event) {
                meaningful = true
            }
            events = store.events
            if meaningful {
                reloadSnapshot()
                refreshCount += 1
            }
        } catch {
            self.error = "\(error.localizedDescription)"
        }
    }

    func loadOlder() {
        guard let reader else { return }
        do {
            let older = try reader.loadOlder()
            hasOlderDays = reader.hasOlderDays
            guard !older.isEmpty else { return }
            // Older events go in front, keeping journal order; the RAM cap still applies.
            var combined = older + store.events
            if combined.count > store.capacity {
                combined.removeFirst(combined.count - store.capacity)
            }
            store = JournalStore(capacity: store.capacity)
            store.append(combined)
            events = store.events
            chain = ChainChecker.check(events)
        } catch {
            self.error = "\(error.localizedDescription)"
        }
    }

    private func reloadSnapshot() {
        guard let folder else { return }
        snapshot = RelaySnapshot.load(folder: folder)
        chain = ChainChecker.check(store.events)
        if let message = snapshot.loadErrors.first {
            error = message
        }
        if let event = store.events.last(where: { EventFilter.meaningful.contains($0.type) }) {
            lastEventID = event.id
            lastUpdate = "\(event.shortTime) (\(event.id))"
        } else {
            lastUpdate = "no events yet"
        }
        scheduleStalenessCheck()
    }

    /// One wake-up at heartbeat + 5 minutes, so a dead Relay turns the glyph red without any
    /// new journal event (N7). Once stale, no further timer is armed; a file event re-arms it.
    private func scheduleStalenessCheck() {
        stalenessTimer?.invalidate()
        stalenessTimer = nil
        guard let heartbeat = snapshot.orchestratorHeartbeat, let age = heartbeat.age() else { return }
        guard age < 300 else { return }
        let timer = Timer.scheduledTimer(withTimeInterval: max(1, 300 - age + 1), repeats: false) {
            [weak self] _ in
            Task { @MainActor in
                self?.reloadSnapshot()
                self?.scheduleStalenessCheck()
            }
        }
        timer.tolerance = 10
        RunLoop.main.add(timer, forMode: .common)
        stalenessTimer = timer
    }

    func inspectorEvent(for round: Int) -> JournalEvent? {
        store.events.last { $0.round == round && $0.type == "turn.end" }
            ?? store.events.last { $0.round == round && $0.type == "turn.start" }
    }

    // MARK: - Heartbeat (30 s, only while the UI is open)

    func uiAppeared() {
        visibleCount += 1
        startHeartbeatTimer()
    }

    func uiDisappeared() {
        visibleCount = max(0, visibleCount - 1)
        if visibleCount == 0 { stopHeartbeatTimer() }
    }

    private func startHeartbeatTimer() {
        guard heartbeatTimer == nil else { return }
        let timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.reloadSnapshot() }
        }
        timer.tolerance = 10
        RunLoop.main.add(timer, forMode: .common)
        heartbeatTimer = timer
    }

    private func stopHeartbeatTimer() {
        heartbeatTimer?.invalidate()
        heartbeatTimer = nil
    }

    deinit {
        heartbeatTimer?.invalidate()
        stalenessTimer?.invalidate()
    }

    // MARK: - Menu bar

    var menuGlyph: String {
        MenuGlyph.symbol(
            phase: snapshot.state?.phase,
            pendingDecision: pendingDecision != nil,
            halt: snapshot.halt != nil,
            paused: snapshot.state?.phase == "PAUSED",
            stopped: snapshot.pause != nil || snapshot.stop != nil,
            chainOK: chainHealthy,
            error: error != nil,
            heartbeatAge: snapshot.orchestratorHeartbeat?.age()
        )
    }

    private var chainHealthy: Bool {
        guard chain.ok else { return false }
        if let verification = ChainChecker.monitorVerification(events), verification.ok == false {
            return false
        }
        return true
    }

    var menuColor: Color {
        switch menuGlyph {
        case "circle.fill": return .green
        case "questionmark.circle.fill": return .orange
        case "exclamationmark.octagon.fill": return .red
        default: return .secondary
        }
    }

    var roundLabel: String {
        if let round = snapshot.state?.round { return "\(round)" }
        return "–"
    }

    func copyToPasteboard(_ text: String) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }
}
