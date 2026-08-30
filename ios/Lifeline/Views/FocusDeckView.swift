import Observation
import SwiftUI

/// One thing at a time. Cards come in priority order; how you swipe decides each
/// one's fate — right to finish, left to keep, up to snooze onto the stack. The
/// stack is the honest part: it grows as you defer, so you *see* your life's
/// thread count piling up. A life debugger.
@MainActor
@Observable
final class FocusDeckViewModel {
    private(set) var deck: [Item]?
    private(set) var stack: [Item] = []   // swiped left (snoozed) — the growing pile you can peek into
    private(set) var cleared = 0          // finished this session

    private let sync: SyncService
    init(sync: SyncService) { self.sync = sync }

    func load() async {
        do {
            // One fetch, split locally: still-sleeping snoozes are the stack
            // (so it survives relaunch); everything else is the deck.
            let all = try await sync.api.queue(includeSnoozed: true)
            let asleep = all.filter { $0.isStillSnoozed }
            stack = asleep
            deck = all.filter { !$0.isStillSnoozed }
        } catch {
            if deck == nil { deck = [] }
        }
    }

    var top: Item? { deck?.first }

    func done(_ item: Item) {
        drop(item); cleared += 1
        Task { _ = try? await sync.api.markDone(itemId: item.id) }
    }
    func keep(_ item: Item) {
        guard var d = deck, let i = d.firstIndex(where: { $0.id == item.id }) else { return }
        d.append(d.remove(at: i)); deck = d          // to the back — still open
    }
    func snooze(_ item: Item) {
        drop(item); stack.append(item)
        Task { _ = try? await sync.api.snooze(itemId: item.id, hours: 8) }
    }
    /// Pull a stacked thread back to the top of the deck to deal with now.
    func pullBack(_ item: Item) {
        stack.removeAll { $0.id == item.id }
        if var d = deck { d.insert(item, at: 0); deck = d } else { deck = [item] }
        // Wake it server-side too, so it doesn't re-stack on next launch.
        Task { _ = try? await sync.api.snooze(itemId: item.id, hours: 0) }
    }
    func dismiss(_ item: Item) {
        drop(item)
        Task { _ = try? await sync.api.dismiss(itemId: item.id) }
    }
    private func drop(_ item: Item) { deck?.removeAll { $0.id == item.id } }
}

struct FocusDeckView: View {
    @Environment(\.syncService) private var syncService
    @State private var model: FocusDeckViewModel?
    @State private var drag: CGSize = .zero
    @State private var axis: DragAxis?          // latched direction for the active swipe
    @State private var flinging = false
    @State private var showStack = false
    @State private var showModel = false
    @Namespace private var dockMorph
    // Haptics: a tick when a swipe arms past its threshold, a distinct feel per fate.
    @State private var armed = false
    @State private var armTick = 0
    @State private var actTick = 0
    @State private var actKind: SensoryFeedback = .selection

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                header
                deck
            }
            .background(Theme.paper.ignoresSafeArea())
            // The brain dock — bottom center, always in thumb reach. With cards
            // it's the orb; when the deck clears it stretches into the input bar
            // (triage is over, conversation begins).
            .overlay(alignment: .bottom) {
                Group {
                    if model?.deck?.isEmpty == true {
                        StudyBar { showModel = true }
                            .matchedGeometryEffect(id: "brainDock", in: dockMorph)
                            .padding(.horizontal, 40)
                    } else {
                        BrainDockButton { showModel = true }
                            .matchedGeometryEffect(id: "brainDock", in: dockMorph)
                    }
                }
                .padding(.bottom, 6)
                .animation(.spring(response: 0.45, dampingFraction: 0.8),
                           value: model?.deck?.isEmpty == true)
            }
            .sensoryFeedback(.selection, trigger: armTick)
            .sensoryFeedback(trigger: actTick) { _, _ in actKind }
            .task {
                if model == nil { model = FocusDeckViewModel(sync: syncService) }
                await model?.load()
                // The deck is the only view now — it owns pushing the calendar
                // to the backend (used to live on the retired Now screen; the
                // assistant's calendar answers were stale without it).
                await CalendarSync.shared.sync(via: syncService.api)
            }
            .sheet(isPresented: $showStack) {
                StackSheet(items: model?.stack ?? []) { item in
                    model?.pullBack(item)
                    if model?.stack.isEmpty ?? true { showStack = false }
                }
            }
            .sheet(isPresented: $showModel) {
                ConverseView()
            }
            .onChange(of: showModel) { _, open in
                // Coming back from a conversation: the loop may have closed or
                // surfaced things — refresh the deck.
                if !open { Task { await model?.load() } }
            }
        }
    }

    private var header: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 3) {
                Text("Focus").font(Theme.serif(24, .semibold)).foregroundStyle(Theme.ink)
                if let n = model?.deck?.count {
                    Text("\(n) in the deck").font(.system(size: 12)).foregroundStyle(Theme.inkFaint)
                }
            }
            Spacer()
            Button { if !(model?.stack.isEmpty ?? true) { showStack = true } } label: {
                StackIndicator(count: model?.stack.count ?? 0)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 22).padding(.top, 10).padding(.bottom, 4)
    }

    /// Earliest wake among the stacked (snoozed) items — shown in the study.
    private var nextWake: Date? {
        (model?.stack ?? [])
            .compactMap { $0.snoozedUntil.flatMap { ISO8601DateFormatter.lifeline.date(from: $0) } }
            .min()
    }

    @ViewBuilder
    private var deck: some View {
        if let items = model?.deck {
            if items.isEmpty {
                DeckCleared(cleared: model?.cleared ?? 0,
                            stacked: model?.stack.count ?? 0,
                            nextWake: nextWake)
            } else {
                GeometryReader { geo in
                    ZStack {
                        ForEach(Array(items.prefix(3).enumerated()).reversed(), id: \.element.id) { idx, item in
                            let isTop = idx == 0
                            FocusCard(item: item, isTop: isTop, drag: isTop ? drag : .zero)
                                .scaleEffect(1 - CGFloat(idx) * 0.04)
                                .offset(y: CGFloat(idx) * 12)
                                .offset(isTop ? drag : .zero)
                                .rotationEffect(.degrees(isTop ? Double(drag.width / 22) : 0))
                                .allowsHitTesting(isTop && !flinging)
                                .gesture(isTop ? swipe(item, geo.size) : nil)
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .padding(.horizontal, 22).padding(.top, 16).padding(.bottom, 76)
                }
            }
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func swipe(_ item: Item, _ size: CGSize) -> some Gesture {
        let hx = size.width * 0.26
        let vy: CGFloat = 150
        let deadzone: CGFloat = 10
        return DragGesture()
            .onChanged { value in
                let tx = value.translation
                // Latch onto the dominant axis the moment the drag commits, so a
                // mostly-vertical "snooze" swipe can't drift sideways into Done/Keep
                // (and vice-versa). Once vertical, horizontal motion is ignored.
                if axis == nil, max(abs(tx.width), abs(tx.height)) > deadzone {
                    axis = abs(tx.height) > abs(tx.width) ? .vertical : .horizontal
                }
                switch axis {
                case .vertical:   drag = CGSize(width: 0, height: tx.height)
                case .horizontal: drag = CGSize(width: tx.width, height: 0)
                case .none:       drag = tx
                }
                // Tick once the card crosses into an actionable zone.
                let past = drag.width > hx || drag.width < -hx || drag.height < -vy
                if past && !armed { armed = true; armTick += 1 }
                else if !past && armed { armed = false }
            }
            .onEnded { _ in
                armed = false
                let committed = axis
                axis = nil
                let t = drag   // axis-constrained, never the raw translation
                if committed == .horizontal && t.width > hx {
                    actKind = .success; actTick += 1
                    fling(CGSize(width: size.width * 1.5, height: 0)) { model?.done(item) }
                } else if committed == .horizontal && t.width < -hx {
                    // Left = onto the stack (snooze) — the pile you can peek into.
                    actKind = .impact(weight: .heavy); actTick += 1
                    fling(CGSize(width: -size.width * 1.5, height: 0)) { model?.snooze(item) }
                } else if committed == .vertical && t.height < -vy {
                    // Up = keep — cycle it to the back of the deck, still open.
                    actKind = .impact(flexibility: .soft); actTick += 1
                    fling(CGSize(width: 0, height: -size.height)) { model?.keep(item) }
                } else {
                    withAnimation(.spring(response: 0.35, dampingFraction: 0.7)) { drag = .zero }
                }
            }
    }

    /// Which way the active drag committed. Latched once past the deadzone so the
    /// card moves on one axis only — an up-swipe stays a snooze, never a Done.
    private enum DragAxis { case horizontal, vertical }

    private func fling(_ to: CGSize, _ action: @escaping () -> Void) {
        flinging = true
        withAnimation(.easeOut(duration: 0.28)) { drag = to }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.26) {
            action(); drag = .zero; flinging = false
        }
    }
}

// MARK: - Brain dock (§v1.4)

/// Bottom-center entry to the model of you. No idle motion — it answers touch:
/// a soft haptic the instant your finger lands, a squish while held, and a
/// springy pop on release as the tell screen comes up.
struct BrainDockButton: View {
    var action: () -> Void
    @State private var pressed = false

    var body: some View {
        Button(action: action) {
            Image(systemName: "brain")
                .font(.system(size: 22, weight: .medium))
                .foregroundStyle(Theme.brand)
                .frame(width: 58, height: 58)
                .background(Circle().fill(Theme.card))
                .overlay(Circle().stroke(Theme.rule))
                .shadow(color: .black.opacity(0.12), radius: 14, y: 6)
        }
        .buttonStyle(DockPressStyle(pressed: $pressed))
        // Touch-down feedback, not touch-up: it responds the moment you land.
        .sensoryFeedback(trigger: pressed) { _, isDown in
            isDown ? .impact(flexibility: .soft) : .impact(flexibility: .rigid, intensity: 0.6)
        }
    }
}

/// The orb, stretched into an invitation — shown when the deck is clear. Same
/// press feel as the orb; tapping opens the conversation with the field focused.
struct StudyBar: View {
    var action: () -> Void
    @State private var pressed = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 9) {
                Image(systemName: "brain")
                    .font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.brand)
                Text(ButlerLines.placeholder())
                    .font(.system(size: 13.5)).foregroundStyle(Theme.inkFaint)
                Spacer()
            }
            .padding(.horizontal, 16)
            .frame(height: 50)
            .frame(maxWidth: .infinity)
            .background(Capsule().fill(Theme.card))
            .overlay(Capsule().stroke(Theme.rule))
            .shadow(color: .black.opacity(0.12), radius: 14, y: 6)
        }
        .buttonStyle(DockPressStyle(pressed: $pressed))
        .sensoryFeedback(trigger: pressed) { _, isDown in
            isDown ? .impact(flexibility: .soft) : .impact(flexibility: .rigid, intensity: 0.6)
        }
    }
}

/// Squish to 0.86 while held; overshoot spring back on release makes the
/// "it felt that" moment unmistakable.
private struct DockPressStyle: ButtonStyle {
    @Binding var pressed: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.86 : 1)
            .animation(.spring(response: 0.3, dampingFraction: 0.5), value: configuration.isPressed)
            .onChange(of: configuration.isPressed) { _, isDown in pressed = isDown }
    }
}

// MARK: - Card

private struct FocusCard: View {
    let item: Item
    let isTop: Bool
    let drag: CGSize

    @Environment(\.openURL) private var openURL
    @Environment(\.syncService) private var syncService
    @State private var enriched: Enriched?
    @State private var dossier: Dossier?
    @State private var flipped = false

    private var headline: String {
        if let h = enriched?.headline, !h.isEmpty { return h }
        return item.suggestedAction.isEmpty ? item.rawText : item.suggestedAction
    }
    private var verdict: (String, Color)? {
        // Information cards are acknowledged, not "done" — the swipe reads that way.
        if drag.width > 40 { return (item.isInformation ? "Got it" : "Done", Theme.good) }
        if drag.width < -40 { return ("Stack", Theme.gold) }
        if drag.height < -60 { return ("Keep ↑", Theme.brand) }
        return nil
    }

    var body: some View {
        ZStack {
            face { front }
                .opacity(flipped ? 0 : 1)
                .rotation3DEffect(.degrees(flipped ? 180 : 0), axis: (x: 0, y: 1, z: 0))
                .overlay(alignment: .top) { if !flipped { verdictBadge } }
            face { DossierBack(item: item, dossier: dossier) }
                .opacity(flipped ? 1 : 0)
                .rotation3DEffect(.degrees(flipped ? 0 : -180), axis: (x: 0, y: 1, z: 0))
        }
        .animation(.spring(response: 0.5, dampingFraction: 0.82), value: flipped)
        .contentShape(Rectangle())
        .onTapGesture { toggleFlip() }
        .sensoryFeedback(.impact(flexibility: .soft), trigger: flipped)
        .task(id: isTop) {
            guard isTop, enriched == nil, !FocusDeckIsSample else { return }
            enriched = try? await syncService.api.itemEnriched(itemId: item.id)
        }
    }

    private func face<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        content()
            .padding(20)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 24))
            .overlay(RoundedRectangle(cornerRadius: 24).stroke(Theme.rule))
            .shadow(color: .black.opacity(0.06), radius: 20, y: 10)
    }

    private func toggleFlip() {
        flipped.toggle()
        if flipped, dossier == nil, !FocusDeckIsSample {
            Task { dossier = try? await syncService.api.itemDossier(itemId: item.id) }
        }
    }

    private var front: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 9) {
                Avatar(name: item.person, personId: item.personId, size: 30)
                Text(item.person).font(.system(size: 13.5, weight: .medium)).foregroundStyle(Theme.inkSoft)
                if let due = DueDateFormatter.label(for: item) {
                    Text("· \(due)").font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(item.interruptionLevel == .timeSensitive ? Theme.ember : Theme.inkFaint)
                }
                Spacer()
                if item.isInformation {
                    // §v1.4 — the system noticed this; it isn't asking for work.
                    HStack(spacing: 4) {
                        Image(systemName: "sparkle").font(.system(size: 9))
                        Text("Noticed").font(.system(size: 10.5, weight: .semibold)).tracking(0.6)
                    }
                    .foregroundStyle(Theme.gold)
                    .padding(.horizontal, 9).padding(.vertical, 4)
                    .background(Capsule().fill(Theme.gold.opacity(0.13)))
                }
                Image(systemName: "arrow.trianglehead.2.clockwise.rotate.90")
                    .font(.system(size: 12)).foregroundStyle(Theme.inkGhost)
            }

            Text(headline)
                .font(Theme.serif(25, .semibold)).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true).padding(.top, 14)

            if let briefing = enriched?.briefing, !briefing.isEmpty {
                Text(briefing).font(.system(size: 14)).foregroundStyle(Theme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true).padding(.top, 10)
            } else {
                Text("“\(item.rawText.tightened)”").font(Theme.serif(15)).foregroundStyle(Theme.inkSoft)
                    .lineLimit(4).fixedSize(horizontal: false, vertical: true).padding(.top, 10)
            }

            if let reply = item.suggestedReply, !reply.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Drafted reply").font(.system(size: 9.5, weight: .semibold)).tracking(1.1)
                        .textCase(.uppercase).foregroundStyle(Theme.inkFaint)
                    Text(reply).font(.system(size: 14)).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.chip, in: RoundedRectangle(cornerRadius: 12)).padding(.top, 16)
            }

            Spacer(minLength: 12)
            actions
            Text(item.isInformation
                 ? "tap for the why  ·  → got it · ← stack · ↑ keep"
                 : "tap for the why  ·  → done · ← stack · ↑ keep")
                .font(.system(size: 11)).foregroundStyle(Theme.inkGhost)
                .frame(maxWidth: .infinity).padding(.top, 12)
        }
    }

    @ViewBuilder private var actions: some View {
        let send = item.sendMessagesURL, open = item.openLinkURL, call = item.callURL
        if send != nil || open != nil || call != nil {
            HStack(spacing: 8) {
                if let send { pill("Send", "paperplane.fill", primary: true) { openURL(send) } }
                if let open { pill(item.isVideoLink ? "Watch" : "Open", item.isVideoLink ? "play.fill" : "safari", primary: send == nil) { openURL(open) } }
                if let call, send == nil { pill("Call", "phone.fill", primary: false) { openURL(call) } }
            }
        }
    }

    private func pill(_ t: String, _ icon: String, primary: Bool, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            SwiftUI.Label(t, systemImage: icon).font(.system(size: 13, weight: .semibold))
                .lineLimit(1).fixedSize()
                .padding(.horizontal, 14).padding(.vertical, 9)
                .background(primary ? Theme.brand : Theme.card, in: RoundedRectangle(cornerRadius: 10))
                .foregroundStyle(primary ? .white : Theme.ink)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(primary ? .clear : Theme.rule))
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder private var verdictBadge: some View {
        if let (label, color) = verdict {
            Text(label).font(.system(size: 15, weight: .heavy))
                .foregroundStyle(color)
                .padding(.horizontal, 14).padding(.vertical, 7)
                .background(color.opacity(0.14), in: Capsule())
                .overlay(Capsule().stroke(color, lineWidth: 1.5))
                .padding(.top, 16)
                .opacity(min(1, Double(max(abs(drag.width), abs(drag.height)) / 90)))
        }
    }
}

/// Sample-mode guard (deck doesn't hit a backend for enrichment under -uiSampleData).
private var FocusDeckIsSample: Bool { ProcessInfo.processInfo.arguments.contains("-uiSampleData") }

// MARK: - Stack indicator & empty state

private struct StackIndicator: View {
    let count: Int
    private func barColor(_ i: Int) -> Color {
        count == 0 ? Theme.inkGhost.opacity(0.3) : Theme.gold.opacity(0.5 + Double(i) * 0.1)
    }
    var body: some View {
        HStack(spacing: 8) {
            ZStack(alignment: .bottom) {
                ForEach(0..<max(1, min(count, 5)), id: \.self) { i in
                    RoundedRectangle(cornerRadius: 3)
                        .fill(barColor(i))
                        .frame(width: 26 - CGFloat(i) * 2, height: 6)
                        .offset(y: -CGFloat(i) * 4)
                }
            }
            .frame(width: 30, height: 30, alignment: .bottom)
            VStack(alignment: .leading, spacing: 0) {
                Text("\(count)").font(.system(size: 15, weight: .bold)).monospacedDigit()
                    .foregroundStyle(count == 0 ? Theme.inkFaint : Theme.gold)
                Text("stacked").font(.system(size: 10)).foregroundStyle(Theme.inkFaint)
            }
        }
        .animation(.spring(response: 0.4, dampingFraction: 0.6), value: count)
    }
}

/// §v1.5 — the cleared deck becomes the study: triage is over, conversation
/// begins. The butler takes the room; the stats stay quiet and honest.
private struct DeckCleared: View {
    let cleared: Int
    let stacked: Int
    let nextWake: Date?

    var body: some View {
        VStack(spacing: 0) {
            Image(systemName: "brain")
                .font(.system(size: 26, weight: .medium)).foregroundStyle(Theme.brand)
                .frame(width: 68, height: 68)
                .background(Circle().fill(Theme.card))
                .overlay(Circle().stroke(Theme.rule))
                .shadow(color: .black.opacity(0.08), radius: 12, y: 5)
            Text(ButlerLines.cleared())
                .font(Theme.serif(22, .semibold)).foregroundStyle(Theme.ink)
                .multilineTextAlignment(.center)
                .padding(.top, 16)
            Text("Might I suggest enjoying it while it lasts.")
                .font(.system(size: 13)).foregroundStyle(Theme.inkSoft)
                .padding(.top, 6)
            HStack(spacing: 14) {
                stat("\(cleared)", "handled today", Theme.ink)
                if stacked > 0 { stat("\(stacked)", "on the stack", Theme.gold) }
                if let wake = nextWake {
                    stat(wake.formatted(date: .omitted, time: .shortened), "next wakes", Theme.ink)
                }
            }
            .padding(.top, 16)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity).padding(40)
    }

    private func stat(_ value: String, _ label: String, _ color: Color) -> some View {
        HStack(spacing: 4) {
            Text(value).font(.system(size: 12, weight: .semibold)).foregroundStyle(color)
            Text(label).font(.system(size: 11)).foregroundStyle(Theme.inkFaint)
        }
    }
}

/// Peek into the stack — the threads you've pushed up for later. Pull any back
/// to the top of the deck. This is what makes the single view complete: you can
/// defer *and* retrieve, so nothing is lost, just stacked.
private struct StackSheet: View {
    let items: [Item]
    let onPull: (Item) -> Void

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    Text("\(items.count) stacked for later")
                        .font(.system(size: 12.5)).foregroundStyle(Theme.inkFaint)
                        .padding(.horizontal, 22).padding(.top, 4).padding(.bottom, 10)
                    ForEach(items) { item in
                        HStack(spacing: 12) {
                            Avatar(name: item.person, personId: item.personId, size: 34)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(item.suggestedAction.isEmpty ? item.rawText : item.suggestedAction)
                                    .font(.system(size: 14.5, weight: .medium)).foregroundStyle(Theme.ink).lineLimit(1)
                                Text(item.person).font(.system(size: 12)).foregroundStyle(Theme.inkFaint)
                            }
                            Spacer(minLength: 8)
                            Button { onPull(item) } label: {
                                Text("Pull back").font(.system(size: 12.5, weight: .semibold))
                                    .padding(.horizontal, 12).padding(.vertical, 7)
                                    .background(Theme.brandSoft, in: Capsule()).foregroundStyle(Theme.brand)
                            }
                            .buttonStyle(.plain)
                        }
                        .padding(.horizontal, 22).padding(.vertical, 11)
                        Rectangle().fill(Theme.ruleSoft).frame(height: 1)
                    }
                }
                .padding(.top, 8)
            }
            .background(Theme.paper.ignoresSafeArea())
            .navigationTitle("The stack")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

/// The back of a card — the receipts. Why it surfaced, where you left off (your
/// own last words), whether you're the one still waiting, and the conversation itself.
private struct DossierBack: View {
    let item: Item
    let dossier: Dossier?

    var body: some View {
        if let d = dossier {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if d.awaitingReply {
                        HStack(spacing: 8) {
                            Image(systemName: "hourglass").font(.system(size: 12, weight: .semibold))
                            Text("You spoke last — still waiting to hear back")
                                .font(.system(size: 12.5, weight: .semibold))
                        }
                        .foregroundStyle(Theme.gold)
                        .padding(.horizontal, 11).padding(.vertical, 8)
                        .background(Theme.gold.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                    }

                    if !d.why.isEmpty {
                        section("Why you're seeing this") {
                            VStack(alignment: .leading, spacing: 7) {
                                ForEach(Array(d.why.enumerated()), id: \.offset) { _, reason in
                                    HStack(alignment: .top, spacing: 8) {
                                        Circle().fill(Theme.brand).frame(width: 4, height: 4).padding(.top, 6)
                                        Text(reason).font(.system(size: 13)).foregroundStyle(Theme.inkSoft)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                }
                            }
                        }
                    }

                    if let mine = d.yourLastWord {
                        section("Where you left off") {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(mine.text.tightened).font(.system(size: 13.5)).foregroundStyle(Theme.ink)
                                    .fixedSize(horizontal: false, vertical: true)
                                if let when = RelativeTime.label(from: mine.timestamp) {
                                    Text("you · \(when)").font(.system(size: 11)).foregroundStyle(Theme.inkFaint)
                                }
                            }
                            .padding(11).frame(maxWidth: .infinity, alignment: .leading)
                            .background(Theme.brandSoft, in: RoundedRectangle(cornerRadius: 10))
                        }
                    }

                    if !d.messages.isEmpty {
                        section("The conversation") {
                            VStack(alignment: .leading, spacing: 8) {
                                ForEach(d.messages) { m in
                                    Text(m.text.tightened).font(.system(size: 12.5))
                                        .lineLimit(8)
                                        .foregroundStyle(m.isPivot ? Theme.ink : Theme.inkSoft)
                                        .padding(.horizontal, 10).padding(.vertical, 7)
                                        .background(m.isFromUser ? Theme.chip : Theme.card, in: RoundedRectangle(cornerRadius: 11))
                                        .overlay(RoundedRectangle(cornerRadius: 11)
                                            .stroke(m.isPivot ? Theme.brand : Theme.rule, lineWidth: m.isPivot ? 1.2 : 1))
                                        .fixedSize(horizontal: false, vertical: true)
                                        .frame(maxWidth: .infinity, alignment: m.isFromUser ? .trailing : .leading)
                                }
                            }
                        }
                    }

                    Text("tap to flip back").font(.system(size: 11)).foregroundStyle(Theme.inkGhost)
                        .frame(maxWidth: .infinity).padding(.top, 2)
                }
            }
        } else {
            VStack(spacing: 8) {
                ProgressView()
                Text("gathering the receipts…").font(.system(size: 12)).foregroundStyle(Theme.inkFaint)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder private func section<C: View>(_ title: String, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.system(size: 10, weight: .semibold)).tracking(1.2)
                .textCase(.uppercase).foregroundStyle(Theme.inkFaint)
            content()
        }
    }
}
