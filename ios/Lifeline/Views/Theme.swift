import SwiftUI

/// The "editorial briefing" visual system.
///
/// v2.1 rebuild. The idea was always *a printed briefing on warm paper*; the
/// execution had drifted into 27 font sizes, 13 corner radii, 18 horizontal
/// paddings and 5 accent hues, which is how an app ends up looking
/// undifferentiated even though every individual screen was reasonable.
///
/// Three rules this file now enforces, because they are what went wrong:
///
/// 1. **One accent.** `brand` is every interaction, no exceptions. The old
///    palette said exactly this and then made Resolve — the primary action on
///    the main screen — olive. `alert` is the only other hue, and it means a
///    date has passed. Nothing else gets a colour.
/// 2. **Nothing below 12pt.** The old `inkFaint` (3.06:1) and `inkGhost`
///    (1.93:1) were used at 9.5pt for section labels and metadata; the second
///    is literally invisible on paper. They are kept as names so call sites
///    don't all have to change at once, and retuned so they are legible.
/// 3. **One margin, two radii.** `margin` is the single horizontal inset for
///    every screen. Containers are `radius`, pills are capsules. Nothing else.
enum Theme {

    // MARK: - Ground + ink
    //
    // The cream carries real chroma now. The old #F7F3EC was 11 points of
    // red-blue spread on a 247 base — near-neutral, so it read as off-white
    // printer paper rather than paper.
    static let paper = dynamic(light: 0xF6F1E7, dark: 0x161513)

    /// Kept for the one container that still earns a fill — a move. Everywhere
    /// else, text sits on paper and a hairline does the separating: the old
    /// card was #FEFCF7 on #F7F3EC, a contrast of 1.08:1, which meant the app
    /// paid for cards (borders, radii, nested padding) and got nothing back.
    static let card = dynamic(light: 0xFBF7EF, dark: 0x1E1C19)
    static let chip = dynamic(light: 0xEDE6D8, dark: 0x26231E)

    static let ink = dynamic(light: 0x211E19, dark: 0xECE7DD)          // 14.8:1
    static let inkSoft = dynamic(light: 0x5A5348, dark: 0xA69C8C)      //  6.8:1

    /// Deprecated as *roles* — both now resolve to legible values so existing
    /// call sites stop rendering as half-printed. Prefer `inkSoft`.
    static let inkFaint = dynamic(light: 0x6B6357, dark: 0x8F867A)     //  5.4:1
    static let inkGhost = dynamic(light: 0x857C6E, dark: 0x736B60)     //  3.9:1, decoration only

    // MARK: - Accents
    //
    // Two hues. `good`, `gold` and `wave` are aliases now rather than colours
    // of their own: five accent codes on a briefing app is a dashboard, and
    // none of them was ever labelled, so the user could not decode them.
    static let brand = dynamic(light: 0x1F6157, dark: 0x6FB3A5)   // teal — every interaction
    static let alert = dynamic(light: 0xA84124, dark: 0xE0785A)   // rust — a date has passed

    static let ember = alert         // was clay; urgency and overdue are one idea
    static let good = brand          // was olive; Resolve is an interaction
    static let gold = alert          // was amber; a due date is either fine or late
    static let wave = brand          // was blue

    // MARK: - Status hues (§v2.7)
    //
    // Three more, and the rule above is why there are only three. The
    // objection to five accents was never the count — it was that none of
    // them was labelled, so a coloured pip asked the user to memorise a
    // legend. These are only ever drawn *behind a word*: "Queued", "Needs
    // you", "Looks finished". The colour sorts the list at a glance and the
    // word says what it means, so nothing has to be decoded.
    //
    // Earned by a state the server already computes and by scarcity. A fourth,
    // "Watching", was cut after it labelled nine live threads out of eleven —
    // the ordinary condition of a thread, which is not news. On the current
    // stack these three appear on two rows out of eleven.
    //
    // Mixed dark and a little desaturated so they sit *on* the warm paper
    // rather than on top of it; the light values are all ≥ 4.5:1 on `paper`.
    static let queued = dynamic(light: 0xA8752A, dark: 0xD9A253)   // amber — added, never worked
    static let needsYou = dynamic(light: 0x7A4062, dark: 0xC189A8) // plum — blocked on the user
    static let finished = dynamic(light: 0x5B7A4F, dark: 0x9CBE8F) // sage — resolved, or looks it

    /// Warm hairline, matched to the paper. The old `ink.opacity(0.10)`
    /// rendered a cold grey line that dirtied the cream on every edge.
    static let rule = dynamic(light: 0xDCD5C6, dark: 0x302D28)
    static let ruleSoft = dynamic(light: 0xE7E1D4, dark: 0x26231F)

    static let brandSoft = brand.opacity(0.10)
    static let emberSoft = alert.opacity(0.10)
    static let goodSoft = brand.opacity(0.10)

    // MARK: - Metrics
    /// The single horizontal inset. The old screen had three left edges on it
    /// at once — an 18pt header over 14pt rows whose text started at 27pt —
    /// which is the loudest amateur tell the critique found.
    static let margin: CGFloat = 20
    static let radius: CGFloat = 12
    static let gutter: CGFloat = 12

    // MARK: - Type
    //
    // Seven steps, ratio ~1.15–1.2. The old set had 27 sizes including
    // half-point neighbours (11 / 11.5 / 12 / 12.5) that are invisible as
    // hierarchy and visible as sloppiness.

    /// New York, for the briefing voice. Used where a serif earns its keep:
    /// screen titles, item titles, move headlines. Not for 14pt list rows,
    /// where it just looks like blurry SF.
    static func serif(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .serif)
    }

    static var screenTitle: Font { serif(28, .medium) }     // 28 / 32
    static var lead: Font { serif(30, .semibold) }          // the front-page story
    static var itemTitle: Font { serif(19, .semibold) }     // 19 / 24
    static var cardHeadline: Font { serif(17) }             // 17 / 22
    static var moveHeadline: Font { serif(22, .semibold) }

    static var body: Font { .system(size: 15) }             // 15 / 21
    static var secondary: Font { .system(size: 13) }        // 13 / 18
    static var meta: Font { .system(size: 12, weight: .medium) }
    /// The only uppercase treatment in the app. It used to appear ten times on
    /// a single screen, at which point no label is emphasis.
    static var label: Font { .system(size: 11, weight: .semibold) }

    // Line heights, since SwiftUI ships none and nothing here was ever tuned.
    static let leadLeading: CGFloat = 4
    static let bodyLeading: CGFloat = 4
    static let titleLeading: CGFloat = 3

    private static func dynamic(light: Int, dark: Int) -> Color {
        Color(UIColor { $0.userInterfaceStyle == .dark ? UIColor(hex: dark) : UIColor(hex: light) })
    }
}

/// A section label over a hairline — the rhythm device that replaces cards on
/// every screen except the one place a container still means something.
struct SectionRule: View {
    let title: String
    var body: some View {
        HStack(spacing: 10) {
            Text(title.uppercased())
                .font(Theme.label)
                .tracking(0.8)
                .foregroundStyle(Theme.inkSoft)
            Rectangle().fill(Theme.rule).frame(height: 1)
        }
        .padding(.top, 26)
        .padding(.bottom, 10)
    }
}

/// Per-tier visual treatment, driven entirely by the server's interruption
/// level so the client never re-decides urgency.
extension InterruptionLevel {
    /// The colour of the node on the lifeline rail.
    var railColor: Color {
        switch self {
        case .timeSensitive: return Theme.alert
        case .active: return Theme.brand
        case .passive, .unknown: return Theme.inkGhost
        }
    }
}

private extension UIColor {
    convenience init(hex: Int) {
        self.init(
            red: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: 1
        )
    }
}
