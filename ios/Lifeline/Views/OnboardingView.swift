import SwiftUI

/// §v3 workstream 6 — screens 1 and 2 of the baton pass, built to the signed
/// wireframes: the phone hands you to the Mac, the Mac does almost everything
/// itself, and a QR code hands you back.
///
/// The privacy sentence is copy, not a link — it's the reason this app exists
/// and the reason setup takes ten minutes instead of zero, so it's said out
/// loud before asking for anything.
struct OnboardingView: View {
    /// Called when the phone is paired, or when the demo begins — either way
    /// the stack takes over.
    var onDone: () -> Void

    private enum Step { case welcome, honestAsk, pair }
    @State private var step: Step = .welcome

    var body: some View {
        ZStack {
            Theme.paper.ignoresSafeArea()
            switch step {
            case .welcome:   welcome.transition(.opacity)
            case .honestAsk: honestAsk.transition(.opacity)
            case .pair:      PairScanView(onPaired: onDone) {
                                 withAnimation { step = .honestAsk }
                             }
                             .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.25), value: step == .welcome)
    }

    // MARK: - screen 1

    private var welcome: some View {
        VStack(spacing: 0) {
            Spacer()
            Text("🪢")
                .font(.system(size: 56))
                .padding(.bottom, 18)
            Text("Loose Ends")
                .font(Theme.serif(34, .semibold))
                .foregroundStyle(Theme.ink)
                .padding(.bottom, 12)
            Text("The open loops in your head — noticed in your own mail and messages, worked in the background, tied off.")
                .font(.system(size: 16))
                .foregroundStyle(Theme.inkSoft)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 34)
            Spacer()
            Button { withAnimation { step = .honestAsk } } label: {
                Text("Set it up")
                    .font(.system(size: 16, weight: .semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                    .background(Capsule().fill(Theme.brand))
                    .foregroundStyle(.white)
            }
            .buttonStyle(.plain)
            .padding(.horizontal, Theme.margin)
            .padding(.bottom, 14)
            Text("Everything stays on a computer you own. There is no Loose Ends cloud.")
                .font(.system(size: 12))
                .foregroundStyle(Theme.inkFaint)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 40)
                .padding(.bottom, 22)
        }
    }

    // MARK: - screen 2

    private var honestAsk: some View {
        VStack(alignment: .leading, spacing: 0) {
            Spacer(minLength: 40)
            Text("It runs on your Mac")
                .font(Theme.serif(28, .semibold))
                .foregroundStyle(Theme.ink)
                .padding(.bottom, 12)
            Text("Loose Ends reads your mail and messages on your own Mac — never on our servers, because there aren't any. Install the engine there. About ten minutes, and this screen will wait.")
                .font(.system(size: 15))
                .foregroundStyle(Theme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 20)

            Text("curl -fsSL clyon.dev/install | sh")
                .font(.system(size: 13.5, weight: .medium, design: .monospaced))
                .foregroundStyle(Theme.ink)
                .padding(.horizontal, 14).padding(.vertical, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: Theme.radius).fill(Theme.chip))
                .textSelection(.enabled)
                .padding(.bottom, 10)
            Text("Or download the installer at **clyon.dev/mac**")
                .font(.system(size: 13))
                .foregroundStyle(Theme.inkFaint)
                .padding(.bottom, 26)

            Button { withAnimation { step = .pair } } label: {
                Text("My Mac is ready — pair it")
                    .font(.system(size: 16, weight: .semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                    .background(Capsule().stroke(Theme.brand, lineWidth: 1.5))
                    .foregroundStyle(Theme.brand)
            }
            .buttonStyle(.plain)
            .padding(.bottom, 24)

            Spacer()

            // The dead-end line is deliberate — the app that admits who it's
            // not for earns trust for everything else it claims. The demo is
            // the consolation: the whole app on a crafted world, and the way
            // App Review sees the product without setting up a Mac.
            Text("No Mac? Loose Ends isn't for you yet — we'd rather say so than quietly hold your mail.")
                .font(.system(size: 12))
                .foregroundStyle(Theme.inkFaint)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 10)
            Button {
                DemoMode.enter()
                onDone()
            } label: {
                Text("Look around with sample data")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Theme.inkFaint)
                    .underline()
            }
            .buttonStyle(.plain)
            .padding(.bottom, 22)
        }
        .padding(.horizontal, Theme.margin)
    }
}
