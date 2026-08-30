import SwiftUI

/// §v2.1 — the front page.
///
/// The stack used to be fourteen identical bordered cards whose only variable
/// was a 3pt colour rail nobody could decode. A briefing has a lead and it has
/// briefs; the size jump *is* the hierarchy, and it does the job the stripe
/// was failing to do.
///
/// Nothing here draws a card. Text sits on paper, hairlines separate, and the
/// one container left in the app is a move — so a border finally means
/// "act on this" instead of meaning nothing.

/// The lead story. One per page, and only when something has genuinely earned
/// it — a page with a lead every day regardless teaches you to ignore leads.
struct LeadRow: View {
    let thread: LifeThread
    var arrival: Arrival?
    var onAct: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let kicker {
                Text(kicker.uppercased())
                    .font(Theme.label)
                    .tracking(0.8)
                    .foregroundStyle(Theme.alert)
                    .padding(.bottom, 7)
            }
            Text(thread.title)
                .font(Theme.lead)
                .foregroundStyle(Theme.ink)
                .lineSpacing(Theme.titleLeading)
                .fixedSize(horizontal: false, vertical: true)
                .arriving(arrival)
            if let subtitle = thread.subtitle {
                Text(subtitle)
                    .font(Theme.body)
                    .foregroundStyle(Theme.inkSoft)
                    .lineSpacing(Theme.bodyLeading)
                    .lineLimit(3)
                    .padding(.top, 7)
            }
            if let onAct {
                Button(action: onAct) {
                    Text(thread.leadVerb)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 20)
                        .padding(.vertical, 12)
                        .background(Capsule().fill(Theme.brand))
                }
                .buttonStyle(.plain)
                .padding(.top, 15)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 18)
        .padding(.bottom, 20)
        .arrivalMark(arrival)
    }

    /// Why this is the lead, in three words. Only shown when the reason is a
    /// date — "because the system ranked it highest" is not a reason a person
    /// can act on, so it stays unsaid.
    private var kicker: String? {
        guard let due = thread.dueLabel else { return nil }
        return thread.deadlinePassed ? "Overdue · \(due)" : "Due \(due)"
    }
}

/// Above the fold: title, one line of deck, and the date only when it matters.
struct BriefRow: View {
    let thread: LifeThread
    var showsRule = true
    var arrival: Arrival?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(thread.title)
                    .font(Theme.itemTitle)
                    .foregroundStyle(Theme.ink)
                    .lineSpacing(Theme.titleLeading)
                    .strikethrough(thread.isResolved, color: Theme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                    .arriving(arrival)
                if let subtitle = thread.subtitle {
                    Text(subtitle)
                        .font(Theme.secondary)
                        .foregroundStyle(Theme.inkSoft)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            WhyChip(thread: thread)
        }
        .padding(.vertical, 14)
        .contentShape(Rectangle())
        .opacity(thread.isResolved ? 0.5 : 1)
        .arrivalMark(arrival)
        // The rule rides on the row. As its own List row it inherited the
        // list's minimum row height and ate ~44pt of vertical space per
        // divider, which is what turned a dense front page back into an airy
        // one.
        .hairlineUnder(showing: showsRule)
    }
}

/// The quiet index. One line, title only — everything that isn't pressing
/// still deserves to be *seen*, just not to compete.
struct IndexRow: View {
    let thread: LifeThread
    var showsRule = true
    var arrival: Arrival?

    var body: some View {
        HStack(spacing: 12) {
            Text(thread.title)
                .font(Theme.body)
                .foregroundStyle(Theme.ink)
                .lineLimit(1)
                .strikethrough(thread.isResolved, color: Theme.inkSoft)
                .arriving(arrival)
            Spacer(minLength: 0)
            if thread.unseen > 0 {
                Circle().fill(Theme.brand).frame(width: 6, height: 6)
            }
            WhyChip(thread: thread)
        }
        .padding(.vertical, 11)
        .contentShape(Rectangle())
        .opacity(thread.isResolved ? 0.5 : 1)
        .arrivalMark(arrival)
        .hairlineUnder(showing: showsRule)
    }
}

/// §v3 (Loose Ends) — the reason chip: the pressure score, in words.
///
/// A card that moves without saying which force moved it reads as a shuffle;
/// this is each ranking term made legible. One per row, server-decided.
/// The old gold pill died because every date got the same chip; this earns
/// the shape back by never saying the same thing twice — the tone *is* the
/// kind, and most quiet placements send no chip at all.
struct WhyChip: View {
    let thread: LifeThread

    var body: some View {
        if let text = thread.whyText {
            Text(text.uppercased())
                .font(.system(size: 9, weight: .bold))
                .tracking(0.4)
                .foregroundStyle(tone)
                .padding(.horizontal, 6)
                .padding(.vertical, 3)
                .background(RoundedRectangle(cornerRadius: 5).fill(tone.opacity(0.12)))
                .fixedSize()
        } else {
            // No pressure story — a distant date still deserves its plain text.
            DueText(thread: thread)
        }
    }

    private var tone: Color {
        switch thread.whyKind {
        case "overdue":      return Theme.alert
        case "due":          return Theme.queued
        case "move", "new":  return Theme.brand
        default:             return Theme.inkFaint    // waited, tied
        }
    }
}

/// A date as plain text, red only when it has passed.
///
/// Replaces the gold pill. Every date used to get the same tan chip whether it
/// was three weeks out or nine days gone, so overdue was invisible — the one
/// state the user most needed to see.
struct DueText: View {
    let thread: LifeThread

    var body: some View {
        if let due = thread.dueLabel {
            Text(due)
                .font(Theme.meta)
                .monospacedDigit()
                .foregroundStyle(thread.deadlinePassed ? Theme.alert : Theme.inkSoft)
                .fontWeight(thread.deadlinePassed ? .semibold : .medium)
        }
    }
}

extension LifeThread {
    /// What the lead's button says. Names the outcome rather than the
    /// mechanism — "Open" tells you nothing about what you're about to do.
    var leadVerb: String {
        switch rank {
        case .lead where deadlinePassed: return "Deal with this"
        default: return "Open this"
        }
    }
}

/// Every row on the stack sits on the same left edge. The old screen had three
/// at once — an 18pt header over 14pt rows whose text began at 27pt — which is
/// the single loudest amateur tell the critique found, and free to fix.
extension View {
    /// A warm hairline along the bottom edge, drawn as an overlay so it costs
    /// no layout height at all.
    func hairlineUnder(showing: Bool) -> some View {
        overlay(alignment: .bottom) {
            if showing { Rectangle().fill(Theme.rule).frame(height: 1) }
        }
    }

    func plainRow() -> some View {
        self
            .listRowInsets(EdgeInsets(top: 0, leading: Theme.margin,
                                      bottom: 0, trailing: Theme.margin))
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
    }
}

extension Tier {
    /// The tiers that get a section band, in the order they appear.
    static let banded: [Tier] = [.brief, .index, .quiet]
}

/// Threads bucketed by tier, preserving the server's order within each.
struct TieredThreads {
    let all: [LifeThread]
    init(_ all: [LifeThread]) { self.all = all }

    var lead: [LifeThread] { all.filter { $0.rank == .lead && !$0.isResolved } }
    var closed: [LifeThread] { all.filter { $0.isResolved } }

    /// Every open thread in the order the page actually draws it. The arrival's
    /// "Nth of M" has to count rows down the page — counting entries in the
    /// array the server sent said "13th of 13" about a row sitting fifth.
    var pageOrder: [LifeThread] { lead + Tier.banded.flatMap { of($0) } }

    func of(_ tier: Tier) -> [LifeThread] {
        all.filter { $0.rank == tier && !$0.isResolved }
    }
}

/// Resolved threads stop holding prime real estate. They still ride along for
/// a day — seeing the pile shrink is the point — but as a footer, not a row.
struct ClosedFooter: View {
    let count: Int
    /// §v2.4 — it was a chevron with nothing behind it.
    ///
    /// The row drew a `chevron.right`, which on iOS means "this opens", and
    /// had no tap handler of any kind. Seeing the pile shrink is the entire
    /// reason resolved threads ride along for a day, and the one gesture for
    /// looking at what shrank did nothing at all.
    var isOpen: Bool = false
    var onTap: (() -> Void)?

    var body: some View {
        Button {
            onTap?()
        } label: {
            HStack {
                Text("Tied off this week (\(count))")
                    .font(Theme.secondary)
                    .foregroundStyle(Theme.inkSoft)
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Theme.inkGhost)
                    // Points down when open, so the control says which way it
                    // goes rather than only that it goes somewhere.
                    .rotationEffect(.degrees(isOpen ? 90 : 0))
            }
            .contentShape(Rectangle())      // the whole row, not just the words
            .padding(.top, 22)
            .padding(.bottom, 12)
            .overlay(alignment: .top) {
                Rectangle().fill(Theme.rule).frame(height: 1)
            }
        }
        .buttonStyle(.plain)
        .disabled(onTap == nil)
    }
}

// MARK: - §v2.3, the detail screen

/// The two facts that decide what you do, stated before anything else.
///
/// The complaint this answers was "not informative, and too much" — which was
/// both things at once, and the same cause. The screen rendered every pass the
/// worker had ever made at equal weight, so the date you were racing and the
/// question you were blocked on were in there somewhere, discoverable rather
/// than said. This says them.
struct StateHeader: View {
    let detail: ThreadDetail

    var body: some View {
        // §v2.4 — the date, and nothing else.
        //
        // This used to print the move's first `need` under a BLOCKED ON YOU
        // label, two lines and hard-truncated. The same sentence is already at
        // the foot of the move card, so the first inch of the screen carried a
        // mid-word fragment of something the reader would meet again in full
        // fifteen points lower. One statement of what is blocking, and it lives
        // with the move it belongs to.
        if let binds {
            Text(binds)
                .font(Theme.secondary)
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chip))
                .padding(.top, 14)
        }
    }

    /// Whether this header is carrying the date, so the meta line above can
    /// stand down rather than print it twice.
    var showsDate: Bool { binds != nil }

    /// The date, and only when there is one. A thread with no deadline is not
    /// bound by anything, and inventing a line to fill the space is how the
    /// screen got noisy in the first place.
    private var binds: String? {
        guard let due = detail.thread.dueLabel else { return nil }
        return detail.thread.deadlinePassed ? "Overdue \(due)" : "Due \(due)"
    }
}

/// A superseded finding: what the thread said before, one line, no body.
///
/// The body is dropped deliberately. These are the restatements — five of six
/// on one real thread — and re-rendering their paragraphs inside the drawer
/// would move the noise rather than retire it.
struct PastFindingRow: View {
    let finding: Finding

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(finding.headline)
                .font(Theme.meta)
                .foregroundStyle(Theme.inkFaint)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            Text(Self.day(finding.createdAt))
                .font(Theme.meta)
                .monospacedDigit()
                .foregroundStyle(Theme.inkGhost)
        }
        .padding(.vertical, 7)
    }

    private static func day(_ iso: String) -> String {
        guard let date = ISO8601DateFormatter.lifeline.date(from: iso) else { return "" }
        let f = DateFormatter()
        f.dateFormat = "MMM d"
        return f.string(from: date)
    }
}

/// One verified figure: what it is, what it costs, and where that came from.
///
/// The label/value split is what makes a set of these scannable — the eye runs
/// down the values and compares them, which is exactly what a paragraph
/// containing the same numbers prevents.
/// One staged row: what it is, what it costs, and where it came from.
///
/// §v2.4 — the single row view. `steps` and `facts` arrive from the worker as
/// different fields and mean the same thing to a reader, so they render
/// through here and nowhere else. Before this, a move card drew two stacks
/// that looked alike and a finding drew a third, which is why the same
/// research could read as inventory in one place and as a paragraph in
/// another.
struct OptionRowView: View {
    let row: Finding.OptionRow

    /// Two targets, because looking and buying are different intentions.
    ///
    /// A row that prices three garments is asking the user to *choose*, and
    /// choosing needs a longer look than committing does. Tapping the arrow
    /// leaves for the shop; tapping the photograph enlarges it here, so the
    /// comparison can happen without leaving the app three times. A 72pt
    /// thumbnail is enough to recognise a knit from a vest and not enough to
    /// judge one, which is exactly why it has to open.
    @State private var enlarged = false

    private var alignment: VerticalAlignment { row.picture == nil ? .firstTextBaseline : .center }

    var body: some View {
        HStack(alignment: alignment, spacing: 12) {
            if let picture = row.picture {
                Button { enlarged = true } label: {
                    thumbnail(picture, side: 72, corner: 8)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(row.label.map { "Enlarge \($0)" } ?? "Enlarge photo")
            }
            VStack(alignment: .leading, spacing: 1) {
                if let label = row.label {
                    Text(label)
                        .font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !row.value.isEmpty {
                    Text(row.value)
                        .font(.system(size: 11.5))
                        .monospacedDigit()
                        .foregroundStyle(row.label == nil ? Theme.ink : Theme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            if let link = row.link {
                Link(destination: link) {
                    Image(systemName: "arrow.up.right.square")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Theme.brand)
                }
            }
        }
        .padding(.vertical, 7)
        .sheet(isPresented: $enlarged) {
            OptionPreview(row: row)
                .presentationDetents([.medium])
                .presentationCornerRadius(20)
                .presentationBackground(Theme.card)
        }
    }

    /// The chip colour stands in while it loads and stays if it fails — a row
    /// whose photograph 404s must still read as a row, not as a hole.
    @ViewBuilder
    private func thumbnail(_ url: URL, side: CGFloat, corner: CGFloat) -> some View {
        AsyncImage(url: url) { phase in
            if let image = phase.image {
                image.resizable().scaledToFill()
            } else {
                Theme.chip
            }
        }
        .frame(width: side, height: side)
        .clipShape(RoundedRectangle(cornerRadius: corner))
        .overlay(
            RoundedRectangle(cornerRadius: corner).strokeBorder(Theme.ruleSoft, lineWidth: 1)
        )
    }
}

/// The photograph at a size you can actually judge, with the price and the way
/// out kept together underneath it.
private struct OptionPreview: View {
    let row: Finding.OptionRow
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let picture = row.picture {
                AsyncImage(url: picture) { phase in
                    if let image = phase.image {
                        image.resizable().scaledToFill()
                    } else {
                        Theme.chip.overlay(ProgressView().controlSize(.small))
                    }
                }
                .frame(maxWidth: .infinity)
                .frame(height: 260)
                .clipped()
            }
            VStack(alignment: .leading, spacing: 4) {
                if let label = row.label {
                    Text(label)
                        .font(Theme.serif(19, .semibold))
                        .foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !row.value.isEmpty {
                    Text(row.value)
                        .font(.system(size: 13))
                        .monospacedDigit()
                        .foregroundStyle(Theme.inkSoft)
                }
                if let link = row.link {
                    Link(destination: link) {
                        Text("Open \(link.host ?? "the page")")
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(.white)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 13)
                            .background(Capsule().fill(Theme.brand))
                    }
                    .padding(.top, 10)
                }
            }
            .padding(Theme.margin)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card)
        .overlay(alignment: .topTrailing) {
            Button { dismiss() } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(Theme.ink)
                    .padding(9)
                    .background(Circle().fill(Theme.paper.opacity(0.9)))
            }
            .buttonStyle(.plain)
            .padding(12)
            .accessibilityLabel("Close")
        }
    }
}

/// A verified figure. Kept as its own name because findings still carry
/// `facts` directly; the rendering is `OptionRowView`'s.
struct FactRow: View {
    let fact: ThreadFact

    var body: some View {
        OptionRowView(row: Finding.OptionRow(
            id: fact.id,
            label: fact.label.isEmpty ? nil : fact.label,
            value: fact.value,
            link: fact.link,
            picture: fact.picture
        ))
    }
}



/// §v2.4 — what the screen says when there is no move.
///
/// Six of thirteen live threads have none, and until now the screen simply
/// ended: findings, then a settings drawer. On the basketball-hoop thread that
/// reads as the app giving up — it had searched twenty retailers, established
/// that a glass backboard under the budget does not exist inside the deadline,
/// and then showed a paragraph and stopped. The user's question was "what am I
/// supposed to do with this?", and the honest answer was "nothing", which the
/// screen was not saying out loud.
///
/// Having no move is a real result, not an absence. What it needs is to say so,
/// name the decision if there is one, and show that the system is still
/// looking — three lines, no container, no verb, because inventing a button
/// here is exactly the lie the rest of this redesign removes.
struct NoMoveNote: View {
    let detail: ThreadDetail

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("NO MOVE YET")
                .font(Theme.label)
                .tracking(0.8)
                .foregroundStyle(Theme.inkSoft)

            // The decision, when the worker named one. This is the whole
            // value of the screen on a thread like the hoop: both options
            // break something the user asked for, so the call is theirs.
            if let question {
                Text(question)
                    .font(Theme.secondary)
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Text(reason)
                .font(Theme.secondary)
                .foregroundStyle(Theme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)

            if !detail.watchers.isEmpty {
                Text(watching)
                    .font(Theme.meta)
                    .foregroundStyle(Theme.inkFaint)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 20)
    }

    /// The worker's own `needs` on the newest finding — the thing only the
    /// user can settle. Nothing is invented when there isn't one.
    private var question: String? {
        detail.notes.first?.needs.first
    }

    private var reason: String {
        question == nil
            ? "Nothing here can be done for you yet — what's above is what there is."
            : "Nothing to stage until you decide that."
    }

    private var watching: String {
        let n = detail.watchers.count
        return "Still watching \(n) thing\(n == 1 ? "" : "s") for a change."
    }
}
