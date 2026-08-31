import Foundation

/// §v3 ws6 — the crafted world demo mode runs on.
///
/// Six threads that between them show every lane, both card shapes (a move
/// and a note), a deadline chip, a watcher, a closure question, and a
/// proposal — the product's whole vocabulary in one screen, told in the
/// same fictional universe as the engine's sample corpus.
enum DemoWorld {

    /// Times are minted relative to now so the demo never looks stale, in
    /// the same second-precision format the backend writes.
    private static func at(hoursAgo: Double) -> String {
        ISO8601DateFormatter.lifeline.string(from: Date().addingTimeInterval(-hoursAgo * 3600))
    }

    private static func at(daysAhead: Double) -> String {
        ISO8601DateFormatter.lifeline.string(from: Date().addingTimeInterval(daysAhead * 86400))
    }

    struct World {
        var stack: ThreadStack
        var details: [String: ThreadDetail]
        var closures: [ThreadClosure]
        var proposals: [LifeThread]
    }

    static func make() -> World {
        // 1 — HOT: a drafted move waiting on one answer.
        var bag = LifeThread(
            id: "demo-bag", title: "Get Maya the croissant bag",
            summary: "She's mentioned it twice since Fillmore.",
            origin: .promotedFromItem, contactName: "Maya",
            openedAt: at(hoursAgo: 96), updatedAt: at(hoursAgo: 2),
            tier: "now", lane: .hot, unseen: 1,
            status: "needs_you", statusLabel: "Needs you",
            whyKind: "move", whyText: "Drafted — needs your call.")
        bag.importance = 0.9
        let bagMove = Finding(
            id: "demo-bag-move", kind: "action",
            headline: "Order is staged — it needs the color call",
            body: "",
            importance: 0.9, createdAt: at(hoursAgo: 2),
            loopRunId: "demo-run-1",
            moveKind: "decide",
            steps: ["Found the bag at two stockists",
                    "Compared prices — direct is $40 less",
                    "Cart is filled and sitting at checkout"],
            needs: ["Dark chocolate or tan? She's pointed at both."],
            facts: [ThreadFact(label: "Direct", value: "$1,290"),
                    ThreadFact(label: "Stockist", value: "$1,330")])
        let bagNote = Finding(
            id: "demo-bag-note", kind: "finding",
            headline: "The Fillmore store sold out on Tuesday",
            body: "Their site now lists the nubuck as online-only. That's why the cart is staged direct.",
            importance: 0.6, createdAt: at(hoursAgo: 20), loopRunId: "demo-run-2")
        let bagDetail = ThreadDetail(
            thread: bag,
            evidence: [ThreadEvidence(
                kind: "message", refId: "demo-bag-msg",
                title: "Maya", text: "Saw the Lemaire croissant bag in the window on Fillmore today. I want that bag.",
                person: "Maya", timestamp: at(hoursAgo: 96), source: "imessage",
                role: "founding")],
            findings: [bagMove, bagNote])

        // 2 — WARM: a deadline the system inferred, and a reply ready to go.
        var dinner = LifeThread(
            id: "demo-dinner", title: "RSVP to the Hendersons' dinner",
            summary: "They asked twice. Saturday.",
            origin: .silence, contactName: "The Hendersons",
            openedAt: at(hoursAgo: 120), updatedAt: at(hoursAgo: 8),
            tier: "now", lane: .warm,
            whyKind: "due", whyText: "Saturday. Asked twice.")
        dinner.importance = 0.75
        dinner.deadline = DeadlineChip(
            date: at(daysAhead: 3), source: "inferred",
            reason: "\"a week from Saturday\" in Maya's reminder, sent last Sunday")
        let dinnerMove = Finding(
            id: "demo-dinner-move", kind: "action",
            headline: "A yes is drafted",
            body: "",
            importance: 0.8, createdAt: at(hoursAgo: 8),
            loopRunId: "demo-run-3",
            moveKind: "send",
            steps: ["Checked the calendar — Saturday evening is clear",
                    "Drafted: \"We're in for Saturday — what can we bring?\""],
            needs: [])
        let dinnerDetail = ThreadDetail(
            thread: dinner,
            evidence: [ThreadEvidence(
                kind: "message", refId: "demo-dinner-msg",
                title: "Maya", text: "Reminder that the Hendersons' dinner is a week from Saturday and we still haven't RSVPd",
                person: "Maya", timestamp: at(hoursAgo: 120), source: "imessage",
                role: "founding")],
            findings: [dinnerMove])

        // 3 — LIVE: the system working alone, with a watcher on the door.
        var soccer = LifeThread(
            id: "demo-soccer", title: "Milo's fall soccer signup",
            summary: "Registration opens Monday.",
            origin: .user,
            openedAt: at(hoursAgo: 48), updatedAt: at(hoursAgo: 5),
            tier: "index", lane: .live,
            whyKind: "new", whyText: "Watching the league's mail.")
        soccer.importance = 0.6
        let soccerNote = Finding(
            id: "demo-soccer-note", kind: "finding",
            headline: "The form is filled in up to payment",
            body: "Age group, shirt size, and the medical waiver carry over from spring. Registration opens Monday at 9 — the watcher will catch the mail.",
            importance: 0.6, createdAt: at(hoursAgo: 5), loopRunId: "demo-run-4")
        let soccerDetail = ThreadDetail(
            thread: soccer,
            findings: [soccerNote],
            watchers: [Watcher(
                id: "demo-soccer-watch", kind: "mail",
                what: "League registration mail", everyMinutes: 180)])

        // 4 — QUIET: parked on purpose.
        var wall = LifeThread(
            id: "demo-wall", title: "Priya's contractor for the retaining wall",
            summary: "Luis — she says he's the one.",
            origin: .user, state: .quiet, contactName: "Priya Raman",
            openedAt: at(hoursAgo: 200), updatedAt: at(hoursAgo: 160),
            tier: "index", lane: .idle,
            whyKind: "waited", whyText: "Quiet until you wake it.")
        wall.importance = 0.3
        let wallDetail = ThreadDetail(thread: wall)

        // 5 — a closure question: the evidence says done, only you can say so.
        var padel = LifeThread(
            id: "demo-padel", title: "Lock in padel with Dev",
            summary: "Thursday, 7pm, the usual courts.",
            origin: .user, contactName: "Dev Shah",
            openedAt: at(hoursAgo: 72), updatedAt: at(hoursAgo: 3),
            tier: "index", lane: .live)
        padel.importance = 0.5
        let padelDetail = ThreadDetail(
            thread: padel,
            evidence: [ThreadEvidence(
                kind: "message", refId: "demo-padel-msg",
                title: "Dev Shah", text: "locked in. Thursday 7, I got the court",
                person: "Dev Shah", timestamp: at(hoursAgo: 3), source: "imessage")])
        let padelClosure = ThreadClosure(
            id: "demo-close-padel", thread: padel, confidence: 0.9,
            reasons: ["Dev said \"locked in\" three hours ago",
                      "The court booking confirmation landed in mail"],
            detectedAt: at(hoursAgo: 2))

        // 6 — DONE: struck through, still on the pile today.
        var pickup = LifeThread(
            id: "demo-pickup", title: "Pick up Mom from SFO",
            summary: "", origin: .urgency, state: .resolved,
            contactName: "Mom",
            openedAt: at(hoursAgo: 30),
            resolvedAt: at(hoursAgo: 26), resolvedBy: "system",
            updatedAt: at(hoursAgo: 26),
            tier: "index", lane: .done,
            status: "finished", statusLabel: "Tied off")
        pickup.importance = 0.4
        let pickupDetail = ThreadDetail(thread: pickup)

        // A proposal — the system may suggest a loop; only you put it on the pile.
        var spending = LifeThread(
            id: "demo-proposal", title: "August spending check-in",
            summary: "Three larger-than-usual charges this month point the same direction.",
            origin: .systemProposed, state: .proposed,
            openedAt: at(hoursAgo: 12), updatedAt: at(hoursAgo: 12),
            tier: "index", lane: .idle)
        spending.importance = 0.4

        let threads = [bag, dinner, soccer, padel, wall, pickup]
        return World(
            stack: ThreadStack(
                generatedAt: at(hoursAgo: 0),
                running: threads.filter { !$0.isResolved }.count,
                needsYou: 2,
                threads: threads),
            details: ["demo-bag": bagDetail, "demo-dinner": dinnerDetail,
                      "demo-soccer": soccerDetail, "demo-wall": wallDetail,
                      "demo-padel": padelDetail, "demo-pickup": pickupDetail],
            closures: [padelClosure],
            proposals: [spending])
    }

    /// A thread declared from the composer lands like the real thing.
    static func declared(title: String, summary: String) -> LifeThread {
        var thread = LifeThread(
            id: "demo-declared-\(UUID().uuidString.prefix(8))",
            title: title, summary: summary, origin: .user,
            openedAt: at(hoursAgo: 0), updatedAt: at(hoursAgo: 0),
            tier: "index", lane: .live,
            whyKind: "new", whyText: "Yours — just added.")
        thread.importance = 0.5
        return thread
    }

    /// The ask door, answered from the crafted world. Raw wire JSON rather
    /// than the model type: `AskCard` owns a custom tolerant decoder, so the
    /// honest way to feed it is the same bytes the server would send.
    static var askJSON: Data? {
        let card: [String: Any] = [
            "id": "demo-ask", "question": "",
            "answer": "Saturday evening is the Hendersons' dinner — you haven't RSVP'd yet, and a yes is drafted in that loose end. The calendar is clear otherwise.",
            "receipts": [[
                "kind": "message", "ref_id": "demo-dinner-msg",
                "source": "imessage", "label": "Maya, last Sunday",
                "detail": "Reminder that the Hendersons' dinner is a week from Saturday",
            ]],
            "knew": [], "trace": [],
            "created_at": ISO8601DateFormatter.lifeline.string(from: Date()),
        ]
        return try? JSONSerialization.data(withJSONObject: card)
    }
}
