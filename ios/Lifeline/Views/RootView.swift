import SwiftUI

/// §v2 — **the threads are the app.** One surface, like the deck before it:
/// the stack of open loops you're carrying. Everything else opens from it —
/// a thread pushes onto the nav stack, and declare / proposals arrive as
/// sheets. No tabs.
///
/// The deck (`FocusDeckView`) is retired as the main view. Its type is still
/// here, and so are the item-scoped routes it uses: items didn't disappear in
/// v2, they became *evidence*, and the deck remains the only thing that can
/// still render one on its own. It comes out once nothing needs it.
struct RootView: View {
    @State private var router = PushRouter.shared

    var body: some View {
        ThreadsView()
            .tint(Color("AccentColor"))
            // A notification tap covers the stack rather than navigating into
            // it. Arriving because the app pulled you back is a different
            // state from choosing to come in, and the stack is the answer to
            // the second question, not the first. See `ArrivalView`.
            .fullScreenCover(item: Binding(
                get: { router.arrival.map(ArrivalRoute.init) },
                set: { if $0 == nil { router.clear() } }
            )) { route in
                ArrivalView(arrival: route.arrival) { router.clear() }
                    .tint(Color("AccentColor"))
            }
    }
}

/// `fullScreenCover(item:)` wants Identifiable; the arrival itself is a plain
/// value so it can stay Equatable for change detection.
private struct ArrivalRoute: Identifiable {
    let arrival: PushRouter.Arrival
    var id: String { "\(arrival.threadId):\(arrival.findingId ?? "")" }
}

#Preview {
    RootView()
}
