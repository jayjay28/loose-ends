import SwiftUI

/// §v2.2 — **what happens to a thread the moment you declare it.**
///
/// The complaint: you type the loop you're carrying, the sheet closes, and the
/// page looks exactly as it did before. That wasn't a missing animation, it was
/// the ranking — `pressure()` was built entirely out of things that happen *to*
/// a thread, so a fresh declaration scored 0.1 and landed in the index. The
/// server half of the fix is the declaration floor.
///
/// This is the client half. The words you typed travel from the composer to the
/// row they become, so the thing you just added is never something you have to
/// go and find. Three rules it follows:
///
/// 1. **The words persist, the chrome doesn't.** What leaves the sheet is the
///    text you wrote, not a generic card sliding in from an edge.
/// 2. **Never fly to somewhere off screen.** The stack scrolls the destination
///    into view first — animating into the basement is a prettier abyss, not a
///    fixed one.
/// 3. **Say where it landed, then stop saying it.** A rule in the margin and
///    one line of position, both gone within two seconds. A permanent badge is
///    just another thing to learn to ignore.

/// One thread on its way from the composer into the stack.
struct Arrival: Equatable {
    let id: String
    let title: String
    /// Where it landed, said plainly: "New · 5th of 13". Position is the part
    /// worth saying — it teaches the ranking at the one moment the user is
    /// actually curious about it.
    let place: String
    /// The destination row's type, so the words land at the size they'll keep.
    let font: Font
    /// The composer's 17pt over that destination size. SwiftUI interpolates a
    /// scale and will not interpolate a font, so the flight is rendered at the
    /// destination's type and scaled back to the composer's.
    let startScale: CGFloat

    /// The words are still in the air, so the row's own title waits its turn.
    var flying = true
    /// ...and have reached the row's frame.
    var landed = false
    /// The row is briefly saying where it went.
    var marked = false

    init(thread: LifeThread, place: String) {
        id = thread.id
        title = thread.title
        self.place = place
        switch thread.rank {
        case .lead:
            font = Theme.lead
            startScale = 17.0 / 30.0
        case .brief:
            font = Theme.itemTitle
            startScale = 17.0 / 19.0
        default:
            font = Theme.body
            startScale = 17.0 / 15.0
        }
    }
}

/// The destination row reports where it is, in screen coordinates, so the words
/// can be flown *to* it rather than at a guess about where it probably is.
struct ArrivalTargetKey: PreferenceKey {
    // Computed, not stored: Swift 6 counts a stored static as shared mutable
    // state and refuses it.
    static var defaultValue: CGRect { .zero }
    static func reduce(value: inout CGRect, nextValue: () -> CGRect) {
        let next = nextValue()
        if next != .zero { value = next }
    }
}

extension View {
    /// Marks this as the title the arrival is aimed at.
    func arrivalTarget(_ active: Bool) -> some View {
        background {
            if active {
                GeometryReader { proxy in
                    Color.clear.preference(key: ArrivalTargetKey.self,
                                           value: proxy.frame(in: .global))
                }
            }
        }
    }

    /// Applies an in-flight arrival to a row's title: hidden while its words
    /// are still in the air, and reporting its frame so they know where to go.
    @ViewBuilder
    func arriving(_ arrival: Arrival?) -> some View {
        if let arrival {
            self.opacity(arrival.flying ? 0 : 1)
                .arrivalTarget(true)
        } else {
            self
        }
    }
}

/// The words in flight, drawn over the whole screen and positioned in global
/// coordinates — the composer that launched them is a separate presentation and
/// can only report itself in screen terms.
struct ArrivalFlight: View {
    let arrival: Arrival
    let from: CGRect
    let to: CGRect

    var body: some View {
        GeometryReader { proxy in
            let origin = proxy.frame(in: .global).origin
            let rect = arrival.landed ? to : from
            Text(arrival.title)
                .font(arrival.font)
                .foregroundStyle(Theme.ink)
                .lineSpacing(Theme.titleLeading)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .frame(width: max(rect.width, 1), alignment: .topLeading)
                .scaleEffect(arrival.landed ? 1 : arrival.startScale, anchor: .topLeading)
                .offset(x: rect.minX - origin.x, y: rect.minY - origin.y)
        }
        .allowsHitTesting(false)
    }
}

/// The beat after landing: a rule in the margin and one line of position, both
/// of which leave. The rule is teal because teal is the app's one interaction
/// colour, and this is the only row on the page you just interacted with.
struct ArrivalMark: ViewModifier {
    let arrival: Arrival?

    func body(content: Content) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            content
            if arrival?.marked == true, let place = arrival?.place {
                Text(place.uppercased())
                    .font(Theme.label)
                    .tracking(0.8)
                    .foregroundStyle(Theme.brand)
                    // Clear of whatever comes next: the line sits outside the
                    // row's own padding, so without this it collides with the
                    // section rule below it.
                    .padding(.bottom, 8)
            }
        }
        .overlay(alignment: .leading) {
            if arrival?.marked == true {
                Rectangle()
                    .fill(Theme.brand)
                    .frame(width: 2)
                    .padding(.vertical, 2)
                    .offset(x: -12)
            }
        }
    }
}

extension View {
    func arrivalMark(_ arrival: Arrival?) -> some View {
        modifier(ArrivalMark(arrival: arrival))
    }
}
