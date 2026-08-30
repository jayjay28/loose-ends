import EventKit
import EventKitUI
import SwiftUI

/// "Add to Calendar" for a dated or event item. Tapping it asks for write
/// access, then presents Apple's own event editor prefilled from the item —
/// the user confirms and saves there, so Lifeline never writes to the calendar
/// silently. Works with whatever calendars are on the device (incl. a Google
/// account added in iOS Settings).
struct CalendarButton: View {
    let item: Item
    var onAdded: (() -> Void)? = nil

    @State private var present = false
    @State private var denied = false

    var body: some View {
        Button { request() } label: {
            Image(systemName: "calendar.badge.plus")
                .font(.system(size: 15, weight: .semibold))
                .frame(width: 20, height: 20)
                .padding(.horizontal, 11).padding(.vertical, 9)
                .background(Theme.card, in: RoundedRectangle(cornerRadius: 10))
                .foregroundStyle(Theme.ink)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.rule))
                .accessibilityLabel("Add to Calendar")
        }
        .buttonStyle(.plain)
        .sheet(isPresented: $present) {
            CalendarEditSheet(item: item) { saved in
                present = false
                if saved { onAdded?() }
            }
            .ignoresSafeArea()
        }
        .alert("Calendar access is off", isPresented: $denied) {
            Button("OK", role: .cancel) {}
        } message: {
            Text("Turn on Calendar access for Lifeline in Settings to add events.")
        }
    }

    private func request() {
        let store = EKEventStore()
        store.requestWriteOnlyAccessToEvents { granted, _ in
            DispatchQueue.main.async {
                if granted { present = true } else { denied = true }
            }
        }
    }
}

/// UIKit bridge to `EKEventEditViewController`, prefilled from the item.
struct CalendarEditSheet: UIViewControllerRepresentable {
    let item: Item
    let onFinish: (Bool) -> Void          // true when an event was saved

    func makeCoordinator() -> Coordinator { Coordinator(onFinish: onFinish) }

    func makeUIViewController(context: Context) -> EKEventEditViewController {
        let store = EKEventStore()
        context.coordinator.store = store
        let controller = EKEventEditViewController()
        controller.eventStore = store
        controller.event = makeEvent(in: store)
        controller.editViewDelegate = context.coordinator
        return controller
    }

    func updateUIViewController(_ controller: EKEventEditViewController, context: Context) {}

    private func makeEvent(in store: EKEventStore) -> EKEvent {
        let event = EKEvent(eventStore: store)
        let title = item.suggestedAction.isEmpty
            ? (item.entities.item ?? item.rawText) : item.suggestedAction
        event.title = title
        event.notes = "“\(item.rawText)”\n— \(item.person) · via Lifeline"

        let start = item.dueDate ?? Date().addingTimeInterval(3600)
        // A bare date (midnight) reads as an all-day event; a real time gets a
        // default one-hour block the user can adjust in the editor.
        let comps = Calendar.current.dateComponents([.hour, .minute], from: start)
        if (comps.hour ?? 0) == 0 && (comps.minute ?? 0) == 0 {
            event.isAllDay = true
            event.startDate = start
            event.endDate = start
        } else {
            event.startDate = start
            event.endDate = start.addingTimeInterval(3600)
        }
        event.calendar = store.defaultCalendarForNewEvents
        return event
    }

    final class Coordinator: NSObject, EKEventEditViewDelegate {
        var store: EKEventStore?
        let onFinish: (Bool) -> Void
        init(onFinish: @escaping (Bool) -> Void) { self.onFinish = onFinish }

        func eventEditViewController(_ controller: EKEventEditViewController,
                                     didCompleteWith action: EKEventEditViewAction) {
            onFinish(action == .saved)
            controller.dismiss(animated: true)
        }
    }
}

extension Item {
    /// Worth offering a calendar action: it's an event, or it carries a date.
    var isCalendarable: Bool { type == .event || dueDate != nil }
}
