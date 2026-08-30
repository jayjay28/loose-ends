import Foundation
import UserNotifications

/// On-device notification scheduling — no server push. Reads the latest briefing
/// and schedules a daily morning briefing plus Time-Sensitive reminders for dated
/// items. Re-scheduled every time the app syncs, so the content stays fresh
/// without a backend. (Bounded deferral / net-value delivery lives here.)
@MainActor
final class NotificationScheduler {
    static let shared = NotificationScheduler()
    private let center = UNUserNotificationCenter.current()

    private let briefingID = "briefing.morning"
    private let deadlinePrefix = "deadline."

    /// Ask once; safe to call repeatedly.
    @discardableResult
    func requestPermission() async -> Bool {
        (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
    }

    /// Re-schedule everything from the latest briefing, clearing our own pending
    /// requests first so nothing stacks up.
    func refresh(from briefing: Briefing) async {
        let settings = await center.notificationSettings()
        guard settings.authorizationStatus == .authorized ||
              settings.authorizationStatus == .provisional else { return }

        center.removeAllPendingNotificationRequests()
        if !briefing.caughtUp { scheduleMorningBriefing(briefing) }
        scheduleDeadlineReminders(briefing)
    }

    // A single daily briefing at 8am, summarising the current read. Content is
    // fixed when scheduled; the next sync refreshes it.
    private func scheduleMorningBriefing(_ briefing: Briefing) {
        let content = UNMutableNotificationContent()
        content.title = "Your briefing"
        var parts: [String] = []
        if let one = briefing.oneNow { parts.append("Do now: \(title(one))") }
        if !briefing.waiting.isEmpty {
            parts.append("\(briefing.waiting.count) waiting on you")
        }
        content.body = parts.joined(separator: " · ")
        content.sound = .default

        var when = DateComponents()
        when.hour = 8
        when.minute = 0
        let trigger = UNCalendarNotificationTrigger(dateMatching: when, repeats: true)
        center.add(UNNotificationRequest(identifier: briefingID, content: content, trigger: trigger))
    }

    // A Time-Sensitive nudge the morning of each dated item (or shortly from now
    // if that moment has already passed today).
    private func scheduleDeadlineReminders(_ briefing: Briefing) {
        var items: [Item] = []
        if let one = briefing.oneNow { items.append(one) }
        items.append(contentsOf: briefing.waiting.map(\.topItem))

        let now = Date()
        let calendar = Calendar.current
        var seen = Set<String>()
        for item in items where seen.insert(item.id).inserted {
            guard let due = item.dueDate, due > now else { continue }
            var remind = calendar.date(bySettingHour: 8, minute: 0, second: 0, of: due) ?? due
            if remind < now { remind = now.addingTimeInterval(3600) }

            let content = UNMutableNotificationContent()
            content.title = item.person
            content.body = title(item)
            content.sound = .default
            content.interruptionLevel = .timeSensitive   // breaks through Focus (with entitlement)

            let comps = calendar.dateComponents([.year, .month, .day, .hour, .minute], from: remind)
            let trigger = UNCalendarNotificationTrigger(dateMatching: comps, repeats: false)
            center.add(UNNotificationRequest(identifier: deadlinePrefix + item.id, content: content, trigger: trigger))
        }
    }

    private func title(_ item: Item) -> String {
        item.suggestedAction.isEmpty ? item.rawText : item.suggestedAction
    }
}
