import Foundation
import Observation

/// Cache-first, then refresh — the same shape every screen uses. Mutating
/// actions (done/snooze/dismiss/confirm) don't try to predict the new
/// ranking or grouping locally; they call the API, then re-fetch `/today`
/// so the server's grouping stays the single source of truth (§8.1).
@MainActor
@Observable
final class TodayViewModel {
    private(set) var today: Today?
    private(set) var isLoading = false
    private(set) var isOffline = false
    private(set) var errorMessage: String?

    /// Local-only UI state: which collapsed groups the user has manually
    /// opened, and which items are expanded to show detail. This is a
    /// disclosure toggle, not a re-grouping decision — the server's level,
    /// style and ordering are untouched.
    var expandedGroupIDs: Set<String> = []
    var expandedItemIDs: Set<String> = []

    private let sync: SyncService

    init(sync: SyncService) {
        self.sync = sync
    }

    /// Design/review builds launched with `-uiSampleData` render the mockup
    /// fixtures and skip the network entirely.
    static var usesSampleData: Bool {
        ProcessInfo.processInfo.arguments.contains("-uiSampleData")
    }

    func loadCachedThenRefresh() async {
        if Self.usesSampleData {
            today = SampleData.today
            return
        }
        if today == nil, let cached = await sync.cached(Today.self, key: CacheKey.today) {
            today = cached
            isOffline = true
        }
        await refresh()
    }

    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            today = try await sync.refreshToday()
            isOffline = false
            errorMessage = nil
        } catch {
            isOffline = true
            if today == nil {
                errorMessage = error.localizedDescription
            }
        }
    }

    func toggleGroup(_ groupID: String) {
        if expandedGroupIDs.contains(groupID) {
            expandedGroupIDs.remove(groupID)
        } else {
            expandedGroupIDs.insert(groupID)
        }
    }

    /// §6.2 revisit tracking feeds the avoidance/deprioritization read —
    /// every expand is a real signal, not just a UI toggle.
    func toggleItem(_ item: Item) {
        let isExpanding = !expandedItemIDs.contains(item.id)
        if isExpanding {
            expandedItemIDs.insert(item.id)
        } else {
            expandedItemIDs.remove(item.id)
        }
        Task { try? await sync.api.markViewed(itemId: item.id, expanded: isExpanding) }
    }

    func act(_ item: Item) async {
        _ = try? await sync.api.markActed(itemId: item.id)
    }

    func markDone(_ item: Item) async {
        _ = try? await sync.api.markDone(itemId: item.id)
        await refresh()
    }

    func snooze(_ item: Item, hours: Double) async {
        _ = try? await sync.api.snooze(itemId: item.id, hours: hours)
        await refresh()
    }

    func dismiss(_ item: Item) async {
        _ = try? await sync.api.dismiss(itemId: item.id)
        await refresh()
    }

    func confirm(_ confirmation: Confirmation) async {
        _ = try? await sync.api.confirmMatch(signalId: confirmation.signalId)
        await refresh()
    }

    func reject(_ confirmation: Confirmation) async {
        _ = try? await sync.api.rejectMatch(signalId: confirmation.signalId)
        await refresh()
    }
}
