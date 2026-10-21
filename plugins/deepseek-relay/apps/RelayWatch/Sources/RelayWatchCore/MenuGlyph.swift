import Foundation

/// The menu-bar glyph decision, kept in the core so it is testable (C7, N7).
public enum MenuGlyph {
    public static func symbol(
        phase: String?,
        pendingDecision: Bool,
        halt: Bool,
        paused: Bool,
        stopped: Bool,
        chainOK: Bool,
        error: Bool,
        heartbeatAge: TimeInterval?,
        staleAfter: TimeInterval = 300
    ) -> String {
        if phase == "DONE" { return "checkmark.circle" }
        if pendingDecision { return "questionmark.circle.fill" }
        if halt || !chainOK || error { return "exclamationmark.octagon.fill" }
        if paused || stopped { return "pause.circle" }
        let active = ["PLAN", "CODE", "REVIEW", "ROUTE"].contains(phase ?? "")
        if active, let age = heartbeatAge, age > staleAfter {
            return "exclamationmark.octagon.fill"  // active but the orchestrator is gone
        }
        if let age = heartbeatAge, age < staleAfter { return "circle.fill" }
        return "circle"
    }
}

/// Which FSEvents paths may wake the UI. Relay writes its heartbeats every 10-25 s; watching
/// those caused several wake-ups a minute with no new event (N7).
public enum WatchPaths {
    public static func isRelevant(_ path: String) -> Bool {
        // Only journal day files matter; screenshots/call records and heartbeats do not.
        if path.hasSuffix(".jsonl") && path.contains("/.relay/journal/") { return true }
        return path.hasSuffix("/.relay/STATE.json")
            || path.hasSuffix("/.relay/PENDING_DECISION.md")
    }

    public static func hasRelevant(_ paths: [String]) -> Bool {
        paths.contains(where: isRelevant)
    }
}
