import SwiftUI

/// Inside a thread — a **work log**, not a checklist. What the system found,
/// what it's watching, what it prepared, and the evidence underneath all of it.
///
/// Findings and watchers arrive with the worker loop (step 4). Until then this
/// shows what genuinely exists: the deadline and where it came from, and every
/// row the thread has claimed. The sections are laid out now so step 4 fills
/// them rather than rearranging the screen.
struct ThreadDetailView: View {
    let threadId: String

    @Environment(\.syncService) private var syncService
    @Environment(\.dismiss) private var dismiss
    @State private var detail: ThreadDetail?
    @State private var editingDeadline = false
    @State private var draft: ThreadDraft?
    @State private var drafting = false
    @State private var showDraftSheet = false
    @State private var pickingContact = false
    @State private var contact: Person?
    @State private var pickingContactWasUsed = false
    @State private var showingAutonomy = false
    @State private var showingEvidence = false
    @State private var showingHistory = false
    /// The move awaiting a reason. Non-nil while the reject sheet is up.
    @State private var rejecting: Finding?
    /// True while a correction is being re-worked, so the screen can say so
    /// rather than looking inert for the length of a worker pass.
    @State private var reworking = false

    var body: some View {
        ScrollView {
            if let detail {
                VStack(alignment: .leading, spacing: 0) {
                    title(detail.thread)
                    // The meta line carries the date, and so does the state
                    // header below it. Only one of them should: two "Due Sat"
                    // in the first inch of the screen is exactly the kind of
                    // repetition this pass exists to remove.
                    if !StateHeader(detail: detail).showsDate {
                        metaLine(detail.thread)
                    }

                    // §v2.3 — what binds, before anything else.
                    //
                    // The two facts that decide what you do here used to be
                    // discoverable rather than stated: the date lived in its
                    // own section two screens down, and the question the
                    // thread was actually waiting on sat at the bottom of the
                    // move card under three paragraphs. Both are one line now,
                    // above everything.
                    StateHeader(detail: detail)

                    if reworking {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Taking another look…")
                                .font(Theme.secondary)
                                .foregroundStyle(Theme.inkSoft)
                        }
                        .padding(.top, 16)
                    }

                    // The move first, and as the only container on the screen.
                    // It is the reason you opened this, so it does not need a
                    // section label announcing it.
                    ForEach(detail.moves) { finding in
                        MoveCard(
                            finding: finding,
                            onAct: { act(finding, detail.thread) },
                            onReject: { rejecting = finding },
                            showsAction: finding.id != detail.moves.first?.id
                        )
                        .padding(.top, 18)
                    }

                    if !detail.notes.isEmpty || !detail.carriedProse.isEmpty {
                        SectionRule(title: "What I found")
                        ForEach(detail.notes) { finding in
                            FindingRow(finding: finding)
                        }
                        // Prose the one-line rule moved out of the cards. It
                        // reads as a paragraph because that is what it is —
                        // the mistake was rendering it as inventory.
                        ForEach(Array(detail.carriedProse.enumerated()), id: \.offset) { _, note in
                            Text(note)
                                .font(.system(size: 12))
                                .foregroundStyle(Theme.inkSoft)
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(.top, 6)
                        }
                    }

                    // Having no move is a result, not an absence. Say so.
                    if detail.hasNoMove {
                        NoMoveNote(detail: detail)
                    }

                    // What the thread used to say. A disclosure rather than a
                    // section: it is the receipt that the system was working,
                    // which matters when you doubt the answer and never
                    // otherwise.
                    if !detail.history.isEmpty {
                        DisclosureGroup(isExpanded: $showingHistory) {
                            ForEach(detail.history) { finding in
                                PastFindingRow(finding: finding)
                            }
                        } label: {
                            drawerRow("How this got here",
                                      "\(detail.history.count)")
                        }
                        .tint(Theme.inkSoft)
                        .padding(.top, 18)
                    }

                    if let deadline = detail.thread.deadline, deadline.date != nil {
                        SectionRule(title: "The date")
                        DeadlineReceipt(deadline: deadline, passed: detail.thread.deadlinePassed) {
                            editingDeadline = true
                        }
                    }

                    // Drafting only exists when there is somebody to write to.
                    // On a water bill the screen used to offer "Draft a reply"
                    // directly above "No one — nobody to write to".
                    if detail.thread.contactPersonId != nil {
                        SectionRule(title: "Ready to send")
                        draftSection(detail.thread)
                    }

                    // §v2.4 — the corrections, in the user's own words.
                    //
                    // Placed above Watching and below the findings because it
                    // is about the answer rather than about the machinery, and
                    // it only exists at all once somebody has said something:
                    // an empty "What you've told me" would be a section header
                    // asking to be filled, which is not what this is for.
                    if !detail.corrections.isEmpty {
                        SectionRule(title: "What you've told me")
                        ForEach(detail.corrections) { correction in
                            CorrectionRow(correction: correction) {
                                drop(correction)
                            }
                        }
                    }

                    if !detail.watchers.isEmpty {
                        SectionRule(title: "Watching")
                        ForEach(detail.watchers) { watcher in
                            WatcherRow(watcher: watcher) { stop(watcher) }
                        }
                    }

                    SectionRule(title: "Settings")
                    settingsDrawer(detail)

                    if let checked = detail.lastChecked {
                        Text(checked)
                            .font(Theme.meta)
                            .foregroundStyle(Theme.inkSoft)
                            .padding(.top, 20)
                    }
                }
                .padding(.horizontal, Theme.margin)
                .padding(.bottom, 40)
            } else {
                ProgressView().padding(.top, 80)
            }
        }
        .background(Theme.paper.ignoresSafeArea())
        .navigationBarTitleDisplayMode(.inline)
        // The bar gets a ground of its own. Without it the back circle and the
        // Resolve pill float directly over the content and punch words out of
        // whatever scrolls beneath them — in one screenshot, out of the middle
        // of a finding's headline. Logged a week ago as
        // `eval-chrome-overlaps-content` and it is one line.
        .toolbarBackground(Theme.paper, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .sheet(isPresented: $showDraftSheet) {
            NavigationStack {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        if drafting {
                            HStack(spacing: 8) {
                                ProgressView().controlSize(.small)
                                Text("Writing the message with everything this loose end knows…")
                                    .font(.system(size: 13)).foregroundStyle(Theme.inkSoft)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 8)
                        } else if let result = draft {
                            if let message = result.draft {
                                DraftCard(message: message) { send(message) }
                            } else {
                                // The refusal is a real answer — shown here,
                                // not buried. "No one to write to" beats
                                // silence every time.
                                Waiting(text: result.reason, icon: "hand.raised")
                            }
                        }
                    }
                    .padding(Theme.margin)
                }
                .background(Theme.paper)
                .navigationTitle("Review & send")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") { showDraftSheet = false }
                    }
                }
            }
            .presentationDetents([.medium, .large])
        }
        .safeAreaInset(edge: .bottom) {
            // The verb stays reachable from any scroll position. It used to sit
            // wherever the move happened to land, which on a long thread meant
            // scrolling back up to act on what you had just read.
            // `hasSomewhereToGo` for the same reason MoveCard has it — without
            // it the dead verb doesn't disappear, it just relocates to the bar,
            // where it is the most prominent thing on the screen.
            if let detail, let move = detail.moves.first, !move.isBlocked,
               move.hasSomewhereToGo {
                Button { act(move, detail.thread) } label: {
                    // The tap must never be silent. This bar used to fire a
                    // 20-second server call with no feedback whatsoever, and
                    // the result rendered in a section below the fold — a
                    // button that "did nothing", live, on every silence
                    // thread. Found by pressing it in the simulator.
                    HStack(spacing: 8) {
                        if drafting { ProgressView().tint(.white).controlSize(.small) }
                        Text(drafting ? "Writing the message…"
                             : move.actionLabel(default: move.move?.verb ?? "Open it"))
                            .font(.system(size: 15, weight: .semibold))
                    }
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                    .background(Capsule().fill(Theme.brand.opacity(drafting ? 0.7 : 1)))
                }
                .buttonStyle(.plain)
                .disabled(drafting)
                .padding(.horizontal, Theme.margin)
                .padding(.top, 10)
                .padding(.bottom, 6)
                .background(Theme.paper.opacity(0.97))
                .overlay(alignment: .top) {
                    Rectangle().fill(Theme.rule).frame(height: 1)
                }
            }
        }
        .toolbar {
            if let thread = detail?.thread, !thread.isResolved {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Close") { resolve(thread) }
                        .font(.system(size: 15, weight: .semibold))
                        .tint(Theme.brand)
                }
            }
        }
        .sheet(item: $rejecting) { move in
            RejectSheet(move: move) { reason, text in
                resolveRejection(move, reason: reason, text: text)
            }
            .presentationDetents([.medium])
            .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $pickingContact) {
            ContactPicker(selection: $contact)
        }
        .onChange(of: contact) { _, new in
            guard pickingContactWasUsed else { return }
            saveContact(new?.id)
        }
        .onChange(of: pickingContact) { _, showing in
            if showing { pickingContactWasUsed = true }
        }
        .sheet(isPresented: $editingDeadline) {
            if let thread = detail?.thread {
                DeadlineEditSheet(thread: thread) { date in
                    _ = try? await syncService.api.setThreadDeadline(id: thread.id, date: date)
                    await load()
                }
            }
        }
        .task { await load() }
    }

    /// Autonomy, contact and evidence collapse to three lines. The autonomy
    /// radio block alone was ~250pt — the largest thing on the screen after the
    /// move — which put a preference above the action.
    @ViewBuilder
    private func settingsDrawer(_ detail: ThreadDetail) -> some View {
        DisclosureGroup(isExpanded: $showingAutonomy) {
            AutonomyControl(current: detail.thread.ceiling) { setCeiling($0) }
                .padding(.top, 8)
        } label: {
            drawerRow("How far it may go", detail.thread.ceiling.title)
        }
        .tint(Theme.inkSoft)

        Rectangle().fill(Theme.rule).frame(height: 1)

        Button { pickingContact = true } label: {
            drawerRow("Who to contact", detail.thread.contactName ?? "No one")
        }
        .buttonStyle(.plain)

        Rectangle().fill(Theme.rule).frame(height: 1)

        DisclosureGroup(isExpanded: $showingEvidence) {
            ForEach(detail.evidence) { row in
                EvidenceRow(evidence: row)
            }
        } label: {
            drawerRow("Evidence", "\(detail.evidence.count)")
        }
        .tint(Theme.inkSoft)
    }

    private func drawerRow(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title).font(Theme.body).foregroundStyle(Theme.ink)
            Spacer()
            Text(value)
                .font(Theme.secondary)
                .foregroundStyle(Theme.inkSoft)
                .lineLimit(1)
        }
        .padding(.vertical, 13)
        .contentShape(Rectangle())
    }

    /// Written on demand, not at extraction time. The button is the point:
    /// a draft costs a model call, so it happens when the user wants one and
    /// with the whole thread in hand — not speculatively, from one message,
    /// for every item that ever arrived.
    @ViewBuilder
    private func draftSection(_ thread: LifeThread) -> some View {
        if drafting {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Reading the loose end…").font(.system(size: 11)).foregroundStyle(Theme.inkFaint)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(11)
        } else if let result = draft {
            if let message = result.draft {
                DraftCard(message: message) { send(message) }
            } else {
                // A refusal is a real answer, and usually the more useful one.
                Waiting(text: result.reason, icon: "hand.raised")
            }
            Button("Rewrite it") { write(thread) }
                .font(.system(size: 11, weight: .semibold))
                .tint(Theme.brand)
                .padding(.top, 6)
        } else if thread.ceiling == .silent {
            // The ceiling has to bind here too, or it's a setting about the
            // background worker wearing the label of a setting about the
            // thread. "It only ever reads" is not true while the screen is
            // still offering to write something.
            Waiting(
                text: "This loose end is set to no writing. Raise the ceiling below to draft a reply.",
                icon: "lock"
            )
        } else {
            Button { write(thread) } label: {
                Label("Draft a reply", systemImage: "square.and.pencil")
                    .font(.system(size: 12, weight: .semibold))
            }
            .buttonStyle(.bordered)
            .tint(Theme.brand)
        }
    }

    private func write(_ thread: LifeThread) {
        drafting = true
        Task {
            draft = try? await syncService.api.draftForThread(id: thread.id)
            drafting = false
        }
    }

    /// Opens Messages or Mail with the text prefilled. The app never sends —
    /// that line doesn't move.
    private func send(_ message: MessageDraft) {
        // One URL builder for every send surface: MessageDraft.sendURL.
        // This used to be a hand-rolled copy that had already drifted.
        guard let url = message.sendURL else { return }
        UIApplication.shared.open(url)
    }

    /// Moves the mark first, then tells the server. Refetching the whole detail
    /// to learn what you already know costs a round trip the tap has to wait
    /// out, and rebuilds every section on screen to change one glyph.
    ///
    /// The revert matters more than the speed: this control is a permission
    /// boundary, so a write that failed must not leave the app showing a
    /// narrower ceiling than the server is actually enforcing.
    private func setCeiling(_ ceiling: Autonomy) {
        guard let previous = detail?.thread.autonomy, previous != ceiling.rawValue else { return }
        detail?.thread.autonomy = ceiling.rawValue
        Task {
            do {
                _ = try await syncService.api.setAutonomy(id: threadId, ceiling: ceiling)
            } catch {
                detail?.thread.autonomy = previous
            }
        }
    }

    private func saveContact(_ personId: String?) {
        let previous = detail?.thread.contactPersonId
        guard previous != personId else { return }
        Task {
            do {
                let updated = try await syncService.api.setContact(id: threadId, personId: personId)
                detail?.thread.contactPersonId = updated.contactPersonId
                detail?.thread.contactName = updated.contactName
            } catch {
                // Same rule as the ceiling: never show a contact the server
                // isn't actually holding.
                await load()
            }
        }
    }

    /// Take back something you told it. Removed locally first so the tap
    /// lands; the server soft-deletes, so the fact that it was once said and
    /// then unsaid is not itself thrown away.
    private func drop(_ correction: Correction) {
        detail?.corrections.removeAll { $0.id == correction.id }
        Task {
            do {
                try await syncService.api.dropCorrection(
                    threadId: threadId, correctionId: correction.id
                )
            } catch {
                await load()
            }
        }
    }

    /// What the card's verb does. Each shape hands off somewhere different,
    /// and `do` is the honest one: the app cannot pay a bill, so it opens the
    /// link the worker staged and gets out of the way.
    private func act(_ finding: Finding, _ thread: LifeThread) {
        // Acting is the positive half of the signal. Without it appetite is a
        // ratchet — rejections would accumulate and nothing would ever restore
        // the system's willingness to try.
        Task { try? await syncService.api.acceptMove(threadId: threadId, findingId: finding.id) }
        switch finding.move {
        case .send:
            // The draft path already knows how to write for a thread, and
            // re-uses the review-then-send flow rather than inventing a second.
            // `showDraftSheet` makes the outcome land where the finger is —
            // the writer section it used to land in is below the fold.
            showDraftSheet = true
            write(thread)
        case .do, .gather, .decide, nil:
            // Open what the worker staged — and when it staged nothing, do
            // nothing. This used to fall through to `write(thread)`, so the
            // verb on a `decide` move about buying pyjamas opened a message
            // draft, and on a bill with no payment link it offered to text the
            // water company. `hasSomewhereToGo` keeps the button off the card
            // in that case, so this branch is now only reached with a link.
            if let url = finding.actionLink {
                UIApplication.shared.open(url)
            }
        }
    }

    /// "Not this" — with the reason the sheet collected.
    ///
    /// The card goes first so the tap lands, then the server is told. What
    /// happens next differs per exit, and only one of them narrows anything:
    /// `.wrong` records the user's sentence and re-runs the pass so they watch
    /// the correction take effect, `.handled` closes the thread, and
    /// `.unwanted` is the pre-v2.4 nudge.
    private func resolveRejection(_ finding: Finding, reason: RejectReason, text: String?) {
        detail?.findings.removeAll { $0.id == finding.id }
        if reason == .wrong { reworking = true }
        Task {
            do {
                try await syncService.api.rejectMove(
                    threadId: threadId, findingId: finding.id,
                    reason: reason, text: text
                )
                switch reason {
                case .handled:
                    dismiss()
                case .wrong:
                    // Costs one worker pass. Worth it on the rare occasion
                    // somebody types a sentence to fix the understanding.
                    try? await syncService.api.rework(threadId: threadId)
                    await load()
                case .unwanted:
                    break
                }
            } catch {
                await load()
            }
            reworking = false
        }
    }

    private func stop(_ watcher: Watcher) {
        Task {
            detail = try? await syncService.api.stopWatcher(threadId: threadId, watcherId: watcher.id)
        }
    }

    private func load() async {
        detail = try? await syncService.api.thread(id: threadId)
    }

    private func resolve(_ thread: LifeThread) {
        Task {
            _ = try? await syncService.api.resolveThread(id: thread.id)
            dismiss()
        }
    }

    /// §v2.4 — two lines, hard stop.
    ///
    /// The title is the user's own sentence, and on a declared thread it is
    /// whatever they typed: one real thread opens with six lines of 28pt serif
    /// repeating the request back at them, occupying most of the first screen.
    /// They wrote it; they do not need to re-read it to know where they are.
    /// The full text is one row up on the stack, and in the drawer below.
    private func title(_ thread: LifeThread) -> some View {
        Text(thread.title)
            .font(Theme.screenTitle)
            .foregroundStyle(Theme.ink)
            .lineSpacing(Theme.titleLeading)
            .strikethrough(thread.isResolved, color: Theme.inkSoft)
            .lineLimit(2)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.top, 4)
    }

    /// One line of meta, not three stacked facts. Date first, because that is
    /// the thing that changes what you do next.
    private func metaLine(_ thread: LifeThread) -> some View {
        Text(thread.metaSummary)
            .font(Theme.meta)
            .foregroundStyle(thread.deadlinePassed ? Theme.alert : Theme.inkSoft)
            .padding(.top, 6)
    }

    private func meta(_ thread: LifeThread) -> some View {
        HStack(spacing: 8) {
            Text("opened \(relative(thread.openedAt))")
            if thread.evidenceCount > 0 {
                Text("·")
                Text("\(thread.evidenceCount) piece\(thread.evidenceCount == 1 ? "" : "s") of evidence")
            }
            if let by = thread.resolvedBy {
                Text("·")
                Text("closed by \(by)")
            }
        }
        .font(.system(size: 11))
        .foregroundStyle(Theme.inkFaint)
        .padding(.top, 6)
    }

    private func section(_ label: String) -> some View {
        Text(label)
            .font(.system(size: 9, weight: .semibold))
            .tracking(1.4)
            .foregroundStyle(Theme.inkFaint)
            .padding(.top, 18)
            .padding(.bottom, 7)
    }

    private func relative(_ iso: String) -> String {
        guard let date = ISO8601DateFormatter.lifeline.date(from: iso) else { return "recently" }
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        return f.localizedString(for: date, relativeTo: Date())
    }
}

// MARK: - the deadline, with its receipts

/// A deadline chip that can show its work. An inferred date names the evidence
/// that implied it — that's enforced server-side, so there is always something
/// to show, and tapping through is how the user decides whether to trust it.
private struct DeadlineReceipt: View {
    let deadline: DeadlineChip
    let passed: Bool
    var onEdit: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text(formatted)
                    .font(Theme.body).fontWeight(.semibold)
                    .foregroundStyle(passed ? Theme.inkFaint : Theme.ink)
                    .strikethrough(passed, color: Theme.inkGhost)
                Text(deadline.isInferred ? "inferred" : "you set this")
                    .font(Theme.label)
                    .foregroundStyle(Theme.inkFaint)
                    .padding(.horizontal, 5).padding(.vertical, 2)
                    .background(RoundedRectangle(cornerRadius: 4).fill(Theme.chip))
                Spacer()
                Button("Change", action: onEdit)
                    .font(Theme.secondary).fontWeight(.semibold)
                    .tint(Theme.brand)
            }
            if let reason = deadline.reason {
                Text(reason).font(Theme.secondary).foregroundStyle(Theme.inkSoft)
            }
            ForEach(deadline.evidence) { row in
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "quote.opening")
                        .font(.system(size: 8))
                        .foregroundStyle(Theme.inkGhost)
                        .padding(.top, 2)
                    Text(row.text.isEmpty ? row.title : row.text)
                        .font(Theme.secondary)
                        .foregroundStyle(Theme.inkSoft)
                        .lineLimit(3)
                }
            }
        }
        // Text on paper, like every other section. The move is the one
        // container left in the app, so that a border means "act on this".
        .padding(.vertical, 4)
    }

    private var formatted: String {
        guard let raw = deadline.date,
              let date = ISO8601DateFormatter.lifeline.date(from: raw) else { return "—" }
        // The year is not optional here either. A 2027 deadline rendered as
        // "Tuesday, Aug 3" — the same omission the lane chip had, and just as
        // misleading in the one place the full date is supposed to be shown.
        let calendar = Calendar.current
        let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: Date())
        let f = DateFormatter()
        f.dateFormat = sameYear ? "EEEE, MMM d" : "EEEE, MMM d, yyyy"
        return f.string(from: date)
    }
}

// MARK: - evidence

private struct EvidenceRow: View {
    let evidence: ThreadEvidence

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 9))
                    .foregroundStyle(Theme.inkFaint)
                Text(evidence.title)
                    .font(.system(size: 11.5, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(1)
                if evidence.isFounding {
                    Text("FOUNDING")
                        .font(.system(size: 8, weight: .semibold))
                        .tracking(0.5)
                        .foregroundStyle(Theme.brand)
                }
            }
            if !evidence.text.isEmpty {
                Text(evidence.text)
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.inkSoft)
                    .lineLimit(4)
            }
            if let person = evidence.person {
                Text(person).font(.system(size: 10)).foregroundStyle(Theme.inkFaint)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .padding(.bottom, 6)
    }

    private var icon: String {
        switch evidence.kind {
        case "message": return "bubble.left"
        case "calendar_event": return "calendar"
        default: return "doc.text"
        }
    }
}

/// The autonomy ladder for one thread.
///
/// The fourth rung is on screen and is not a choice. "Never sends as you" is
/// the line the spec says doesn't move at any tier, and a guarantee the user
/// can't see is a guarantee they have no reason to believe.
private struct AutonomyControl: View {
    let current: Autonomy
    var onChange: (Autonomy) -> Void

    var body: some View {
        VStack(spacing: 0) {
            ForEach(Autonomy.allCases, id: \.self) { rung in
                Button { onChange(rung) } label: { row(rung) }
                    .buttonStyle(.plain)
                if rung != Autonomy.allCases.last {
                    Divider().overlay(Theme.ruleSoft).padding(.leading, 34)
                }
            }
            Divider().overlay(Theme.rule).padding(.leading, 34)
            fixedRung
        }
    }

    private func row(_ rung: Autonomy) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: rung == current ? "checkmark.circle.fill" : "circle")
                .font(.system(size: 14))
                .foregroundStyle(rung == current ? Theme.brand : Theme.inkGhost)
            VStack(alignment: .leading, spacing: 2) {
                Text(rung.title)
                    .font(.system(size: 12, weight: rung == current ? .semibold : .regular))
                    .foregroundStyle(Theme.ink)
                Text(rung.detail)
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 9)
        .contentShape(Rectangle())
    }

    /// Always there, never selectable.
    private var fixedRung: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: "lock")
                .font(.system(size: 12))
                .foregroundStyle(Theme.inkGhost)
                .padding(.leading, 1)
            VStack(alignment: .leading, spacing: 2) {
                Text("Never sends as you")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Theme.inkSoft)
                Text("Not available at any setting. You send everything yourself.")
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 9)
        .background(Theme.chip.opacity(0.4))
    }
}

/// A standing monitor, with its cadence and an off switch.
private struct WatcherRow: View {
    let watcher: Watcher
    var onStop: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Text(watcher.kind)
                .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                .foregroundStyle(Theme.brand)
                .padding(.horizontal, 5).padding(.vertical, 2)
                .background(RoundedRectangle(cornerRadius: 4).fill(Theme.chip))
            VStack(alignment: .leading, spacing: 2) {
                Text(watcher.what)
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                Text(watcher.timesFired == 0
                     ? "\(watcher.cadenceLabel) · nothing yet"
                     : "\(watcher.cadenceLabel) · fired \(watcher.timesFired)×")
                    .font(.system(size: 9.5))
                    .foregroundStyle(Theme.inkGhost)
            }
            Spacer(minLength: 0)
            Button(action: onStop) {
                Image(systemName: "xmark").font(.system(size: 9, weight: .semibold))
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.inkGhost)
            .accessibilityLabel("Stop watching")
        }
        .padding(.vertical, 5)
    }
}

/// One thing the system found on its own, with what it read to find it.
private struct FindingRow: View {
    let finding: Finding

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 7) {
                Circle().fill(dot).frame(width: 5, height: 5).padding(.top, 5)
                Text(finding.headline)
                    .font(.system(size: 11.5, weight: .semibold))
                    .foregroundStyle(finding.isNothing ? Theme.inkFaint : Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // §v2.3 — verified figures first, as rows.
            //
            // The same numbers used to arrive only inside `body`, so a pass
            // that searched twenty retailers handed back a paragraph and the
            // user re-derived the decision from it. A price you can compare
            // and a link you can tap is the entire difference between research
            // and a report about research.
            if !finding.facts.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(finding.facts) { fact in
                        FactRow(fact: fact)
                    }
                }
                .padding(.leading, 12)
                .padding(.top, 2)
            }
            if !finding.body.isEmpty {
                Text(finding.body)
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 12)
                    .padding(.top, finding.facts.isEmpty ? 0 : 4)
            }
            if !finding.evidence.isEmpty {
                Text("from \(finding.evidence.count) source\(finding.evidence.count == 1 ? "" : "s")")
                    .font(.system(size: 9.5))
                    .foregroundStyle(Theme.inkGhost)
                    .padding(.leading, 12)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .padding(.bottom, 6)
        .opacity(finding.isNothing ? 0.7 : 1)
    }

    /// Blue is a finding, green an action it prepared, grey "I looked and
    /// found nothing" — matching the marks on the lane's activity track.
    private var dot: Color {
        switch finding.kind {
        case "action": return Theme.brand
        case "nothing": return Theme.inkGhost
        default: return Theme.wave
        }
    }
}

/// A message the writer produced, ready to review and send.
struct DraftCard: View {
    let message: MessageDraft
    var onSend: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("TO \(message.person.uppercased())")
                .font(.system(size: 9, weight: .semibold))
                .tracking(1.2)
                .foregroundStyle(Theme.inkFaint)
            Text(message.text)
                .font(.system(size: 12))
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 7) {
                Button(action: onSend) {
                    Text("Review & send").font(.system(size: 11, weight: .semibold))
                }
                .buttonStyle(.borderedProminent)
                .tint(Theme.brand)
                .disabled(message.handle == nil)
                if message.handle == nil {
                    Text("no address on file")
                        .font(.system(size: 10)).foregroundStyle(Theme.inkFaint)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(11)
        .background(Theme.chip)
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Theme.brand.opacity(0.25), lineWidth: 1))
    }
}

/// A section that has nothing in it yet. Says so plainly rather than hiding —
/// an empty section the user can see is honest; one that vanishes reads as if
/// the feature doesn't exist.
struct Waiting: View {
    let text: String
    let icon: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon).font(.system(size: 12)).foregroundStyle(Theme.inkGhost)
            Text(text).font(.system(size: 11)).foregroundStyle(Theme.inkFaint)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(11)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chip.opacity(0.5)))
    }
}

// MARK: - declare a thread

/// The primary capture verb of v2: the user saying "this is on my mind"
/// without waiting for a message to imply it.
struct DeclareThreadSheet: View {
    /// Where the title field sits on screen. A sheet is its own presentation,
    /// so the stack behind it cannot measure this — the arrival animation needs
    /// it to fly the words from where they were actually typed rather than from
    /// a guess about where the field probably was.
    var onFieldFrame: ((CGRect) -> Void)?
    var onCreate: (String, String, Date?, String?) async -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var summary = ""
    @State private var hasDeadline = false
    @State private var deadline = Date().addingTimeInterval(86_400)
    @State private var saving = false
    @State private var contact: Person?
    @State private var pickingContact = false
    @State private var expanded = false
    /// The first beat of the arrival, and the reason this is not a `Form`:
    /// everything except the words you typed leaves first, so what the sheet
    /// hands to the stack is your sentence rather than a card.
    @State private var shedding = false
    @State private var detent: PresentationDetent = .height(Self.compact)
    @FocusState private var titleFocused: Bool

    /// Sized to the content and nothing more. The composer asks for one
    /// sentence, so it should occupy about one sentence of the screen — a
    /// sheet with a hundred points of empty below the last control reads as a
    /// form that forgot to have fields.
    private static let compact: CGFloat = 200
    private static let full: CGFloat = 344

    private var canAdd: Bool {
        !title.trimmingCharacters(in: .whitespaces).isEmpty && !saving
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            bar
            field
            if expanded { details } else { affordances }
        }
        .padding(.horizontal, Theme.margin)
        .padding(.top, 14)
        .padding(.bottom, 16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(Theme.card)
        .presentationDetents([.height(Self.compact), .height(Self.full)], selection: $detent)
        .presentationCornerRadius(20)
        .presentationDragIndicator(.hidden)
        .presentationBackground(Theme.card)
        .sheet(isPresented: $pickingContact) {
            ContactPicker(selection: $contact)
        }
        // The launch. Soft, because this is the words leaving your hands — the
        // landing tick on the stack answers it.
        .sensoryFeedback(.impact(flexibility: .soft), trigger: shedding)
        .task { titleFocused = true }
    }

    // MARK: - the chrome, which is the part that leaves

    private var bar: some View {
        HStack {
            Button("Cancel") { dismiss() }
                .foregroundStyle(Theme.inkFaint)
            Spacer()
            Text("New loose end")
                .font(Theme.label)
                .tracking(0.8)
                .foregroundStyle(Theme.inkFaint)
            Spacer()
            Button("Add") { save() }
                .foregroundStyle(canAdd ? Theme.brand : Theme.inkGhost)
                .fontWeight(.semibold)
                .disabled(!canAdd)
        }
        .font(Theme.body)
        .padding(.bottom, 14)
        .opacity(shedding ? 0 : 1)
    }

    // MARK: - the words, which are the part that stays

    private var field: some View {
        TextField("What's on your mind?", text: $title, axis: .vertical)
            .font(Theme.serif(17))
            .foregroundStyle(Theme.ink)
            .lineSpacing(Theme.titleLeading)
            .tint(Theme.brand)
            .focused($titleFocused)
            .lineLimit(1...4)
            .background {
                GeometryReader { proxy in
                    Color.clear
                        .onAppear { onFieldFrame?(proxy.frame(in: .global)) }
                        .onChange(of: proxy.frame(in: .global)) { _, f in
                            onFieldFrame?(f)
                        }
                }
            }
    }

    /// What the composer offers before you ask for more: the sentence about
    /// what a thread *is*, and two ways in. A date and a person are the only
    /// two things worth setting at capture time — everything else about a
    /// thread is easier to say once it exists.
    private var affordances: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("A loose end you're carrying — a trip, a bill, a decision. Not a single to-do.")
                .font(Theme.secondary)
                .foregroundStyle(Theme.inkFaint)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                chip("Add a date", icon: "calendar") { reveal(deadline: true) }
                chip("Someone to contact", icon: "person") { reveal(deadline: false) }
            }
        }
        .padding(.top, 14)
        .opacity(shedding ? 0 : 1)
    }

    private func chip(_ label: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: icon).font(.system(size: 11, weight: .medium))
                Text(label).font(Theme.meta)
            }
            .foregroundStyle(Theme.inkSoft)
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .background(Capsule().fill(Theme.chip))
        }
        .buttonStyle(.plain)
    }

    /// Everything the old form showed at once, shown once you've asked for it.
    /// Hairlines rather than grouped-list cards, so the composer reads like the
    /// rest of the app instead of like Settings.
    private var details: some View {
        VStack(alignment: .leading, spacing: 0) {
            TextField("What's open about it (optional)", text: $summary, axis: .vertical)
                .font(Theme.body)
                .foregroundStyle(Theme.inkSoft)
                .lineLimit(1...3)
                .padding(.vertical, 12)
                .hairlineUnder(showing: true)

            Toggle(isOn: $hasDeadline.animation()) {
                Text("Has a deadline").font(Theme.body).foregroundStyle(Theme.ink)
            }
            .tint(Theme.brand)
            .padding(.vertical, 10)
            .hairlineUnder(showing: true)

            if hasDeadline {
                DatePicker(selection: $deadline, displayedComponents: .date) {
                    Text("Due").font(Theme.body).foregroundStyle(Theme.ink)
                }
                .tint(Theme.brand)
                .padding(.vertical, 6)
                .hairlineUnder(showing: true)
            }

            Button { pickingContact = true } label: {
                HStack {
                    Text("Who to contact").font(Theme.body).foregroundStyle(Theme.ink)
                    Spacer()
                    Text(contact?.displayName ?? "No one")
                        .font(Theme.body)
                        .foregroundStyle(contact == nil ? Theme.inkFaint : Theme.brand)
                }
                .padding(.vertical, 12)
            }
            .buttonStyle(.plain)

            Text("Only if this might need a message. Most don't.")
                .font(Theme.secondary)
                .foregroundStyle(Theme.inkFaint)
                .padding(.top, 4)
        }
        .padding(.top, 6)
        .opacity(shedding ? 0 : 1)
    }

    private func reveal(deadline wantsDeadline: Bool) {
        withAnimation(.spring(response: 0.34, dampingFraction: 0.9)) {
            expanded = true
            if wantsDeadline { hasDeadline = true }
            detent = .height(Self.full)
        }
        if !wantsDeadline { pickingContact = true }
    }

    private func save() {
        saving = true
        // Beat one: the chrome goes, and for a moment the sheet is only your
        // sentence. Then the sheet leaves *around* the words rather than
        // taking them with it.
        withAnimation(.easeOut(duration: 0.13)) { shedding = true }
        Task {
            try? await Task.sleep(for: .milliseconds(150))
            await onCreate(title.trimmingCharacters(in: .whitespaces),
                           summary.trimmingCharacters(in: .whitespaces),
                           hasDeadline ? deadline : nil,
                           contact?.id)
            dismiss()
        }
    }
}

/// Overruling a date the system inferred. The user always wins — that's
/// enforced on the server, and once they've set one, inference can't take it
/// back.
struct DeadlineEditSheet: View {
    let thread: LifeThread
    var onSave: (Date?) async -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var date = Date()

    var body: some View {
        NavigationStack {
            Form {
                DatePicker("Due", selection: $date, displayedComponents: .date)
                Section {
                    Button("Remove the deadline", role: .destructive) {
                        Task { await onSave(nil); dismiss() }
                    }
                } footer: {
                    Text("Your date always wins. Once you set one, the system won't replace it with a guess.")
                }
            }
            .navigationTitle(thread.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { Task { await onSave(date); dismiss() } }
                }
            }
            .task {
                if let raw = thread.deadline?.date,
                   let existing = ISO8601DateFormatter.lifeline.date(from: raw) {
                    date = existing
                }
            }
        }
    }
}

// MARK: - proposals

/// Threads the system proposed, waiting where they can be ignored. **Never in
/// the main stack** — that separation is what makes the running count mean
/// something, so it's a separate door rather than a filter.
struct ProposalsSheet: View {
    var onChange: () async -> Void

    @Environment(\.syncService) private var syncService
    @Environment(\.dismiss) private var dismiss
    @State private var proposals: [LifeThread] = []
    @State private var loaded = false

    var body: some View {
        NavigationStack {
            List {
                ForEach(proposals) { proposal in
                    VStack(alignment: .leading, spacing: 5) {
                        Text(proposal.title)
                            .font(Theme.serif(15, .semibold))
                            .foregroundStyle(Theme.ink)
                        if !proposal.summary.isEmpty {
                            Text(proposal.summary)
                                .font(.system(size: 11.5))
                                .foregroundStyle(Theme.inkSoft)
                        }
                        HStack(spacing: 10) {
                            Button("Carry this") { act(proposal, accept: true) }
                                .font(.system(size: 12, weight: .semibold))
                                .buttonStyle(.borderedProminent)
                                .tint(Theme.brand)
                            Button("No thanks") { act(proposal, accept: false) }
                                .font(.system(size: 12))
                                .buttonStyle(.bordered)
                                .tint(Theme.inkSoft)
                        }
                        .padding(.top, 3)
                    }
                    .padding(.vertical, 4)
                }
                if loaded && proposals.isEmpty {
                    Text("Nothing proposed. The system only suggests one when something looks like a loose end you're carrying.")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.inkFaint)
                }
            }
            .navigationTitle("Proposed")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .task { await load() }
        }
    }

    private func load() async {
        proposals = (try? await syncService.api.proposals()) ?? []
        loaded = true
    }

    private func act(_ proposal: LifeThread, accept: Bool) {
        proposals.removeAll { $0.id == proposal.id }
        Task {
            if accept {
                _ = try? await syncService.api.acceptProposal(id: proposal.id)
            } else {
                _ = try? await syncService.api.dismissProposal(id: proposal.id)
            }
            await onChange()
        }
    }
}

/// The thread's counterpart, and the door to changing it.
///
/// Most threads legitimately have none — "Pay the American Water bill"
/// concerns nobody — so the empty state is a normal, unalarming row rather
/// than a prompt to fill something in.
private struct ContactRow: View {
    let thread: LifeThread
    var onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 9) {
                Image(systemName: thread.contactName == nil ? "person.crop.circle.dashed" : "person.crop.circle.fill")
                    .font(.system(size: 15))
                    .foregroundStyle(thread.contactName == nil ? Theme.inkGhost : Theme.brand)
                VStack(alignment: .leading, spacing: 2) {
                    Text(thread.contactName ?? "No one")
                        .font(.system(size: 12, weight: thread.contactName == nil ? .regular : .semibold))
                        .foregroundStyle(thread.contactName == nil ? Theme.inkSoft : Theme.ink)
                    Text(thread.contactName == nil
                         ? "Nobody to write to. Most loose ends are like this."
                         : "Drafts for this one go to them.")
                        .font(.system(size: 10.5))
                        .foregroundStyle(Theme.inkFaint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Text("Change")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.brand)
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 10)
            .background(Theme.card)
            .clipShape(RoundedRectangle(cornerRadius: 10))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}
