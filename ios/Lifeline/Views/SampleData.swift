import Foundation

/// Design/preview fixtures that mirror the editorial-briefing mockup. Seeded
/// into `TodayViewModel` when the app launches with `-uiSampleData` (see the
/// scheme / launch args), so the UI can be built and reviewed without a live
/// backend. Never used in a normal run.
enum SampleData {
    /// A little while from now, so the Time-Sensitive item always reads as
    /// due later today regardless of the simulator's clock.
    private static let dueLaterToday = ISO8601DateFormatter.lifeline
        .string(from: Calendar.current.date(bySettingHour: 17, minute: 0, second: 0, of: .now) ?? .now)

    private static func item(
        id: String,
        person: String,
        type: ItemType,
        raw: String,
        action: String,
        reply: String? = nil,
        level: InterruptionLevel,
        due: String? = nil,
        pattern: BehaviorPattern? = nil,
        why: [Explanation] = [],
        handle: String? = nil,
        link: String? = nil,
        linkKind: String? = nil
    ) -> Item {
        Item(
            id: id,
            source: "gmail",
            conversationId: "t-\(id)",
            person: person,
            personId: person.lowercased(),
            timestamp: "2026-08-01T08:00:00+00:00",
            type: type,
            rawText: raw,
            entities: Entities(item: nil, date: due, link: nil),
            suggestedAction: action,
            suggestedReply: reply,
            status: .pending,
            createdAt: "2026-08-01T07:00:00+00:00",
            updatedAt: "2026-08-01T08:00:00+00:00",
            score: 0.9,
            interruptionLevel: level,
            why: why,
            behaviorPattern: pattern,
            snoozedUntil: nil,
            completedAt: nil,
            completedBy: nil,
            linksToItemId: nil,
            handle: handle,
            link: link,
            linkKind: linkKind
        )
    }

    private static func why(_ detail: String, _ contribution: Double) -> Explanation {
        Explanation(signal: detail, value: 1, weight: contribution, contribution: contribution, detail: detail)
    }

    static let today = Today(
        mode: .briefing,
        headline: "Good morning, Alex.",
        subhead: "One thing needs you before lunch. The rest can wait.",
        generatedAt: "2026-08-01T08:41:00+00:00",
        groups: [
            ItemGroup(
                level: .timeSensitive,
                title: "Needs you now",
                subtitle: nil,
                style: .expanded,
                items: [
                    item(
                        id: "dana",
                        person: "Dana Whitlock",
                        type: .promise,
                        raw: "The venue needs the deposit confirmed by end of day or we lose the Saturday hold.",
                        action: "Give Dana an answer on the venue deposit.",
                        reply: "Yes — go ahead and confirm the deposit. I'll send my half over tonight.",
                        level: .timeSensitive,
                        due: dueLaterToday,
                        why: [
                            why("Hard deadline today, 5:00 PM", 1.0),
                            why("Dana usually hears back from you same day", 0.72),
                            why("You opened this conversation twice this morning", 0.48),
                        ]
                    )
                ]
            ),
            ItemGroup(
                level: .active,
                title: "When you get a moment",
                subtitle: nil,
                style: .expanded,
                items: [
                    item(
                        id: "marcus",
                        person: "Marcus Vale",
                        type: .promise,
                        raw: "No rush — whenever you get a sec, that doc would help me hit the ground running Monday.",
                        action: "Send Marcus the onboarding doc you promised.",
                        level: .active,
                        why: [
                            why("You made an explicit promise", 0.66),
                            why("He said “no rush”", -0.34),
                        ]
                    ),
                    item(
                        id: "insurance",
                        person: "Reminder",
                        type: .followup,
                        raw: "You've snoozed this four times since Monday.",
                        action: "Call the insurance company back.",
                        level: .active,
                        pattern: .avoidance,
                        why: [
                            why("Nudged up — you keep avoiding it, not deprioritizing it", 0.58),
                            why("Open longest of anything here", 0.40),
                        ]
                    ),
                    item(
                        id: "priya",
                        person: "Priya Rao",
                        type: .question,
                        raw: "Do the Q3 figures include the November true-up or not? Trying to close the deck.",
                        action: "Reply to Priya about the Q3 numbers.",
                        reply: "They don't — the true-up lands in Q4. Happy to send the reconciled version if useful.",
                        level: .active
                    ),
                ]
            ),
            ItemGroup(
                level: .passive,
                title: "Quieter",
                subtitle: "3 quieter things",
                style: .collapsed,
                items: [
                    item(id: "sam", person: "Sam", type: .reading, raw: "A link from Sam", action: "A link from Sam", level: .passive),
                    item(id: "article", person: "You", type: .reading, raw: "An article you saved on sleep", action: "An article you saved on sleep", level: .passive),
                    item(id: "chris", person: "Chris", type: .event, raw: "Maybe-lunch with Chris next week", action: "Maybe-lunch with Chris next week", level: .passive),
                ]
            ),
        ],
        confirmations: [
            Confirmation(
                signalId: "water-bill",
                item: item(id: "water", person: "Bank alert", type: .purchase, raw: "Water bill", action: "Looks like you paid the water bill.", level: .passive),
                source: "gmail",
                confidence: 0.82,
                evidenceText: "A $74.20 payment cleared this morning — matches the reminder from Tuesday.",
                reasons: ["amount matches", "same biller"],
                detectedAt: "2026-08-01T08:30:00+00:00"
            )
        ],
        counts: ["pending": 4],
        carousel: [
            item(id: "c-priya", person: "Priya Rao", type: .question, raw: "does Q3 include the true-up?",
                 action: "Answer Priya's Q3 question", reply: "They don't — the true-up lands in Q4.",
                 level: .active, handle: "+15551234567"),
            item(id: "c-vid", person: "Sam", type: .reading, raw: "watch this",
                 action: "The talk Sam sent you", level: .passive,
                 link: "https://youtu.be/dQw4w9WgXcQ", linkKind: "video"),
            item(id: "c-read", person: "Sam", type: .reading, raw: "good read",
                 action: "Read the article on rest", level: .passive,
                 link: "https://www.nytimes.com/well/rest", linkKind: "article"),
        ]
    )

    static let conversations: [ConversationSummary] = [
        ConversationSummary(personId: "dana", person: "Dana Whitlock", relationship: "friend", sources: ["imessage"], openCount: 2, topLevel: .timeSensitive, lastActivity: "2026-08-01T08:10:00+00:00"),
        ConversationSummary(personId: "marcus", person: "Marcus Vale", relationship: "coworker", sources: ["gmail"], openCount: 1, topLevel: .active, lastActivity: "2026-07-31T16:00:00+00:00"),
        ConversationSummary(personId: "priya", person: "Priya Rao", relationship: "coworker", sources: ["gmail", "imessage"], openCount: 3, topLevel: .active, lastActivity: "2026-08-01T06:30:00+00:00"),
        ConversationSummary(personId: "sam", person: "Sam", relationship: nil, sources: ["imessage"], openCount: 1, topLevel: .passive, lastActivity: "2026-07-29T12:00:00+00:00"),
    ]

    static let conversationItems: [Item] = [
        item(id: "dana", person: "Dana Whitlock", type: .promise, raw: "The venue needs the deposit confirmed by end of day or we lose the Saturday hold.", action: "Give Dana an answer on the venue deposit.", level: .timeSensitive, due: dueLaterToday, why: [
            why("Hard deadline today, 5:00 PM", 1.0),
            why("Dana usually hears back from you same day", 0.72),
            why("You opened this conversation twice this morning", 0.48),
        ]),
        item(id: "dana2", person: "Dana Whitlock", type: .event, raw: "Saturday hold", action: "Confirm the Saturday date works for everyone.", level: .active),
    ]

    static let briefing = Briefing(
        generatedAt: "2026-08-04T08:00:00+00:00",
        mode: "morning",
        caughtUp: false,
        oneNow: conversationItems[0],
        waiting: [
            WaitingPerson(personId: "dana", person: "Dana Whitlock", tieStrength: 0.9,
                          tieLabel: "one of your closest — you two go back and forth constantly",
                          waitedSince: "2026-08-01T08:10:00+00:00", openCount: 2, topItem: conversationItems[0]),
            WaitingPerson(personId: "priya", person: "Priya Rao", tieStrength: 0.55,
                          tieLabel: "a real back-and-forth relationship",
                          waitedSince: "2026-08-01T06:30:00+00:00", openCount: 3, topItem: conversationItems[1]),
            WaitingPerson(personId: "sam", person: "Sam", tieStrength: 0.2,
                          tieLabel: "you're in touch now and then",
                          waitedSince: "2026-07-29T12:00:00+00:00", openCount: 1, topItem: conversationItems[1]),
        ]
    )

    static let history = History(
        entries: [
            HistoryEntry(item: item(id: "h1", person: "Bank alert", type: .purchase, raw: "Water bill", action: "Pay the water bill", level: .passive), closedBy: "auto", closedAt: "2026-08-01T08:30:00+00:00", evidence: "a $74.20 payment cleared", evidenceSource: "gmail"),
            HistoryEntry(item: item(id: "h2", person: "Dana Whitlock", type: .promise, raw: "Deposit", action: "Confirm the venue deposit", level: .timeSensitive), closedBy: "you", closedAt: "2026-08-01T07:12:00+00:00", evidence: nil, evidenceSource: nil),
            HistoryEntry(item: item(id: "h3", person: "Airline", type: .event, raw: "Flight", action: "Check in for your flight", level: .active), closedBy: "auto", closedAt: "2026-07-31T20:05:00+00:00", evidence: "boarding pass detected", evidenceSource: "gmail"),
        ],
        autoClosed: 2,
        manualClosed: 1,
        streakDays: 5
    )
}
