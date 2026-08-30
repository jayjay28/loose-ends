import Observation
import SwiftUI

/// The Conversations tab as living strands. Every open loop is a conversation running from
/// when it started (left) to now (the node on the right): length = how long
/// you've carried it, thickness = how close the person, colour = heat. Tap a
/// strand to pull it open. Renders from `/conversations` — no new data.
@MainActor
@Observable
final class LifeConversationsViewModel {
    private(set) var conversations: [ConversationSummary]?
    private(set) var error: String?

    private let sync: SyncService
    init(sync: SyncService) { self.sync = sync }

    func load() async {
        if ConversationsViewModel.usesSample { conversations = SampleData.conversations; return }
        do { conversations = try await sync.refreshConversations(); error = nil }
        catch let err { if conversations == nil { error = err.localizedDescription } }
    }
}

struct LifeConversationsView: View {
    @Environment(\.syncService) private var syncService
    @State private var model: LifeConversationsViewModel?

    var body: some View {
        NavigationStack {
            content
                .background(Theme.paper.ignoresSafeArea())
                .navigationTitle("Conversations")
                .navigationBarTitleDisplayMode(.inline)
                .task {
                    if model == nil { model = LifeConversationsViewModel(sync: syncService) }
                    await model?.load()
                }
                .refreshable { await model?.load() }
        }
    }

    @ViewBuilder
    private var content: some View {
        if let conversations = model?.conversations {
            if conversations.isEmpty {
                QuietState(title: "Your loom is clear", subhead: "No conversations open. Nothing running.")
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        StrandsHeader(conversations: conversations)
                        StrandAxis()
                        ForEach(conversations) { conversation in
                            NavigationLink { ConversationDetailView(conversation: conversation) } label: {
                                StrandRow(conversation: conversation, oldest: oldestAgeDays(conversations))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal, 22).padding(.top, 6).padding(.bottom, 40)
                }
            }
        } else if let error = model?.error {
            QuietState(title: "Couldn't load", subhead: error)
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func oldestAgeDays(_ conversations: [ConversationSummary]) -> Double {
        max(14, conversations.map { StrandTime.ageDays($0.lastActivity) }.max() ?? 14)
    }
}

// MARK: - Header

private struct StrandsHeader: View {
    let conversations: [ConversationSummary]
    private var hot: Int { conversations.filter { $0.topLevel == .timeSensitive }.count }
    private var active: Int { conversations.filter { $0.topLevel == .active }.count }
    private var quiet: Int { conversations.count - hot - active }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Still running")
                .font(.system(size: 11, weight: .semibold)).tracking(1.6)
                .textCase(.uppercase).foregroundStyle(Theme.inkFaint)
            HStack(alignment: .firstTextBaseline, spacing: 9) {
                Text("\(conversations.count)").font(Theme.serif(42, .semibold)).foregroundStyle(Theme.ink)
                Text("conversations open").font(Theme.serif(16)).foregroundStyle(Theme.inkSoft)
            }
            GeometryReader { geo in
                let total = CGFloat(max(conversations.count, 1))
                HStack(spacing: 2) {
                    if hot > 0 { Capsule().fill(Theme.ember).frame(width: geo.size.width * CGFloat(hot) / total) }
                    if active > 0 { Capsule().fill(Theme.brand).frame(width: geo.size.width * CGFloat(active) / total) }
                    if quiet > 0 { Capsule().fill(Theme.inkGhost.opacity(0.6)) }
                }
            }
            .frame(height: 6)
            HStack(spacing: 13) {
                legend(hot, "need you", Theme.ember)
                legend(active, "running", Theme.brand)
                legend(quiet, "slack", Theme.inkFaint)
            }
            .font(.system(size: 11))
        }
        .padding(.bottom, 12)
    }

    private func legend(_ n: Int, _ label: String, _ color: Color) -> some View {
        HStack(spacing: 4) {
            Text("\(n)").fontWeight(.semibold).foregroundStyle(color)
            Text(label).foregroundStyle(Theme.inkFaint)
        }
    }
}

/// Faint time gridline labels under the header.
private struct StrandAxis: View {
    var body: some View {
        GeometryReader { geo in
            let nodeX = min(geo.size.width * 0.56, geo.size.width - 130)
            ZStack(alignment: .topLeading) {
                ForEach(Array([("now", 1.0), ("this wk", 0.78), ("this mo", 0.5), ("older", 0.1)].enumerated()), id: \.offset) { _, tick in
                    let x = 8 + CGFloat(tick.1) * (nodeX - 8)
                    Text(tick.0).font(.system(size: 9)).foregroundStyle(Theme.inkGhost)
                        .position(x: x, y: 8)
                }
            }
        }
        .frame(height: 18)
    }
}

// MARK: - Strand row

private struct StrandRow: View {
    let conversation: ConversationSummary
    let oldest: Double
    @State private var pulse = false

    private var isQuiet: Bool { conversation.topLevel == .passive || conversation.topLevel == .unknown }
    private var heat: Color {
        switch conversation.topLevel {
        case .timeSensitive: return Theme.ember
        case .active: return Theme.brand
        case .passive, .unknown: return Theme.inkGhost
        }
    }
    private var lineWidth: CGFloat {
        let tie = CGFloat(conversation.tieStrength ?? 0.5)
        let base = 1.4 + 1.6 * tie
        return isQuiet ? min(base, 1.6) : base
    }

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width, h = geo.size.height
            let nodeX = min(w * 0.56, w - 130)
            // Log scale so a cluster of recent conversations still spreads across the
            // width instead of piling up under "now".
            let age = StrandTime.ageDays(conversation.lastActivity)
            let frac = 1 - min(log(1 + age) / log(1 + oldest), 1)   // new→1, old→0
            let originX = 10 + CGFloat(frac) * (nodeX - 54)
            let labelW = max(96, w - nodeX - 24)

            ZStack(alignment: .topLeading) {
                StrandShape(fromX: originX, toX: nodeX, y: h / 2)
                    .stroke(
                        LinearGradient(colors: [heat.opacity(0.06), heat.opacity(isQuiet ? 0.6 : 0.92)],
                                       startPoint: .leading, endPoint: .trailing),
                        style: StrokeStyle(lineWidth: lineWidth, lineCap: .round,
                                           dash: isQuiet ? [1, 5] : [])
                    )

                if conversation.topLevel == .timeSensitive {
                    Circle().fill(Theme.ember).frame(width: 30, height: 30)
                        .opacity(pulse ? 0.28 : 0.14).position(x: nodeX, y: h / 2)
                }
                Avatar(name: conversation.person, personId: conversation.personId, level: conversation.topLevel, size: 34)
                    .position(x: nodeX, y: h / 2)

                VStack(alignment: .leading, spacing: 2) {
                    Text(conversation.person)
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(isQuiet ? Theme.inkSoft : Theme.ink).lineLimit(1)
                    Text(subtitle).font(.system(size: 11.5)).foregroundStyle(Theme.inkFaint).lineLimit(1)
                }
                .frame(width: labelW, alignment: .leading)
                .position(x: nodeX + 24 + labelW / 2, y: h / 2)
            }
        }
        .frame(height: 58)
        .contentShape(Rectangle())
        .onAppear {
            guard conversation.topLevel == .timeSensitive else { return }
            withAnimation(.easeInOut(duration: 2.4).repeatForever(autoreverses: true)) { pulse = true }
        }
    }

    private var subtitle: String {
        var parts: [String] = []
        if let topic = conversation.topic, !topic.isEmpty { parts.append(topic) }
        else { parts.append(conversation.openCount == 1 ? "1 open" : "\(conversation.openCount) open") }
        if let age = RelativeTime.label(from: conversation.lastActivity) { parts.append(age) }
        return parts.joined(separator: " · ")
    }
}

/// The strand path — a gentle wave from origin to the node.
private struct StrandShape: Shape {
    var fromX: CGFloat
    var toX: CGFloat
    var y: CGFloat
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: fromX, y: y))
        let mid = (fromX + toX) / 2
        path.addCurve(to: CGPoint(x: toX, y: y),
                      control1: CGPoint(x: mid, y: y - 7),
                      control2: CGPoint(x: mid, y: y + 7))
        return path
    }
}

enum StrandTime {
    static func ageDays(_ iso: String?) -> Double {
        guard let iso, let date = ISO8601DateFormatter.lifeline.date(from: iso) else { return 3 }
        return max(0, Date.now.timeIntervalSince(date) / 86_400)
    }
}
