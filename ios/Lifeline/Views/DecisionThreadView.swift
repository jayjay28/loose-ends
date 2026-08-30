import SwiftUI

/// §8.3 "show your work" — the rail unrolled into the chain of reasoning that
/// put an item in front of you: Heard → Understood → Weighed → Surfaced.
/// Renders entirely from data the item already carries (raw text, type,
/// entities, and the scorer's `why` contributions).
struct DecisionThreadView: View {
    let item: Item
    var onDone: (() -> Void)? = nil

    private var sortedWhy: [Explanation] {
        item.why.sorted { abs($0.contribution) > abs($1.contribution) }
    }
    private var maxMagnitude: Double {
        max(0.001, sortedWhy.map { abs($0.contribution) }.max() ?? 1)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            heard
            understood
            if !sortedWhy.isEmpty { weighed }
            surfaced
            ItemActionBar(item: item, onDone: onDone)
                .padding(.leading, 26)
        }
        .background(alignment: .topLeading) {
            Rectangle()
                .fill(Theme.rule)
                .frame(width: 2)
                .padding(.leading, 5)
                .padding(.top, 16).padding(.bottom, 18)
        }
    }

    // MARK: Stations

    private var heard: some View {
        station("Heard", hot: true) {
            Text(metaLine).font(.system(size: 12.5)).foregroundStyle(Theme.inkFaint)
            Text("“\(item.rawText.tightened)”")
                .font(Theme.serif(14)).foregroundStyle(Theme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.leading, 11)
                .overlay(alignment: .leading) { Rectangle().fill(Theme.rule).frame(width: 2) }
                .padding(.top, 6)
        }
    }

    private var understood: some View {
        station("Understood", hot: false) {
            Text(item.suggestedAction.isEmpty ? item.rawText : item.suggestedAction)
                .font(Theme.serif(16, .medium)).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true).padding(.top, 5)
            Text(understoodSub).font(.system(size: 12.5)).foregroundStyle(Theme.inkFaint).padding(.top, 4)
        }
    }

    private var weighed: some View {
        station("Weighed", hot: false) {
            VStack(alignment: .leading, spacing: 9) {
                ForEach(Array(sortedWhy.prefix(5).enumerated()), id: \.element.id) { idx, ex in
                    let positive = ex.contribution >= 0
                    let tipped = idx == 0 && positive
                    HStack(spacing: 9) {
                        Text(ex.detail)
                            .font(.system(size: 13))
                            .foregroundStyle(positive ? Theme.ink : Theme.inkFaint)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                        Capsule().fill(Theme.rule).frame(width: 60, height: 6)
                            .overlay(alignment: .leading) {
                                Capsule()
                                    .fill(tipped ? Theme.ember : (positive ? Theme.brand : Theme.inkGhost))
                                    .frame(width: 60 * abs(ex.contribution) / maxMagnitude, height: 6)
                            }
                    }
                }
            }
            .padding(.top, 8)
        }
    }

    private var surfaced: some View {
        station("Surfaced", hot: true) {
            HStack {
                Text(surfacedLabel).font(Theme.serif(16, .medium)).foregroundStyle(Theme.ink)
                Spacer()
                if let age = ThreadAge.waited(item.timestamp) {
                    Text(age.text)
                        .font(.system(size: 12, weight: .semibold)).monospacedDigit()
                        .padding(.horizontal, 9).padding(.vertical, 5)
                        .background(age.urgent ? Theme.emberSoft : Theme.chip, in: Capsule())
                        .foregroundStyle(age.urgent ? Theme.ember : Theme.inkFaint)
                }
            }.padding(.top, 5)
        }
    }

    // MARK: Station scaffold

    @ViewBuilder
    private func station<Content: View>(_ label: String, hot: Bool, @ViewBuilder _ content: () -> Content) -> some View {
        HStack(alignment: .top, spacing: 0) {
            StationNode(hot: hot).frame(width: 26).padding(.top, 14)
            VStack(alignment: .leading, spacing: 0) {
                Text(label.uppercased())
                    .font(.system(size: 10, weight: .semibold)).tracking(1.3).foregroundStyle(Theme.inkFaint)
                content()
            }
            .padding(13)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.rule))
        }
    }

    // MARK: Copy

    private var metaLine: String {
        var parts = [item.person]
        parts.append(item.source)
        if let age = ThreadAge.waited(item.timestamp) { parts.append("\(age.text) ago") }
        return parts.joined(separator: " · ")
    }
    private var understoodSub: String {
        var parts = [item.type.rawValue]
        if let entity = item.entities.item, !entity.isEmpty { parts.append("about: \(entity)") }
        return parts.joined(separator: " · ")
    }
    private var surfacedLabel: String {
        switch item.interruptionLevel {
        case .timeSensitive: return "Needs you now"
        case .active: return "Brought to you"
        case .passive, .unknown: return "Kept quiet"
        }
    }
}

private struct StationNode: View {
    var hot: Bool
    var body: some View {
        Group {
            if hot {
                Circle().fill(Theme.ember).frame(width: 12, height: 12)
                    .background(Circle().fill(Theme.emberSoft).frame(width: 20, height: 20))
            } else {
                Circle().fill(Theme.paper).frame(width: 11, height: 11)
                    .overlay(Circle().stroke(Theme.brand, lineWidth: 2))
            }
        }
        .frame(maxWidth: .infinity)
    }
}

/// How long something has been waiting, with a 30-day urgency threshold.
enum ThreadAge {
    static func waited(_ iso: String?, now: Date = .now) -> (text: String, urgent: Bool)? {
        guard let iso, let date = ISO8601DateFormatter.lifeline.date(from: iso) else { return nil }
        let days = Int(max(0, now.timeIntervalSince(date)) / 86400)
        func unit(_ n: Int, _ w: String) -> String { "\(n) \(w)\(n == 1 ? "" : "s")" }
        let text: String
        if days < 1 { text = "today" }
        else if days < 14 { text = unit(days, "day") }
        else if days < 60 { text = unit(days / 7, "week") }
        else { text = unit(days / 30, "month") }
        return (text, days >= 30)
    }
}
