import Foundation

/// The proactive read — mirrors `BriefingOut`. The server decides what's worth
/// surfacing; this view just renders it.
struct Briefing: Codable, Sendable {
    var generatedAt: String
    var mode: String                 // morning | day | evening
    var caughtUp: Bool
    var oneNow: Item?
    var waiting: [WaitingPerson] = []
}

/// The context engine's grounded read on an item — mirrors `/items/{id}/enriched`.
struct Enriched: Codable, Sendable {
    var headline: String
    var briefing: String
    var sources: [String] = []
}

/// The receipts behind a surfaced item — mirrors `/items/{id}/dossier`.
struct Dossier: Codable, Sendable {
    var why: [String] = []
    var yourLastWord: ContextMessage?      // your last words in the conversation
    var awaitingReply: Bool = false        // you spoke last; no answer yet
    var messages: [ContextMessage] = []    // the surrounding conversation
}

/// A person who's waiting on you — mirrors `WaitingPersonOut`.
struct WaitingPerson: Codable, Identifiable, Hashable, Sendable {
    var personId: String?
    var person: String
    var tieStrength: Double
    var tieLabel: String
    var waitedSince: String
    var openCount: Int
    var topItem: Item

    var id: String { personId ?? person }
}

extension WaitingPerson {
    /// A ConversationSummary so tapping a waiting person opens their open items in the
    /// same actionable per-person view Conversations uses (send / done / batch).
    var asConversation: ConversationSummary {
        ConversationSummary(
            personId: personId, person: person, relationship: nil,
            sources: [topItem.source], openCount: openCount,
            topLevel: topItem.interruptionLevel, lastActivity: waitedSince, topic: nil
        )
    }
}
