import Foundation
import SwiftData

/// On-device cache (§9: "Core Data or SQLite via GRDB"; SwiftData is Core
/// Data underneath). Deliberately not a relational mirror of the backend
/// schema — it caches each server response verbatim, keyed by endpoint, so
/// offline reads show exactly what the server last decided.
///
/// This is a hard requirement, not a nice-to-have: §8.1's adaptive grouping
/// (mode, per-group style, ordering) is computed server-side in
/// `api/presentation.py` specifically so there is one source of truth for
/// layout. Caching raw items and re-deriving groups on-device would create
/// a second, divergent implementation of that logic — exactly what
/// PLAN.md's Phase 2 notes warn against. Caching the rendered response
/// instead means "offline" and "online" always render through the same
/// code path.
@Model
final class CachedResponse {
    @Attribute(.unique) var key: String
    var payload: Data
    var savedAt: Date

    init(key: String, payload: Data, savedAt: Date = .now) {
        self.key = key
        self.payload = payload
        self.savedAt = savedAt
    }
}

@ModelActor
actor LocalStore {
    static let schema = Schema([CachedResponse.self])

    static func makeContainer(inMemory: Bool = false) -> ModelContainer {
        let configuration: ModelConfiguration
        if inMemory {
            configuration = ModelConfiguration(isStoredInMemoryOnly: true)
        } else {
            // On a fresh install "Application Support" doesn't exist yet, and
            // SwiftData's default store URL there fails to open (file-write-
            // create denied) — crashing at launch. Point at an explicit URL
            // and make sure the directory exists first.
            let dir = URL.applicationSupportDirectory
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            configuration = ModelConfiguration(url: dir.appending(path: "Lifeline.store"))
        }
        // swiftlint:disable:next force_try — a container failing to open is
        // unrecoverable at launch; the app has no reasonable degraded mode.
        return try! ModelContainer(for: schema, configurations: [configuration])
    }

    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    func save<T: Encodable & Sendable>(_ value: T, forKey key: String) throws {
        let payload = try encoder.encode(value)
        let descriptor = FetchDescriptor<CachedResponse>(predicate: #Predicate { $0.key == key })
        if let existing = try modelContext.fetch(descriptor).first {
            existing.payload = payload
            existing.savedAt = .now
        } else {
            modelContext.insert(CachedResponse(key: key, payload: payload))
        }
        try modelContext.save()
    }

    func load<T: Decodable & Sendable>(_ type: T.Type, forKey key: String) throws -> (value: T, savedAt: Date)? {
        let descriptor = FetchDescriptor<CachedResponse>(predicate: #Predicate { $0.key == key })
        guard let entry = try modelContext.fetch(descriptor).first else { return nil }
        return (try decoder.decode(T.self, from: entry.payload), entry.savedAt)
    }

    /// Metadata-only read — avoids forcing a payload shape (some cache
    /// entries are top-level JSON arrays, which a placeholder `Decodable`
    /// struct can't stand in for).
    func savedAt(forKey key: String) throws -> Date? {
        let descriptor = FetchDescriptor<CachedResponse>(predicate: #Predicate { $0.key == key })
        return try modelContext.fetch(descriptor).first?.savedAt
    }
}

/// Cache keys — one per endpoint the app needs to survive offline.
enum CacheKey {
    static let today = "today"
    static let conversations = "conversations"
    static let history = "history"
}
