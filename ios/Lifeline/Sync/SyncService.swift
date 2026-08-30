import Foundation
import SwiftData

/// Cache-first-then-refresh glue between `APIClient` and `LocalStore`.
/// Every screen follows the same shape: show what's on disk immediately
/// (instant, works offline), then replace it with a live fetch.
final class SyncService: Sendable {
    let api: APIClient
    let store: LocalStore

    init(api: APIClient = .shared, store: LocalStore) {
        self.api = api
        self.store = store
    }

    /// Cached value, if any, decoded and returned immediately.
    func cached<T: Decodable & Sendable>(_ type: T.Type, key: String) async -> T? {
        (try? await store.load(type, forKey: key))?.value
    }

    func cachedAge(key: String) async -> Date? {
        (try? await store.savedAt(forKey: key)) ?? nil
    }

    func refreshToday(at: Date? = nil) async throws -> Today {
        let today = try await api.today(at: at)
        try? await store.save(today, forKey: CacheKey.today)
        return today
    }

    func refreshConversations() async throws -> [ConversationSummary] {
        let conversations = try await api.conversations()
        try? await store.save(conversations, forKey: CacheKey.conversations)
        return conversations
    }

    func refreshHistory(limit: Int = 100) async throws -> History {
        let history = try await api.history(limit: limit)
        try? await store.save(history, forKey: CacheKey.history)
        return history
    }
}
