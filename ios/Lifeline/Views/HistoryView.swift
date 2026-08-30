import Observation
import SwiftUI

/// §8.2 History — what closed and *how* (auto-detected vs. you did it),
/// plus the streak. Read-only.
@MainActor
@Observable
final class HistoryViewModel {
    private(set) var history: History?
    private(set) var errorMessage: String?

    private let sync: SyncService
    init(sync: SyncService) { self.sync = sync }

    func load() async {
        if ConversationsViewModel.usesSample { history = SampleData.history; return }
        if history == nil, let cached = await sync.cached(History.self, key: CacheKey.history) {
            history = cached
        }
        await refresh()
    }

    func refresh() async {
        do { history = try await sync.refreshHistory(); errorMessage = nil }
        catch { if history == nil { errorMessage = error.localizedDescription } }
    }
}

struct HistoryView: View {
    @Environment(\.syncService) private var syncService
    @State private var viewModel: HistoryViewModel?

    var body: some View {
        NavigationStack {
            content
                .background(Theme.paper.ignoresSafeArea())
                .navigationTitle("History")
                .task {
                    if viewModel == nil { viewModel = HistoryViewModel(sync: syncService) }
                    await viewModel?.load()
                }
                .refreshable { await viewModel?.refresh() }
        }
    }

    @ViewBuilder
    private var content: some View {
        if let history = viewModel?.history {
            if history.entries.isEmpty {
                QuietState(title: "Nothing closed yet", subhead: "Finished items show up here.")
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        HistoryHeader(history: history)
                        ForEach(history.entries) { entry in
                            HistoryRow(entry: entry)
                            Rectangle().fill(Theme.ruleSoft).frame(height: 1)
                        }
                    }
                    .padding(.horizontal, 22)
                    .padding(.top, 8)
                    .padding(.bottom, 40)
                }
            }
        } else if viewModel?.errorMessage != nil {
            QuietState(title: "Couldn't load history", subhead: viewModel?.errorMessage ?? "")
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

private struct HistoryHeader: View {
    let history: History

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(history.streakDays == 1 ? "1-day streak" : "\(history.streakDays)-day streak")
                .font(Theme.serif(26, .medium))
                .foregroundStyle(Theme.ink)
            HStack(spacing: 18) {
                stat("\(history.autoClosed)", "auto-closed")
                stat("\(history.manualClosed)", "you closed")
            }
        }
        .padding(.top, 8).padding(.bottom, 12)
    }

    private func stat(_ value: String, _ label: String) -> some View {
        HStack(spacing: 6) {
            Text(value).font(.system(size: 15, weight: .semibold)).monospacedDigit().foregroundStyle(Theme.ink)
            Text(label).font(.system(size: 13)).foregroundStyle(Theme.inkFaint)
        }
    }
}

private struct HistoryRow: View {
    let entry: HistoryEntry

    private var wasAuto: Bool { entry.closedBy.lowercased().contains("auto") }

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            ZStack {
                Circle().fill(wasAuto ? Theme.goodSoft : Theme.chip).frame(width: 22, height: 22)
                Image(systemName: "checkmark")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(wasAuto ? Theme.good : Theme.inkFaint)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(entry.item.suggestedAction.isEmpty ? entry.item.rawText : entry.item.suggestedAction)
                    .font(.system(size: 15))
                    .foregroundStyle(Theme.inkFaint)
                    .strikethrough(color: Theme.inkGhost)
                    .fixedSize(horizontal: false, vertical: true)
                Text(closedLine)
                    .font(.system(size: 12.5))
                    .foregroundStyle(Theme.inkFaint)
            }
            Spacer()
        }
        .padding(.vertical, 13)
    }

    private var closedLine: String {
        var parts: [String] = []
        parts.append(wasAuto ? "Auto-closed" : "You closed it")
        if let ev = entry.evidence, !ev.isEmpty { parts.append(ev) }
        if let when = RelativeTime.label(from: entry.closedAt) { parts.append(when) }
        return parts.joined(separator: " · ")
    }
}

// MARK: - Shared small pieces

/// The quiet, calm empty/error state used across the secondary tabs.
struct QuietState: View {
    let title: String
    let subhead: String

    var body: some View {
        VStack(spacing: 6) {
            Text(title).font(Theme.serif(22, .medium)).foregroundStyle(Theme.ink)
            Text(subhead).font(Theme.serif(15)).foregroundStyle(Theme.inkSoft).multilineTextAlignment(.center)
        }
        .padding(.horizontal, 42)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// "3h ago", "yesterday", "2 days ago" from an ISO-8601 string.
enum RelativeTime {
    static func label(from iso: String?, relativeTo now: Date = .now) -> String? {
        guard let iso, let date = ISO8601DateFormatter.lifeline.date(from: iso) else { return nil }
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .full
        return f.localizedString(for: date, relativeTo: now)
    }
}
