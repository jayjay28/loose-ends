import SwiftUI

/// The panel behind the icon. Reads top to bottom as an answer: what it is
/// doing, what it can see, and how to stop it.
struct MenuPanel: View {
    @Bindable var engine: EngineMonitor
    @Environment(\.openURL) private var openURL

    private let brand = Color(red: 0.12, green: 0.38, blue: 0.34)

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().padding(.vertical, 10)
            if engine.state.isRunning { sourceList }
            actions
        }
        .padding(14)
        .frame(width: 268)
    }

    // MARK: - what it is doing

    private var header: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 7) {
                Circle()
                    .fill(statusColor)
                    .frame(width: 8, height: 8)
                Text("Loose Ends")
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                if let spend = engine.spendToday {
                    Text(spend)
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
            }
            Text(statusLine)
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if case .degraded(_, let why) = engine.state {
                Text(why)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.orange)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 3)
            } else if let error = engine.lastError, !error.isEmpty {
                Text(error)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.red)
                    .lineLimit(2)
                    .padding(.top, 2)
            }
        }
    }

    private var statusColor: Color {
        switch engine.state {
        case .running:      return brand
        case .degraded:     return .orange
        case .paused:       return .secondary
        case .unreachable:  return .orange
        case .notInstalled: return .secondary
        }
    }

    private var statusLine: String {
        switch engine.state {
        case .running(let open):
            return "Reading your Mac · \(open) open loose end\(open == 1 ? "" : "s")"
        case .degraded(let open, _):
            return "Reading, but thinking with rules · \(open) open — quality is reduced"
        case .paused:
            return "Paused — nothing is being read"
        case .unreachable:
            return "Installed, but not answering. It may still be starting."
        case .notInstalled:
            return "No engine installed on this Mac"
        }
    }

    // MARK: - what it can see

    private var sourceList: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(engine.sources, id: \.name) { source in
                HStack(spacing: 6) {
                    Image(systemName: source.ok ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 10))
                        .foregroundStyle(source.ok ? brand : Color.secondary)
                    Text(source.name)
                        .font(.system(size: 11.5))
                        .foregroundStyle(source.ok ? .primary : .secondary)
                }
            }
        }
        .padding(.bottom, 12)
    }

    // MARK: - how to stop it

    private var actions: some View {
        VStack(alignment: .leading, spacing: 2) {
            if engine.state.isRunning || engine.state == .unreachable {
                // The honest verb. This unloads the job — the engine really
                // stops, rather than the icon going quiet while it keeps
                // reading, which would be the version worth distrusting.
                row("Pause the engine", "pause.circle") { engine.pause() }
            } else if engine.state == .paused {
                row("Start the engine", "play.circle") { engine.resume() }
            }
            row("Open setup…", "gearshape") { engine.openSetup() }
            row("Show logs…", "doc.text") { engine.openLogs() }
            Divider().padding(.vertical, 5)
            row("Quit this menu icon", "xmark.circle") { NSApplication.shared.terminate(nil) }
            Text("Quitting hides this icon. The engine keeps running — use Pause to stop it.")
                .font(.system(size: 10))
                .foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 3)
        }
    }

    private func row(_ title: String, _ symbol: String,
                     action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 7) {
                Image(systemName: symbol)
                    .font(.system(size: 11))
                    .frame(width: 14)
                Text(title).font(.system(size: 12))
                Spacer()
            }
            .contentShape(Rectangle())
            .padding(.vertical, 3)
        }
        .buttonStyle(.plain)
    }
}
