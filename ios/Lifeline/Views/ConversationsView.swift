import Observation
import SwiftUI

/// §8.2 Conversations — per-person view of everything still open with someone.
/// Cache-first like Today; the server owns ordering and level.
@MainActor
@Observable
final class ConversationsViewModel {
    private(set) var conversations: [ConversationSummary]?
    private(set) var isLoading = false
    private(set) var errorMessage: String?

    private let sync: SyncService
    init(sync: SyncService) { self.sync = sync }

    func load() async {
        if ConversationsViewModel.self.usesSample {
            conversations = SampleData.conversations
            return
        }
        if conversations == nil, let cached = await sync.cached([ConversationSummary].self, key: CacheKey.conversations) {
            conversations = cached
        }
        await refresh()
    }

    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            conversations = try await sync.refreshConversations()
            errorMessage = nil
        } catch {
            if conversations == nil { errorMessage = error.localizedDescription }
        }
    }

    static var usesSample: Bool { ProcessInfo.processInfo.arguments.contains("-uiSampleData") }
}

struct ConversationsView: View {
    @Environment(\.syncService) private var syncService
    @State private var viewModel: ConversationsViewModel?

    var body: some View {
        NavigationStack {
            content
                .background(Theme.paper.ignoresSafeArea())
                .navigationTitle("Conversations")
                .task {
                    if viewModel == nil { viewModel = ConversationsViewModel(sync: syncService) }
                    await viewModel?.load()
                }
                .refreshable { await viewModel?.refresh() }
        }
    }

    @ViewBuilder
    private var content: some View {
        if let conversations = viewModel?.conversations {
            if conversations.isEmpty {
                QuietState(title: "No open conversations", subhead: "Every conversation is caught up.")
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(conversations) { conversation in
                            NavigationLink {
                                ConversationDetailView(conversation: conversation)
                            } label: {
                                ConversationRow(conversation: conversation)
                            }
                            .buttonStyle(.plain)
                            Rectangle().fill(Theme.ruleSoft).frame(height: 1)
                        }
                    }
                    .padding(.horizontal, 22)
                    .padding(.top, 8)
                }
            }
        } else if viewModel?.errorMessage != nil {
            QuietState(title: "Couldn't load conversations", subhead: viewModel?.errorMessage ?? "")
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

private struct ConversationRow: View {
    let conversation: ConversationSummary

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            Avatar(name: conversation.person, personId: conversation.personId, level: conversation.topLevel, size: 38)
            VStack(alignment: .leading, spacing: 3) {
                Text(conversation.topic ?? conversation.person)
                    .font(.system(size: 16, weight: .medium))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(1)
                Text(subtitle)
                    .font(.system(size: 12.5))
                    .foregroundStyle(Theme.inkFaint)
            }
            Spacer()
            Text("\(conversation.openCount)")
                .font(.system(size: 13, weight: .semibold))
                .monospacedDigit()
                .foregroundStyle(Theme.inkSoft)
                .padding(.horizontal, 9).padding(.vertical, 4)
                .background(Theme.chip, in: Capsule())
            Image(systemName: "chevron.right").font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.inkGhost)
        }
        .padding(.vertical, 14)
        .contentShape(Rectangle())
    }

    private var subtitle: String {
        var parts: [String] = []
        // When the topic is the title, lead the subtitle with who it's with.
        if conversation.topic != nil { parts.append(conversation.person) }
        if let rel = conversation.relationship, !rel.isEmpty { parts.append(rel) }
        parts.append(conversation.sources.joined(separator: ", "))
        if let last = RelativeTime.label(from: conversation.lastActivity) { parts.append(last) }
        return parts.joined(separator: " · ")
    }
}

/// A single person's still-open items — actionable per-item, and stageable:
/// tick a few, draft one reply that answers them together, send once to clear
/// them all. Conversations is where you close the loop with a *person* (Phase D).
struct ConversationDetailView: View {
    let conversation: ConversationSummary
    @Environment(\.syncService) private var syncService
    @State private var items: [Item]?
    @State private var error: String?
    @State private var selection: Set<String> = []
    @State private var staging = false

    private var selected: [Item] { (items ?? []).filter { selection.contains($0.id) } }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array((items ?? []).enumerated()), id: \.element.id) { idx, item in
                    HStack(alignment: .top, spacing: 11) {
                        SelectToggle(on: selection.contains(item.id)) { toggle(item.id) }
                            .padding(.top, 15)
                        DecisionItemCard(item: item, startExpanded: idx == 0 && selection.isEmpty) {
                            await markDone(item)
                        }
                    }
                    .padding(.vertical, 8)
                    if idx < (items?.count ?? 0) - 1 {
                        Rectangle().fill(Theme.ruleSoft).frame(height: 1)
                    }
                }
                if let error { Text(error).font(.footnote).foregroundStyle(Theme.inkFaint).padding(.top, 20) }
            }
            .padding(.horizontal, 22)
            .padding(.top, 8)
        }
        .background(Theme.paper.ignoresSafeArea())
        .navigationTitle(conversation.person)
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) { if !selection.isEmpty { stageBar } }
        .sheet(isPresented: $staging) {
            BatchReplySheet(personId: conversation.id, person: conversation.person, items: selected) { cleared in
                withAnimation {
                    items?.removeAll { cleared.contains($0.id) }
                    selection.subtract(cleared)
                }
            }
        }
        .task {
            if ConversationsViewModel.usesSample { items = SampleData.conversationItems; return }
            do { items = try await syncService.api.conversationItems(personId: conversation.id) }
            catch { self.error = error.localizedDescription }
        }
    }

    private var stageBar: some View {
        Button { staging = true } label: {
            HStack(spacing: 9) {
                Image(systemName: "arrow.turn.up.right")
                Text(selection.count == 1 ? "Draft a reply" : "Draft one reply")
                    .fontWeight(.semibold)
                Text("· \(selection.count) selected").foregroundStyle(.white.opacity(0.75))
            }
            .font(.system(size: 15))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 15)
            .background(Theme.brand, in: RoundedRectangle(cornerRadius: 14))
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 22).padding(.bottom, 6)
        .background(.ultraThinMaterial)
    }

    private func toggle(_ id: String) {
        withAnimation(.easeOut(duration: 0.15)) {
            if selection.contains(id) { selection.remove(id) } else { selection.insert(id) }
        }
    }

    private func markDone(_ item: Item) async {
        if ConversationsViewModel.usesSample {
            withAnimation { items?.removeAll { $0.id == item.id } }
            return
        }
        do {
            _ = try await syncService.api.markDone(itemId: item.id)
            withAnimation { items?.removeAll { $0.id == item.id } }
        } catch { self.error = error.localizedDescription }
    }
}

/// A round check used to stage items for a batched reply.
private struct SelectToggle: View {
    let on: Bool
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            ZStack {
                Circle()
                    .fill(on ? Theme.brand : Color.clear)
                    .overlay(Circle().stroke(on ? Theme.brand : Theme.inkGhost, lineWidth: 1.6))
                    .frame(width: 24, height: 24)
                if on {
                    Image(systemName: "checkmark")
                        .font(.system(size: 12, weight: .bold)).foregroundStyle(.white)
                }
            }
        }
        .buttonStyle(.plain)
    }
}

/// One item in a conversation: a payoff header that pulls open into its full
/// decision thread (Heard → Understood → Weighed → Surfaced).
private struct DecisionItemCard: View {
    let item: Item
    let startExpanded: Bool
    var onDone: () async -> Void
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button {
                withAnimation(.easeOut(duration: 0.22)) { expanded.toggle() }
            } label: {
                VStack(alignment: .leading, spacing: 6) {
                    Text(item.suggestedAction.isEmpty ? item.rawText : item.suggestedAction)
                        .font(Theme.serif(19, .semibold))
                        .foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 6) {
                        Text(item.person).foregroundStyle(Theme.inkSoft).fontWeight(.medium)
                        if let age = ThreadAge.waited(item.timestamp) {
                            Text("· waiting \(age.text)")
                                .foregroundStyle(age.urgent ? Theme.ember : Theme.inkFaint)
                                .fontWeight(age.urgent ? .semibold : .regular)
                        }
                    }
                    .font(.system(size: 12.5))
                    Text(expanded ? "Hide the thread ↑" : "Pull the thread ↓")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(Theme.brand)
                        .padding(.top, 2)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded {
                DecisionThreadView(item: item) { Task { await onDone() } }
            }
        }
        .onAppear { if startExpanded { expanded = true } }
    }
}

/// Phase D — the staged items folded into one editable reply. Sending it opens
/// Messages prefilled and clears every item it covered.
private struct BatchReplySheet: View {
    let personId: String
    let person: String
    let items: [Item]
    let onCleared: (Set<String>) -> Void

    @Environment(\.syncService) private var syncService
    @Environment(\.openURL) private var openURL
    @Environment(\.dismiss) private var dismiss

    @State private var reply = ""
    @State private var handle: String?
    @State private var loading = true
    @State private var sending = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    header
                    if loading {
                        ProgressView("Folding \(items.count) into one reply…")
                            .frame(maxWidth: .infinity).padding(.vertical, 40)
                    } else {
                        replyEditor
                        folded
                        if let error {
                            Text(error).font(.system(size: 12.5)).foregroundStyle(Theme.ember)
                        }
                    }
                }
                .padding(22)
            }
            .background(Theme.paper.ignoresSafeArea())
            .navigationTitle("Reply to \(person)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }.foregroundStyle(Theme.inkSoft)
                }
            }
            .safeAreaInset(edge: .bottom) { if !loading { sendBar } }
        }
        .task { await load() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("One message").font(.system(size: 10, weight: .semibold)).tracking(1.3)
                .foregroundStyle(Theme.inkFaint)
            Spacer()
            Text("clears \(items.count)")
                .font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.brand)
        }
    }

    private var replyEditor: some View {
        TextEditor(text: $reply)
            .font(.system(size: 15))
            .foregroundStyle(Theme.ink)
            .scrollContentBackground(.hidden)
            .frame(minHeight: 120)
            .padding(12)
            .background(Theme.chip, in: RoundedRectangle(cornerRadius: 14))
    }

    private var folded: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text("Answers folded in").font(.system(size: 10, weight: .semibold)).tracking(1.2)
                .foregroundStyle(Theme.inkFaint)
            ForEach(items) { item in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "checkmark").font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Theme.good).padding(.top, 2)
                    Text(item.suggestedAction.isEmpty ? item.rawText : item.suggestedAction)
                        .font(.system(size: 13.5)).foregroundStyle(Theme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private var sendBar: some View {
        Button { send() } label: {
            HStack(spacing: 8) {
                if sending { ProgressView().tint(.white) }
                else { Image(systemName: "paperplane.fill") }
                Text("Send one reply").fontWeight(.semibold)
            }
            .font(.system(size: 15)).foregroundStyle(.white)
            .frame(maxWidth: .infinity).padding(.vertical, 15)
            .background(sendURL == nil ? Theme.inkGhost : Theme.brand, in: RoundedRectangle(cornerRadius: 14))
        }
        .buttonStyle(.plain)
        .disabled(sendURL == nil || sending)
        .padding(.horizontal, 22).padding(.bottom, 6)
        .background(.ultraThinMaterial)
    }

    private var sendURL: URL? {
        guard let handle, !reply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return nil }
        var components = URLComponents()
        components.scheme = "sms"
        components.path = handle
        components.queryItems = [URLQueryItem(name: "body", value: reply)]
        return components.url
    }

    private func load() async {
        if ConversationsViewModel.usesSample {
            reply = items.compactMap { $0.suggestedReply }.joined(separator: " ")
            handle = items.first?.handle ?? "+15555550123"
            loading = false
            return
        }
        do {
            let draft = try await syncService.api.draftBatch(personId: personId, itemIds: items.map(\.id))
            reply = draft.reply
            handle = draft.handle
        } catch { self.error = error.localizedDescription }
        loading = false
    }

    private func send() {
        guard let url = sendURL else { return }
        openURL(url)
        let ids = Set(items.map(\.id))
        sending = true
        Task {
            if !ConversationsViewModel.usesSample {
                _ = try? await syncService.api.batchDone(itemIds: Array(ids))
            }
            onCleared(ids)
            dismiss()
        }
    }
}
