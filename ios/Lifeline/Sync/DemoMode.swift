import Foundation

/// §v3 workstream 6 — demo mode.
///
/// App Review will not set up a Mac, and neither will someone deciding in a
/// store aisle whether this app is for them. Demo mode is the whole app
/// running against a crafted world instead of an engine: same screens, same
/// verbs, same wire format — the requests just never leave the phone.
///
/// The world lives here, in the same fictional universe as the engine's
/// sample corpus (Alex Carter's family), so the demo and the backend's
/// `lifeline demo` tell one story. Leaving demo is pairing: the moment a
/// real engine hands over a token, the crafted world steps aside.
enum DemoMode {
    static let key = "LifelineDemoMode"

    static var active: Bool { UserDefaults.standard.bool(forKey: key) }
    static func enter() { UserDefaults.standard.set(true, forKey: key) }
    static func leave() { UserDefaults.standard.removeObject(forKey: key) }
}

/// Answers API calls from the crafted world. `respond` returns wire-format
/// JSON — encoded the way the server writes it — or nil for routes the demo
/// doesn't stage, which the client turns into the same offline-safe failure
/// every screen already survives.
actor DemoEngine {
    static let shared = DemoEngine()

    private var stack: ThreadStack
    private var details: [String: ThreadDetail]
    private var closures: [ThreadClosure]
    private var proposals: [LifeThread]

    /// Matches the server's writing side: snake_case keys, mirroring the
    /// decoder's `convertFromSnakeCase` on the reading side.
    private let encoder: JSONEncoder

    init() {
        let world = DemoWorld.make()
        stack = world.stack
        details = world.details
        closures = world.closures
        proposals = world.proposals
        encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
    }

    // MARK: - routing

    func respond(_ method: String, _ path: String, body: Data?) -> Data? {
        let parts = path.split(separator: "/").map(String.init)
        switch (method, parts.first, parts.count) {
        case ("GET", "threads", 1):
            return encode(recounted())
        case ("GET", "threads", 2) where parts[1] == "closures":
            return encode(closures)
        case ("POST", "threads", 4) where parts[1] == "closures":
            return answerClosure(id: parts[2], confirm: parts[3] == "confirm")
        case ("GET", "threads", 2):
            return details[parts[1]].flatMap(encode)
        case ("POST", "threads", 1):
            return create(from: body)
        case ("POST", "threads", 3):
            return verb(parts[2], on: parts[1])
        case ("GET", "proposals", 1):
            return encode(proposals)
        case ("POST", "proposals", 3):
            return answerProposal(id: parts[1], accept: parts[2] == "accept")
        case ("POST", "devices", 1):
            return encode(DeviceRegistration(registered: true, devices: 1))
        case ("POST", "calendar", 2):
            return encode(CalendarSyncResponse(stored: 0))
        case ("GET", "transport", 1):
            return #"{"urls": [], "port": 0, "service_type": "_loose-ends._tcp"}"#
                .data(using: .utf8)
        case ("POST", "ask", 1):
            return DemoWorld.askJSON
        default:
            return nil
        }
    }

    // MARK: - the verbs

    private func verb(_ verb: String, on id: String) -> Data? {
        guard let index = stack.threads.firstIndex(where: { $0.id == id }) else { return nil }
        switch verb {
        case "resolve":
            stack.threads[index].state = .resolved
            stack.threads[index].lane = .done
            stack.threads[index].resolvedBy = "user"
        case "quiet":
            stack.threads[index].state = .quiet
            stack.threads[index].lane = .idle
        case "dig-in":
            stack.threads[index].importance = min(1.0, stack.threads[index].importance + 0.25)
            if stack.threads[index].state == .quiet { stack.threads[index].state = .live }
        case "seen":
            stack.threads[index].unseen = 0
        default:
            return nil
        }
        syncDetail(id)
        return encode(stack.threads[index])
    }

    private func create(from body: Data?) -> Data? {
        struct In: Decodable { let title: String; let summary: String? }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let body, let input = try? decoder.decode(In.self, from: body) else { return nil }
        let thread = DemoWorld.declared(title: input.title, summary: input.summary ?? "")
        stack.threads.insert(thread, at: 0)
        details[thread.id] = ThreadDetail(thread: thread)
        return encode(thread)
    }

    private func answerClosure(id: String, confirm: Bool) -> Data? {
        guard let closure = closures.first(where: { $0.id == id }) else { return nil }
        closures.removeAll { $0.id == id }
        if confirm, let index = stack.threads.firstIndex(where: { $0.id == closure.thread.id }) {
            stack.threads[index].state = .resolved
            stack.threads[index].lane = .done
            stack.threads[index].resolvedBy = "system"
            syncDetail(closure.thread.id)
        }
        return encode(closure.thread)
    }

    private func answerProposal(id: String, accept: Bool) -> Data? {
        guard let proposal = proposals.first(where: { $0.id == id }) else { return nil }
        proposals.removeAll { $0.id == id }
        if accept {
            var accepted = proposal
            accepted.state = .live
            accepted.lane = .live
            stack.threads.append(accepted)
            details[accepted.id] = ThreadDetail(thread: accepted)
        }
        return encode(proposal)
    }

    // MARK: - plumbing

    /// The detail screen must agree with the lane it was opened from.
    private func syncDetail(_ id: String) {
        guard var detail = details[id],
              let thread = stack.threads.first(where: { $0.id == id }) else { return }
        detail.thread = thread
        details[id] = detail
    }

    private func recounted() -> ThreadStack {
        var s = stack
        s.running = s.threads.filter { !$0.isResolved }.count
        s.needsYou = s.threads.filter { !$0.isResolved && ($0.lane == .hot || $0.unseen > 0) }.count
        return s
    }

    private func encode<T: Encodable>(_ value: T) -> Data? {
        try? encoder.encode(value)
    }
}
