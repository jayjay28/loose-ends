import EventKit
import Foundation

/// Reads the device calendar (EventKit) and pushes events to the backend, so
/// ranking + completion get calendar context and the model of you knows your
/// schedule — no Google Calendar scope needed; it reads whatever calendars are
/// on the phone (incl. a Google account added in iOS Settings). Events stay on
/// the user's own server.
struct CalendarEventPayload: Encodable, Sendable {
    let id: String
    let calendarId: String
    let summary: String
    let description: String
    let location: String
    let startAt: String?
    let endAt: String?
    let selfResponse: String?
}

struct CalendarSyncResponse: Codable, Sendable { var stored: Int }

@MainActor
final class CalendarSync {
    static let shared = CalendarSync()
    private let store = EKEventStore()

    /// Ask for read access, then push a window of events. Silently no-ops if
    /// access isn't granted — calendar context is optional.
    func sync(via api: APIClient) async {
        let granted = (try? await store.requestFullAccessToEvents()) ?? false
        guard granted else { return }

        let start = Date().addingTimeInterval(-7 * 86_400)
        let end = Date().addingTimeInterval(45 * 86_400)
        let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)

        let payload = store.events(matching: predicate).prefix(200).map { event in
            CalendarEventPayload(
                id: event.eventIdentifier ?? UUID().uuidString,
                calendarId: event.calendar.title,
                summary: event.title ?? "(no title)",
                description: event.notes ?? "",
                location: event.location ?? "",
                startAt: iso(event.startDate),
                endAt: iso(event.endDate),
                selfResponse: nil
            )
        }
        guard !payload.isEmpty else { return }
        try? await api.syncCalendar(events: Array(payload))
    }

    private func iso(_ date: Date?) -> String? {
        guard let date else { return nil }
        return ISO8601DateFormatter.lifeline.string(from: date)
    }
}
