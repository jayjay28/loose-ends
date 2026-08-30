import SwiftUI

/// §7 / milestone 8 — a fuzzy completion match: "did this already happen?"
/// Never auto-closed by the engine; the user's yes/no here is the only thing
/// that closes it, and both answers feed the learning loop. Rendered as a
/// raised card that sits apart from the ranked rail.
struct ConfirmationRow: View {
    let confirmation: Confirmation
    let onConfirm: () -> Void
    let onReject: () -> Void

    /// Evidence often arrives as raw markdown ("1. …") with preserved blank
    /// lines — strip list markers and collapse whitespace before showing it.
    private var cleanEvidence: String {
        confirmation.evidenceText
            .replacingOccurrences(of: "(?m)^\\s*(\\d+\\.|[-*])\\s+", with: "", options: .regularExpression)
            .tightened
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Circle().fill(Theme.good).frame(width: 7, height: 7)
                Text("DID THIS ALREADY HAPPEN?")
                    .font(.system(size: 10.5, weight: .semibold))
                    .tracking(1.2)
                    .foregroundStyle(Theme.good)
                Spacer()
                Text("\(Int(confirmation.confidence * 100))% sure")
                    .font(.system(size: 11.5))
                    .foregroundStyle(Theme.inkFaint)
                    .monospacedDigit()
            }

            Text(confirmation.item.suggestedAction.isEmpty ? confirmation.item.rawText : confirmation.item.suggestedAction)
                .font(.system(size: 16, weight: .medium))
                .foregroundStyle(Theme.ink)
                .padding(.top, 2)

            Text(cleanEvidence)
                .font(.system(size: 12.5))
                .foregroundStyle(Theme.inkFaint)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 8)

            HStack(spacing: 8) {
                Button(action: onConfirm) {
                    Text("Yes, close it").fontWeight(.medium)
                        .padding(.horizontal, 14).padding(.vertical, 9)
                        .background(Theme.good, in: RoundedRectangle(cornerRadius: 10))
                        .foregroundStyle(.white)
                }
                Button(action: onReject) {
                    Text("Not yet").fontWeight(.medium)
                        .padding(.horizontal, 14).padding(.vertical, 9)
                        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.rule))
                        .foregroundStyle(Theme.inkFaint)
                }
            }
            .font(.system(size: 13))
            .buttonStyle(.plain)
        }
        .padding(15)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(Theme.rule))
        .shadow(color: Theme.ink.opacity(0.06), radius: 14, y: 6)
    }
}
