import SwiftUI

/// Shared visual vocabulary for interruption levels (§6.1) — used by every
/// screen that renders items, so Time-Sensitive/Active/Passive always look
/// the same regardless of which list they're in.
extension InterruptionLevel {
    var color: Color {
        switch self {
        case .timeSensitive: return .red
        case .active: return Color("AccentColor")
        case .passive, .unknown: return .secondary
        }
    }

    var symbol: String {
        switch self {
        case .timeSensitive: return "exclamationmark.triangle.fill"
        case .active: return "circle.fill"
        case .passive, .unknown: return "moon.zzz.fill"
        }
    }

    var label: String {
        switch self {
        case .timeSensitive: return "Time-Sensitive"
        case .active: return "Active"
        case .passive: return "Passive"
        case .unknown: return "Other"
        }
    }
}

extension ItemType {
    var symbol: String {
        switch self {
        case .purchase: return "cart"
        case .event: return "calendar"
        case .promise: return "hand.raised"
        case .followup: return "arrow.uturn.left"
        case .reading: return "book"
        case .question: return "questionmark.circle"
        case .unknown: return "circle"
        }
    }
}

/// Formats an item's `entities.date` relative to now — "due tomorrow",
/// "was due 3 days ago" — the same vocabulary the ranking engine's
/// explanations use server-side, kept consistent on the client.
enum DueDateFormatter {
    static func label(for item: Item, relativeTo now: Date = .now) -> String? {
        guard let due = item.dueDate else { return nil }
        let days = due.timeIntervalSince(now) / 86400

        switch days {
        case ..<(-1): return "was due \(relativeDays(-days)) ago"
        case -1..<0: return "was due yesterday"
        case 0..<1:
            // Due today — lead with the clock time, the way the mockup does.
            if Calendar.current.isDateInToday(due) {
                let f = DateFormatter()
                f.dateFormat = "h:mm a"
                return "due \(f.string(from: due))"
            }
            return "due today"
        case 1..<2: return "due tomorrow"
        default: return "due in \(relativeDays(days))"
        }
    }

    private static func relativeDays(_ days: Double) -> String {
        let rounded = max(1, Int(days.rounded()))
        return rounded == 1 ? "1 day" : "\(rounded) days"
    }
}
