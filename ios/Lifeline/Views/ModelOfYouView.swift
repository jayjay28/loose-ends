import Observation
import SwiftUI

/// The model of you (§v1.4 pillar B): tell Lifeline things in your own words,
/// and inspect/correct everything it believes. The user overrules the system,
/// never the reverse — an edit here is the highest-quality signal we get.
@MainActor
@Observable
final class ModelOfYouViewModel {
    private(set) var model: ModelOfYou?
    private(set) var sending = false
    private(set) var lastReply: String?          // the assistant's confirmation
    private(set) var captured: [FactItem] = []   // facts from the latest tell
    var draft = ""

    private let sync: SyncService
    init(sync: SyncService) { self.sync = sync }

    func load() async {
        model = (try? await sync.api.modelOfYou()) ?? model ?? ModelOfYou()
    }

    func tell() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sending else { return }
        sending = true
        defer { sending = false }
        do {
            let response = try await sync.api.tell(text)
            lastReply = response.reply
            captured = response.facts
            draft = ""
            await load()
        } catch {
            lastReply = "Couldn't reach the backend — try again."
        }
    }

    func edit(_ fact: FactItem, to statement: String) async {
        let text = statement.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text != fact.statement else { return }
        _ = try? await sync.api.editFact(id: fact.id, statement: text)
        captured.removeAll { $0.id == fact.id }
        await load()
    }

    func dismiss(_ fact: FactItem) async {
        _ = try? await sync.api.dismissFact(id: fact.id)
        captured.removeAll { $0.id == fact.id }
        await load()
    }
}

/// A page, not a form — the Notion feel. Big title, borderless writing block,
/// captured facts bloom beneath what you wrote, the full model reads below.
struct ModelOfYouView: View {
    /// Pushed inside another stack (ConverseView) rather than presented as its
    /// own sheet — skips the extra NavigationStack and the Done button.
    var embedded = false

    @Environment(\.syncService) private var syncService
    @Environment(\.dismiss) private var dismiss
    @State private var model: ModelOfYouViewModel?
    @State private var editing: FactItem?
    @State private var editText = ""
    @FocusState private var writing: Bool

    var body: some View {
        if embedded {
            content
        } else {
            NavigationStack { content }
        }
    }

    private var content: some View {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    breadcrumb
                    Text("Tell me something")
                        .font(.system(size: 28, weight: .bold))
                        .tracking(-0.4)
                        .foregroundStyle(Theme.ink)
                        .padding(.top, 14)
                    Text("\(Date.now.formatted(.dateTime.weekday(.wide).month().day()))  ·  shapes what surfaces")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.inkFaint)
                        .padding(.top, 4)

                    composer
                    capturedBlocks

                    if let m = model?.model, !m.isEmpty {
                        knownDivider
                        factsBlock("About you", facts: m.you)
                        ForEach(m.people) { subjectBlock($0) }
                        ForEach(m.topics) { subjectBlock($0, prefix: "Topic") }
                    } else if model?.model != nil {
                        Text("Nothing on file yet. Priorities, people, plans — it starts here.")
                            .font(.system(size: 13)).foregroundStyle(Theme.inkGhost)
                            .padding(.top, 28)
                    }
                }
                .padding(.horizontal, 24)
                .padding(.bottom, 40)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .scrollDismissesKeyboard(.interactively)
            .background(Theme.paper.ignoresSafeArea())
            .toolbar {
                if !embedded {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Done") { dismiss() }.foregroundStyle(Theme.brand)
                    }
                }
            }
            .task {
                if model == nil { model = ModelOfYouViewModel(sync: syncService) }
                await model?.load()
                writing = true
            }
            // One success tick per capture batch — "it entered the brain."
            .sensoryFeedback(.success, trigger: model?.captured.count ?? 0) { _, n in n > 0 }
            .alert("Correct this fact", isPresented: .init(
                get: { editing != nil },
                set: { if !$0 { editing = nil } }
            )) {
                TextField("Fact", text: $editText)
                Button("Save") {
                    if let fact = editing {
                        Task { await model?.edit(fact, to: editText) }
                    }
                    editing = nil
                }
                Button("Cancel", role: .cancel) { editing = nil }
            } message: {
                Text("Your version wins — the system treats it as ground truth.")
            }
    }

    private var breadcrumb: some View {
        HStack(spacing: 6) {
            Image(systemName: "brain")
                .font(.system(size: 11, weight: .medium)).foregroundStyle(Theme.inkFaint)
            Text("Model of you").font(.system(size: 12)).foregroundStyle(Theme.inkFaint)
        }
        .padding(.top, 6)
    }

    // MARK: - the writing block

    private var composer: some View {
        VStack(alignment: .leading, spacing: 10) {
            TextField(
                "Katie is the recruiter for the job I want…",
                text: Binding(get: { model?.draft ?? "" }, set: { model?.draft = $0 }),
                axis: .vertical
            )
            .lineLimit(2...8)
            .focused($writing)
            .font(.system(size: 16))
            .foregroundStyle(Theme.ink)
            .tint(Theme.wave)                     // the caret is the capture-blue
            .submitLabel(.send)
            .onSubmit { Task { await model?.tell() } }

            HStack {
                if let reply = model?.lastReply {
                    Text(reply)
                        .font(.system(size: 12.5)).foregroundStyle(Theme.inkSoft)
                        .transition(.opacity)
                }
                Spacer()
                Button {
                    Task { await model?.tell() }
                } label: {
                    if model?.sending == true {
                        ProgressView().controlSize(.small).frame(width: 32, height: 32)
                    } else {
                        Image(systemName: "arrow.up")
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(.white)
                            .frame(width: 32, height: 32)
                            .background(Circle().fill(
                                (model?.draft.isEmpty ?? true) ? Theme.inkGhost : Theme.wave))
                    }
                }
                .buttonStyle(.plain)
                .disabled(model?.draft.isEmpty ?? true || model?.sending == true)
            }
        }
        .padding(.top, 22)
    }

    // MARK: - captured (the bloom)

    @ViewBuilder
    private var capturedBlocks: some View {
        if let captured = model?.captured, !captured.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 5) {
                    Circle().fill(Theme.wave).frame(width: 5, height: 5)
                    Text("CAPTURED · TAP TO CORRECT")
                        .font(.system(size: 9.5, weight: .semibold)).tracking(1.2)
                        .foregroundStyle(Theme.inkFaint)
                }
                ForEach(Array(captured.enumerated()), id: \.element.id) { i, fact in
                    factRow(fact, dotted: true)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                        .animation(.spring(response: 0.45, dampingFraction: 0.8).delay(Double(i) * 0.12),
                                   value: captured.count)
                }
            }
            .padding(.top, 18)
        }
    }

    // MARK: - what I know

    private var knownDivider: some View {
        HStack(spacing: 8) {
            Text("What I know").font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.inkFaint)
            Rectangle().fill(Theme.rule).frame(height: 1)
        }
        .padding(.top, 34).padding(.bottom, 6)
    }

    @ViewBuilder
    private func factsBlock(_ header: String, facts: [FactItem]) -> some View {
        if !facts.isEmpty {
            sectionHeader(header, tie: nil)
            ForEach(facts) { factRow($0) }
        }
    }

    @ViewBuilder
    private func subjectBlock(_ subject: SubjectFacts, prefix: String? = nil) -> some View {
        sectionHeader(prefix.map { "\($0) · \(subject.name)" } ?? subject.name, tie: subject.tieLabel)
        ForEach(subject.facts) { factRow($0) }
    }

    private func sectionHeader(_ title: String, tie: String?) -> some View {
        HStack(spacing: 6) {
            Text(title).font(.system(size: 13.5, weight: .semibold)).foregroundStyle(Theme.ink)
            if let tie {
                Text(tie).font(.system(size: 11)).foregroundStyle(Theme.inkFaint)
            }
        }
        .padding(.top, 16).padding(.bottom, 2)
    }

    private func factRow(_ fact: FactItem, dotted: Bool = false) -> some View {
        HStack(alignment: .top, spacing: 9) {
            if dotted {
                Circle().fill(Theme.wave).frame(width: 6, height: 6)
                    .shadow(color: Theme.wave.opacity(0.6), radius: 3)
                    .padding(.top, 6)
            } else {
                Image(systemName: fact.source == "user" ? "person.fill" : "sparkle")
                    .font(.system(size: 9))
                    .foregroundStyle(fact.source == "user" ? Theme.brand : Theme.gold)
                    .padding(.top, 4)
            }
            Text(fact.statement)
                .font(.system(size: 14))
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 7).padding(.horizontal, dotted ? 11 : 0)
        .background(
            dotted ? RoundedRectangle(cornerRadius: 10).fill(Theme.chip) : nil
        )
        .contentShape(Rectangle())
        .onTapGesture {
            editText = fact.statement
            editing = fact
        }
        .contextMenu {
            Button("Correct…") { editText = fact.statement; editing = fact }
            Button("Forget", role: .destructive) { Task { await model?.dismiss(fact) } }
        }
    }
}
