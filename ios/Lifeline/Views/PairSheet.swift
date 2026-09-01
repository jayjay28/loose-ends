import SwiftUI
import UIKit

/// §v3 — how this phone earns its way into an engine.
///
/// Presented whenever the API answers 401, and as the floor under the QR
/// scanner: no camera, no permission, or a simulator all land here.
///
/// **It asks where the Mac is, because nothing else does.** The build used to
/// ship with an engine address baked in — the author's own — so a code minted
/// on a stranger's Mac was sent to his, which had never heard of it, and the
/// app blamed the code: "expired or already used." The address is the first
/// question now, answered by Bonjour where it can be and by the person where
/// it can't.
struct PairSheet: View {
    var onPaired: () -> Void

    @Environment(\.syncService) private var syncService
    @State private var code = ""
    @State private var address = ""
    @State private var found: [URL] = []
    @State private var looking = true
    @State private var claiming = false
    @State private var error: String?
    @FocusState private var focused: Bool

    private var doors: [URL] {
        // What the person typed wins; otherwise everything Bonjour saw.
        if let typed = normalised(address) { return [typed] }
        return found
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Text("PAIR THIS DEVICE")
                    .font(Theme.label).tracking(0.8)
                    .foregroundStyle(Theme.inkFaint)
                    .padding(.bottom, 10)

                Text("Point at the Mac")
                    .font(Theme.serif(25, .semibold))
                    .foregroundStyle(Theme.ink)
                    .padding(.bottom, 6)

                Text("On your Mac, open **localhost:8000/setup** and tap *Show a pairing code*. Codes live for ten minutes and work once.")
                    .font(Theme.secondary)
                    .foregroundStyle(Theme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 16)

                whichMac
                codeField

                if let error {
                    Text(error)
                        .font(Theme.secondary)
                        .foregroundStyle(Theme.alert)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 10)
                }

                Button(action: claim) {
                    HStack(spacing: 8) {
                        if claiming { ProgressView().controlSize(.small).tint(.white) }
                        Text(claiming ? "Pairing…" : "Pair")
                            .font(.system(size: 15, weight: .semibold))
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 13)
                    .background(Capsule().fill(ready ? Theme.brand : Theme.inkGhost))
                    .foregroundStyle(.white)
                }
                .buttonStyle(.plain)
                .disabled(!ready || claiming)
                .padding(.top, 16)
            }
            .padding(Theme.margin)
            .padding(.top, 10)
        }
        .background(Theme.paper)
        .task {
            focused = true
            found = await EngineLocator.shared.discover()
            looking = false
        }
    }

    private var ready: Bool { code.count == 8 && !doors.isEmpty }

    /// Which Mac — found for you where possible, typed where not.
    private var whichMac: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("WHICH MAC")
                .font(Theme.label).tracking(0.8)
                .foregroundStyle(Theme.inkFaint)

            if looking {
                HStack(spacing: 7) {
                    ProgressView().controlSize(.small)
                    Text("looking on this network…")
                        .font(.system(size: 12.5)).foregroundStyle(Theme.inkFaint)
                }
            } else if !found.isEmpty && normalised(address) == nil {
                ForEach(found, id: \.absoluteString) { url in
                    HStack(spacing: 7) {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 11)).foregroundStyle(Theme.brand)
                        Text(url.host ?? url.absoluteString)
                            .font(.system(size: 13, design: .monospaced))
                            .foregroundStyle(Theme.ink)
                    }
                }
            }

            TextField(found.isEmpty ? "192.168.1.20:8000" : "or type an address",
                      text: $address)
                .font(.system(size: 14, design: .monospaced))
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .padding(.vertical, 9).padding(.horizontal, 11)
                .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chip))

            if found.isEmpty && !looking && normalised(address) == nil {
                Text("Nothing found nearby. The setup page shows the address under the QR code.")
                    .font(.system(size: 11.5))
                    .foregroundStyle(Theme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.bottom, 16)
    }

    private var codeField: some View {
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
                code = String(raw.uppercased()
                    .filter { $0.isLetter || $0.isNumber }
                    .prefix(8))
            }
    }

    /// A host, a host:port, or a full URL — all the shapes a person types.
    private func normalised(_ raw: String) -> URL? {
        let text = raw.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return nil }
        if text.contains("://") { return URL(string: text) }
        return URL(string: text.contains(":") ? "http://\(text)" : "http://\(text):8000")
    }

    private func claim() {
        claiming = true
        error = nil
        Task {
            do {
                try await syncService.api.pair(code: code,
                                               deviceName: UIDevice.current.name,
                                               doors: doors)
                await EngineLocator.shared.remember(urls: doors.map(\.absoluteString))
                claiming = false
                onPaired()
            } catch is URLError {
                claiming = false
                let tried = doors.first?.host ?? "that address"
                self.error = "Couldn't reach \(tried). Check the phone and Mac are on the same Wi-Fi, and that the address matches the one under the QR code."
            } catch {
                claiming = false
                self.error = "That Mac didn't recognise the code — it may have expired or been used. Show a fresh one and try again."
            }
        }
    }
}
