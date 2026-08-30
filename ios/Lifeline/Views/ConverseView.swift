import Observation
import SwiftUI

// MARK: - The voice

/// Original lines in the spirit of the great TV butlers — courteous, precise,
/// one dry aside allowed. Rotates by context, never repeating the last line.
enum ButlerLines {
    static func greeting(deckEmpty: Bool = false, stackCount: Int = 0) -> String {
        let hour = Calendar.current.component(.hour, from: .now)
        var pool: [String]
        switch hour {
        case 5..<12:
            pool = ["Good morning. What may I look into?",
                    "Good morning. The day awaits — as do I."]
        case 12..<18:
            pool = ["Good afternoon. How may I be of service?",
                    "At your service, as ever."]
        case 18..<23:
            pool = ["Good evening. What may I attend to?",
                    "Good evening. I'm at your disposal."]
        default:
            pool = ["Still up? Very well — how may I help?",
                    "The small hours. I'm here nonetheless."]
        }
        if deckEmpty {
            pool += ["The deck is clear. A rare state of grace.",
                     "All handled. Might I suggest enjoying it while it lasts."]
        }
        if stackCount >= 4 {
            pool += ["The stack grows. I say this with love."]
        }
        return pick(pool, key: "butler.greeting")
    }

    static func cleared() -> String {
        pick(["The deck is clear, sir.",
              "All handled. Well done.",
              "Nothing needs you. Savor it."], key: "butler.cleared")
    }

    static func placeholder() -> String {
        pick(["How may I be of service?",
              "Ask, or tell me things…",
              "Anything you'd like looked into?"], key: "butler.placeholder")
    }

    /// Pick from a pool without repeating the previous choice.
    private static func pick(_ pool: [String], key: String) -> String {
        let last = UserDefaults.standard.string(forKey: key)
        let candidates = pool.filter { $0 != last }
        let line = (candidates.isEmpty ? pool : candidates).randomElement() ?? pool[0]
        UserDefaults.standard.set(line, forKey: key)
        return line
    }
}

// MARK: - Model

/// One back-and-forth in the conversation.
struct Exchange: Identifiable {
    let id = UUID()
    let you: String
    var response: ConverseResponse?
    var failed = false
}

@MainActor
@Observable
final class ConverseViewModel {
    private(set) var exchanges: [Exchange] = []
    private(set) var sending = false
    var draft = ""

    /// The conversation continues across launches — it's the aide's memory,
    /// and memory that resets on relaunch isn't memory.
    private static let sessionKey = "LifelineConverseSession"
    private var sessionId: String? {
        get { UserDefaults.standard.string(forKey: Self.sessionKey) }
        set { UserDefaults.standard.set(newValue, forKey: Self.sessionKey) }
    }

    private let sync: SyncService
    init(sync: SyncService) { self.sync = sync }

    /// Reopen the transcript so context is visible, not just remembered.
    func load() async {
        guard exchanges.isEmpty, let session = sessionId,
              let conversation = try? await sync.api.conversation(sessionId: session) else { return }
        var restored: [Exchange] = []
        for turn in conversation.turns {
            if turn.role == "user" {
                restored.append(Exchange(you: turn.text))
            } else if !restored.isEmpty {
                restored[restored.count - 1].response = ConverseResponse(
                    reply: turn.text, sessionId: session, facts: turn.facts, trace: turn.trace
                )
            }
        }
        exchanges = restored
    }

    func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sending else { return }
        sending = true
        defer { sending = false }
        draft = ""
        exchanges.append(Exchange(you: text))
        do {
            let response = try await sync.api.converse(text, sessionId: sessionId)
            sessionId = response.sessionId
            exchanges[exchanges.count - 1].response = response
        } catch {
            exchanges[exchanges.count - 1].failed = true
        }
    }

    /// Start fresh — a new subject deserves a clean slate.
    func newConversation() {
        sessionId = nil
        exchanges = []
    }
}

// MARK: - The page

/// The one conversational door (§v1.5). Statements become facts; questions get
/// investigated — and the trace shows every tool that fired, receipts not
/// spinners. Speaks with a butler's courtesy; the sass stays in the asides.
struct ConverseView: View {
    @Environment(\.syncService) private var syncService
    @Environment(\.dismiss) private var dismiss
    @State private var model: ConverseViewModel?
    @FocusState private var writing: Bool
    // Rolled once per appearance. Calling ButlerLines.* inside `body` re-rolled
    // them on every keystroke — the greeting visibly changed while typing.
    @State private var greeting = ButlerLines.greeting()
    @State private var placeholder = ButlerLines.placeholder()

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        header
                        ForEach(model?.exchanges ?? []) { exchange in
                            ExchangeBlock(exchange: exchange).id(exchange.id)
                        }
                        if model?.sending == true {
                            thinking
                        }
                        Color.clear.frame(height: 8).id("tail")
                    }
                    .padding(.horizontal, 22)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: model?.exchanges.count ?? 0) {
                    withAnimation { proxy.scrollTo("tail") }
                }
            }
            .scrollDismissesKeyboard(.interactively)
            .background(Theme.paper.ignoresSafeArea())
            .safeAreaInset(edge: .bottom) { inputBar }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    NavigationLink {
                        ModelOfYouView(embedded: true)
                    } label: {
                        SwiftUI.Label("What I know", systemImage: "text.book.closed")
                            .font(.system(size: 13)).foregroundStyle(Theme.inkSoft)
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    HStack(spacing: 14) {
                        if !(model?.exchanges.isEmpty ?? true) {
                            Button {
                                model?.newConversation()
                                greeting = ButlerLines.greeting()
                            } label: {
                                Image(systemName: "square.and.pencil")
                                    .font(.system(size: 15)).foregroundStyle(Theme.inkSoft)
                            }
                        }
                        Button("Done") { dismiss() }.foregroundStyle(Theme.brand)
                    }
                }
            }
            .task {
                if model == nil { model = ConverseViewModel(sync: syncService) }
                await model?.load()      // reopen where the conversation left off
                writing = true
            }
            // One success tick when facts are captured — "it entered the brain."
            .sensoryFeedback(.success,
                             trigger: model?.exchanges.last?.response?.facts.count ?? 0) { _, n in n > 0 }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Image(systemName: "brain")
                    .font(.system(size: 11, weight: .medium)).foregroundStyle(Theme.inkFaint)
                Text("Model of you").font(.system(size: 12)).foregroundStyle(Theme.inkFaint)
            }
            Text(greeting)
                .font(.system(size: 24, weight: .bold)).tracking(-0.4)
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, 8).padding(.bottom, 14)
    }

    private var thinking: some View {
        HStack(spacing: 7) {
            ProgressView().controlSize(.small)
            Text("looking into it…").font(.system(size: 12)).foregroundStyle(Theme.inkFaint)
        }
        .padding(.vertical, 12)
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            TextField(placeholder,
                      text: Binding(get: { model?.draft ?? "" }, set: { model?.draft = $0 }),
                      axis: .vertical)
                .lineLimit(1...4)
                .focused($writing)
                .font(.system(size: 15))
                .tint(Theme.wave)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(Theme.card, in: RoundedRectangle(cornerRadius: 16))
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(Theme.rule))
                .submitLabel(.send)
                .onSubmit { Task { await model?.send() } }

            Button {
                Task { await model?.send() }
            } label: {
                Image(systemName: "arrow.up")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 34, height: 34)
                    .background(Circle().fill(
                        (model?.draft.isEmpty ?? true) ? Theme.inkGhost : Theme.wave))
            }
            .buttonStyle(.plain)
            .disabled(model?.draft.isEmpty ?? true || model?.sending == true)
        }
        .padding(.horizontal, 18).padding(.top, 8).padding(.bottom, 10)
        .background(Theme.paper)
    }
}

// MARK: - One exchange

private struct ExchangeBlock: View {
    let exchange: Exchange
    @Environment(\.openURL) private var openURL
    @State private var revealed = 0     // trace rows shown so far (staggered)

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("YOU")
                .font(.system(size: 9, weight: .semibold)).tracking(1.4)
                .foregroundStyle(Theme.inkGhost)
            Text(exchange.you)
                .font(.system(size: 14.5)).foregroundStyle(Theme.ink)
                .padding(.top, 3)

            if exchange.failed {
                Text("Couldn't reach the backend — try again.")
                    .font(.system(size: 12.5)).foregroundStyle(Theme.ember)
                    .padding(.top, 8)
            }

            if let response = exchange.response {
                if !response.trace.isEmpty { trace(response.trace) }
                if !response.facts.isEmpty { captured(response.facts) }
                reply(response.reply, receipts: response.trace)
                if let draft = response.draft { draftCard(draft) }
            }
        }
        .padding(.vertical, 12)
        .task { await reveal(exchange.response?.trace.count ?? 0) }
        .onChange(of: exchange.response?.trace.count ?? 0) { _, n in
            Task { await reveal(n) }
        }
    }

    /// The receipts stream in one by one — you watch it think.
    private func reveal(_ count: Int) async {
        guard count > 0, revealed == 0 else { return }
        for i in 1...count {
            try? await Task.sleep(for: .milliseconds(260))
            withAnimation(.easeOut(duration: 0.3)) { revealed = i }
        }
    }

    private func trace(_ steps: [TraceStep]) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(steps.enumerated()), id: \.offset) { i, step in
                if i < revealed {
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Text(step.tool)
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(Theme.wave)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Theme.chip, in: RoundedRectangle(cornerRadius: 5))
                        Text(step.summary)
                            .font(.system(size: 11.5)).foregroundStyle(Theme.inkSoft)
                            .fixedSize(horizontal: false, vertical: true)
                        Image(systemName: step.ok ? "checkmark" : "minus")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(step.ok ? Theme.good : Theme.inkGhost)
                    }
                    .transition(.opacity.combined(with: .move(edge: .leading)))
                }
            }
        }
        .padding(.leading, 11)
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 1).fill(Theme.wave.opacity(0.6)).frame(width: 2)
        }
        .padding(.top, 12)
    }

    private func captured(_ facts: [FactItem]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(facts) { fact in
                HStack(alignment: .top, spacing: 8) {
                    Circle().fill(Theme.wave).frame(width: 6, height: 6)
                        .shadow(color: Theme.wave.opacity(0.6), radius: 3)
                        .padding(.top, 5)
                    Text(fact.statement)
                        .font(.system(size: 13)).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.vertical, 7).padding(.horizontal, 11)
                .background(Theme.chip, in: RoundedRectangle(cornerRadius: 10))
            }
        }
        .padding(.top, 12)
    }

    /// The written message, ready to go out. Editable-by-review: you see the
    /// exact words before Messages/Mail opens — the app never sends for you.
    private func draftCard(_ draft: MessageDraft) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 6) {
                Image(systemName: draft.channel == "email" ? "envelope" : "message")
                    .font(.system(size: 10)).foregroundStyle(Theme.brand)
                Text("DRAFT TO \(draft.person.uppercased())")
                    .font(.system(size: 9, weight: .semibold)).tracking(1.2)
                    .foregroundStyle(Theme.inkFaint)
                    .lineLimit(1)
            }
            Text(draft.text)
                .font(.system(size: 14)).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            if let url = draft.sendURL {
                Button {
                    openURL(url)
                } label: {
                    SwiftUI.Label("Review & send", systemImage: "paperplane.fill")
                        .font(.system(size: 13, weight: .semibold))
                        .lineLimit(1).fixedSize()
                        .padding(.horizontal, 14).padding(.vertical, 9)
                        .background(Theme.brand, in: RoundedRectangle(cornerRadius: 10))
                        .foregroundStyle(.white)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(13)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.chip, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.brand.opacity(0.25)))
        .padding(.top, 10)
    }

    private func reply(_ text: String, receipts: [TraceStep]) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 6) {
                Circle().fill(Theme.wave).frame(width: 5, height: 5)
                    .shadow(color: Theme.wave.opacity(0.6), radius: 3)
                Text("LIFELINE")
                    .font(.system(size: 9, weight: .semibold)).tracking(1.4)
                    .foregroundStyle(Theme.inkFaint)
            }
            Text(text)
                .font(.system(size: 13.5)).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            if !receipts.isEmpty {
                Text("Grounded in \(receipts.count) check\(receipts.count == 1 ? "" : "s")")
                    .font(.system(size: 10)).foregroundStyle(Theme.inkGhost)
            }
        }
        .padding(13)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.rule))
        .padding(.top, 12)
    }
}
