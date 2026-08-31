import AVFoundation
import SwiftUI
import UIKit

/// §v3 workstream 6 — screen 8: the baton returns.
///
/// One scan sets the server URL and auth — the step where self-hosted apps
/// usually lose people (typing IP addresses) becomes a photo. The QR is the
/// wizard's done screen: `{"v":1,"url":…,"urls":[…],"code":…}` — every door
/// the engine has, plus a one-time pairing code.
///
/// Typing the eight characters stays as the floor: no camera, no permission,
/// or a simulator all land on the same claim the scanner uses.
struct PairScanView: View {
    var onPaired: () -> Void
    var onBack: () -> Void

    @Environment(\.syncService) private var syncService
    @State private var claiming = false
    @State private var error: String?
    @State private var typing = false
    @State private var cameraDenied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: onBack) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(Theme.inkSoft)
                    .padding(.vertical, 8)
            }
            .buttonStyle(.plain)

            Text("Point at the Mac")
                .font(Theme.serif(28, .semibold))
                .foregroundStyle(Theme.ink)
                .padding(.bottom, 6)
            Text("Scan the code on the setup screen.")
                .font(.system(size: 15))
                .foregroundStyle(Theme.inkSoft)
                .padding(.bottom, 18)

            ZStack {
                if cameraDenied {
                    VStack(spacing: 10) {
                        Image(systemName: "camera.on.rectangle")
                            .font(.system(size: 28, weight: .light))
                            .foregroundStyle(Theme.inkGhost)
                        Text("No camera here — type the code instead.")
                            .font(.system(size: 13))
                            .foregroundStyle(Theme.inkFaint)
                    }
                } else {
                    QRScanner(paused: claiming) { handle(payload: $0) }
                }
            }
            .frame(maxWidth: .infinity)
            .aspectRatio(1, contentMode: .fit)
            .background(RoundedRectangle(cornerRadius: 18).fill(Theme.chip.opacity(0.6)))
            .clipShape(RoundedRectangle(cornerRadius: 18))
            .padding(.bottom, 14)

            if claiming {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Pairing…").font(Theme.secondary).foregroundStyle(Theme.inkSoft)
                }
                .padding(.bottom, 10)
            }
            if let error {
                Text(error)
                    .font(Theme.secondary)
                    .foregroundStyle(Theme.alert)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 10)
            }

            Button { typing = true } label: {
                Text("Type the code instead")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Theme.brand)
            }
            .buttonStyle(.plain)

            Spacer()

            Text("Pairs over your own network. Works from anywhere once your Mac and phone share a tailnet.")
                .font(.system(size: 12))
                .foregroundStyle(Theme.inkFaint)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 22)
        }
        .padding(.horizontal, Theme.margin)
        .task {
            let granted = await AVCaptureDevice.requestAccess(for: .video)
            cameraDenied = !granted
        }
        .sheet(isPresented: $typing) {
            PairSheet {
                typing = false
                onPaired()
            }
            .presentationDetents([.medium])
            .presentationCornerRadius(22)
        }
    }

    /// The wizard's QR, decoded and spent. The URL is adopted *before* the
    /// claim so the claim itself travels to the engine the code came from.
    private func handle(payload: String) {
        struct QR: Decodable { let url: String?; let urls: [String]?; let code: String? }
        guard !claiming,
              let data = payload.data(using: .utf8),
              let qr = try? JSONDecoder().decode(QR.self, from: data),
              let code = qr.code else {
            error = "That's not a Loose Ends pairing code — scan the QR on the Mac's setup screen."
            return
        }
        claiming = true
        error = nil
        Task {
            if let raw = qr.url, let url = URL(string: raw) {
                await syncService.api.rebase(to: url)
            }
            await EngineLocator.shared.remember(urls: qr.urls ?? [qr.url].compactMap { $0 })
            do {
                try await syncService.api.pair(code: code, deviceName: UIDevice.current.name)
                claiming = false
                onPaired()
            } catch is URLError {
                // The field test's hour of confusion: an unreachable engine
                // used to wear the "code expired" message. Say which it is.
                claiming = false
                self.error = "Couldn't reach the engine — the phone and Mac need the same Wi-Fi (or a shared tailnet), and the engine must allow network connections."
            } catch {
                claiming = false
                self.error = "That code didn't take — it may have expired or been used. Mint a fresh one on the Mac and scan again."
            }
        }
    }
}

/// The camera, reduced to one job: report QR payloads. AVFoundation because
/// it works back to every supported OS and never needs a photo library.
private struct QRScanner: UIViewRepresentable {
    var paused: Bool
    var onCode: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onCode: onCode) }

    func makeUIView(context: Context) -> ScannerPreview {
        let view = ScannerPreview()
        context.coordinator.attach(to: view)
        return view
    }

    func updateUIView(_ view: ScannerPreview, context: Context) {
        context.coordinator.paused = paused
    }

    static func dismantleUIView(_ view: ScannerPreview, coordinator: Coordinator) {
        coordinator.stop()
    }

    final class Coordinator: NSObject, AVCaptureMetadataOutputObjectsDelegate {
        var paused = false
        private let onCode: (String) -> Void
        private let session = AVCaptureSession()
        private var lastPayload = ""

        init(onCode: @escaping (String) -> Void) { self.onCode = onCode }

        func attach(to view: ScannerPreview) {
            guard let device = AVCaptureDevice.default(for: .video),
                  let input = try? AVCaptureDeviceInput(device: device),
                  session.canAddInput(input) else { return }
            session.addInput(input)
            let output = AVCaptureMetadataOutput()
            guard session.canAddOutput(output) else { return }
            session.addOutput(output)
            output.setMetadataObjectsDelegate(self, queue: .main)
            output.metadataObjectTypes = [.qr]
            view.previewLayer.session = session
            view.previewLayer.videoGravity = .resizeAspectFill
            let session = self.session
            DispatchQueue.global(qos: .userInitiated).async { session.startRunning() }
        }

        func stop() {
            let session = self.session
            DispatchQueue.global(qos: .userInitiated).async { session.stopRunning() }
        }

        func metadataOutput(_ output: AVCaptureMetadataOutput,
                            didOutput objects: [AVMetadataObject],
                            from connection: AVCaptureConnection) {
            guard !paused,
                  let qr = objects.compactMap({ $0 as? AVMetadataMachineReadableCodeObject }).first,
                  qr.type == .qr, let payload = qr.stringValue,
                  payload != lastPayload else { return }
            // One fire per distinct payload: the camera reports the same code
            // thirty times a second, and the claim must be sent exactly once.
            lastPayload = payload
            onCode(payload)
        }
    }

    /// A UIView whose backing layer *is* the preview layer, so the video
    /// always fills whatever SwiftUI decides the frame is.
    final class ScannerPreview: UIView {
        override static var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }
}
