import SwiftUI

/// Where a tapped notification lands.
///
/// The premise, from the field notes: *"users don't want to come into the app
/// … we are going to be responsible for them, so we have to hold their hand
/// back into the app, rather than expect them to come in of their own
/// volition."*
///
/// Three decisions follow from that, and each one is a departure from what the
/// app does when someone opens it themselves:
///
/// **1. Not the stack.** The stack answers "what am I carrying?" — the
/// question someone asks when *they* chose to open the app. A person arriving
/// from a notification was just told one specific thing; showing them fifteen
/// lanes makes them re-find it. The app would be handing work back.
///
/// **2. Not the thread detail either.** That screen is a work log: deadline,
/// findings, watchers, drafts, the autonomy ladder, evidence. Right for
/// browsing, wrong for someone who was interrupted mid-life. This shows the
/// one finding that earned the interruption, in full, with the move already
/// staged.
///
/// **3. Leaving is one tap and costs nothing.** "Hold their hand" cannot mean
/// trap them. The dismissal is as prominent as the action, and it is phrased
/// as a decision the user is allowed to make rather than a failure.
///
/// After acting, it offers the next thread that needs them — the same card,
/// one more loop. That is the hand-holding: momentum the app supplies, so
/// clearing two things costs one decision instead of two.
struct ArrivalView: View {
    let arrival: PushRouter.Arrival
    var onDone: () -> Void

    @Environment(\.syncService) private var syncService
    @State private var detail: ThreadDetail?
    @State private var draft: ThreadDraft?
    @State private var drafting = false
    @State private var acted = false
    @State private var next: LifeThread?
    @State private var loading = true

    /// The finding that pushed. Falls back to the newest one — a notification
    /// that outlived its finding should still land somewhere true.
    private var finding: Finding? {
        guard let detail else { return nil }
        if let id = arrival.findingId, let match = detail.findings.first(where: { $0.id == id }) {
            return match
        }
        return detail.findings.first
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if loading {
                        ProgressView().padding(.top, 100).frame(maxWidth: .infinity)
                    } else if let detail {
                        header(detail.thread)
                        if let finding { findingCard(finding) }
                        actions(detail.thread)
                        if acted { nextUp() }
                    } else {
                        Waiting(text: "That loose end isn't here any more.", icon: "questionmark.circle")
                            .padding(.top, 80)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 40)
            }
            .background(Theme.paper.ignoresSafeArea())
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    // Deliberately plain and always present. The way out is
                    // never hidden behind the way forward.
                    Button("Close") { onDone() }
                        .font(.system(size: 13))
                        .tint(Theme.inkSoft)
                }
            }
            .navigationBarTitleDisplayMode(.inline)
        }
        .task { await load() }
    }

    // MARK: - pieces

    private func header(_ thread: LifeThread) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("WHILE YOU WERE OUT")
                .font(.system(size: 10, weight: .semibold))
                .tracking(1.1)
                .foregroundStyle(Theme.inkFaint)
            Text(thread.title)
                .font(Theme.serif(25))
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, 8)
        .padding(.bottom, 16)
    }

    /// The headline was the notification. This is the rest of what it knows —
    /// the reason the interruption was worth it.
    @ViewBuilder
    private func findingCard(_ finding: Finding) -> some View {
        // Someone arriving from a notification is the *most* likely to want
        // the move rather than the write-up — they were interrupted, so the
        // staged work is the whole reason it was worth interrupting them.
        if finding.isMove {
            MoveCard(finding: finding, onAct: nil)
        } else {
            plainFindingCard(finding)
        }
    }

    private func plainFindingCard(_ finding: Finding) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(finding.headline)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            if !finding.body.isEmpty {
                Text(finding.body)
                    .font(.system(size: 13.5))
                    .foregroundStyle(Theme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.rule, lineWidth: 1))
    }

    @ViewBuilder
    private func actions(_ thread: LifeThread) -> some View {
        VStack(spacing: 9) {
            if let message = draft?.draft {
                DraftCard(message: message) { send(message) }
            } else if drafting {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Writing it…").font(.system(size: 12)).foregroundStyle(Theme.inkFaint)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } else if !acted {
                if thread.ceiling != .silent {
                    Button { write(thread) } label: {
                        Label("Write the reply", systemImage: "square.and.pencil")
                            .font(.system(size: 14, weight: .semibold))
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Theme.brand)
                    .controlSize(.large)
                }
                Button {
                    resolve(thread)
                } label: {
                    Label("This is handled", systemImage: "checkmark")
                        .font(.system(size: 14, weight: .semibold))
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .tint(Theme.good)
                .controlSize(.large)
            }

            // Never a dead end and never a trap: the full work log is one tap
            // away for anyone who wants it, and leaving is right there.
            NavigationLink {
                ThreadDetailView(threadId: arrival.threadId)
            } label: {
                Text("See the whole loose end")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Theme.brand)
            }
            .padding(.top, 2)

            if !acted {
                Button("Not now") { onDone() }
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.inkFaint)
                    .padding(.top, 2)
            }
        }
        .padding(.top, 18)
    }

    /// The hand-hold. One decision can clear two loops, and declining is the
    /// same single tap as accepting.
    @ViewBuilder
    private func nextUp() -> some View {
        if let next {
            VStack(alignment: .leading, spacing: 9) {
                Text("ONE MORE?")
                    .font(.system(size: 10, weight: .semibold))
                    .tracking(1.1)
                    .foregroundStyle(Theme.inkFaint)
                NavigationLink {
                    ThreadDetailView(threadId: next.id)
                } label: {
                    ThreadLane(thread: next)
                }
                .buttonStyle(.plain)
                Button("I'm done for now") { onDone() }
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Theme.brand)
            }
            .padding(.top, 26)
        } else {
            VStack(spacing: 6) {
                Text("That's everything.")
                    .font(Theme.serif(19))
                    .foregroundStyle(Theme.ink)
                Text("Nothing else needs you right now.")
                    .font(.system(size: 12.5))
                    .foregroundStyle(Theme.inkFaint)
                Button("Close") { onDone() }
                    .font(.system(size: 13, weight: .semibold))
                    .tint(Theme.brand)
                    .padding(.top, 6)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 30)
        }
    }

    // MARK: - work

    private func load() async {
        detail = try? await syncService.api.thread(id: arrival.threadId)
        loading = false
    }

    private func write(_ thread: LifeThread) {
        drafting = true
        Task {
            draft = try? await syncService.api.draftForThread(id: thread.id)
            drafting = false
        }
    }

    private func send(_ message: MessageDraft) {
        // One builder for every send surface — MessageDraft.sendURL.
        guard let url = message.sendURL else { return }
        UIApplication.shared.open(url)
        markActed()
    }

    private func resolve(_ thread: LifeThread) {
        Task {
            _ = try? await syncService.api.resolveThread(id: thread.id)
            markActed()
        }
    }

    private func markActed() {
        Task {
            let stack = try? await syncService.api.threadStack()
            // The next loop that actually wants attention — not merely the
            // next row. Offering something idle would make the chain feel like
            // a chore list.
            next = stack?.threads.first {
                $0.id != arrival.threadId && !$0.isResolved && ($0.lane == .hot || $0.unseen > 0)
            }
            withAnimation { acted = true }
        }
    }
}
