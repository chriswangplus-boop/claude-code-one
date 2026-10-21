import AppKit
import RelayWatchCore
import SwiftUI

@main
struct RelayWatchApp: App {
    @StateObject private var controller = RelayController()

    var body: some Scene {
        MenuBarExtra {
            MenuContent(controller: controller)
        } label: {
            HStack(spacing: 4) {
                Image(systemName: controller.menuGlyph)
                    .symbolRenderingMode(.palette)
                    .foregroundStyle(controller.menuColor)
                Text(controller.roundLabel)
                    .font(.system(size: 11, weight: .semibold).monospacedDigit())
            }
        }
        .menuBarExtraStyle(.menu)

        Window("Relay Watch", id: "relay-watch") {
            ContentView(controller: controller)
        }
        .defaultSize(width: 980, height: 760)
        .windowResizability(.contentMinSize)
    }
}

// MARK: - Menu

struct MenuContent: View {
    @ObservedObject var controller: RelayController
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        if let state = controller.snapshot.state {
            Text("\(state.phase ?? "—") · round \(state.round ?? 0) · milestone \(state.milestone ?? 0)")
            if let turn = controller.currentTurn {
                Text("\(turn.role ?? "—") on \(turn.model ?? "—") · \(turn.effort ?? "—")")
            }
            if let decision = controller.pendingDecision {
                Divider()
                Text("Needs you: \(decision.id)")
                Text(decision.question).lineLimit(2)
                Button("Copy decision command") {
                    controller.copyToPasteboard(decision.command)
                }
            }
        } else {
            Text("No project folder yet")
        }
        Divider()
        Button("Open Relay Watch") { openWindow(id: "relay-watch") }
        Button("Choose project folder…") { controller.chooseFolder() }
        if controller.hasOlderDays {
            Button("Load older journal days") { controller.loadOlder() }
        }
        Divider()
        Button("Quit Relay Watch") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
        Color.clear.frame(height: 0)
            .onAppear { controller.uiAppeared() }
            .onDisappear { controller.uiDisappeared() }
    }
}

// MARK: - Main window

struct ContentView: View {
    @ObservedObject var controller: RelayController
    @State private var inspected: JournalEvent?

    var body: some View {
        VStack(spacing: 0) {
            if let error = controller.error {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    Text(error).font(.callout).textSelection(.enabled)
                    Spacer()
                }
                .padding(10)
                .background(Color.orange.opacity(0.12))
            }
            if controller.snapshot.state == nil {
                WelcomeView(controller: controller)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        HeaderView(controller: controller)
                        HealthLights(items: controller.health)
                        if let decision = controller.pendingDecision {
                            NeedsYouCard(decision: decision, controller: controller)
                        }
                        TimelineView(controller: controller, inspected: $inspected)
                        HStack(alignment: .top, spacing: 18) {
                            UsageView(usage: controller.usage)
                            AlertsView(alerts: controller.alerts, inspected: $inspected)
                        }
                        if controller.hasOlderDays {
                            Button("Load older journal days") { controller.loadOlder() }
                                .buttonStyle(.link)
                        }
                        Text("Read-only: Relay Watch never writes in the project and never answers a decision.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(20)
                }
            }
        }
        .frame(minWidth: 760, minHeight: 520)
        .sheet(item: $inspected) { event in
            EventInspector(event: event)
        }
        .onAppear { controller.uiAppeared() }
        .onDisappear { controller.uiDisappeared() }
    }
}

struct WelcomeView: View {
    @ObservedObject var controller: RelayController

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "eye.circle").font(.system(size: 44)).foregroundStyle(.secondary)
            Text("Pick the project folder Relay runs in").font(.title3)
            Text("Relay Watch keeps a read-only bookmark and never writes there.")
                .font(.callout).foregroundStyle(.secondary)
            Button("Choose folder…") { controller.chooseFolder() }
                .keyboardShortcut(.defaultAction)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }
}

// MARK: - Header

struct HeaderView: View {
    @ObservedObject var controller: RelayController

    private var state: RelayState? { controller.snapshot.state }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Relay Watch").font(.title2.weight(.semibold))
                    Text(controller.projectName ?? "")
                        .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
                Spacer()
                if let turn = controller.currentTurn {
                    ModelBadge(model: turn.model, effort: turn.effort)
                }
            }
            Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 4) {
                GridRow {
                    field("phase", state?.phase)
                    field("round", state?.round.map(String.init))
                    field("milestone", state?.milestone.map(String.init))
                    field("role", state?.role ?? state?.pending?.role)
                }
                GridRow {
                    field("baton", batonText)
                    field("current turn", controller.currentTurn.map { "\($0.role ?? "—") · round \($0.round ?? 0)" })
                    field("paused", state?.pausedReason)
                    Spacer()
                }
            }
            .font(.callout)
            Text("last update \(controller.lastUpdate)")
                .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
        }
    }

    private var batonText: String? {
        guard let baton = controller.snapshot.baton, let holder = baton.holder else { return nil }
        guard holder != "none" else { return "none" }
        let app = holder == "coder" ? "Zcode" : "Claude"
        return "\(holder) in \(app) (round \(baton.round ?? 0))"
    }

    private func field(_ title: String, _ value: String?) -> some View {
        HStack(spacing: 4) {
            Text(title + ":").foregroundStyle(.secondary)
            Text(value ?? "—").monospacedDigit()
        }
    }
}

struct ModelBadge: View {
    let model: String?
    let effort: String?

    private var color: Color {
        let name = (model ?? "").lowercased()
        if name.contains("opus") { return .purple }
        if name.contains("sonnet") { return .blue }
        return .secondary
    }

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text([model, effort].compactMap { $0 }.joined(separator: " · "))
                .font(.callout.weight(.medium))
                .foregroundStyle(color)
                .monospacedDigit()
        }
        .padding(.horizontal, 10).padding(.vertical, 5)
        .background(color.opacity(0.12), in: Capsule())
    }
}

// MARK: - Health

struct HealthLights: View {
    let items: [HealthItem]

    var body: some View {
        HStack(spacing: 14) {
            ForEach(items) { item in
                HStack(spacing: 5) {
                    Circle().fill(color(item.status)).frame(width: 9, height: 9)
                    Text(item.title).font(.callout)
                }
                .help(item.source)
            }
            Spacer()
            Text("hover a light for its source file").font(.caption2).foregroundStyle(.tertiary)
        }
    }

    private func color(_ status: Health) -> Color {
        switch status {
        case .green: return .green
        case .amber: return .amberFallback
        case .red: return .red
        case .unknown: return .gray
        }
    }
}

private extension Color {
    static var amberFallback: Color { Color(red: 0.85, green: 0.55, blue: 0.05) }
}

// MARK: - Needs you

struct NeedsYouCard: View {
    let decision: PendingDecision
    @ObservedObject var controller: RelayController

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: "questionmark.circle.fill").foregroundStyle(.orange)
                Text("Needs you: \(decision.id)").font(.headline)
                Spacer()
                if let kind = decision.kind {
                    Text(kind).font(.caption).foregroundStyle(.secondary)
                }
            }
            Text(decision.question).font(.callout).textSelection(.enabled)
            ForEach(Array(decision.options.enumerated()), id: \.offset) { index, option in
                Text("\(letter(index)). \(option.label) — \(option.consequence)")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            HStack {
                Text(decision.command)
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                Button("Copy command") { controller.copyToPasteboard(decision.command) }
            }
            Text("The one-time code is in the desktop notification. Relay Watch never shows it and cannot answer.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(14)
        .background(Color.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.orange.opacity(0.4)))
    }

    private func letter(_ index: Int) -> String {
        guard let scalar = UnicodeScalar(65 + index) else { return "\(index + 1)" }
        return String(Character(scalar))
    }
}

// MARK: - Timeline

struct TimelineView: View {
    @ObservedObject var controller: RelayController
    @Binding var inspected: JournalEvent?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Round timeline").font(.headline)
            if controller.timeline.isEmpty {
                Text("No rounds yet.").font(.callout).foregroundStyle(.secondary)
            } else {
                header
                ForEach(controller.timeline.reversed()) { row in
                    Button {
                        inspected = controller.inspectorEvent(for: row.round)
                    } label: {
                        rowView(row)
                    }
                    .buttonStyle(.plain)
                    .help("turn source: \(controller.inspectorEvent(for: row.round)?.id ?? "no event")")
                    Divider().opacity(0.4)
                }
            }
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            cell("round", 46).bold()
            cell("role", 80).bold()
            cell("model · effort", 170).bold()
            cell("outcome", 130).bold()
            cell("duration", 70).bold()
            cell("files", 42).bold()
            cell("commit", 66).bold()
            cell("ctx", 44).bold()
        }
        .font(.caption).foregroundStyle(.secondary)
    }

    private func rowView(_ row: RoundRow) -> some View {
        HStack(spacing: 10) {
            cell("\(row.round)", 46).monospacedDigit()
            cell(row.role ?? "—", 80)
            Text(row.modelLabel)
                .foregroundStyle((row.model ?? "").lowercased().contains("opus") ? .purple : .primary)
                .frame(width: 170, alignment: .leading)
            cell([row.outcome, row.markers.joined(separator: ", ")]
                .compactMap { $0 }
                .filter { !$0.isEmpty }
                .joined(separator: " · "), 130)
            cell(row.durationLabel, 70).monospacedDigit()
            cell(row.filesChanged.map(String.init) ?? "—", 42).monospacedDigit()
            cell(row.commit ?? "—", 66).monospacedDigit()
            cell(row.ctxPct.map { String(format: "%.0f%%", $0) } ?? "—", 44).monospacedDigit()
            Spacer(minLength: 0)
        }
        .font(.callout)
        .contentShape(Rectangle())
        .padding(.vertical, 2)
    }

    private func cell(_ text: String, _ width: CGFloat) -> some View {
        Text(text).lineLimit(1).frame(width: width, alignment: .leading)
    }
}

// MARK: - Usage

struct UsageView: View {
    let usage: UsageToday

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Usage today").font(.headline)
            Text("DeepSeek spend: \(money(usage.spendUSD))"
                 + (usage.budgetUSD.map { " / \(money($0)) budget" } ?? " (budget not in config)"))
                .font(.callout).monospacedDigit()
            Text("Opus today: \(usage.opusToday)/\(usage.opusTodayLimit), "
                 + "this milestone: \(usage.opusMilestone)/\(usage.opusMilestoneLimit)")
                .font(.callout).monospacedDigit()
            Text(usage.ratio).font(.callout).foregroundStyle(.secondary)
            Text("Source: deepseek.call cost_usd in today's journal; Opus counters in STATE.json; limits in config.json.")
                .font(.caption2).foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
    }

    private func money(_ value: Double) -> String {
        String(format: "$%.4f", value)
    }
}

// MARK: - Alerts

struct AlertsView: View {
    let alerts: [AlertItem]
    @Binding var inspected: JournalEvent?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Alerts").font(.headline)
            if alerts.isEmpty {
                Text("None.").font(.callout).foregroundStyle(.secondary)
            } else {
                ForEach(alerts.prefix(12)) { alert in
                    HStack(alignment: .top, spacing: 6) {
                        Text(alert.time).font(.caption).monospacedDigit().foregroundStyle(.secondary)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(alert.type).font(.caption.weight(.semibold))
                            Text(alert.message).font(.caption).lineLimit(3)
                        }
                    }
                }
                Text("Every alert is an event; open the round row for the raw JSON.")
                    .font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
    }
}

// MARK: - Event inspector

struct EventInspector: View {
    let event: JournalEvent
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Event \(event.id)").font(.headline).monospacedDigit()
                Spacer()
                Button("Close") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text("\(event.type) · round \(event.round) · \(event.shortTime)")
                .font(.callout).foregroundStyle(.secondary)
            ScrollView {
                Text(event.rawJSON())
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
            }
            .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
            Text("Read-only view of the journal line. Screenshots, prompts and file contents are never shown.")
                .font(.caption2).foregroundStyle(.secondary)
        }
        .padding(18)
        .frame(minWidth: 620, minHeight: 420)
    }
}
