import Foundation

/// One piece of the model of you — mirrors `FactOut` (§v1.4 pillar B).
struct FactItem: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var subjectType: String            // self|person|topic
    var subjectId: String?
    var statement: String
    var predicate: String?
    var value: String?
    var source: String                 // user|derived
    var confidence: Double
    var status: String                 // active|dismissed
    var updatedAt: String
}

/// What `/tell` captured from the user's words.
struct TellResponse: Codable, Sendable {
    var reply: String
    var facts: [FactItem] = []
}

/// Facts grouped under one person or topic — mirrors `PersonFactsOut`.
struct SubjectFacts: Codable, Identifiable, Hashable, Sendable {
    var personId: String?
    var name: String
    var tieLabel: String?
    var facts: [FactItem] = []

    var id: String { personId ?? name }
}

/// One tool call behind an answer — mirrors `TraceStepOut` (§v1.5). The
/// sanity trail: what fired, what it found, honest about misses.
struct TraceStep: Codable, Identifiable, Hashable, Sendable {
    var tool: String
    var summary: String
    var ok: Bool

    var id: String { tool + summary }
}

/// What `/converse` came back with — mirrors `ConverseOut`. `sessionId` is
/// carried into the next turn so the aide remembers what was just said.
struct ConverseResponse: Codable, Sendable {
    var reply: String
    var sessionId: String
    var facts: [FactItem] = []
    var draft: MessageDraft?
    var trace: [TraceStep] = []
}

/// A message the aide wrote for you to review and send — mirrors `DraftOut`.
/// The app never sends on its own; one tap opens Messages/Mail prefilled.
struct MessageDraft: Codable, Hashable, Sendable {
    var person: String
    var personId: String?
    var handle: String?
    var channel: String                // imessage|email
    var text: String

    /// Opens the right app with the draft prefilled — review, then send.
    ///
    /// Built with URLComponents, not string interpolation. The old
    /// `sms:<number>&body=` form was an iOS-8-era trick that modern iOS
    /// refuses to open: `UIApplication.open` completes with success=false and
    /// the tap does nothing at all — a dead button with no error anywhere.
    /// `.urlQueryAllowed` also left `&` and `+` unescaped inside the body, so
    /// a draft containing either arrived truncated or with stray spaces.
    var sendURL: URL? {
        guard let handle else { return nil }
        var components = URLComponents()
        components.scheme = channel == "email" ? "mailto" : "sms"
        components.path = handle
        components.queryItems = [URLQueryItem(name: "body", value: text)]
        return components.url
    }
}

/// One persisted turn — mirrors `TurnOut`.
struct ConversationTurn: Codable, Identifiable, Sendable {
    var id: String
    var role: String                   // user|assistant
    var text: String
    var facts: [FactItem] = []
    var trace: [TraceStep] = []
    var createdAt: String
}

/// A conversation's transcript — mirrors `ConversationOut`.
struct Conversation: Codable, Sendable {
    var sessionId: String
    var turns: [ConversationTurn] = []
}

/// Everything the system believes — mirrors `ModelOut`. Inspectable and
/// editable: the user overrules the system, never the reverse.
struct ModelOfYou: Codable, Sendable {
    var you: [FactItem] = []
    var people: [SubjectFacts] = []
    var topics: [SubjectFacts] = []

    var isEmpty: Bool { you.isEmpty && people.isEmpty && topics.isEmpty }
}
