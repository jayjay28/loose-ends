import SwiftUI

/// §v2.1 — a **move**: the specific next thing that would advance or end a
/// thread, with the work already done.
///
/// Deliberately not a `FindingRow` with extra fields. A finding is something
/// to know and a move is something to act on, and if they look the same the
/// whole sprint is invisible — the user still has to read every card to work
/// out which ones want them. So a move gets the things a finding never has: a
/// shape label, the staged work shown as work, and a verb.
///
/// The verb is the point. `MoveKind.verb` is literal about what pressing it
/// does, because the largest shape of move — `do` — cannot be completed by
/// this app at all. A bill is paid on someone else's website. Promising more
/// than "Open it" would be a lie the user discovers one tap later.
struct MoveCard: View {
    let finding: Finding
    var onAct: (() -> Void)?
    var onReject: (() -> Void)?
    var showsAction = true

    @State private var showingAll = false

    private var move: MoveKind { finding.move ?? .gather }

    /// Three, then the rest behind a tap.
    private var visibleRows: [Finding.OptionRow] {
        showingAll ? finding.optionRows : Array(finding.optionRows.prefix(Self.rowCap))
    }
    private static let rowCap = 3

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            header
            headline

            if finding.isBlocked {
                blocked
            } else {
                // One list, capped at three.
                //
                // `steps` and `facts` used to render as two stacks that looked
                // identical, and a card could carry five of each. A fourth
                // option does not help anyone choose; it makes them re-read.
                // Anything past three collapses, and prose that cannot be a
                // row is not shown here at all — see `notes`.
                if !finding.optionRows.isEmpty { options }
                // The draft, when there is one. A `send` move's message is
                // the deliverable, so it stays in the card even though the
                // one-line rule would otherwise call it prose.
                if let draft = finding.draftBody { draftBlock(draft) }
                if !finding.needs.isEmpty { needs }
                // The verb lives in the sticky bar on the detail screen, so
                // it is only drawn here when nothing else is carrying it —
                // two identical buttons on one screen is not emphasis.
                //
                // And only when it leads somewhere. A move with nothing staged
                // to open and no one to write to is already whole on the card;
                // a verb there is a button that does nothing when pressed.
                if let onAct, showsAction, finding.hasSomewhereToGo {
                    actionButton(onAct)
                }
            }
            if let onReject { rejectRow(onReject) }


        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(finding.isBlocked ? Theme.chip.opacity(0.6) : Theme.brand.opacity(0.035))
        .clipShape(RoundedRectangle(cornerRadius: Theme.radius))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radius)
                .strokeBorder(finding.isBlocked ? Theme.rule : Theme.brand,
                              lineWidth: finding.isBlocked ? 1 : 1.5)
        )
        .padding(.bottom, 6)
    }

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: finding.isBlocked ? "hand.raised" : move.icon)
                .font(.system(size: 10, weight: .semibold))
            Text(finding.isBlocked ? "NEEDS YOU" : move.tag)
                .font(Theme.label)
                .tracking(0.8)
            Spacer(minLength: 0)
        }
        .foregroundStyle(finding.isBlocked ? Theme.inkSoft : Theme.brand)
    }

    private var headline: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(finding.leadLine)
                .font(Theme.moveHeadline)
                .foregroundStyle(Theme.ink)
                .lineSpacing(Theme.titleLeading)
                .fixedSize(horizontal: false, vertical: true)
            if let deck = finding.deckLine {
                Text(deck)
                    .font(.system(size: 14))
                    .foregroundStyle(Theme.inkSoft)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// The staged work as inventory, not prose. These are the figures, links
    /// and options the system already assembled — the thing that separates a
    /// move from a suggestion.
    private var options: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(visibleRows) { row in
                OptionRowView(row: row)
                if row.id != visibleRows.last?.id {
                    Rectangle().fill(Theme.ruleSoft).frame(height: 1)
                }
            }
            if finding.optionRows.count > Self.rowCap && !showingAll {
                Button {
                    showingAll = true
                } label: {
                    Text("\(finding.optionRows.count - Self.rowCap) more")
                        .font(Theme.secondary)
                        .foregroundStyle(Theme.brand)
                }
                .buttonStyle(.plain)
                .padding(.top, 9)
            }
        }
    }

    /// The message, as a quotation. Set apart from the staged rows because
    /// approving a sentence is a different act from picking an option.
    @State private var copiedTick = false

    private func draftBlock(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(text)
                .font(.system(size: 12.5))
                .foregroundStyle(Theme.ink)
                .lineSpacing(2)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            // A draft you can see but not move is a screenshot of an email.
            // No recipient yet, so Mail opens with the body prefilled and
            // To: blank — the user pastes the address they just found. Copy
            // exists because a blocked send often goes out another door
            // entirely: a contact form, an Instagram DM.
            HStack(spacing: 8) {
                Button {
                    var components = URLComponents()
                    components.scheme = "mailto"
                    components.path = ""
                    components.queryItems = [URLQueryItem(name: "body", value: text)]
                    if let url = components.url { UIApplication.shared.open(url) }
                } label: {
                    SwiftUI.Label("Open in Mail", systemImage: "envelope")
                        .font(.system(size: 11, weight: .semibold))
                }
                .buttonStyle(.bordered)
                .tint(Theme.brand)
                Button {
                    UIPasteboard.general.string = text
                    copiedTick = true
                    Task { try? await Task.sleep(for: .seconds(2)); copiedTick = false }
                } label: {
                    SwiftUI.Label(copiedTick ? "Copied" : "Copy",
                                  systemImage: copiedTick ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 11, weight: .semibold))
                }
                .buttonStyle(.bordered)
                .tint(Theme.inkSoft)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(11)
        .background(RoundedRectangle(cornerRadius: 9).fill(Theme.card))
        .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(Theme.rule, lineWidth: 1))
    }

    /// What the app can't do for you. Stated plainly and without apology —
    /// this is the honest part of the design, not a shortfall to bury.
    /// What the app can't do for you, as a sentence rather than a labelled
    /// list. It's an aside, not a checklist — a second uppercase heading inside
    /// a card that already has one is noise.
    private var needs: some View {
        Text(finding.needs.joined(separator: " · "))
            .font(Theme.secondary)
            .italic()
            .foregroundStyle(Theme.inkSoft)
            .fixedSize(horizontal: false, vertical: true)
    }

    private var blocked: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(finding.blockedReason ?? "")
                .font(Theme.secondary)
                .foregroundStyle(Theme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
            // A blocked send still carries its deliverable — the draft. This
            // branch used to hide it, so a card could claim "draft ready"
            // over a message that appeared nowhere on screen (Marcus Reed).
            if let draft = finding.draftBody {
                draftBlock(draft)
            }
            if !finding.needs.isEmpty { needs }
            if finding.draftBody == nil && finding.optionRows.isEmpty {
                Text("Nothing was staged for this one.")
                    .font(Theme.meta)
                    .foregroundStyle(Theme.inkSoft)
            }
        }
    }

    /// Saying no has to be as easy as saying yes, or the "yes" stops meaning
    /// anything. Understated rather than hidden: this is the signal the engine
    /// learns most from, so it must not feel like a failure to press.
    private func rejectRow(_ reject: @escaping () -> Void) -> some View {
        Button(action: reject) {
            Text("Not this")
                .font(Theme.secondary)
                .foregroundStyle(Theme.inkSoft)
        }
        .buttonStyle(.plain)
        .padding(.top, 1)
    }

    private func actionButton(_ act: @escaping () -> Void) -> some View {
        Button(action: act) {
            Text(finding.actionLabel(default: move.verb))
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 13)
                .background(Capsule().fill(Theme.brand))
        }
        .buttonStyle(.plain)
        .padding(.top, 4)
    }
}
