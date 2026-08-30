import SwiftUI

/// §8.2 Today — the ranked briefing, in the "editorial" treatment. Shape
/// (mode, grouping, per-group style) is the server's decision (§8.1); this
/// view renders it as a calm, time-of-day page rather than a dashboard: a
/// serif headline, one lifeline rail the day hangs from, and a level of
/// detail per item that rises with urgency.
struct TodayView: View {
    @Environment(\.syncService) private var syncService
    @State private var viewModel: TodayViewModel?

    var body: some View {
        ZStack {
            Theme.paper.ignoresSafeArea()
            content
        }
            .task {
                if viewModel == nil {
                    viewModel = TodayViewModel(sync: syncService)
                }
                await viewModel?.loadCachedThenRefresh()
            }
            .refreshable { await viewModel?.refresh() }
    }

    @ViewBuilder
    private var content: some View {
        if let viewModel {
            TodayContentView(viewModel: viewModel)
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

private struct TodayContentView: View {
    @Bindable var viewModel: TodayViewModel

    var body: some View {
        Group {
            if let today = viewModel.today {
                if today.mode == .empty {
                    EmptyTodayView(today: today)
                } else {
                    briefing(today)
                }
            } else if viewModel.isLoading {
                ProgressView("Loading…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ErrorStateView(message: viewModel.errorMessage ?? "Couldn't load Today.") {
                    Task { await viewModel.refresh() }
                }
            }
        }
    }

    private func briefing(_ today: Today) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if viewModel.isOffline { OfflineBanner() }
                Header(today: today)

                if !today.carousel.isEmpty {
                    QuickCarousel(
                        items: today.carousel,
                        onDone: { item in Task { await viewModel.markDone(item) } }
                    )
                    .padding(.horizontal, -22)   // bleed the scroller to the screen edges
                }

                if !today.confirmations.isEmpty {
                    VStack(spacing: 12) {
                        ForEach(today.confirmations) { confirmation in
                            ConfirmationRow(
                                confirmation: confirmation,
                                onConfirm: { Task { await viewModel.confirm(confirmation) } },
                                onReject: { Task { await viewModel.reject(confirmation) } }
                            )
                        }
                    }
                    .padding(.top, 20)
                }

                rail(today)
            }
            .padding(.horizontal, 22)
            .padding(.top, 12)
            .padding(.bottom, 48)
        }
    }

    /// The whole set of groups laid along one continuous rail line.
    private func rail(_ today: Today) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(today.groups.enumerated()), id: \.element.id) { index, group in
                TierLabel(title: group.title, isFirst: index == 0)
                groupBody(group)
            }
        }
        .background(alignment: .topLeading) {
            Rectangle()
                .fill(Theme.rule)
                .frame(width: 1.5)
                .padding(.leading, 12.25)
                .padding(.top, 34)
                .padding(.bottom, 16)
        }
        .padding(.top, 4)
    }

    @ViewBuilder
    private func groupBody(_ group: ItemGroup) -> some View {
        if group.style == .collapsed {
            CollapsedGroupRow(
                group: group,
                isExpanded: viewModel.expandedGroupIDs.contains(group.id),
                onToggle: { viewModel.toggleGroup(group.id) }
            )
            if viewModel.expandedGroupIDs.contains(group.id) {
                ForEach(group.items) { itemRow($0) }
            }
        } else {
            ForEach(group.items) { itemRow($0) }
        }
    }

    private func itemRow(_ item: Item) -> some View {
        ItemRow(
            item: item,
            isExpanded: viewModel.expandedItemIDs.contains(item.id),
            onToggle: { withAnimation(.easeOut(duration: 0.22)) { viewModel.toggleItem(item) } },
            onDone: { Task { await viewModel.markDone(item) } },
            onSnooze: { hours in Task { await viewModel.snooze(item, hours: hours) } },
            onDismiss: { Task { await viewModel.dismiss(item) } }
        )
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.ruleSoft).frame(height: 1)
        }
    }
}

// MARK: - Pieces

/// §8.x — the swipeable "do now" carousel: a lens over the one-tap items.
private struct QuickCarousel: View {
    let items: [Item]
    let onDone: (Item) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Do now")
                .font(.system(size: 11, weight: .semibold)).tracking(1.6)
                .foregroundStyle(Theme.inkFaint)
                .padding(.horizontal, 22)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 12) {
                    ForEach(items) { item in
                        QuickActionCard(item: item, onDone: { onDone(item) })
                    }
                }
                .padding(.horizontal, 22)
            }
        }
        .padding(.top, 22)
    }
}

private struct QuickActionCard: View {
    let item: Item
    let onDone: () -> Void
    @Environment(\.openURL) private var openURL

    private enum Kind { case reply, watch, read }
    private var kind: Kind {
        if item.sendMessagesURL != nil { return .reply }
        return item.isVideoLink ? .watch : .read
    }
    private var primary: (title: String, icon: String, url: URL)? {
        switch kind {
        case .reply: return item.sendMessagesURL.map { ("Send in Messages", "paperplane.fill", $0) }
        case .watch: return item.openLinkURL.map { ("Watch", "play.fill", $0) }
        case .read:  return item.openLinkURL.map { ("Open", "safari", $0) }
        }
    }
    private var kindLabel: (String, Color) {
        switch kind {
        case .reply: return ("Reply", Theme.ember)
        case .watch: return ("Watch", Theme.brand)
        case .read:  return ("Read", Theme.brand)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(kindLabel.0.uppercased())
                .font(.system(size: 10, weight: .semibold)).tracking(1.2)
                .foregroundStyle(kindLabel.1)
            Text(item.suggestedAction.isEmpty ? item.rawText : item.suggestedAction)
                .font(Theme.serif(16, .medium)).foregroundStyle(Theme.ink)
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            Text(item.person).font(.system(size: 12.5)).foregroundStyle(Theme.inkFaint)
            Spacer(minLength: 4)
            HStack(spacing: 8) {
                if let primary {
                    Button { openURL(primary.url) } label: {
                        SwiftUI.Label(primary.title, systemImage: primary.icon)
                            .font(.system(size: 13, weight: .semibold))
                            .padding(.horizontal, 13).padding(.vertical, 10)
                            .frame(maxWidth: .infinity)
                            .background(Theme.brand, in: RoundedRectangle(cornerRadius: 10))
                            .foregroundStyle(.white)
                    }.buttonStyle(.plain)
                }
                Button(action: onDone) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 13, weight: .bold))
                        .padding(11)
                        .background(Theme.card, in: RoundedRectangle(cornerRadius: 10))
                        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.rule))
                        .foregroundStyle(Theme.inkFaint)
                }.buttonStyle(.plain)
            }
        }
        .padding(14)
        .frame(width: 262, height: 168, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).stroke(Theme.rule))
        .shadow(color: Theme.ink.opacity(0.05), radius: 12, y: 5)
    }
}

private struct Header: View {
    let today: Today

    private var eyebrow: String? {
        guard let date = ISO8601DateFormatter.lifeline.date(from: today.generatedAt) else { return nil }
        let f = DateFormatter()
        f.dateFormat = "EEEE · MMMM d"
        return f.string(from: date).uppercased()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let eyebrow {
                Text(eyebrow)
                    .font(.system(size: 11, weight: .semibold))
                    .tracking(1.6)
                    .foregroundStyle(Theme.inkFaint)
            }
            Text(today.headline)
                .font(Theme.serif(32, .semibold))
                .tracking(-0.5)
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            if let subhead = today.subhead {
                Text(subhead)
                    .font(Theme.serif(16, .medium))
                    .foregroundStyle(Theme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 8)
    }
}

private struct TierLabel: View {
    let title: String
    let isFirst: Bool

    var body: some View {
        HStack(spacing: 0) {
            Color.clear.frame(width: 26)
            Text(title.uppercased())
                .font(.system(size: 10.5, weight: .semibold))
                .tracking(1.5)
                .foregroundStyle(Theme.inkFaint)
        }
        .padding(.top, isFirst ? 4 : 26)
        .padding(.bottom, 8)
    }
}

/// A collapsed Passive group — one quiet line standing in for the batch.
/// Tapping discloses the members locally; it never changes the server's style.
struct CollapsedGroupRow: View {
    let group: ItemGroup
    let isExpanded: Bool
    let onToggle: () -> Void

    private var detail: String {
        group.items.map { $0.suggestedAction.isEmpty ? $0.rawText : $0.suggestedAction }
            .joined(separator: " · ")
    }

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            RailNode(level: .passive).padding(.top, 8)
            VStack(alignment: .leading, spacing: 4) {
                Text(group.subtitle ?? "\(group.items.count) items")
                    .font(.system(size: 16))
                    .foregroundStyle(Theme.inkSoft)
                if !isExpanded {
                    Text(detail)
                        .font(.system(size: 13.5))
                        .foregroundStyle(Theme.inkFaint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.vertical, 6)
            Spacer()
        }
        .contentShape(Rectangle())
        .onTapGesture(perform: onToggle)
    }
}

/// §8.1 — an empty queue is a genuinely different, quieter state.
private struct EmptyTodayView: View {
    let today: Today

    var body: some View {
        VStack(spacing: 6) {
            ZStack {
                Circle().stroke(Theme.good.opacity(0.4), lineWidth: 1.5).frame(width: 54, height: 54)
                Circle().fill(Theme.good).frame(width: 9, height: 9)
            }
            .padding(.bottom, 22)
            Text(today.headline)
                .font(Theme.serif(26, .medium))
                .foregroundStyle(Theme.ink)
            if let subhead = today.subhead {
                Text(subhead)
                    .font(Theme.serif(15))
                    .foregroundStyle(Theme.inkSoft)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(.horizontal, 42)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct OfflineBanner: View {
    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "wifi.slash")
            Text("Showing the last briefing — you're offline")
        }
        .font(.caption)
        .foregroundStyle(Theme.inkFaint)
        .padding(.top, 8)
    }
}

private struct ErrorStateView: View {
    let message: String
    let retry: () -> Void

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle").font(.title2).foregroundStyle(Theme.inkFaint)
            Text("Couldn't load Today").font(Theme.serif(20, .medium)).foregroundStyle(Theme.ink)
            Text(message).font(.subheadline).foregroundStyle(Theme.inkSoft).multilineTextAlignment(.center)
            Button("Retry", action: retry).padding(.top, 4).tint(Theme.brand)
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

#Preview {
    TodayView()
}
