import Foundation

/// §8.1 — level-of-detail hint the server computed for this group.
enum GroupStyle: String, Codable, Sendable {
    case expanded, compact, collapsed, unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = GroupStyle(rawValue: raw) ?? .unknown
    }
}

/// §8.1 — "layout is system-determined based on context." The client never
/// computes this; it only renders whatever the server decided.
enum TodayMode: String, Codable, Sendable {
    case empty, surge, briefing, day, evening, unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = TodayMode(rawValue: raw) ?? .unknown
    }
}

struct ItemGroup: Codable, Identifiable, Hashable, Sendable {
    var level: InterruptionLevel
    var title: String
    var subtitle: String?
    var style: GroupStyle
    var items: [Item]

    var id: String { level.rawValue }
}

/// Mirrors `TodayOut`.
struct Today: Codable, Sendable {
    var mode: TodayMode
    var headline: String
    var subhead: String?
    var generatedAt: String
    var groups: [ItemGroup]
    var confirmations: [Confirmation]
    var counts: [String: Int]
    var carousel: [Item] = []
}

// `carousel` is additive (v1.1). Decode it tolerantly so the app keeps working
// against an older server that doesn't send the key yet — a missing additive
// field should never blank out Today. In an extension so the memberwise
// initializer (used by SampleData) is still synthesized.
extension Today {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mode = try c.decode(TodayMode.self, forKey: .mode)
        headline = try c.decode(String.self, forKey: .headline)
        subhead = try c.decodeIfPresent(String.self, forKey: .subhead)
        generatedAt = try c.decode(String.self, forKey: .generatedAt)
        groups = try c.decode([ItemGroup].self, forKey: .groups)
        confirmations = try c.decode([Confirmation].self, forKey: .confirmations)
        counts = try c.decode([String: Int].self, forKey: .counts)
        carousel = try c.decodeIfPresent([Item].self, forKey: .carousel) ?? []
    }
}

/// One line of the surrounding conversation, for the in-card context strip.
struct ContextMessage: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var sender: String
    var isFromUser: Bool
    var timestamp: String
    var text: String
    var isPivot: Bool
}

/// A short window of a conversation around a surfaced item — the memory-jog that
/// replaces a single orphaned line. Mirrors `ConversationContextOut`.
struct ConversationContext: Codable, Sendable {
    var itemId: String
    var conversationId: String
    var person: String
    var messages: [ContextMessage]
}

struct ConversationSummary: Codable, Identifiable, Hashable, Sendable {
    var personId: String?
    var person: String
    var relationship: String?
    var sources: [String]
    var openCount: Int
    var topLevel: InterruptionLevel
    var lastActivity: String?
    var topic: String?
    var tieStrength: Double?          // 0..1; optional so an older server can't break decode

    var id: String { personId ?? person }
}

struct HistoryEntry: Codable, Identifiable, Hashable, Sendable {
    var item: Item
    var closedBy: String
    var closedAt: String?
    var evidence: String?
    var evidenceSource: String?

    var id: String { item.id }
}

struct History: Codable, Sendable {
    var entries: [HistoryEntry]
    var autoClosed: Int
    var manualClosed: Int
    var streakDays: Int
}

struct Confirmation: Codable, Identifiable, Hashable, Sendable {
    var signalId: String
    var item: Item
    var source: String
    var confidence: Double
    var evidenceText: String
    var reasons: [String]
    var detectedAt: String

    var id: String { signalId }
}

// MARK: - Requests

struct SnoozeRequest: Encodable, Sendable {
    var hours: Double?
    var until: String?
}

/// Phase D — the items the user staged to clear together.
struct BatchRequest: Encodable, Sendable {
    var itemIds: [String]
}

/// One reply that answers several owed items to one person.
struct DraftBatch: Codable, Sendable {
    var reply: String
    var handle: String?
    var itemIds: [String]
}

struct BatchDone: Codable, Sendable {
    var completed: [String]
}

struct DeviceRequest: Encodable, Sendable {
    var token: String
    var platform: String = "ios"
}

struct ImportRequest: Encodable, Sendable {
    var source: String
    var path: String
    var contactName: String?
    var isGroup: Bool = false
}

struct ActionResult: Codable, Sendable {
    var ok: Bool
    var item: Item?
    var detail: String?
}

struct SyncChanges: Codable, Sendable {
    var serverTime: String
    var items: [Item]
    var confirmations: [Confirmation]
}
