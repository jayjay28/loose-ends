import SwiftUI

/// The act-on-it bar: Send (prefilled Messages), Open / Watch (a shared link),
/// Call, and an optional Done. Shared so you can act on an item wherever it
/// lives — Today's expanded row and the Conversations decision card both use it.
/// (Eval 2026-08-04: Conversations was a read-only ledger you couldn't act from.)
struct ItemActionBar: View {
    let item: Item
    var onDone: (() -> Void)? = nil

    @Environment(\.openURL) private var openURL

    var body: some View {
        let send = item.sendMessagesURL
        let open = item.openLinkURL
        let call = item.callURL
        if send != nil || open != nil || call != nil || item.isCalendarable || onDone != nil {
            HStack(spacing: 8) {
                if let send {
                    button("Send", "paperplane.fill", primary: true) { openURL(send) }
                }
                if let open {
                    button(item.linkKind == "video" ? "Watch" : "Open",
                           item.linkKind == "video" ? "play.fill" : "safari",
                           primary: send == nil) { openURL(open) }
                }
                if let call, send == nil {
                    button("Call", "phone.fill", primary: false) { openURL(call) }
                }
                if item.isCalendarable { CalendarButton(item: item) }
                Spacer(minLength: 0)
                if let onDone {
                    button("Done", "checkmark", primary: false, tint: Theme.good, action: onDone)
                }
            }
        }
    }

    private func button(_ title: String, _ icon: String, primary: Bool,
                        tint: Color = Theme.brand, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            SwiftUI.Label(title, systemImage: icon)
                .fontWeight(.semibold)
                .font(.system(size: 13))
                .lineLimit(1).fixedSize()
                .padding(.horizontal, 13).padding(.vertical, 9)
                .background(primary ? tint : Theme.card, in: RoundedRectangle(cornerRadius: 10))
                .foregroundStyle(primary ? Color.white : tint)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(primary ? Color.clear : Theme.rule))
        }
        .buttonStyle(.plain)
    }
}
