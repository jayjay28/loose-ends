import SwiftUI
import UIKit

/// §v3 — how this phone earns its way into the engine.
///
/// Presented whenever the API answers 401: first run on a new install, a
/// revoked token, or an engine reset. The code comes from the Mac —
/// `lifeline pair` today, the setup wizard's screen tomorrow — and the QR
/// scanner of onboarding screen 8 will sit on top of this same claim. Typing
/// eight characters is the floor, not the ceiling.
struct PairSheet: View {
    var onPaired: () -> Void

    @Environment(\.syncService) private var syncService
    @State private var code = ""
    @State private var claiming = false
    @State private var error: String?
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("PAIR THIS DEVICE")
                .font(Theme.label)
                .tracking(0.8)
                .foregroundStyle(Theme.inkFaint)
                .padding(.bottom, 10)

            Text("Point at the Mac")
                .font(Theme.serif(25, .semibold))
                .foregroundStyle(Theme.ink)
                .padding(.bottom, 6)

            Text("On your Mac, run **lifeline pair** and type the eight-character code here. Codes live for ten minutes and work once.")
                .font(Theme.secondary)
                .foregroundStyle(Theme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 18)

            TextField("XXXXXXXX", text: $code)
                .font(.system(size: 26, weight: .semibold, design: .monospaced))
                .textInputAutocapitalization(.characters)
                .autocorrectionDisabled()
                .keyboardType(.asciiCapable)
                .multilineTextAlignment(.center)
                .focused($focused)
                .padding(.vertical, 14)
                .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chip))
                .onChange(of: code) { _, raw in
                    // The field shapes itself to what a code can be — the
                    // server is forgiving too, but the keyboard shouldn't
                    // let a mistake grow past eight characters.
                    code = String(raw.uppercased()
                        .filter { $0.isLetter || $0.isNumber }
                        .prefix(8))
                }

            if let error {
                Text(error)
                    .font(Theme.secondary)
                    .foregroundStyle(Theme.alert)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 10)
            }

            Button {
                claim()
            } label: {
                HStack(spacing: 8) {
                    if claiming { ProgressView().controlSize(.small).tint(.white) }
                    Text(claiming ? "Pairing…" : "Pair")
                        .font(.system(size: 15, weight: .semibold))
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 13)
                .background(Capsule().fill(code.count == 8 ? Theme.brand : Theme.inkGhost))
                .foregroundStyle(.white)
            }
            .buttonStyle(.plain)
            .disabled(code.count != 8 || claiming)
            .padding(.top, 16)

            Spacer(minLength: 0)
        }
        .padding(Theme.margin)
        .padding(.top, 10)
        .background(Theme.paper)
        .task { focused = true }
    }

    private func claim() {
        claiming = true
        error = nil
        Task {
            do {
                try await syncService.api.pair(code: code,
                                               deviceName: UIDevice.current.name)
                claiming = false
                onPaired()
            } catch is URLError {
                claiming = false
                self.error = "Couldn't reach the engine — this phone is trying \(APIClient.defaultBaseURL.host ?? "an unknown host"). Same Wi-Fi as the Mac? Scanning the wizard's QR sets the address."
            } catch {
                claiming = false
                self.error = "That code didn't take — it may have expired or been used. Mint a fresh one on the Mac and try again."
            }
        }
    }
}
