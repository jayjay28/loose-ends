import SwiftUI

/// §v2.9 — **Ask is not a chat.** A question here is ask → answer → done or
/// act: no session, no bubbles. The answer is a card in the house style of a
/// finding — one serif sentence, receipts that open, the fact it drew on
/// (correctable on the spot) — and past answers stack below as a reference.
///
/// The signed mockup is `docs/design/v2.9-ask.html`. One deliberate departure
/// for v1: the trace renders on the finished card rather than streaming
/// during the wait (streaming needs SSE through the loop runner — the
/// fast-follow if the wait feels dead, not a schema change).
struct AskView: View {
    @Environment(\.syncService) private var syncService

    @State private var question = ""
    @State private var asking = false
    @State private var waitingCopy = "Reading your world…"
    @State private var cards: [AskCard] = []
    @State private var openReceipt: Receipt?
    @State private var correcting: KnownFact?
    @FocusState private var fieldFocused: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                askField
                if asking { progress }
                ForEach(cards) { card in
                    AnswerCard(card: card,
                               onOpenReceipt: { openReceipt = $0 },
                               onWrong: { correcting = $0 })
                }
                if cards.isEmpty && !asking { emptyState }
            }
            .padding(Theme.margin)
        }
        .background(Theme.paper)
        .navigationTitle("Ask")
        .navigationBarTitleDisplayMode(.large)
        .task { await loadHistory() }
        .sheet(item: $openReceipt) { receipt in
            ReceiptSheet(receipt: receipt)
        }
        .sheet(item: $correcting) { fact in
            CorrectFactSheet(fact: fact) { action, value in
                Task { await correct(fact, action: action, value: value) }
            }
            .presentationDetents([.medium])
        }
    }

    private var askField: some View {
        HStack(spacing: 8) {
            Image(systemName: "sparkle.magnifyingglass")
                .font(.system(size: 14)).foregroundStyle(Theme.inkFaint)
            TextField("Ask about your life", text: $question)
                .font(.system(size: 15))
                .focused($fieldFocused)
                .submitLabel(.go)
                .onSubmit { Task { await submit() } }
                .disabled(asking)
        }
        .padding(.horizontal, 13).padding(.vertical, 11)
        .background(Theme.chip, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12)
            .strokeBorder(fieldFocused ? Theme.brand : Theme.rule, lineWidth: 1))
    }

    /// Not dead air: the copy walks through what the loop is actually doing,
    /// on a timer since v1 doesn't stream the real steps.
    private var progress: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(waitingCopy).font(.system(size: 13)).foregroundStyle(Theme.inkSoft)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(13)
        .background(Theme.chip.opacity(0.6), in: RoundedRectangle(cornerRadius: 12))
        .task {
            for copy in ["Resolving who's who…", "Searching mail and messages…",
                         "Reading what it found…", "Writing the answer…"] {
                try? await Task.sleep(for: .seconds(4))
                if !asking { break }
                withAnimation { waitingCopy = copy }
            }
        }
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Ask about the life in your messages.")
                .font(.system(size: 13)).foregroundStyle(Theme.inkSoft)
            Text("“Where is Lia's daycare?” · “What's wrong with the water account?”")
                .font(.system(size: 12)).foregroundStyle(Theme.inkFaint)
        }
        .padding(.top, 8)
    }

    private func submit() async {
        let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty, !asking else { return }
        asking = true
        waitingCopy = "Reading your world…"
        defer { asking = false }
        do {
            let card = try await syncService.api.ask(q)
            withAnimation { cards.insert(card, at: 0) }
            question = ""
        } catch {
            // The failure is a card too — silence is how buttons die.
            withAnimation {
                cards.insert(errorCard(question: q, error: error), at: 0)
            }
        }
    }

    private func loadHistory() async {
        if cards.isEmpty {
            cards = (try? await syncService.api.askHistory()) ?? []
        }
    }

    private func correct(_ fact: KnownFact, action: String, value: String?) async {
        try? await syncService.api.correctFact(id: fact.factId, action: action, value: value)
        // Reflect locally: the strip should stop showing what was retired.
        for i in cards.indices {
            cards[i].knew.removeAll { $0.factId == fact.factId }
        }
    }

    private func errorCard(question: String, error: Error) -> AskCard {
        let json = """
        {"id": "\(UUID().uuidString)", "question": \(jsonString(question)),
         "answer": "Couldn't reach the backend — \(error.localizedDescription.replacingOccurrences(of: "\"", with: "'").prefix(90)). Try again.",
         "created_at": ""}
        """
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return (try? decoder.decode(AskCard.self, from: Data(json.utf8)))
            ?? (try! decoder.decode(AskCard.self, from: Data("{\"answer\": \"Something went wrong.\"}".utf8)))
    }

    private func jsonString(_ s: String) -> String {
        String(data: (try? JSONEncoder().encode(s)) ?? Data("\"?\"".utf8), encoding: .utf8) ?? "\"?\""
    }
}

// MARK: - the card

struct AnswerCard: View {
    let card: AskCard
    var onOpenReceipt: (Receipt) -> Void
    var onWrong: (KnownFact) -> Void

    @State private var showTrace = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(card.question.uppercased())
                .font(.system(size: 10, weight: .semibold)).tracking(0.8)
                .foregroundStyle(Theme.inkFaint)
            Text(card.answer)
                .font(Theme.serif(17, .semibold))
                .foregroundStyle(Theme.ink)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)

            if !card.receipts.isEmpty {
                FlowChips(receipts: card.receipts, onTap: onOpenReceipt)
            }

            ForEach(card.knew) { fact in
                HStack(alignment: .firstTextBaseline, spacing: 7) {
                    Text("KNEW").font(.system(size: 9, weight: .semibold)).tracking(1)
                        .foregroundStyle(Theme.brand)
                    Text(fact.line).font(.system(size: 12)).foregroundStyle(Theme.inkSoft)
                        .lineLimit(2)
                    Spacer(minLength: 4)
                    Button("wrong?") { onWrong(fact) }
                        .font(.system(size: 12)).tint(Theme.alert)
                        .buttonStyle(.plain).foregroundStyle(Theme.alert)
                }
                .padding(.top, 2)
            }

            if !card.trace.isEmpty {
                Button {
                    withAnimation { showTrace.toggle() }
                } label: {
                    Text(showTrace ? "Hide how it looked" : "How it looked")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Theme.inkFaint)
                }
                .buttonStyle(.plain)
                if showTrace {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(card.trace.enumerated()), id: \.offset) { _, step in
                            HStack(spacing: 6) {
                                Image(systemName: step.ok ? "checkmark" : "minus")
                                    .font(.system(size: 9))
                                    .foregroundStyle(step.ok ? Theme.brand : Theme.inkGhost)
                                Text("\(step.tool) — \(step.summary)")
                                    .font(.system(size: 11)).foregroundStyle(Theme.inkSoft)
                                    .lineLimit(1)
                            }
                        }
                    }
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.chip.opacity(0.55), in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Theme.rule, lineWidth: 1))
    }
}

/// Receipt chips that wrap. A simple flow: rows of chips via LazyVGrid would
/// force uniform widths, so lay them in a wrapping HStack the cheap way.
private struct FlowChips: View {
    let receipts: [Receipt]
    var onTap: (Receipt) -> Void

    var body: some View {
        FlexibleRow(spacing: 7) {
            ForEach(receipts) { receipt in
                Button { onTap(receipt) } label: {
                    HStack(spacing: 5) {
                        Text(receipt.sourceTag)
                            .font(.system(size: 8, weight: .bold)).tracking(0.5)
                            .foregroundStyle(Theme.inkGhost)
                        Text(receipt.label)
                            .font(.system(size: 12)).foregroundStyle(Theme.ink)
                            .lineLimit(1)
                    }
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .background(Theme.paper, in: Capsule())
                    .overlay(Capsule().strokeBorder(Theme.rule, lineWidth: 1))
                }
                .buttonStyle(.plain)
            }
        }
    }
}

/// Minimal wrapping layout for chips.
private struct FlexibleRow: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x + size.width > width, x > 0 { x = 0; y += rowHeight + spacing; rowHeight = 0 }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: width, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x + size.width > bounds.maxX, x > bounds.minX {
                x = bounds.minX; y += rowHeight + spacing; rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: .unspecified)
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}

// MARK: - receipt sheet

/// What a chip opens: the message, whole. The receipt is a product feature —
/// an answer whose sources can't be read is an assertion.
private struct ReceiptSheet: View {
    let receipt: Receipt
    @Environment(\.syncService) private var syncService
    @State private var message: MessageInFull?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if let message {
                        Text("\(message.sender) · \(String(message.timestamp.prefix(10)))")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(Theme.inkFaint)
                        if !message.subject.isEmpty {
                            Text(message.subject).font(Theme.serif(16, .semibold))
                                .foregroundStyle(Theme.ink)
                        }
                        Text(message.text).font(.system(size: 13))
                            .foregroundStyle(Theme.ink).textSelection(.enabled)
                        ForEach(message.attachments, id: \.filename) { attachment in
                            SwiftUI.Label(attachment.filename,
                                          systemImage: attachment.hasText ? "doc.text" : "doc")
                                .font(.system(size: 12)).foregroundStyle(Theme.inkSoft)
                        }
                    } else {
                        ProgressView().frame(maxWidth: .infinity)
                    }
                }
                .padding(Theme.margin)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(Theme.paper)
            .navigationTitle(receipt.sourceTag)
            .navigationBarTitleDisplayMode(.inline)
        }
        .task {
            if receipt.kind == "message" {
                message = try? await syncService.api.messageInFull(id: receipt.refId)
            }
        }
    }
}

// MARK: - the wrong? sheet

/// The correction door, exactly the mockup's three options. Each writes a
/// user-sourced fact that outranks the derived one; nothing is deleted.
private struct CorrectFactSheet: View {
    let fact: KnownFact
    var onAction: (String, String?) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var replacement = ""

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 14) {
                Text("Fix what it knows").font(Theme.serif(18, .semibold))
                Text(fact.line)
                    .font(.system(size: 12, design: .monospaced))
                    .strikethrough()
                    .foregroundStyle(Theme.inkSoft)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Theme.chip, in: RoundedRectangle(cornerRadius: 9))

                Button {
                    onAction("correct", "this is the user")
                    dismiss()
                } label: { optionLabel("That's me — I'm the user") }
                .buttonStyle(.plain)

                HStack(spacing: 8) {
                    TextField("It's actually…", text: $replacement)
                        .font(.system(size: 14))
                        .padding(10)
                        .background(Theme.paper, in: RoundedRectangle(cornerRadius: 9))
                        .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(Theme.rule))
                    Button("Save") {
                        let value = replacement.trimmingCharacters(in: .whitespaces)
                        guard !value.isEmpty else { return }
                        onAction("correct", value)
                        dismiss()
                    }
                    .font(.system(size: 13, weight: .semibold)).tint(Theme.brand)
                    .disabled(replacement.trimmingCharacters(in: .whitespaces).isEmpty)
                }

                Button {
                    onAction("forget", nil)
                    dismiss()
                } label: {
                    Text("Just forget this").font(.system(size: 14))
                        .foregroundStyle(Theme.alert)
                }
                .buttonStyle(.plain)

                Spacer()
            }
            .padding(Theme.margin)
            .background(Theme.paper)
        }
    }

    private func optionLabel(_ text: String) -> some View {
        Text(text).font(.system(size: 14))
            .padding(.vertical, 11).padding(.horizontal, 13)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.paper, in: RoundedRectangle(cornerRadius: 9))
            .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(Theme.rule))
    }
}
