import SwiftUI

/// §v2.4 — "Not this" stops being a verb and becomes a door.
///
/// Until now a rejected move could say exactly one thing, and it always meant
/// the same thing: fewer moves of this shape on threads like this one. Three
/// unrelated intents collapsed into that single nudge —
///
///   "I already did it"        the move was right, the timing wasn't
///   "you misread the job"     the shape was right and not decisive enough
///   "that reason is stale"    nothing about the judgement was wrong
///
/// — and because `may_propose` only ever narrows, each one could permanently
/// close a door. It already happened: a `gather` move on the hoop thread was
/// turned down for a reason that was merely out of date, and that shape now
/// sits at 0.45 against a 0.40 floor. One more no and the worker can never
/// offer it there again.
///
/// So the exits are ordered by how much they teach, with the blunt one last —
/// it is the only one that costs a capability, and it should read like the
/// deliberate choice it is rather than the default.
struct RejectSheet: View {
    let move: Finding
    /// `text` is non-nil only for `.wrong`.
    let onChoose: (RejectReason, String?) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var writing = false
    @State private var text = ""
    @FocusState private var typing: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if writing { correction } else { exits }
        }
        .padding(.horizontal, Theme.margin)
        .padding(.top, 22)
        .padding(.bottom, 28)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card)
    }

    // MARK: - The three exits
    private var exits: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Why not?")
                .font(Theme.serif(20, .semibold))
                .foregroundStyle(Theme.ink)
            Text("This one shapes what I do next.")
                .font(Theme.secondary)
                .foregroundStyle(Theme.inkFaint)
                .padding(.top, 2)
                .padding(.bottom, 18)

            exit(
                icon: "checkmark",
                title: "Already handled",
                detail: "You did it yourself. Closes the thread.",
                teaches: "teaches nothing",
                emphasised: false
            ) {
                onChoose(.handled, nil)
                dismiss()
            }

            exit(
                icon: "square.and.pencil",
                title: "I got it wrong",
                detail: "Tell me what I misread. I'll try again.",
                teaches: "teaches this loose end, specifically",
                emphasised: true
            ) {
                writing = true
                typing = true
            }

            exit(
                icon: "nosign",
                title: "Don't offer this here",
                detail: offerDetail,
                teaches: "narrows what I may propose",
                emphasised: false
            ) {
                onChoose(.unwanted, nil)
                dismiss()
            }
        }
    }

    /// Named for the shape being turned away, so the cost of this exit is
    /// legible before it is paid rather than after.
    private var offerDetail: String {
        switch move.move {
        case .send:   return "Fewer drafted messages on threads like this."
        case .decide: return "Fewer decisions on threads like this."
        case .gather: return "Less research on threads like this."
        case .do:     return "Fewer things to go and do on threads like this."
        case nil:     return "Fewer moves like this on threads like this."
        }
    }

    private func exit(icon: String, title: String, detail: String,
                      teaches: String, emphasised: Bool,
                      action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 7) {
                    Image(systemName: icon)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Theme.brand)
                    Text(title)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Theme.ink)
                }
                Text(detail)
                    .font(Theme.secondary)
                    .foregroundStyle(Theme.inkSoft)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                Text(teaches.uppercased())
                    .font(.system(size: 10, weight: .medium))
                    .tracking(0.6)
                    .foregroundStyle(Theme.inkGhost)
                    .padding(.top, 5)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(13)
            .background(
                RoundedRectangle(cornerRadius: Theme.radius)
                    .fill(emphasised ? Theme.brand.opacity(0.04) : Theme.paper)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.radius)
                    .strokeBorder(emphasised ? Theme.brand : Theme.rule,
                                  lineWidth: emphasised ? 1.5 : 1)
            )
        }
        .buttonStyle(.plain)
        .padding(.bottom, 9)
    }

    // MARK: - Saying what was wrong
    private var correction: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("What did I get wrong?")
                .font(Theme.serif(20, .semibold))
                .foregroundStyle(Theme.ink)
            Text("One line is plenty.")
                .font(Theme.secondary)
                .foregroundStyle(Theme.inkFaint)
                .padding(.top, 2)
                .padding(.bottom, 14)

            TextEditor(text: $text)
                .focused($typing)
                .font(Theme.body)
                .foregroundStyle(Theme.ink)
                .scrollContentBackground(.hidden)
                .frame(height: 92)
                .padding(9)
                .background(RoundedRectangle(cornerRadius: Theme.radius).fill(Theme.paper))
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.radius)
                        .strokeBorder(Theme.brand, lineWidth: 1.5)
                )

            // Says where it goes and that it can be undone. A correction the
            // user doesn't trust to stick is one they won't bother typing.
            Text("Saved to this loose end. I'll read it on every pass from now on, and you can delete it any time.")
                .font(.system(size: 12))
                .foregroundStyle(Theme.inkFaint)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 9)
                .padding(.bottom, 15)

            Button {
                onChoose(.wrong, text)
                dismiss()
            } label: {
                Text("Save & try again")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 13)
                    .background(Capsule().fill(Theme.brand))
            }
            .buttonStyle(.plain)
            .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .opacity(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.4 : 1)

            Button("Cancel") { writing = false }
                .font(Theme.secondary)
                .tint(Theme.inkSoft)
                .frame(maxWidth: .infinity)
                .padding(.top, 11)
        }
    }
}

/// One thing the user has told this thread. Reads as a quote, because it is
/// one — their sentence, not the system's summary of it.
struct CorrectionRow: View {
    let correction: Correction
    let onDelete: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(correction.statement)
                    .font(Theme.secondary)
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                Text("you said this \(said)")
                    .font(Theme.meta)
                    .foregroundStyle(Theme.inkFaint)
            }
            Spacer(minLength: 0)
            Button(action: onDelete) {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.inkGhost)
            }
            .buttonStyle(.plain)
        }
        .padding(.vertical, 9)
    }

    private var said: String {
        guard let when = ISO8601DateFormatter().date(from: correction.saidAt) else {
            return "earlier"
        }
        if Calendar.current.isDateInToday(when) { return "today" }
        let f = DateFormatter()
        f.dateFormat = "MMM d"
        return f.string(from: when)
    }
}
