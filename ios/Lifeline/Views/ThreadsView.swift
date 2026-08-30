import Observation
import SwiftUI

/// §v2 — **the threads are the app.** Every open loop in your head is a lane;
/// the system works them while you're living your life and marks the lane each
/// time it finds something. The stack is honest: you see what your mind is
/// carrying, and you see it shrink.
///
/// This replaces the deck as the main view. The deck's swipe machinery carries
/// over — axis-latched drag, an arming tick, a distinct feel per verb — but the
/// object being swiped changes from a card to a lane.
/// What the navigation path carries: an identity, and nothing that can change
/// underneath it. A pushed screen has to keep pointing at the same thread no
/// matter how much the stack churns while it's open.
struct ThreadRoute: Hashable, Identifiable {
    let id: String
}

@MainActor
@Observable
final class ThreadsViewModel {
    private(set) var stack: ThreadStack?
    private(set) var closures: [ThreadClosure] = []
    private(set) var failed = false

    private let sync: SyncService
    init(sync: SyncService) { self.sync = sync }

    var threads: [LifeThread] { stack?.threads ?? [] }
    var running: Int { stack?.running ?? 0 }
    var needsYou: Int { stack?.needsYou ?? 0 }

    func load() async {
        do {
            stack = try await sync.api.threadStack()
            failed = false
        } catch {
            failed = stack == nil
        }
        closures = (try? await sync.api.threadClosures()) ?? []
    }

    /// Answering the question resolves it either way — the thread closes, or
    /// it stays and the correction is kept. A rejection is the most
    /// informative row the system has.
    func answer(_ closure: ThreadClosure, closed: Bool) {
        closures.removeAll { $0.id == closure.id }
        Task {
            if closed {
                _ = try? await sync.api.confirmClosure(id: closure.id)
            } else {
                _ = try? await sync.api.rejectClosure(id: closure.id)
            }
            await load()
        }
    }

    // MARK: the swipe verbs
    //
    // Each one updates the lane locally first so the gesture feels instant,
    // then tells the server. A failed call reloads rather than silently
    // diverging — the stack is the product's one source of truth about what
    // the user is carrying, and it must not lie.

    func resolve(_ thread: LifeThread) {
        mutate(thread.id) { $0.state = .resolved; $0.lane = .done }
        Task { await settle { try await self.sync.api.resolveThread(id: thread.id) } }
    }

    /// Keeps working, stops surfacing. "Right thread, wrong moment."
    func quiet(_ thread: LifeThread) {
        mutate(thread.id) { $0.state = .quiet; $0.lane = .idle }
        Task { await settle { try await self.sync.api.quietThread(id: thread.id) } }
    }

    /// "This matters more than you judged." Raises importance rather than
    /// changing state — the thread was already live, it just wasn't ranked
    /// high enough.
    func digIn(_ thread: LifeThread) {
        let raised = min(1.0, thread.importance + 0.25)
        mutate(thread.id) { $0.importance = raised; if $0.state == .quiet { $0.state = .live } }
        Task { await settle { try await self.sync.api.digInThread(id: thread.id) } }
    }

    /// Takes an id rather than a thread, because the caller is a navigation
    /// destination and the thread it was pushed with is a stale snapshot by
    /// the time this runs — this method is what makes it stale.
    func markSeen(id: String) {
        guard threads.first(where: { $0.id == id })?.unseen ?? 0 > 0 else { return }
        mutate(id) { $0.unseen = 0 }
        Task { await settle { try await self.sync.api.markThreadSeen(id: id) } }
    }

    /// Declaring puts the thread on the stack *now*, at the tier the server
    /// says it belongs to, and returns it so the view can fly the words into
    /// it. It used to `load()` the whole stack instead, which meant a network
    /// round trip and a wholesale re-render stood between typing something and
    /// seeing it — the gap the arrival animation exists to close.
    func declare(title: String, summary: String, deadline: Date?,
                 contactPersonId: String? = nil) async -> LifeThread? {
        guard let created = try? await sync.api.createThread(
            title: title, summary: summary, deadline: deadline,
            contactPersonId: contactPersonId
        ) else {
            await load()
            return nil
        }
        // Reconcile *before* returning, so the row the words fly to is already
        // the row the server will keep.
        //
        // The first cut appended the new thread locally and reloaded after the
        // animation. Two orderings then disagreed for two seconds: locally it
        // was last in the array, so it drew at the bottom of its band, while
        // `db.list_threads` sorts upcoming deadlines, then undated by
        // importance, then passed ones — and a fresh undated thread lands
        // above every thread whose date has gone. So it touched down at the
        // bottom and jumped up the moment the stack reloaded, which is the one
        // thing the arrival exists to stop happening.
        await load()
        return threads.first { $0.id == created.id } ?? created
    }

    private func mutate(_ id: String, _ change: (inout LifeThread) -> Void) {
        guard var s = stack, let i = s.threads.firstIndex(where: { $0.id == id }) else { return }
        change(&s.threads[i])
        stack = recount(s)
    }

    /// The header has to move with the stack or the count reads as a lie.
    private func recount(_ s: ThreadStack) -> ThreadStack {
        var s = s
        s.running = s.threads.filter { !$0.isResolved }.count
        s.needsYou = s.threads.filter { !$0.isResolved && ($0.lane == .hot || $0.unseen > 0) }.count
        return s
    }

    private func settle(_ call: @escaping () async throws -> Any) async {
        do { _ = try await call() } catch { await load() }
    }
}

struct ThreadsView: View {
    @Environment(\.syncService) private var syncService
    @State private var model: ThreadsViewModel?
    @State private var declaring = false
    @State private var showAsk = false
    /// §v3 — the engine answered 401 somewhere: this phone needs to pair.
    @State private var pairing = false
    @State private var showProposals = false
    @State private var proposalCount = 0
    @State private var openThread: ThreadRoute?
    @State private var showingClosed = false
    /// Explicit so a destination-less notification tap can pop it — the
    /// value-based `NavigationLink`s below land here.
    @State private var navPath = NavigationPath()
    @State private var router = PushRouter.shared

    // Haptics, carried over from the deck: a tick when a swipe arms past its
    // threshold, a distinct feel per verb when it commits.
    @State private var armTick = 0
    @State private var actTick = 0
    @State private var actKind: SensoryFeedback = .selection

    // §v2.2 — the arrival. `launchFrame` is where the composer's field was, in
    // screen coordinates: a sheet is its own presentation, so it can't be
    // measured from here and has to report itself on the way out.
    @State private var arrival: Arrival?
    @State private var pending: LifeThread?
    @State private var launchFrame: CGRect = .zero
    @State private var targetFrame: CGRect = .zero
    @State private var scrollTarget: String?

    var body: some View {
        NavigationStack(path: $navPath) {
            VStack(spacing: 0) {
                header
                askEntry
                stack
            }
            .background(Theme.paper.ignoresSafeArea())
            // Routed on the id, not the thread. `LifeThread`'s synthesised
            // `Hashable` covers every stored property, so a value in the
            // navigation path stops matching the moment anything about the
            // thread changes — and something always does: `markSeen` clears
            // the unseen count on the destination's own `onAppear`, the
            // worker adds findings, setting the autonomy ceiling rewrites the
            // row. The symptom was the detail screen silently re-binding to a
            // *different* thread while you were reading it.
            .navigationDestination(item: $openThread) { route in
                ThreadDetailView(threadId: route.id)
                    .onAppear { model?.markSeen(id: route.id) }
            }
            .navigationDestination(for: ThreadRoute.self) { route in
                ThreadDetailView(threadId: route.id)
                    .onAppear { model?.markSeen(id: route.id) }
            }
            .toolbar(.hidden, for: .navigationBar)
            .navigationDestination(isPresented: $showAsk) { AskView() }
            // A destination-less notification tap: pop everything so the
            // stack itself is what greets them. Pairing stays — a 401 gate
            // outranks a landing. See `PushRouter.landHome()`.
            .onChange(of: router.homeBeat) {
                navPath = NavigationPath()
                openThread = nil
                showAsk = false
                declaring = false
                showProposals = false
            }
            .sensoryFeedback(.selection, trigger: armTick)
            .sensoryFeedback(trigger: actTick) { _, _ in actKind }
            .task {
                if model == nil { model = ThreadsViewModel(sync: syncService) }
                await model?.load()
                await refreshProposalCount()
                // Asked for here rather than at launch: a push prompt before
                // the user has seen a single thread is the surest way to get a
                // permanent no, and iOS never shows it twice.
                await PushRegistrar.shared.start(api: syncService.api)
                // The main view owns pushing the device calendar to the backend
                // (it moved here from the deck, which moved it from Now).
                await CalendarSync.shared.sync(via: syncService.api)
                // §v3 ws4 — keep the engine's door list fresh so failover
                // still works after DHCP moves the Mac.
                await syncService.api.refreshEngineDoors()
            }
            // The flight starts on dismissal rather than on Add: the words have
            // to be visible to be followed, and until the sheet is out of the
            // way they are behind it.
            .sheet(isPresented: $declaring, onDismiss: beginArrival) {
                DeclareThreadSheet(onFieldFrame: { launchFrame = $0 }) { title, summary, deadline, contactId in
                    pending = await model?.declare(title: title, summary: summary,
                                                   deadline: deadline, contactPersonId: contactId)
                }
            }
            .onPreferenceChange(ArrivalTargetKey.self) { targetFrame = $0 }
            .overlay {
                if let arrival, arrival.flying, launchFrame != .zero, targetFrame != .zero {
                    ArrivalFlight(arrival: arrival, from: launchFrame, to: targetFrame)
                }
            }
            .sheet(isPresented: $showProposals) {
                ProposalsSheet {
                    await model?.load()
                    await refreshProposalCount()
                }
            }
            // §v3 — any 401 anywhere funnels here. The sheet claims a code,
            // the token lands in the Keychain, and the stack reloads as the
            // proof that the pairing took.
            .onReceive(NotificationCenter.default.publisher(for: .pairingRequired)
                .receive(on: DispatchQueue.main)) { _ in
                pairing = true
            }
            .sheet(isPresented: $pairing) {
                PairSheet {
                    pairing = false
                    Task { await model?.load() }
                }
                .presentationDetents([.medium])
                .presentationCornerRadius(22)
            }
        }
    }

    /// The stack itself. A plain header above the list rather than a navigation
    /// §v2.9 — the ask door. A field-shaped button, not a live field: typing
    /// happens on the Ask screen where the answer will land, so the keyboard
    /// never fights the stack for the screen.
    private var askEntry: some View {
        Button { showAsk = true } label: {
            HStack(spacing: 8) {
                Image(systemName: "sparkle.magnifyingglass")
                    .font(.system(size: 13)).foregroundStyle(Theme.inkFaint)
                Text("Ask about your life")
                    .font(.system(size: 14)).foregroundStyle(Theme.inkFaint)
                Spacer()
            }
            .padding(.horizontal, 13).padding(.vertical, 10)
            .background(Theme.chip.opacity(0.7), in: RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(.plain)
        .padding(.horizontal, Theme.margin)
        .padding(.bottom, 6)
    }

    /// bar: the title is two lines and a toolbar item won't give it the width —
    /// the first run rendered it as "T…" over "15 ru..".
    ///
    /// `List` + `.swipeActions` rather than the deck's hand-rolled drag. The
    /// deck could own its gestures outright because it was one card on a static
    /// screen; a lane lives inside a scroll view *and* a navigation link, and
    /// a custom `DragGesture` loses to both — the first build swiped and
    /// nothing happened. Swipe actions are also what the mockup draws: the verb
    /// revealed behind the row, committed by carrying the swipe through.
    private var stack: some View {
        ScrollViewReader { scroller in
            list.onChange(of: scrollTarget) { _, id in
                guard let id else { return }
                // Rule 2 of the arrival: never fly to somewhere off screen.
                scroller.scrollTo(id, anchor: .center)
                scrollTarget = nil
            }
        }
    }

    private var list: some View {
        List {
            ForEach(model?.closures ?? []) { closure in
                ClosureCard(closure: closure) { closed in
                    model?.answer(closure, closed: closed)
                }
                .plainRow()
            }

            // The lead. No band above it — it *is* the top of the page, and
            // labelling it would be explaining the joke.
            ForEach(grouped.lead) { thread in
                row(thread) {
                    LeadRow(thread: thread, arrival: arrivalFor(thread)) {
                        openThread = ThreadRoute(id: thread.id)
                    }
                }
            }

            ForEach(Tier.banded, id: \.self) { tier in
                let items = grouped.of(tier)
                if !items.isEmpty {
                    if let band = tier.band {
                        SectionRule(title: band).plainRow()
                    }
                    ForEach(Array(items.enumerated()), id: \.element.id) { index, thread in
                        let last = index == items.count - 1
                        row(thread) {
                            if tier == .brief {
                                BriefRow(thread: thread, showsRule: !last,
                                         arrival: arrivalFor(thread))
                            } else {
                                IndexRow(thread: thread, showsRule: !last,
                                         arrival: arrivalFor(thread))
                            }
                        }
                    }
                }
            }

            if !grouped.closed.isEmpty {
                ClosedFooter(count: grouped.closed.count, isOpen: showingClosed) {
                    withAnimation(.snappy(duration: 0.22)) { showingClosed.toggle() }
                }
                .plainRow()

                if showingClosed {
                    // Struck through by `IndexRow`, and still openable: the
                    // receipt for a thread that closed itself is the reason to
                    // trust the next one that does.
                    ForEach(Array(grouped.closed.enumerated()), id: \.element.id) { index, thread in
                        ZStack {
                            IndexRow(thread: thread,
                                     showsRule: index < grouped.closed.count - 1,
                                     arrival: nil)
                            NavigationLink(value: ThreadRoute(id: thread.id)) { EmptyView() }
                                .opacity(0)
                        }
                        .plainRow()
                    }
                }
            }

            if let m = model, m.threads.isEmpty, m.stack != nil {
                NothingRunning().plainRow()
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .animation(.spring(response: 0.4, dampingFraction: 0.85), value: model?.threads ?? [])
        .refreshable { await model?.load() }
    }

    /// Threads bucketed by the tier the server assigned.
    private var grouped: TieredThreads { TieredThreads(model?.threads ?? []) }

    /// Where it landed, counted down the page the way the reader counts it.
    private func placeOf(_ id: String) -> String {
        let page = grouped.pageOrder
        guard let i = page.firstIndex(where: { $0.id == id }) else { return "New" }
        return "New \u{00B7} \(ordinal(i + 1)) of \(page.count)"
    }

    private func ordinal(_ n: Int) -> String {
        switch (n % 100, n % 10) {
        case (11, _), (12, _), (13, _): return "\(n)th"
        case (_, 1): return "\(n)st"
        case (_, 2): return "\(n)nd"
        case (_, 3): return "\(n)rd"
        default: return "\(n)th"
        }
    }

    /// The arrival, if this is the row it belongs to.
    private func arrivalFor(_ thread: LifeThread) -> Arrival? {
        guard let arrival, arrival.id == thread.id else { return nil }
        return arrival
    }

    // MARK: - the arrival
    //
    // Four beats, in the order the eye needs them: the row makes room, the
    // page scrolls so the landing is witnessed, the words fly, and the row
    // says where it went and then stops saying it.

    private func beginArrival() {
        // `declare` has already reconciled the stack, so the thread is sitting
        // in its settled position before a single frame of this runs.
        guard let thread = pending else { return }
        pending = nil
        targetFrame = .zero
        arrival = Arrival(thread: thread, place: placeOf(thread.id))

        Task { @MainActor in
            // Let the row take its place and report its frame before anything
            // is aimed at it. Flying at a stale frame is how an animation ends
            // up landing in the wrong row.
            withAnimation(.spring(response: 0.34, dampingFraction: 0.9)) {
                scrollTarget = thread.id
            }
            try? await Task.sleep(for: .milliseconds(260))
            guard arrival?.id == thread.id else { return }

            withAnimation(.spring(response: 0.42, dampingFraction: 0.84)) {
                arrival?.landed = true
            }
            try? await Task.sleep(for: .milliseconds(420))
            guard arrival?.id == thread.id else { return }

            // The stand-in comes down and the row's own title comes up in the
            // same frame, so nothing blinks. The tick is the landing: light,
            // because it is an arrival and not a verb — the swipe verbs own
            // the heavier feels and this must not compete with them.
            commit(.impact(weight: .light)) { arrival?.flying = false }
            withAnimation(.easeOut(duration: 0.22)) { arrival?.marked = true }

            try? await Task.sleep(for: .milliseconds(1_800))
            guard arrival?.id == thread.id else { return }
            withAnimation(.easeInOut(duration: 0.3)) { arrival?.marked = false }
            try? await Task.sleep(for: .milliseconds(320))
            arrival = nil

            await refreshProposalCount()
        }
    }

    /// One row, with the swipe verbs and the invisible push link. Kept in one
    /// place so every tier gets identical behaviour and only the look differs.
    @ViewBuilder
    private func row<Content: View>(_ thread: LifeThread,
                                    @ViewBuilder _ content: () -> Content) -> some View {
        ZStack {
            content()
            NavigationLink(value: ThreadRoute(id: thread.id)) { EmptyView() }.opacity(0)
        }
        .plainRow()
        .swipeActions(edge: .leading, allowsFullSwipe: true) {
            Button {
                commit(.success) { model?.resolve(thread) }
            } label: {
                Label("Tie off", systemImage: "checkmark")
            }
            .tint(Theme.brand)
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            Button {
                commit(.impact(weight: .heavy)) { model?.quiet(thread) }
            } label: {
                Label("Quiet", systemImage: "moon.zzz")
            }
            .tint(Theme.inkSoft)

            Button {
                commit(.impact(flexibility: .soft)) { model?.digIn(thread) }
            } label: {
                Label("Sooner", systemImage: "chevron.up.circle")
            }
            .tint(Theme.brand)
        }
    }

    private var header: some View {
        HStack(alignment: .bottom) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Loose Ends").font(Theme.screenTitle).foregroundStyle(Theme.ink)
                Text(headline)
                    .font(Theme.meta)
                    .foregroundStyle(Theme.inkSoft)
            }
            Spacer()
            trailingButtons
        }
        .padding(.horizontal, Theme.margin)
        .padding(.top, 8)
        .padding(.bottom, 6)
    }

    private var headline: String {
        guard let m = model, m.stack != nil else { return " " }
        let running = "\(m.running) loose end\(m.running == 1 ? "" : "s")"
        guard m.needsYou > 0 else { return running }
        return "\(running) · \(m.needsYou) need\(m.needsYou == 1 ? "s" : "") you"
    }

    private var trailingButtons: some View {
        HStack(spacing: 12) {
            // Proposals live behind their own door — never mixed into the
            // stack, so the count above stays something the user consented to.
            if proposalCount > 0 {
                Button { showProposals = true } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "tray").font(.system(size: 12, weight: .semibold))
                        Text("\(proposalCount)").font(.system(size: 11, weight: .semibold))
                    }
                    .foregroundStyle(Theme.inkSoft)
                }
                .accessibilityLabel("\(proposalCount) proposed loose ends")
            }
            Button { declaring = true } label: {
                Image(systemName: "plus")
                    .font(.system(size: 15, weight: .light))
                    .foregroundStyle(.white)
                    .frame(width: 30, height: 30)
                    .background(Circle().fill(Theme.brand))
            }
            .accessibilityLabel("Add a loose end")
        }
    }

    private func commit(_ feel: SensoryFeedback, _ action: () -> Void) {
        actKind = feel
        actTick += 1
        action()
    }

    private func refreshProposalCount() async {
        proposalCount = (try? await syncService.api.proposals().count) ?? 0
    }
}

// MARK: - the lane

/// One open loop, as a row. The stripe, the due chip and the track are all
/// server-decided; this only draws them.
struct ThreadLane: View {
    let thread: LifeThread

    var body: some View {
        HStack(spacing: 0) {
            Rectangle().fill(stripe).frame(width: 3)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 7) {
                    Text(thread.title)
                        .font(Theme.serif(14, .semibold))
                        .foregroundStyle(Theme.ink)
                        .strikethrough(thread.isResolved, color: Theme.inkGhost)
                        .lineLimit(1)
                    Spacer(minLength: 0)
                    if thread.unseen > 0 {
                        Text("\(thread.unseen) NEW")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 5).padding(.vertical, 3)
                            .background(RoundedRectangle(cornerRadius: 4).fill(Theme.wave))
                    }
                    if let due = thread.dueLabel { DueChip(text: due, lane: thread.lane) }
                }
                if let subtitle = thread.subtitle {
                    Text(subtitle)
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.inkSoft)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                }
                ActivityTrack(marks: thread.activity, openedAt: thread.openedAt)
            }
            .padding(.leading, 10).padding(.trailing, 11)
            .padding(.top, 9).padding(.bottom, 8)
        }
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 11))
        .overlay(RoundedRectangle(cornerRadius: 11).strokeBorder(Theme.rule, lineWidth: 1))
        .opacity(thread.isResolved ? 0.55 : 1)
    }

    private var stripe: Color {
        switch thread.lane {
        case .hot: return Theme.ember
        case .warm: return Theme.gold
        case .live: return Theme.brand
        case .done: return Theme.good
        case .idle, .unknown: return Theme.inkGhost
        }
    }
}

/// "Looks like this one's finished?" — asked only when the evidence is
/// suggestive but not conclusive, and always showing its argument.
private struct ClosureCard: View {
    let closure: ThreadClosure
    var onAnswer: (Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("LOOKS FINISHED?")
                .font(.system(size: 9, weight: .semibold))
                .tracking(1.2)
                .foregroundStyle(Theme.good)
            Text(closure.thread.title)
                .font(Theme.serif(14, .semibold))
                .foregroundStyle(Theme.ink)
            ForEach(Array(closure.reasons.prefix(3).enumerated()), id: \.offset) { _, reason in
                HStack(alignment: .top, spacing: 5) {
                    Text("·").foregroundStyle(Theme.inkGhost)
                    Text(reason)
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            HStack(spacing: 7) {
                Button("Yes, done") { onAnswer(true) }
                    .font(.system(size: 11, weight: .semibold))
                    .buttonStyle(.borderedProminent)
                    .tint(Theme.good)
                Button("Still open") { onAnswer(false) }
                    .font(.system(size: 11))
                    .buttonStyle(.bordered)
                    .tint(Theme.inkSoft)
            }
            .padding(.top, 2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(11)
        .background(Theme.goodSoft)
        .clipShape(RoundedRectangle(cornerRadius: 11))
        .overlay(RoundedRectangle(cornerRadius: 11).strokeBorder(Theme.good.opacity(0.3), lineWidth: 1))
    }
}

/// The due chip. Inferred dates and user-set ones look the same here — the
/// distinction shows in the thread, where the receipts are.
private struct DueChip: View {
    let text: String
    let lane: LaneState

    var body: some View {
        Text(text)
            .font(.system(size: 9.5, weight: .semibold))
            .foregroundStyle(fg)
            .padding(.horizontal, 6).padding(.vertical, 3)
            .background(RoundedRectangle(cornerRadius: 5).fill(bg))
            .fixedSize()
    }

    private var fg: Color {
        switch lane {
        case .hot: return Theme.ember
        case .warm: return Theme.gold
        default: return Theme.inkFaint
        }
    }

    private var bg: Color {
        switch lane {
        case .hot: return Theme.emberSoft
        case .warm: return Theme.gold.opacity(0.15)
        default: return Theme.chip
        }
    }
}

/// The system's work, as marks over time. Blue is a finding, green an action it
/// prepared, grey "I looked and found nothing" — the last of which is the
/// honest one, and the reason the track exists at all.
struct ActivityTrack: View {
    let marks: [ActivityMark]
    /// The track spans the thread's life, not the marks' own range. Normalising
    /// against the marks put a lone mark hard against the right edge, which
    /// read as a stalled progress bar; against the thread's lifetime it lands
    /// where it actually happened, and a young thread reads as young.
    let openedAt: String

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 3).fill(Theme.chip)
                ForEach(Array(positions.enumerated()), id: \.offset) { _, mark in
                    RoundedRectangle(cornerRadius: 1)
                        .fill(color(mark.kind))
                        .frame(width: 3, height: 7)
                        .offset(x: mark.x * max(0, geo.size.width - 3), y: 2)
                }
            }
        }
        .frame(height: 11)
        .accessibilityLabel("\(marks.count) piece\(marks.count == 1 ? "" : "s") of activity")
    }

    private var positions: [(x: CGFloat, kind: String)] {
        let opened = ISO8601DateFormatter.lifeline.date(from: openedAt) ?? Date()
        let span = max(60, Date().timeIntervalSince(opened))   // never divide by zero
        return marks.compactMap { mark in
            guard let at = ISO8601DateFormatter.lifeline.date(from: mark.at) else { return nil }
            let x = min(1, max(0, at.timeIntervalSince(opened) / span))
            return (CGFloat(x), mark.kind)
        }
    }

    private func color(_ kind: String) -> Color {
        switch kind {
        case "finding": return Theme.wave
        case "action": return Theme.brand
        default: return Theme.inkGhost
        }
    }
}

/// The empty state. "Fewer threads" is the goal, so arriving here is a win and
/// should read like one — not like a screen that failed to load.
private struct NothingRunning: View {
    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "checkmark.circle")
                .font(.system(size: 30, weight: .light))
                .foregroundStyle(Theme.good)
            Text("All tied off")
                .font(Theme.serif(19, .semibold))
                .foregroundStyle(Theme.ink)
            Text("Nothing loose. Add one with +, or let something find you.")
                .font(.system(size: 12))
                .foregroundStyle(Theme.inkFaint)
                .multilineTextAlignment(.center)
        }
        .padding(.top, 90)
        .padding(.horizontal, 40)
    }
}

#Preview {
    ThreadsView()
}
