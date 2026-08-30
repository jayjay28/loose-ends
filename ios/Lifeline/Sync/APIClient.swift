import Foundation

/// Thin async wrapper over the backend's HTTP API (§9). The backend owns
/// OAuth, polling, and the learned model; this client only ever talks to
/// `api/app.py`'s routes.
extension Notification.Name {
    /// §v3 — posted on any 401: the engine no longer knows this phone.
    static let pairingRequired = Notification.Name("LoosePairingRequired")
}

actor APIClient {
    static let shared = APIClient()

    private let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    init(baseURL: URL = APIClient.defaultBaseURL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session

        decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase

        encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
    }

    /// Resolves the backend address, most specific first:
    ///   1. a `LifelineAPIBaseURL` UserDefaults override (a Settings screen or
    ///      `-LifelineAPIBaseURL <url>` launch arg can set this at runtime),
    ///   2. the `LifelineAPIBaseURL` baked into Info.plist (the dev machine's
    ///      LAN address, so a physical phone on the same Wi-Fi can reach it),
    ///   3. localhost, for the simulator with no configuration.
    static var defaultBaseURL: URL {
        if let raw = UserDefaults.standard.string(forKey: "LifelineAPIBaseURL"),
           let url = URL(string: raw) {
            return url
        }
        if let raw = Bundle.main.object(forInfoDictionaryKey: "LifelineAPIBaseURL") as? String,
           let url = URL(string: raw) {
            return url
        }
        return URL(string: "http://127.0.0.1:8000")!
    }

    enum APIError: Error, LocalizedError {
        case http(status: Int, body: String)
        case decoding(Error)

        var errorDescription: String? {
            switch self {
            case .http(let status, let body):
                return "server returned \(status): \(body)"
            case .decoding(let error):
                return "failed to decode response: \(error)"
            }
        }
    }

    // MARK: - low-level request building

    /// Builds the URL via `URLComponents` rather than string interpolation —
    /// an ISO-8601 offset like `+00:00` corrupts to a space if it's ever
    /// concatenated into a raw query string by hand. See PLAN.md's Phase 1
    /// notes for the backend-side version of this exact bug.
    private func url(_ path: String, query: [String: String?] = [:]) -> URL {
        var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        let items = query.compactMapValues { $0 }.map { URLQueryItem(name: $0.key, value: $0.value) }
        components.queryItems = items.isEmpty ? nil : items
        return components.url!
    }

    private func send<Response: Decodable>(
        _ method: String,
        _ path: String,
        query: [String: String?] = [:],
        body: Encodable? = nil,
        timeout: TimeInterval? = nil
    ) async throws -> Response {
        var request = URLRequest(url: url(path, query: query))
        request.httpMethod = method
        // The ask loop legitimately runs past URLSession's 60s default —
        // eight tool iterations against two sources is slow, not stuck.
        if let timeout { request.timeoutInterval = timeout }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try encoder.encode(AnyEncodable(body))
        }
        // §v3 — the phone's identity. The engine trusts exactly two callers:
        // itself, and a device carrying a token a pairing minted.
        if let token = TokenStore.token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw APIError.http(status: -1, body: "no HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            if http.statusCode == 401 {
                // The engine no longer knows this phone. Every screen that
                // talks to the API funnels through here, so this is the one
                // place that can say "go pair" — the UI listens and presents.
                NotificationCenter.default.post(name: .pairingRequired, object: nil)
            }
            throw APIError.http(status: http.statusCode, body: String(data: data, encoding: .utf8) ?? "")
        }
        do {
            return try decoder.decode(Response.self, from: data)
        } catch {
            throw APIError.decoding(error)
        }
    }

    // MARK: - pairing (§v3)

    /// Spend a pairing code for this phone's bearer token. The claim itself
    /// is the API's one open door, so it works before any token exists.
    func pair(code: String, deviceName: String) async throws {
        struct Body: Encodable { let code: String; let deviceName: String }
        struct Response: Decodable { let token: String }
        let response: Response = try await send(
            "POST", "/pair/claim", body: Body(code: code, deviceName: deviceName))
        TokenStore.save(response.token)
    }

    // MARK: - views (§8.2)

    func today(at: Date? = nil) async throws -> Today {
        var query: [String: String?] = [:]
        if let at {
            query["at"] = ISO8601DateFormatter.lifeline.string(from: at)
        }
        return try await send("GET", "/today", query: query)
    }

    @discardableResult
    func syncCalendar(events: [CalendarEventPayload]) async throws -> CalendarSyncResponse {
        struct Body: Encodable { let events: [CalendarEventPayload] }
        return try await send("POST", "/calendar/events", body: Body(events: events))
    }

    func briefing(at: Date? = nil) async throws -> Briefing {
        var query: [String: String?] = [:]
        if let at {
            query["at"] = ISO8601DateFormatter.lifeline.string(from: at)
        }
        return try await send("GET", "/briefing", query: query)
    }

    /// The triage deck — ranked open items, one card at a time. With
    /// `includeSnoozed` the stack's contents come back too, so the deck can
    /// rebuild its stack across launches.
    func queue(includeSnoozed: Bool = false) async throws -> [Item] {
        try await send("GET", "/queue",
                       query: includeSnoozed ? ["include_snoozed": "true"] : [:])
    }

    func conversations() async throws -> [ConversationSummary] {
        try await send("GET", "/conversations")
    }

    func conversationItems(personId: String) async throws -> [Item] {
        try await send("GET", "/conversations/\(personId)")
    }

    func history(limit: Int = 100) async throws -> History {
        try await send("GET", "/history", query: ["limit": String(limit)])
    }

    func item(id: String) async throws -> Item {
        try await send("GET", "/items/\(id)")
    }

    /// The context engine's grounded headline + briefing for an item — loaded
    /// when it's shown/opened, so the title reflects what the engine found.
    func itemEnriched(itemId: String) async throws -> Enriched {
        try await send("GET", "/items/\(itemId)/enriched")
    }

    /// The receipts behind an item — why it surfaced, the evidence, your last
    /// words in the conversation. Loaded when the card is flipped over.
    func itemDossier(itemId: String) async throws -> Dossier {
        try await send("GET", "/items/\(itemId)/dossier")
    }

    /// The few messages surrounding a surfaced item — loaded lazily when a card
    /// is expanded, so the item reads with its conversation, not alone.
    func itemContext(itemId: String, before: Int = 4, after: Int = 2) async throws -> ConversationContext {
        try await send("GET", "/items/\(itemId)/context",
                       query: ["before": String(before), "after": String(after)])
    }

    // MARK: - threads (§v2)

    /// **The main view.** What you're carrying, plus what you just put down —
    /// resolved threads ride along for a day so the pile is seen to shrink.
    func threadStack() async throws -> ThreadStack {
        try await send("GET", "/threads")
    }

    /// A thread and everything it has claimed — the work log behind a lane.
    func thread(id: String) async throws -> ThreadDetail {
        try await send("GET", "/threads/\(id)")
    }

    /// Declare a thread. The primary capture verb of v2: the user saying
    /// "this is on my mind" without waiting for a message to imply it.
    func createThread(title: String, summary: String = "", deadline: Date? = nil,
                      contactPersonId: String? = nil) async throws -> LifeThread {
        struct Body: Encodable {
            let title: String
            let summary: String
            let deadline: String?
            let contactPersonId: String?
        }
        return try await send("POST", "/threads", body: Body(
            title: title,
            summary: summary,
            deadline: deadline.map { ISO8601DateFormatter.lifeline.string(from: $0) },
            contactPersonId: contactPersonId
        ))
    }

    /// The swipe verbs. Resolve has its own route because *who* closed a loop
    /// is part of the record; quiet and dig-in are a state and an importance.
    @discardableResult
    func resolveThread(id: String) async throws -> LifeThread {
        try await send("POST", "/threads/\(id)/resolve")
    }

    /// ← Quiet and ↑ Dig in have their own routes, not a PATCH: the swipe is
    /// the training signal, and a PATCH body carrying both importance and
    /// state is ambiguous about which verb was meant.
    @discardableResult
    func quietThread(id: String) async throws -> LifeThread {
        try await send("POST", "/threads/\(id)/quiet")
    }

    @discardableResult
    func digInThread(id: String) async throws -> LifeThread {
        try await send("POST", "/threads/\(id)/dig-in")
    }

    @discardableResult
    func updateThread(
        id: String,
        title: String? = nil,
        summary: String? = nil,
        state: String? = nil,
        importance: Double? = nil
    ) async throws -> LifeThread {
        struct Body: Encodable {
            let title: String?
            let summary: String?
            let state: String?
            let importance: Double?
        }
        return try await send("PATCH", "/threads/\(id)",
                              body: Body(title: title, summary: summary,
                                         state: state, importance: importance))
    }

    /// The user opened it, so nothing in it is new any more.
    @discardableResult
    func markThreadSeen(id: String) async throws -> LifeThread {
        try await send("POST", "/threads/\(id)/seen")
    }

    /// People the user actually talks to, busiest first — an A-Z list of
    /// every handle that ever texted is mostly robots.
    func people(matching query: String = "") async throws -> [Person] {
        try await send("GET", "/people", query: ["q": query.isEmpty ? nil : query])
    }

    /// Who to write to when this thread needs a message. `nil` clears it.
    @discardableResult
    func setContact(id: String, personId: String?) async throws -> LifeThread {
        struct Body: Encodable { let personId: String? }
        return try await send("POST", "/threads/\(id)/contact", body: Body(personId: personId))
    }

    /// "Not this" — and which kind of no.
    ///
    /// `reason` is what stops one tap from meaning three things. Only
    /// `.unwanted` narrows what the worker may propose here; `.wrong` records
    /// the user's words against the thread and leaves the appetite alone, and
    /// `.handled` closes the thread and teaches nothing at all.
    @discardableResult
    func rejectMove(threadId: String, findingId: String,
                    reason: RejectReason = .unwanted,
                    text: String? = nil) async throws -> String? {
        struct Body: Encodable { let reason: String; let text: String? }
        struct Ack: Decodable { let rejected: String; let correctionId: String? }
        let ack: Ack = try await send(
            "POST", "/threads/\(threadId)/findings/\(findingId)/reject",
            body: Body(reason: reason.rawValue, text: text)
        )
        return ack.correctionId
    }

    /// Tell a thread what the system got wrong, outside the reject sheet.
    @discardableResult
    func addCorrection(threadId: String, statement: String) async throws -> Correction {
        struct Body: Encodable { let statement: String }
        return try await send("POST", "/threads/\(threadId)/corrections",
                              body: Body(statement: statement))
    }

    /// Take one back. Soft-deleted server-side.
    func dropCorrection(threadId: String, correctionId: String) async throws {
        struct Ack: Decodable { let dropped: String }
        let _: Ack = try await send("DELETE", "/threads/\(threadId)/corrections/\(correctionId)")
    }

    /// Work this thread again now, so the user watches their correction land
    /// instead of waiting for the next scheduled pass.
    func rework(threadId: String) async throws {
        struct Ack: Decodable { let threadId: String }
        let _: Ack = try await send("POST", "/threads/\(threadId)/rework")
    }

    /// Recorded when the user acts on a staged move, so appetite is not a
    /// ratchet that only ever falls.
    func acceptMove(threadId: String, findingId: String) async throws {
        struct Ack: Decodable { let accepted: String }
        let _: Ack = try await send("POST", "/threads/\(threadId)/findings/\(findingId)/accept")
    }

    /// Hands APNs the address to push to. Without this the `devices` table
    /// stays empty and every send is a no-op with nothing to send to.
    @discardableResult
    func registerDevice(token: String, platform: String = "ios") async throws -> DeviceRegistration {
        struct Body: Encodable { let token: String; let platform: String }
        return try await send("POST", "/devices", body: Body(token: token, platform: platform))
    }

    /// How far this thread may go on its own. Only the user raises a ceiling.
    @discardableResult
    func setAutonomy(id: String, ceiling: Autonomy) async throws -> LifeThread {
        struct Body: Encodable { let ceiling: String }
        return try await send("POST", "/threads/\(id)/autonomy", body: Body(ceiling: ceiling.rawValue))
    }

    /// A user-set deadline — always wins over anything the system inferred.
    @discardableResult
    func setThreadDeadline(id: String, date: Date?, reason: String? = nil) async throws -> LifeThread {
        struct Body: Encodable {
            let date: String?
            let source: String
            let reason: String?
        }
        return try await send("POST", "/threads/\(id)/deadline", body: Body(
            date: date.map { ISO8601DateFormatter.lifeline.string(from: $0) },
            source: "user",
            reason: reason
        ))
    }

    /// Write a reply for this thread **now**, with everything it knows.
    /// May come back with no draft: plenty of threads are real work whose work
    /// isn't a message, and `reason` then says what to do instead.
    func draftForThread(id: String) async throws -> ThreadDraft {
        try await send("POST", "/threads/\(id)/draft")
    }

    /// Promote a surfaced item into a loop you're carrying. The item survives
    /// as the thread's founding evidence.
    func promoteItem(itemId: String, title: String? = nil) async throws -> ThreadDetail {
        struct Body: Encodable { let title: String?; let summary: String? }
        return try await send("POST", "/items/\(itemId)/promote",
                              body: Body(title: title, summary: nil))
    }

    /// Retire a monitor. The user gets the last word on what the system keeps
    /// an eye on — an autonomous watch nobody can switch off is surveillance.
    @discardableResult
    func stopWatcher(threadId: String, watcherId: String) async throws -> ThreadDetail {
        try await send("DELETE", "/threads/\(threadId)/watchers/\(watcherId)")
    }

    // MARK: - closing the loop (§v2 step 5)

    /// Threads the evidence suggests are finished but can't settle alone.
    func threadClosures() async throws -> [ThreadClosure] {
        try await send("GET", "/threads/closures")
    }

    @discardableResult
    func confirmClosure(id: String) async throws -> LifeThread {
        try await send("POST", "/threads/closures/\(id)/confirm")
    }

    @discardableResult
    func rejectClosure(id: String) async throws -> LifeThread {
        try await send("POST", "/threads/closures/\(id)/reject")
    }

    // MARK: - proposals (§v2 — never in the main stack)

    func proposals() async throws -> [LifeThread] {
        try await send("GET", "/proposals")
    }

    @discardableResult
    func acceptProposal(id: String) async throws -> LifeThread {
        try await send("POST", "/proposals/\(id)/accept")
    }

    @discardableResult
    func dismissProposal(id: String) async throws -> LifeThread {
        try await send("POST", "/proposals/\(id)/dismiss")
    }

    // MARK: - the model of you (§v1.4)

    /// Tell the assistant something about your life; it extracts durable facts.
    func tell(_ text: String) async throws -> TellResponse {
        struct Body: Encodable { let text: String }
        return try await send("POST", "/tell", body: Body(text: text))
    }

    /// §v1.5 — the one conversational door. The loop decides whether this was
    /// a tell (facts back) or an ask (answer + the tool trace as receipts).
    /// Pass `sessionId` to continue a conversation so pronouns resolve.
    // MARK: - ask (§v2.9 — answer cards)

    func ask(_ question: String) async throws -> AskCard {
        struct Body: Encodable { let question: String }
        return try await send("POST", "/ask", body: Body(question: question), timeout: 150)
    }

    func askHistory(limit: Int = 20) async throws -> [AskCard] {
        try await send("GET", "/asks", query: ["limit": String(limit)])
    }

    func messageInFull(id: String) async throws -> MessageInFull {
        try await send("GET", "/messages/\(id)")
    }

    /// The "wrong?" door: forget retires a fact, correct replaces it with the
    /// user's words at full confidence. The user's word beats the model's.
    func correctFact(id: String, action: String, value: String? = nil) async throws {
        struct Body: Encodable { let action: String; let value: String? }
        struct Ack: Decodable { let status: String }
        let _: Ack = try await send("POST", "/world/facts/\(id)",
                                    body: Body(action: action, value: value))
    }

    func converse(_ text: String, sessionId: String? = nil) async throws -> ConverseResponse {
        struct Body: Encodable { let text: String; let sessionId: String? }
        return try await send("POST", "/converse", body: Body(text: text, sessionId: sessionId))
    }

    /// A conversation's transcript, so the page reopens where you left off.
    func conversation(sessionId: String) async throws -> Conversation {
        try await send("GET", "/converse/\(sessionId)")
    }

    /// Everything the system believes about you, grouped and editable.
    func modelOfYou() async throws -> ModelOfYou {
        try await send("GET", "/model")
    }

    /// A correction from the user — becomes the authoritative version.
    @discardableResult
    func editFact(id: String, statement: String) async throws -> FactItem {
        struct Body: Encodable { let statement: String }
        return try await send("PATCH", "/facts/\(id)", body: Body(statement: statement))
    }

    /// Soft delete — the dismissal itself is signal.
    @discardableResult
    func dismissFact(id: String) async throws -> FactItem {
        try await send("DELETE", "/facts/\(id)")
    }

    // MARK: - Phase D — one reply clears many

    /// Fold the staged items owed to one person into a single reply.
    func draftBatch(personId: String, itemIds: [String]) async throws -> DraftBatch {
        try await send("POST", "/conversations/\(personId)/draft", body: BatchRequest(itemIds: itemIds))
    }

    /// Sending the reply clears every item it covered.
    @discardableResult
    func batchDone(itemIds: [String]) async throws -> BatchDone {
        try await send("POST", "/items/batch/done", body: BatchRequest(itemIds: itemIds))
    }

    // MARK: - actions (§8.3)

    func markViewed(itemId: String, expanded: Bool = false) async throws -> ActionResult {
        try await send("POST", "/items/\(itemId)/view", query: ["expanded": expanded ? "true" : "false"])
    }

    func markActed(itemId: String) async throws -> ActionResult {
        try await send("POST", "/items/\(itemId)/act")
    }

    func markDone(itemId: String) async throws -> ActionResult {
        try await send("POST", "/items/\(itemId)/done")
    }

    func snooze(itemId: String, hours: Double? = nil, until: Date? = nil) async throws -> ActionResult {
        let untilString = until.map { ISO8601DateFormatter.lifeline.string(from: $0) }
        return try await send("POST", "/items/\(itemId)/snooze", body: SnoozeRequest(hours: hours, until: untilString))
    }

    func dismiss(itemId: String) async throws -> ActionResult {
        try await send("POST", "/items/\(itemId)/dismiss")
    }

    // MARK: - confirmations (§7, milestone 8)

    func confirmations() async throws -> [Confirmation] {
        try await send("GET", "/confirmations")
    }

    func confirmMatch(signalId: String) async throws -> ActionResult {
        try await send("POST", "/confirmations/\(signalId)/confirm")
    }

    func rejectMatch(signalId: String) async throws -> ActionResult {
        try await send("POST", "/confirmations/\(signalId)/reject")
    }

    // MARK: - sync

    func syncChanges(since: Date?) async throws -> SyncChanges {
        var query: [String: String?] = [:]
        if let since {
            query["since"] = ISO8601DateFormatter.lifeline.string(from: since)
        }
        return try await send("GET", "/sync/changes", query: query)
    }

    // MARK: - devices (§8.4)

    @discardableResult
    func registerDevice(token: String) async throws -> DeviceRegistrationResponse {
        try await send("POST", "/devices", body: DeviceRequest(token: token))
    }

    func health() async throws -> HealthResponse {
        try await send("GET", "/health")
    }
}

// MARK: - helpers

struct DeviceRegistrationResponse: Decodable {
    var registered: Bool
    var devices: Int
}

struct HealthResponse: Decodable {
    var ok: Bool
    var version: String
    var googleConnected: Bool
    var claudeConfigured: Bool
    var apnsConfigured: Bool
    var openItems: Int
}

/// `Encodable` existentials can't be encoded directly — this box makes the
/// generic `send(body:)` call site take a plain `Encodable` argument.
private struct AnyEncodable: Encodable {
    private let encodeFunc: (Encoder) throws -> Void

    init(_ wrapped: Encodable) {
        encodeFunc = wrapped.encode
    }

    func encode(to encoder: Encoder) throws {
        try encodeFunc(encoder)
    }
}
