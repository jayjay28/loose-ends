import Foundation
// `statusColor` below resolves the status word to a Theme hue, so this model
// reaches for SwiftUI's Color. Kept here rather than in the view because the
// server decides the word and the mapping must not fork per screen.
import SwiftUI

/// §v2 — a background thread: one open loop in the user's head, and the unit
/// the whole app is now built around.
///
/// Named `LifeThread` rather than `Thread` on purpose. Step 0 freed the word
/// *thread* from meaning "conversation", but Foundation still ships a `Thread`
/// class, and a module-level `struct Thread` shadows it everywhere — so the
/// day someone writes `Thread.sleep` they get a baffling error instead of a
/// concurrency primitive. The product word stays "thread" in every string the
/// user reads; only the Swift symbol is qualified.

/// The stripe down the left of a lane. The server decides this — same rule
/// `InterruptionLevel` already follows — so the client never re-litigates
/// urgency it doesn't have the evidence for.
enum LaneState: String, Codable, Sendable {
    case hot        // ember — needs you today
    case warm       // gold  — carries a deadline
    case live       // teal  — running
    case idle       // grey  — quiet, nothing moving
    case done       // olive — resolved, struck through
    case unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = LaneState(rawValue: raw) ?? .unknown
    }
}

enum ThreadState: String, Codable, Sendable {
    case proposed, live, quiet, resolved, archived, unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ThreadState(rawValue: raw) ?? .unknown
    }
}

/// Where the thread came from. `silence` and `urgency` are system-opened;
/// `systemProposed` is the only one that waits in the proposals view.
enum ThreadOrigin: String, Codable, Sendable {
    case user
    case promotedFromItem = "promoted-from-item"
    case systemProposed = "system-proposed"
    case silence
    case urgency
    case unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ThreadOrigin(rawValue: raw) ?? .unknown
    }
}

/// §v2 — how far a thread may go on its own.
///
/// **User-set, never learned.** `prepared` already needs no permission, so the
/// only rung learning could promote *into* is `ask` — spending money, things
/// that are hard to undo — which would turn unrelated draft approvals into
/// consent for irreversible acts. Learning may lower a thread; only the person
/// raises one.
enum Autonomy: String, Codable, CaseIterable, Sendable {
    case silent, prepared, ask

    var title: String {
        switch self {
        case .silent:   return "No writing"
        case .prepared: return "Prepare things"
        case .ask:      return "Ask before acting"
        }
    }

    /// Written against `registry.scoped_for`, not against the intent. The first
    /// version of this said "Read only — it only ever reads", which was not
    /// true: at this ceiling the worker still sets deadlines, adds watchers and
    /// files findings. What it genuinely cannot do is write a message.
    var detail: String {
        switch self {
        case .silent:
            return "Reads your mail, messages and calendar. Keeps this loose end's deadline, watchers and notes current. Never writes a message, and never prepares one."
        case .prepared:
            return "Also reads whole conversations, writes drafts, and lines up options for you to pick from. Nothing is sent."
        case .ask:
            return "Also gets things that spend money or can't be undone ready to go — a bill with the amount and the link, a form filled in. You still do the final tap."
        }
    }

    var icon: String {
        switch self {
        case .silent:   return "eye"
        case .prepared: return "square.and.pencil"
        case .ask:      return "hand.raised"
        }
    }
}

/// A deadline chip and where it came from. `source == "inferred"` always
/// carries evidence — that's enforced server-side, and it's why the chip can
/// be tapped to show its receipts.
struct DeadlineChip: Codable, Hashable, Sendable {
    var date: String?
    var source: String?          // inferred | user
    var reason: String?
    var evidence: [ThreadEvidence] = []

    var isInferred: Bool { source == "inferred" }
}

/// One row a thread has claimed, resolved for reading.
struct ThreadEvidence: Codable, Hashable, Identifiable, Sendable {
    var kind: String             // item | message | calendar_event
    var refId: String
    var title: String
    var text: String = ""
    var person: String?
    var personId: String?
    var timestamp: String?
    var date: String?
    var status: String?
    var source: String?
    var role: String = "claimed" // claimed | founding
    var note: String?
    var linkedAt: String?

    var id: String { "\(kind):\(refId)" }
    var isFounding: Bool { role == "founding" }
}

/// One mark on the activity track — the system's work over time. Only
/// `evidence` exists today; `finding` and `action` arrive with the worker loop.
struct ActivityMark: Codable, Hashable, Sendable {
    var at: String
    var kind: String             // evidence | finding | action
}

struct LifeThread: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var title: String
    var summary: String = ""
    var origin: ThreadOrigin = .user
    var state: ThreadState = .live
    var importance: Double = 0.5
    var autonomy: String = "prepared"
    /// Who to write to when this thread needs a message. Not "who this
    /// thread is about" — most threads are about no one.
    var contactPersonId: String?
    var contactName: String?
    /// Falls back to `.prepared` on an unrecognised value rather than failing
    /// to decode — the server owns this vocabulary, same rule as `LaneState`.
    var ceiling: Autonomy { Autonomy(rawValue: autonomy) ?? .prepared }
    var deadline: DeadlineChip?
    var evidenceCount: Int = 0
    var openedAt: String
    var resolvedAt: String?
    var resolvedBy: String?
    var updatedAt: String
    // lane presentation
    /// §v2.1 — where this sits on the front page. Server-decided, same rule
    /// as `lane`: the client doesn't have the evidence to re-judge urgency.
    var tier: String = "index"
    var pressure: Double = 0
    var lane: LaneState = .live
    var subtitle: String?
    var unseen: Int = 0
    var activity: [ActivityMark] = []
    /// §v2.7 — what the system has done with this thread, decided server-side
    /// like `lane` and `tier`. The wording travels with it so the phone never
    /// owns a second copy of it.
    var status: String = "none"
    var statusLabel: String?
    /// §v3 (Loose Ends) — the reason chip: why this card sits where it does,
    /// in words. Server-decided like `tier`; `whyKind` picks the tone and
    /// `whyText` is printed verbatim.
    var whyKind: String?             // overdue|due|move|new|waited|tied
    var whyText: String?

    /// The hue behind the word. `nil` for the ordinary case, which is most
    /// threads — a label on everything is a label on nothing.
    var statusColor: Color? {
        switch status {
        case "overdue":   return Theme.alert
        case "needs_you": return Theme.needsYou
        case "queued":    return Theme.queued
        case "finished":  return Theme.finished
        default:          return nil
        }
    }

    var isResolved: Bool { state == .resolved || state == .archived }
    var rank: Tier { Tier(rawValue: tier) ?? .index }
}

/// The main view's payload: the header line plus the stack itself.
struct ThreadStack: Codable, Sendable {
    var generatedAt: String
    var running: Int
    var needsYou: Int
    var threads: [LifeThread] = []
}

/// What the worker brought back on its own.
///
/// `kind == "nothing"` is a real result, not an absence: the system looked and
/// found nothing, and hiding that would misrepresent its work. `loopRunId` is
/// always set, so the receipts can always be opened.
struct Finding: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var kind: String                 // finding | action | nothing
    var headline: String
    var body: String = ""
    var importance: Double = 0.5
    var createdAt: String
    var loopRunId: String?
    var evidence: [ThreadEvidence] = []
    // --- §v2.1: set only on a move (kind == "action") ---
    var moveKind: String?            // send | decide | gather | do
    var steps: [String] = []         // the work already done
    var needs: [String] = []         // what only you can supply
    /// Named but not staged. A move the system could identify and not prepare
    /// is worth showing, and has to read differently from one that's ready.
    var blockedReason: String?
    /// §v2.3 — false while this is the thread's current picture, true once a
    /// newer finding of the same kind replaced it. The screen leads with what
    /// is current; the rest is history, and history is a drawer.
    var superseded: Bool = false
    /// §v2.3 — what the pass verified, as data rather than prose. Legal on a
    /// finding, not just a move: research that concluded "no move yet" used to
    /// have nowhere to go but the body, so thirteen web-enabled passes
    /// produced zero links and every price the system paid for arrived as a
    /// paragraph the user had to read and re-derive.
    var facts: [ThreadFact] = []

    var isNothing: Bool { kind == "nothing" }
    var isMove: Bool { kind == "action" && moveKind != nil }
    var isBlocked: Bool { blockedReason?.isEmpty == false }
    var move: MoveKind? { moveKind.flatMap(MoveKind.init(rawValue:)) }
}

/// The four shapes a move takes. Each wants a different verb on the button,
/// because "send this" and "pay this" are not the same offer.
enum MoveKind: String, Codable, Sendable {
    case send, decide, gather, `do`

    /// What the card's primary action says. Deliberately literal — the button
    /// should describe what happens when you press it, not how the app feels
    /// about it.
    var verb: String {
        switch self {
        case .send:   return "Review & send"
        case .decide: return "Make the call"
        case .gather: return "Take a look"
        case .do:     return "Open it"
        }
    }

    var icon: String {
        switch self {
        case .send:   return "paperplane"
        case .decide: return "arrow.triangle.branch"
        case .gather: return "books.vertical"
        case .do:     return "arrow.up.forward.square"
        }
    }

    /// The one-word label on the card, so the shape is legible before reading.
    var tag: String {
        switch self {
        case .send:   return "TO SEND"
        case .decide: return "TO DECIDE"
        case .gather: return "TO READ"
        case .do:     return "TO DO"
        }
    }
}

/// "Did this close?" — the ask band, with its argument spelled out.
///
/// Auto-closes never appear here; they already happened and show as a resolved
/// lane on the stack. This is only for cases the evidence suggests but cannot
/// settle, and the reasons are the whole point: closing someone's loop for
/// them has to be defensible.
struct ThreadClosure: Codable, Identifiable, Sendable {
    var id: String
    var thread: LifeThread
    var confidence: Double
    var reasons: [String] = []
    var evidence: [ThreadEvidence] = []
    var detectedAt: String
}

/// A standing monitor the thread implied.
///
/// `timesFired == 0` is shown, not hidden: a watcher that has never fired is
/// still doing its job, and implying activity that didn't happen is the kind
/// of small dishonesty that makes a system untrustworthy.
struct Watcher: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var kind: String                 // mail | messages | calendar | deadline
    var what: String
    var everyMinutes: Int
    var until: String?
    var timesFired: Int = 0
    var lastFiredAt: String?

    /// "every 3h", "every 30m" — the cadence in the fewest characters.
    var cadenceLabel: String {
        everyMinutes % 60 == 0 ? "every \(everyMinutes / 60)h" : "every \(everyMinutes)m"
    }
}

/// A thread and everything it has claimed — the work log behind a lane.
/// One verified figure and where it came from.
struct ThreadFact: Codable, Hashable, Sendable, Identifiable {
    var label: String = ""
    var value: String = ""
    var url: String = ""
    /// The product photograph, read off the linked page's `og:image` by the
    /// backend. Never supplied by the model — asking it for one would invent a
    /// plausible url the same way it invented the product urls that started
    /// this. Empty whenever the link was sourced from mail rather than the web.
    var image: String = ""

    var id: String { label + value + url }
    var link: URL? { url.isEmpty ? nil : URL(string: url) }
    var picture: URL? { image.isEmpty ? nil : URL(string: image) }
}

/// §v2.4 — something the user told this thread after the system got it wrong.
///
/// Deliberately a sentence rather than a weight. The learned appetite behind
/// "Not this" is invisible and cannot be read back or undone; a correction is
/// the user's own words, shown on the thread, deletable, and handed to the
/// worker on every pass.
struct Correction: Codable, Hashable, Sendable, Identifiable {
    var id: String
    var statement: String
    /// `said_at` on the wire — the client decodes with `.convertFromSnakeCase`,
    /// so spelling the snake form as a CodingKey here would stop it matching.
    var saidAt: String
}

/// Why a move was turned down. One tap used to mean all three at once.
enum RejectReason: String, Codable, Sendable {
    /// The user did it themselves. The move was right; the timing wasn't.
    case handled
    /// The system misread the job. Carries the user's words; costs no appetite.
    case wrong
    /// Fewer moves of this shape here — the pre-v2.4 behaviour.
    case unwanted
}

struct ThreadDetail: Codable, Sendable {
    var thread: LifeThread
    var evidence: [ThreadEvidence] = []
    var findings: [Finding] = []
    var watchers: [Watcher] = []
    var corrections: [Correction] = []

    /// Things to act on. They lead the screen.
    var moves: [Finding] { findings.filter { $0.isMove && !$0.superseded } }

    /// Things to know, as of now. "Nothing new" is deliberately excluded — the
    /// worker logging that it looked and found nothing is honest, and it is a
    /// log entry, not five full-width rows of content.
    ///
    /// §v2.3: superseded findings are excluded too. A thread used to gain a
    /// paragraph every pass — one real thread held six findings of which five
    /// were the same observation with a fresher number — and the screen showed
    /// all of them at equal weight, so the answer was indistinguishable from
    /// the four restatements of the question.
    var notes: [Finding] { findings.filter { !$0.isMove && !$0.isNothing && !$0.superseded } }

    /// What the thread used to say. Kept, because the history is the receipt
    /// that the system was working — but behind a disclosure, because on any
    /// given morning it is not what you came for.
    var history: [Finding] { findings.filter { $0.superseded && !$0.isNothing } }

    /// The question only the user can answer, taken from the current move.
    /// This is the thing the screen is actually blocked on, and it used to sit
    /// at the bottom of a card below three paragraphs.
    var blockedOn: String? { moves.first?.needs.first }

    /// Prose the one-line rule demoted out of the move cards.
    ///
    /// Not discarded — the worker found it, and it is usually the reasoning
    /// behind the options rather than filler. It just belongs under "What I
    /// found", where a reader expects sentences, instead of inside a card
    /// they are trying to scan.
    var carriedProse: [String] { moves.flatMap(\.demotedProse) }

    /// True when the thread has been worked and the honest answer is that
    /// there is nothing to do yet. Six of thirteen live threads sit here, and
    /// until v2.4 the screen said so by simply ending.
    var hasNoMove: Bool { moves.isEmpty && !notes.isEmpty }

    /// The worker's own record, as one line at the foot of the screen.
    var lastChecked: String? {
        guard let latest = findings.filter(\.isNothing).first else { return nil }
        return "Last checked \(Self.ago(latest.createdAt)) · nothing new"
    }

    private static func ago(_ iso: String) -> String {
        guard let date = ISO8601DateFormatter.lifeline.date(from: iso) else { return "recently" }
        let mins = Int(Date().timeIntervalSince(date) / 60)
        if mins < 60 { return "\(max(1, mins))m ago" }
        if mins < 1440 { return "\(mins / 60)h ago" }
        return "\(mins / 1440)d ago"
    }
}

/// A reply written for a thread at write-time — or a refusal.
///
/// `draft` is nil when the writer decided a message isn't the work. A bill
/// gets paid and a booking gets checked; neither is answered by writing to a
/// no-reply address, and saying so is more useful than a draft nobody can
/// send. `reason` carries what to do instead.
/// Reuses `MessageDraft` from the converse path — same wire shape, and the
/// review-and-send affordance should be one thing, not two.
struct ThreadDraft: Codable, Sendable {
    var threadId: String
    var draft: MessageDraft?
    var reason: String = ""
    var trace: [TraceStep] = []
}

// MARK: - display helpers

extension LifeThread {
    /// The short due chip on the lane ("Fri", "due 12th", "Aug 31"). Deliberately
    /// terse — the lane has one line and the title owns it.
    var dueLabel: String? {
        guard let raw = deadline?.date, let date = ISO8601DateFormatter.lifeline.date(from: raw)
        else { return nil }
        let calendar = Calendar.current
        let days = calendar.dateComponents([.day], from: Date(), to: date).day ?? 0
        // A date in another year must say so. "Aug 3" for a 2027 deadline
        // rendered as if it were this week on the first run — the chip is the
        // only place that date appears on the stack, so it has to be complete.
        let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: Date())
        let formatter = sameYear ? Self.shortDate : Self.dateWithYear
        if days < 0 { return "was \(formatter.string(from: date))" }
        if days == 0 { return "today" }
        if days == 1 { return "tomorrow" }
        if days < 7 && sameYear { return Self.weekday.string(from: date) }
        return formatter.string(from: date)
    }

    /// True when the date has already gone by. The lane strikes it through
    /// rather than hiding it: it's real, it just isn't pressure any more.
    var deadlinePassed: Bool {
        guard let raw = deadline?.date, let date = ISO8601DateFormatter.lifeline.date(from: raw)
        else { return false }
        return date < Date()
    }

    private static let weekday: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "EEE"; return f
    }()

    private static let shortDate: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "MMM d"; return f
    }()

    private static let dateWithYear: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "MMM d ''yy"; return f
    }()
}

/// What `POST /devices` answers with.
struct DeviceRegistration: Codable, Sendable {
    var registered: Bool
    var devices: Int
}

/// Someone the user actually talks to — a candidate for a thread's contact.
struct Person: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var displayName: String
    var handle: String?
    var messageCount: Int = 0
}

/// Where a thread sits on the front page. A briefing has a lead and it has
/// briefs; the old stack had nine equal shouts and a 3pt colour stripe.
enum Tier: String, Codable, Sendable {
    case lead, brief, index, quiet, closed

    /// The section a tier files under. `lead` has no band — it *is* the top of
    /// the page, and labelling it would be explaining the joke.
    var band: String? {
        switch self {
        case .lead:   return nil
        case .brief:  return "This week"
        // §v3 — was "Waiting on you", which promised urgency exactly where
        // there was least: this is the *lowest*-pressure band. "Needs you"
        // now lives only in the header count, where it's true.
        case .index:  return "The rest"
        case .quiet:  return "Quiet"
        case .closed: return nil
        }
    }
}

/// How a move presents itself. The worker writes for precision; these turn
/// that into something a person reads in one glance.
extension Finding {
    /// One staged fact. `Past due · $153.52` reads as a record; the same
    /// content as loose prose reads as a paragraph you have to parse.
    struct Step {
        let label: String?
        let value: String
    }

    /// Steps with URLs stripped and `label: value` split out.
    ///
    /// A raw payment URL wrapped over three lines mid-token — it destroyed the
    /// rag and told the reader nothing. The link isn't content; it's where the
    /// button goes.
    var displaySteps: [Step] {
        finding_steps().compactMap { raw in
            let cleaned = raw.replacingOccurrences(
                of: #"https?://\S+"#, with: "", options: .regularExpression
            ).trimmingCharacters(in: .whitespaces)

            // A step that was only a link leaves a label pointing at nothing
            // once the URL is pulled out — "Direct payment link:" on its own
            // row. The link became the button; the row goes with it. Checked
            // before the colon is trimmed, or there's nothing left to detect.
            if cleaned.hasSuffix(":") || cleaned.isEmpty { return nil }

            let trimmed = cleaned.trimmingCharacters(in: CharacterSet(charactersIn: " :·-—"))
            guard !trimmed.isEmpty else { return nil }

            // Split on the first colon when the left side is short enough to
            // read as a label rather than a clause.
            if let colon = trimmed.firstIndex(of: ":") {
                let key = String(trimmed[trimmed.startIndex..<colon]).trimmingCharacters(in: .whitespaces)
                let val = String(trimmed[trimmed.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
                // "Direct payment link:" with the URL stripped is a label
                // pointing at nothing. The link became the button; the row goes.
                if val.isEmpty { return nil }
                if key.count <= 22 { return Step(label: key, value: val) }
            }
            return Step(label: nil, value: trimmed)
        }
    }

    private func finding_steps() -> [String] { steps }

    // §v2.4 — the one-line rule.
    //
    // A move card is scanned, not read. `steps` and `facts` render as the same
    // thing on screen and are merged here into one list of rows, and a step
    // that cannot be reduced to a line stops being a row at all: it is prose,
    // and prose belongs under "What I found".
    //
    // The thresholds come from measuring the screen that started this. Of five
    // prose steps on the pajamas card exactly one made it through the
    // `label: value` split — "Target/Cat & Jack", whose 70-character value then
    // wrapped to three lines and read as a paragraph with a heading. A label
    // buys about 92pt of the row, so a labelled value gets less room than a
    // bare one, not more.
    private static let labelledRowMax = 48
    private static let plainRowMax = 60

    struct OptionRow: Identifiable {
        let id: String
        let label: String?
        let value: String
        let link: URL?
        /// Present only on rows that came from a fact with a fetched page.
        /// A step-derived row has no page behind it and stays text.
        var picture: URL? = nil
    }

    /// Everything the card renders as a row. Facts lead: they carry a real
    /// price and a real link, which is the whole difference between research
    /// and a report about research.
    var optionRows: [OptionRow] {
        var rows = facts.enumerated().compactMap { index, fact -> OptionRow? in
            let label = fact.label.trimmingCharacters(in: .whitespaces)
            let value = fact.value.trimmingCharacters(in: .whitespaces)
            guard !label.isEmpty || !value.isEmpty else { return nil }
            return OptionRow(id: "fact-\(index)",
                             label: label.isEmpty ? nil : label,
                             value: value,
                             link: fact.link,
                             picture: fact.picture)
        }
        for (index, step) in displaySteps.enumerated() where Self.fitsOneLine(step) {
            rows.append(OptionRow(id: "step-\(index)",
                                  label: step.label,
                                  value: step.value,
                                  link: nil))
        }
        return rows
    }

    /// Steps too long to be a row. Kept rather than dropped — the worker found
    /// this and it is often the reasoning behind the options — but shown below
    /// the card as prose, where prose is what the reader expects.
    var overflowNotes: [String] {
        displaySteps.filter { !Self.fitsOneLine($0) }.map { step in
            guard let label = step.label else { return step.value }
            return "\(label): \(step.value)"
        }
    }

    /// A `send` move's drafted message.
    ///
    /// The one-line rule would demote this as prose — it is a sentence, and a
    /// long one. But on a `send` the draft *is* the staged work, and demoting
    /// it left the card showing a headline, a deck and nothing else while the
    /// message the user was being asked to approve sat two sections below
    /// under "What I found". It stays, and renders as a quotation rather than
    /// as a row, because that is what it is.
    var draftBody: String? {
        guard move == .send else { return nil }
        return overflowNotes.first
    }

    /// Overflow that genuinely belongs below the card — the draft excepted.
    var demotedProse: [String] {
        guard move == .send else { return overflowNotes }
        return Array(overflowNotes.dropFirst())
    }

    /// Can this be read in one glance, on one line?
    static func fitsOneLine(_ step: Step) -> Bool {
        step.value.count <= (step.label == nil ? plainRowMax : labelledRowMax)
    }

    /// The first link among the staged steps — the button's destination.
    var stagedLink: URL? {
        for step in steps {
            guard let detector = try? NSDataDetector(
                types: NSTextCheckingResult.CheckingType.link.rawValue) else { return nil }
            let range = NSRange(step.startIndex..., in: step)
            if let match = detector.firstMatch(in: step, range: range), let url = match.url {
                return url
            }
        }
        return nil
    }

    /// The headline, cut at the first parenthetical or em dash. The worker
    /// writes "Pay $385.40 ($153.52 past due, $231.88 due Aug 31) on the
    /// American Water portal before trip to San Juan" — accurate, and two
    /// lines of display serif with two parentheticals in it.
    var leadLine: String {
        let cut = headline.split(separator: "(", maxSplits: 1).first.map(String.init) ?? headline
        return cut.split(separator: "—", maxSplits: 1).first.map(String.init)?
            .trimmingCharacters(in: .whitespaces) ?? headline
    }

    /// What the headline gave up, kept as a deck underneath it.
    var deckLine: String? {
        let rest = headline.dropFirst(leadLine.count).trimmingCharacters(
            in: CharacterSet(charactersIn: " ()—-"))
        let cleaned = rest.replacingOccurrences(of: ")", with: "")
        return cleaned.isEmpty ? (body.isEmpty ? nil : body) : cleaned
    }

    /// A staged value, with any parenthetical dropped — the deck above the
    /// facts has already said it once, and saying it twice in a five-line card
    /// is how a move stops looking scannable.
    static func tightened(_ value: String) -> String {
        let cut = value.split(separator: "(", maxSplits: 1).first.map(String.init) ?? value
        return cut.trimmingCharacters(in: .whitespaces)
    }

    /// Where the verb goes, if anywhere.
    ///
    /// `facts` first, because that is where a verified figure and its url now
    /// belong; `steps` second, because that is where the worker put links
    /// before `facts` existed and still does. Checking only the steps was how a
    /// `decide` move with four perfectly linked options found no link at all
    /// and fell through to opening a message draft — the app answering a
    /// shopping decision by offering to text somebody about it.
    var actionLink: URL? {
        if let fromFacts = facts.lazy.compactMap(\.link).first { return fromFacts }
        return Self.detectLink(in: steps)
    }

    /// Nothing to open and nothing to send: the card itself is the whole move.
    /// A verb here would be a button that visibly does nothing, which is worse
    /// than no verb at all.
    var hasSomewhereToGo: Bool { move == .send || actionLink != nil }

    static func detectLink(in steps: [String]) -> URL? {
        let detector = try? NSDataDetector(
            types: NSTextCheckingResult.CheckingType.link.rawValue
        )
        for step in steps {
            let range = NSRange(step.startIndex..., in: step)
            if let match = detector?.firstMatch(in: step, range: range),
               let url = match.url {
                return url
            }
        }
        return nil
    }

    /// The button names the outcome, not the mechanism. "Open it" told the
    /// user nothing about what they were about to do.
    func actionLabel(default fallback: String) -> String {
        guard move == .do else { return fallback }
        if let amount = leadLine.firstMatch(of: /\$[\d,]+\.?\d*/) {
            return "Pay \(String(amount.output))"
        }
        return fallback
    }
}

extension LifeThread {
    /// `Aug 31 · 1 source` — the whole header in one line.
    var metaSummary: String {
        var parts: [String] = []
        if let due = dueLabel {
            parts.append(deadlinePassed ? "Was due \(due)" : "Due \(due)")
        }
        if evidenceCount > 0 {
            parts.append("\(evidenceCount) source\(evidenceCount == 1 ? "" : "s")")
        }
        if let by = resolvedBy { parts.append("closed by \(by)") }
        return parts.isEmpty ? "Just opened" : parts.joined(separator: " · ")
    }
}
