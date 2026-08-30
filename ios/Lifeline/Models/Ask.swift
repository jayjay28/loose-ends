import Foundation

/// §v2.9 — an answer is a card, not a chat turn: the answer, the receipts it
/// rests on, the facts the system already knew (correctable on the spot), and
/// the trace of how it looked. Mirrors `AskOut`.
struct AskCard: Codable, Identifiable, Sendable {
    var id: String
    var question: String
    var answer: String
    var receipts: [Receipt] = []
    var knew: [KnownFact] = []
    var trace: [TraceStep] = []
    var createdAt: String

    /// Tolerant decode: additive fields must never blank the surface.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        question = try c.decodeIfPresent(String.self, forKey: .question) ?? ""
        answer = try c.decode(String.self, forKey: .answer)
        receipts = (try? c.decode([Receipt].self, forKey: .receipts)) ?? []
        knew = (try? c.decode([KnownFact].self, forKey: .knew)) ?? []
        trace = (try? c.decode([TraceStep].self, forKey: .trace)) ?? []
        createdAt = try c.decodeIfPresent(String.self, forKey: .createdAt) ?? ""
    }
}

/// One source an answer rests on — tappable, openable, checkable.
struct Receipt: Codable, Identifiable, Hashable, Sendable {
    var kind: String              // message | fact
    var refId: String
    var source: String = ""       // imessage | gmail | world
    var label: String
    var detail: String = ""

    var id: String { "\(kind):\(refId)" }

    /// The little source tag on the chip: MAIL, TEXT, DOC.
    var sourceTag: String {
        if !detail.isEmpty { return "DOC" }        // came in via an attachment
        switch source {
        case "gmail": return "MAIL"
        case "imessage": return "TEXT"
        default: return source.uppercased()
        }
    }
}

/// A fact the world model contributed, correctable by id — the "knew" strip.
struct KnownFact: Codable, Identifiable, Hashable, Sendable {
    var factId: String
    var entityId: String
    var entity: String
    var predicate: String
    var value: String

    var id: String { factId }

    /// "Lia Carter — attends: Brightwood Pre-School", readable at a glance.
    var line: String { "\(entity) — \(predicate.replacingOccurrences(of: "_", with: " ")): \(value)" }
}

/// A message opened from a receipt chip. Mirrors `GET /messages/{id}`.
struct MessageInFull: Codable, Sendable {
    var messageId: String
    var source: String
    var sender: String
    var timestamp: String
    var subject: String = ""
    var text: String
    var attachments: [AttachmentRef] = []

    struct AttachmentRef: Codable, Sendable {
        var filename: String
        var hasText: Bool
    }
}
