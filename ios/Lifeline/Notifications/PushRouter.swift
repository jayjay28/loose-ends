import Observation
import SwiftUI

/// Where a tapped notification should land.
///
/// **Arriving from a notification is a different state from opening the app.**
/// That distinction is the whole reason this type exists rather than the tap
/// just calling `navigate(to:)`.
///
/// When someone opens Lifeline themselves they are asking "what am I
/// carrying?", and the stack is the honest answer. When a notification brings
/// them back, the app has already told them one specific thing — and dropping
/// them on a list of fifteen lanes makes them re-find it. That is the app
/// handing work back to the person it exists to carry work *for*.
///
/// So a tap does not open the stack, and it does not open the thread detail
/// either: the detail is a work log with six sections, which is the right
/// screen for someone browsing and the wrong one for someone who was
/// interrupted. It opens one card with one move on it.
@MainActor
@Observable
final class PushRouter {
    static let shared = PushRouter()

    /// Set by the notification tap, consumed by the arrival screen. Optional
    /// rather than a queue: if two notifications are tapped the second one is
    /// the one the user meant.
    var arrival: Arrival?

    struct Arrival: Equatable {
        let threadId: String
        let findingId: String?
    }

    func clear() { arrival = nil }

    /// A tap that carried no destination — a digest, or a completion the
    /// engine couldn't tie to a thread. The stack itself is the answer, but
    /// *deliberately*: whatever the app was doing gets popped so the stack is
    /// actually what greets them, not whichever screen was left up. The
    /// promise this keeps: a notification tap never silently no-ops.
    /// A counter rather than a Bool so consecutive destination-less taps
    /// each land.
    private(set) var homeBeat = 0

    func landHome() {
        arrival = nil
        homeBeat += 1
    }

#if DEBUG
    /// Notification-tap arrival is otherwise only reachable by physically
    /// tapping a banner before it auto-dismisses, which makes the screen it
    /// opens effectively untestable. `-LifelineArrival <threadId>` seeds it.
    func seedFromLaunchArguments() {
        let args = ProcessInfo.processInfo.arguments
        guard let i = args.firstIndex(of: "-LifelineArrival"), i + 1 < args.count else { return }
        arrival = Arrival(threadId: args[i + 1], findingId: nil)
    }
#endif
}
