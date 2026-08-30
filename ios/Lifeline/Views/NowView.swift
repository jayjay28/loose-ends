import Observation
import SwiftUI

/// The proactive front door: the one thing to do now, then the people waiting on
/// you (ranked by how close you are). Consumes `/briefing` — the server decides
/// what rises; this only renders it.
@MainActor
@Observable
final class NowViewModel {
    private(set) var briefing: Briefing?
    private(set) var error: String?

    private let sync: SyncService
    private var askedForPermission = false
    init(sync: SyncService) { self.sync = sync }

    func load() async {
        if NowViewModel.usesSample { briefing = SampleData.briefing; return }
        do { briefing = try await sync.api.briefing(); error = nil }
        catch let err { if briefing == nil { error = err.localizedDescription } }

        // First run: ask for notifications, then push the device calendar so the
        // backend has schedule context. Both are one-time; both degrade quietly.
        if !askedForPermission {
            askedForPermission = true
            await NotificationScheduler.shared.requestPermission()
            await CalendarSync.shared.sync(via: sync.api)
        }
        if let briefing { await NotificationScheduler.shared.refresh(from: briefing) }
    }

    func markDone(_ item: Item) async {
        guard !NowViewModel.usesSample else { await load(); return }
        _ = try? await sync.api.markDone(itemId: item.id)
        await load()
    }

    static var usesSample: Bool { ProcessInfo.processInfo.arguments.contains("-uiSampleData") }
}

struct NowView: View {
    @Environment(\.syncService) private var syncService
    @State private var model: NowViewModel?
    @AppStorage("announceNudgeDismissed") private var announceDismissed = false

    var body: some View {
        NavigationStack {
            content
                .background(Theme.paper.ignoresSafeArea())
                .task {
                    if model == nil { model = NowViewModel(sync: syncService) }
                    await model?.load()
                }
                .refreshable { await model?.load() }
        }
    }

    @ViewBuilder
    private var content: some View {
        if let briefing = model?.briefing {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Header(mode: briefing.mode)
                    if briefing.caughtUp {
                        QuietState(title: "You're clear",
                                   subhead: "Nothing needs you right now. Nice.")
                            .padding(.top, 40)
                    } else {
                        if let one = briefing.oneNow {
                            OneNowCard(item: one) { await model?.markDone(one) }
                        }
                        if !briefing.waiting.isEmpty {
                            WaitingSection(people: briefing.waiting)
                        }
                    }
                    if !announceDismissed {
                        AnnounceNudge { announceDismissed = true }
                    }
                }
                .padding(.horizontal, 22).padding(.top, 8).padding(.bottom, 40)
            }
        } else if let error = model?.error {
            QuietState(title: "Couldn't load", subhead: error)
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

private struct Header: View {
    let mode: String
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(Date.now.formatted(.dateTime.weekday(.wide).month(.wide).day()).uppercased())
                .font(.system(size: 12, weight: .semibold)).tracking(1.4)
                .foregroundStyle(Theme.inkFaint)
            Text(greeting)
                .font(Theme.serif(32, .semibold))
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
    private var greeting: String {
        switch mode {
        case "morning": return "Good morning"
        case "evening": return "Winding down"
        default: return "Where things stand"
        }
    }
}

/// The single highest-value decision, given room to breathe.
private struct OneNowCard: View {
    let item: Item
    let onDone: () async -> Void

    @Environment(\.syncService) private var syncService
    @State private var enriched: Enriched?

    private var headline: String {
        if let h = enriched?.headline, !h.isEmpty { return h }
        return item.suggestedAction.isEmpty ? item.rawText : item.suggestedAction
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Do now")
                .font(.system(size: 11, weight: .semibold)).tracking(1.6)
                .textCase(.uppercase)
                .foregroundStyle(item.interruptionLevel == .timeSensitive ? Theme.ember : Theme.brand)

            HStack(spacing: 9) {
                Avatar(name: item.person, personId: item.personId, size: 26)
                Text(item.person).font(.system(size: 13.5, weight: .medium)).foregroundStyle(Theme.inkSoft)
                if let due = DueDateFormatter.label(for: item) {
                    Text("· \(due)")
                        .font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(item.interruptionLevel == .timeSensitive ? Theme.ember : Theme.inkFaint)
                }
            }
            .padding(.top, 12)

            Text(headline)
                .font(Theme.serif(24, .semibold))
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 8)
                .animation(.easeOut(duration: 0.2), value: headline)

            if let briefing = enriched?.briefing, !briefing.isEmpty {
                Text(briefing)
                    .font(.system(size: 13.5)).foregroundStyle(Theme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 7)
            }

            if let reply = item.suggestedReply, !reply.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Drafted reply")
                        .font(.system(size: 9.5, weight: .semibold)).tracking(1.1)
                        .textCase(.uppercase).foregroundStyle(Theme.inkFaint)
                    Text(reply)
                        .font(.system(size: 14)).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(11)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.chip, in: RoundedRectangle(cornerRadius: 12))
                .padding(.top, 14)
            }

            ItemActionBar(item: item, onDone: { Task { await onDone() } })
                .padding(.top, 16)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).stroke(Theme.rule))
        .shadow(color: .black.opacity(0.05), radius: 18, y: 8)
        .task(id: item.id) {
            guard !NowViewModel.usesSample else { return }
            enriched = try? await syncService.api.itemEnriched(itemId: item.id)
        }
    }
}

private struct WaitingSection: View {
    let people: [WaitingPerson]
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Waiting on you")
                .font(.system(size: 11, weight: .semibold)).tracking(1.4)
                .textCase(.uppercase).foregroundStyle(Theme.inkFaint)
                .padding(.bottom, 6)
            ForEach(people) { person in
                NavigationLink {
                    ConversationDetailView(conversation: person.asConversation)
                } label: {
                    WaitingRow(person: person)
                }
                .buttonStyle(.plain)
                Rectangle().fill(Theme.ruleSoft).frame(height: 1)
            }
        }
    }
}

private struct WaitingRow: View {
    let person: WaitingPerson
    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            Avatar(name: person.person, personId: person.personId,
                   level: person.topItem.interruptionLevel, size: 42)
            VStack(alignment: .leading, spacing: 3) {
                Text(person.person)
                    .font(.system(size: 15.5, weight: .medium)).foregroundStyle(Theme.ink)
                    .lineLimit(1)
                Text(subtitle)
                    .font(.system(size: 12.5)).foregroundStyle(Theme.inkFaint).lineLimit(1)
            }
            Spacer(minLength: 8)
            if let waited = RelativeTime.label(from: person.waitedSince) {
                Text(waited)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Theme.inkFaint)
            }
            if person.openCount > 1 {
                Text("\(person.openCount)")
                    .font(.system(size: 12, weight: .semibold)).monospacedDigit()
                    .foregroundStyle(Theme.inkSoft)
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Theme.chip, in: Capsule())
            }
            Image(systemName: "chevron.right")
                .font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.inkGhost)
        }
        .padding(.vertical, 12)
        .contentShape(Rectangle())
    }
    private var subtitle: String {
        let what = person.topItem.suggestedAction.isEmpty
            ? person.topItem.rawText : person.topItem.suggestedAction
        return person.tieStrength >= 0.4 ? "\(person.tieLabel) · \(what)" : what
    }
}

/// A one-time opt-in: turning on Announce Notifications (per-app) lets Siri read
/// your briefing aloud on AirPods / CarPlay. We can't flip the switch — it's a
/// system setting — so we point the way and get out of the way.
private struct AnnounceNudge: View {
    let onDismiss: () -> Void
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "speaker.wave.2.circle.fill")
                .font(.system(size: 26)).foregroundStyle(Theme.brand)
            VStack(alignment: .leading, spacing: 4) {
                Text("Hear your briefing")
                    .font(.system(size: 14.5, weight: .semibold)).foregroundStyle(Theme.ink)
                Text("Turn on Announce Notifications for Lifeline and Siri will read it aloud on AirPods or in the car.")
                    .font(.system(size: 12.5)).foregroundStyle(Theme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 14) {
                    Button("Open Settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) {
                            UIApplication.shared.open(url)
                        }
                    }
                    .font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.brand)
                    Button("Not now", action: onDismiss)
                        .font(.system(size: 13)).foregroundStyle(Theme.inkFaint)
                }
                .padding(.top, 4)
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .background(Theme.brandSoft, in: RoundedRectangle(cornerRadius: 14))
        .padding(.top, 8)
    }
}
