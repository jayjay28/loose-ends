import SwiftUI

/// One item on the lifeline rail, rendered at a level of detail that matches
/// its interruption level (§8.1). Time-Sensitive reads as a serif headline;
/// Active as a clean body row. Tapping expands to the full §8.3 surface:
/// the original message, the drafted reply, the "why this is ranked here"
/// contributions, and Done / Snooze / Dismiss.
struct ItemRow: View {
    let item: Item
    let isExpanded: Bool
    let onToggle: () -> Void
    let onDone: () -> Void
    let onSnooze: (Double) -> Void
    let onDismiss: () -> Void

    @Environment(\.openURL) private var openURL
    @Environment(\.syncService) private var syncService

    @State private var context: ConversationContext?
    @State private var contextTried = false
    @State private var enriched: Enriched?

    private var isUrgent: Bool { item.interruptionLevel == .timeSensitive }

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            RailNode(level: item.interruptionLevel)
                .padding(.top, isUrgent ? 20 : 18)

            VStack(alignment: .leading, spacing: 0) {
                header
                if isExpanded { detail.padding(.top, 12) }
            }
            .padding(.vertical, 12)
        }
        .contentShape(Rectangle())
        .onTapGesture(perform: onToggle)
        .task(id: isExpanded) { await loadContextIfNeeded() }
    }

    /// Pull the surrounding conversation the first time a card is opened, so the
    /// surfaced line reads with the back-and-forth around it.
    private func loadContextIfNeeded() async {
        guard isExpanded, context == nil, !contextTried else { return }
        contextTried = true
        context = try? await syncService.api.itemContext(itemId: item.id)
        enriched = try? await syncService.api.itemEnriched(itemId: item.id)
    }

    // MARK: Collapsed header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(item.suggestedAction.isEmpty ? item.rawText : item.suggestedAction)
                .font(isUrgent ? Theme.serif(21, .semibold) : .system(size: 16.5, weight: .medium))
                .foregroundStyle(Theme.ink)
                .lineLimit(isExpanded ? nil : 3)
                .fixedSize(horizontal: false, vertical: true)

            meta
            linkChip
        }
    }

    /// A tappable link chip, visible without expanding, so a shared link opens
    /// in one tap.
    @ViewBuilder
    private var linkChip: some View {
        if let url = item.openLinkURL {
            Button { openURL(url) } label: {
                SwiftUI.Label(item.isVideoLink ? "Watch video" : (url.host ?? "Open link"),
                              systemImage: item.isVideoLink ? "play.circle.fill" : "link")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Theme.brand)
                    .padding(.horizontal, 9).padding(.vertical, 6)
                    .background(Theme.brandSoft, in: RoundedRectangle(cornerRadius: 8))
            }
            .buttonStyle(.plain)
            .padding(.top, 2)
        }
    }

    private var meta: some View {
        HStack(spacing: 6) {
            Avatar(name: item.person, personId: item.personId, size: 17)
            Text(item.person).foregroundStyle(Theme.inkSoft).fontWeight(.medium)
            Text("· \(item.type.rawValue)").foregroundStyle(Theme.inkFaint)
            if let due = DueDateFormatter.label(for: item) {
                Text("· \(due)")
                    .foregroundStyle(isUrgent ? Theme.ember : Theme.inkFaint)
                    .fontWeight(isUrgent ? .semibold : .regular)
            }
            if item.behaviorPattern == .avoidance {
                Text("keeps coming back")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.brand)
                    .padding(.horizontal, 7).padding(.vertical, 3)
                    .background(Theme.brandSoft, in: RoundedRectangle(cornerRadius: 5))
            }
        }
        .font(.system(size: 12.5))
    }

    // MARK: Expanded detail

    /// The surrounding conversation — a few messages of back-and-forth so the surfaced
    /// line isn't an orphan. Falls back to the raw quote while it loads or when
    /// no context is available.
    @ViewBuilder
    private var conversation: some View {
        if let msgs = context?.messages, !msgs.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    Label("The conversation")
                    if let when = ConversationWhen.label(for: msgs) {
                        Text("· \(when)")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Theme.inkFaint)
                    }
                }
                ForEach(msgs) { ContextBubble(message: $0) }
            }
        } else {
            Text("“\(item.rawText.tightened)”")
                .font(Theme.serif(14))
                .foregroundStyle(Theme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.leading, 12)
                .overlay(alignment: .leading) {
                    Rectangle().fill(Theme.rule).frame(width: 2)
                }
        }
    }

    private var detail: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let briefing = enriched?.briefing, !briefing.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "sparkle").font(.system(size: 11)).foregroundStyle(Theme.brand).padding(.top, 2)
                    Text(briefing)
                        .font(.system(size: 13.5)).foregroundStyle(Theme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            conversation

            if let reply = item.suggestedReply, !reply.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Drafted reply")
                    Text(reply)
                        .font(.system(size: 14))
                        .foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(11)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Theme.chip, in: RoundedRectangle(cornerRadius: 12))
                }
            }

            if !item.why.isEmpty {
                WhyExplanationView(explanations: item.why, urgent: isUrgent)
            }

            doActions
            actions
        }
    }

    // MARK: Do-it actions (v1.1) — open Messages/browser/dialer

    private var sendURL: URL? { item.sendMessagesURL }
    private var openURLTarget: URL? { item.openLinkURL }
    private var callURL: URL? { item.callURL }

    @ViewBuilder
    private var doActions: some View {
        let send = sendURL
        let open = openURLTarget
        let call = callURL
        if send != nil || open != nil || call != nil || item.isCalendarable {
            HStack(spacing: 8) {
                if let send { doButton("Send", "paperplane.fill", url: send) }
                if let open {
                    doButton(item.linkKind == "video" ? "Watch" : "Open",
                             item.linkKind == "video" ? "play.fill" : "safari", url: open)
                }
                if let call, send == nil { doButton("Call", "phone.fill", url: call, primary: false) }
                if item.isCalendarable { CalendarButton(item: item) }
            }
        }
    }

    private func doButton(_ title: String, _ icon: String, url: URL, primary: Bool = true) -> some View {
        Button { openURL(url) } label: {
            SwiftUI.Label(title, systemImage: icon)
                .fontWeight(.semibold)
                .font(.system(size: 13))
                .lineLimit(1).fixedSize()
                .padding(.horizontal, 13).padding(.vertical, 9)
                .background(primary ? Theme.brand : Theme.card, in: RoundedRectangle(cornerRadius: 10))
                .foregroundStyle(primary ? Color.white : Theme.ink)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(primary ? Color.clear : Theme.rule))
        }
        .buttonStyle(.plain)
    }

    private var actions: some View {
        HStack(spacing: 8) {
            Button(action: onDone) {
                Text("Done").fontWeight(.medium)
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(Theme.good, in: RoundedRectangle(cornerRadius: 10))
                    .foregroundStyle(.white)
            }
            Menu {
                Button("1 hour") { onSnooze(1) }
                Button("Tonight") { onSnooze(8) }
                Button("Tomorrow") { onSnooze(24) }
                Button("Next week") { onSnooze(24 * 7) }
            } label: {
                Text("Snooze").fontWeight(.medium)
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.rule))
                    .foregroundStyle(Theme.ink)
            }
            Button(action: onDismiss) {
                Text("Dismiss").fontWeight(.medium)
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .foregroundStyle(Theme.inkFaint)
            }
        }
        .font(.system(size: 13))
        .buttonStyle(.plain)
    }
}

/// An uppercase micro-label used above the drafted reply and "why" blocks.
private struct Label: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .tracking(1.3)
            .foregroundStyle(Theme.inkFaint)
    }
}

/// One line of the surrounding conversation. Alignment carries who spoke — the
/// user's own messages sit right, the counterpart's left — and the message the
/// item was drawn from is tinted so the eye lands on it.
private struct ContextBubble: View {
    let message: ContextMessage

    var body: some View {
        HStack(spacing: 0) {
            if message.isFromUser { Spacer(minLength: 36) }
            VStack(alignment: message.isFromUser ? .trailing : .leading, spacing: 3) {
                if !message.isFromUser {
                    Text(message.sender)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Theme.inkFaint)
                        .padding(.leading, 2)
                }
                Text(message.text)
                    .font(.system(size: 13.5))
                    .foregroundStyle(message.isPivot ? Theme.ink : Theme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 11).padding(.vertical, 8)
                    .background(background, in: RoundedRectangle(cornerRadius: 13))
                    .overlay(
                        RoundedRectangle(cornerRadius: 13)
                            .stroke(message.isPivot ? Theme.brand : Theme.rule,
                                    lineWidth: message.isPivot ? 1.4 : 1)
                    )
            }
            if !message.isFromUser { Spacer(minLength: 36) }
        }
    }

    private var background: Color {
        if message.isPivot { return Theme.brandSoft }
        return message.isFromUser ? Theme.chip : Theme.card
    }
}

/// When the surfaced exchange actually happened — surfaced next to "The
/// conversation" so a months-old conversation reads as months-old.
enum ConversationWhen {
    static func label(for messages: [ContextMessage]) -> String? {
        let pivot = messages.first(where: \.isPivot) ?? messages.first
        return RelativeTime.label(from: pivot?.timestamp)
    }
}

/// The node on the lifeline rail — its shape encodes the tier.
struct RailNode: View {
    let level: InterruptionLevel

    var body: some View {
        Group {
            switch level {
            case .timeSensitive:
                Circle().fill(Theme.ember).frame(width: 9, height: 9)
                    .background(Circle().fill(Theme.emberSoft).frame(width: 17, height: 17))
            case .active:
                Circle().fill(Theme.paper).frame(width: 8, height: 8)
                    .overlay(Circle().stroke(Theme.brand, lineWidth: 1.5))
            case .passive, .unknown:
                Circle().fill(Theme.inkGhost).frame(width: 5, height: 5)
            }
        }
        .frame(width: 26, alignment: .center)
    }
}

/// §8.3 — the scorer's own contributions, in rank order, shown as signed
/// rows with a proportional bar. Transparency into the ranking, not a black box.
struct WhyExplanationView: View {
    let explanations: [Explanation]
    var urgent = false

    private var maxMagnitude: Double {
        max(0.001, explanations.map { abs($0.contribution) }.max() ?? 1)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text((urgent ? "Why this is first" : "Why this is here").uppercased())
                .font(.system(size: 10, weight: .semibold))
                .tracking(1.3)
                .foregroundStyle(Theme.inkFaint)

            ForEach(explanations.prefix(4)) { ex in
                let positive = ex.contribution >= 0
                HStack(alignment: .top, spacing: 8) {
                    Text(positive ? "+" : "−")
                        .font(.system(size: 13, weight: .semibold, design: .monospaced))
                        .foregroundStyle(positive ? Theme.good : Theme.ember)
                        .frame(width: 12)
                    Text(ex.detail)
                        .font(.system(size: 12.5))
                        .foregroundStyle(Theme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Capsule().fill(Theme.rule).frame(width: 44, height: 4)
                        .overlay(alignment: .leading) {
                            Capsule().fill(positive ? Theme.good : Theme.ember)
                                .frame(width: 44 * abs(ex.contribution) / maxMagnitude, height: 4)
                        }
                        .padding(.top, 5)
                }
            }
        }
    }
}

/// A contact face: the real photo (served by the backend) when available, a
/// coloured monogram otherwise, optionally wrapped in a tier ring.
struct Avatar: View {
    let name: String
    var personId: String? = nil
    var level: InterruptionLevel? = nil
    var size: CGFloat = 34

    private static let palette: [Color] = [
        Color(red: 0.43, green: 0.56, blue: 0.61), Color(red: 0.69, green: 0.48, blue: 0.37),
        Color(red: 0.50, green: 0.54, blue: 0.33), Color(red: 0.60, green: 0.44, blue: 0.53),
        Color(red: 0.36, green: 0.52, blue: 0.50),
    ]
    private var color: Color {
        // Stable across launches: Swift randomizes String.hashValue per process,
        // so a contact's colour would change every cold start. Sum the bytes of a
        // stable key instead.
        let key = personId ?? name
        let seed = key.utf8.reduce(0) { ($0 &* 31 &+ Int($1)) & 0xFFFFFF }
        return Self.palette[seed % Self.palette.count]
    }
    private var initials: String {
        let words = name.split(separator: " ").filter { $0.contains(where: \.isLetter) }
        let letters = words.prefix(2).compactMap { $0.first(where: \.isLetter) }
        return letters.isEmpty ? "•" : String(letters).uppercased()
    }
    private var photoURL: URL? {
        guard let personId, let encoded = personId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
        else { return nil }
        return APIClient.defaultBaseURL.appendingPathComponent("people/\(encoded)/avatar")
    }

    var body: some View {
        face
            .frame(width: size, height: size)
            .overlay(Circle().stroke(Theme.paper, lineWidth: level == nil ? 0 : 2))
            .padding(level == nil ? 0 : 2.5)
            .background(Circle().fill(level?.railColor ?? .clear))
    }

    private var face: some View {
        ZStack {
            Circle().fill(color)
            Text(initials)
                .font(.system(size: size * 0.4, weight: .semibold))
                .foregroundStyle(.white)
            if let photoURL {
                AsyncImage(url: photoURL) { image in
                    image.resizable().scaledToFill()
                } placeholder: { Color.clear }
                .clipShape(Circle())
            }
        }
        .clipShape(Circle())
    }
}
