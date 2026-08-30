import Foundation

/// §6.1 — mirrors `InterruptionLevel` in the backend. Kept as a raw-string
/// enum with a `.unknown` fallback rather than a strict enum: the server
/// owns this vocabulary and a client that hard-fails on an unrecognized
/// value is worse than one that just renders it plainly.
enum InterruptionLevel: String, Codable, CaseIterable, Sendable {
    case timeSensitive = "time_sensitive"
    case active
    case passive
    case unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = InterruptionLevel(rawValue: raw) ?? .unknown
    }

    var sortOrder: Int {
        switch self {
        case .timeSensitive: return 0
        case .active: return 1
        case .passive, .unknown: return 2
        }
    }
}

enum ItemType: String, Codable, Sendable {
    case purchase, event, promise, followup, reading, question, unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ItemType(rawValue: raw) ?? .unknown
    }
}

enum ItemStatus: String, Codable, Sendable {
    case pending, completed, snoozed, dismissed, unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ItemStatus(rawValue: raw) ?? .unknown
    }
}

enum BehaviorPattern: String, Codable, Sendable {
    case avoidance, deprioritized
}

struct Entities: Codable, Hashable, Sendable {
    var item: String?
    var date: String?
    var link: String?
}

struct Explanation: Codable, Hashable, Identifiable, Sendable {
    var signal: String
    var value: Double
    var weight: Double
    var contribution: Double
    var detail: String

    var id: String { signal }
}

/// Mirrors `ItemOut` in `api/schemas.py`.
struct Item: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var source: String
    var conversationId: String
    var person: String
    var personId: String?
    var timestamp: String
    var type: ItemType
    var rawText: String
    var entities: Entities
    var suggestedAction: String
    var suggestedReply: String?
    var status: ItemStatus
    // §v1.4 surfacing axis: an action you owe, or information worth knowing.
    // Optional so decoding survives a backend that predates the field.
    var kind: String?                    // action|information
    var category: String?                // information: discovery|context|external
    var createdAt: String
    var updatedAt: String
    var score: Double
    var interruptionLevel: InterruptionLevel
    var why: [Explanation]
    var behaviorPattern: BehaviorPattern?
    var snoozedUntil: String?
    var completedAt: String?
    var completedBy: String?
    var linksToItemId: String?
    // actions (v1.1)
    var handle: String?
    var link: String?
    var linkKind: String?
}

extension String {
    /// Collapse runs of blank lines and trailing space — quoted email bodies
    /// preserve the original spacing, producing huge dead vertical gaps.
    var tightened: String {
        replacingOccurrences(of: "[ \\t]*\\n[ \\t]*(\\n[ \\t]*)+", with: "\n\n", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

extension Item {
    /// §v1.4 — information cards read differently and ack instead of "done".
    var isInformation: Bool { kind == "information" }

    /// On the stack: snoozed and the wake time hasn't passed. (The server keeps
    /// status "snoozed" after the wake passes; the comparison is what decides.)
    var isStillSnoozed: Bool {
        guard status == .snoozed else { return false }
        guard let raw = snoozedUntil,
              let wake = ISO8601DateFormatter.lifeline.date(from: raw) else { return true }
        return wake > Date.now
    }

    /// Parses `entities.date` / deadline-style fields — used by views that
    /// need to know "how soon", not just the raw ISO string.
    var dueDate: Date? {
        guard let raw = entities.date else { return nil }
        return ISO8601DateFormatter.lifeline.date(from: raw)
    }

    // MARK: Action targets (v1.1) — one source of truth for row + carousel.

    /// Opens Messages to the counterpart with the drafted reply prefilled.
    /// URLComponents, not interpolation: the `sms:…&body=` form is an
    /// iOS-8-era trick modern iOS silently refuses to open.
    var sendMessagesURL: URL? {
        guard let handle, let reply = suggestedReply, !reply.isEmpty else { return nil }
        var components = URLComponents()
        components.scheme = "sms"
        components.path = handle
        components.queryItems = [URLQueryItem(name: "body", value: reply)]
        return components.url
    }
    /// The link to read/watch, if any.
    var openLinkURL: URL? { link.flatMap { URL(string: $0) } }
    /// The dialer, when the handle is a phone number.
    var callURL: URL? {
        guard let handle, handle.hasPrefix("+") || handle.allSatisfy(\.isNumber) else { return nil }
        return URL(string: "tel:\(handle)")
    }
    var isVideoLink: Bool { linkKind == "video" }
}

extension ISO8601DateFormatter {
    /// Parses the backend's second-precision timestamps, e.g.
    /// `2026-07-27T09:00:00+00:00`. `nonisolated(unsafe)`: configured once
    /// here and never mutated afterward, so concurrent reads are safe even
    /// though `ISO8601DateFormatter` itself predates `Sendable`.
    nonisolated(unsafe) static let lifeline: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()
}
