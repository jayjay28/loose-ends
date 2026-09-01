import Foundation
import Network
import os

/// §v3 workstream 4 — how the phone finds the engine.
///
/// The engine has more than one door: its LAN address at home, its tailnet
/// name from anywhere, and whatever Bonjour can see right now. The phone
/// learns the list at pairing (`/pair/claim` returns it), keeps it fresh from
/// `/transport`, and when the current door stops answering it walks the list
/// instead of showing an error for a problem the network already solved.
///
/// Nothing here decides trust — a discovered engine still 401s everything
/// until this phone's token says otherwise. Discovery is convenience;
/// the gate is the security.
actor EngineLocator {
    static let shared = EngineLocator()

    /// Doors learned from the engine itself, newest list wins. UserDefaults
    /// rather than Keychain: addresses are routing hints, not secrets.
    static let knownURLsKey = "LifelineEngineURLs"

    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    // MARK: - the list

    func remember(urls: [String]) {
        let cleaned = urls.compactMap { URL(string: $0)?.absoluteString }
        guard !cleaned.isEmpty else { return }
        UserDefaults.standard.set(cleaned, forKey: Self.knownURLsKey)
    }

    /// Every door worth knocking on, most likely first, no duplicates:
    /// the engine's own list, then the baked-in default, then localhost
    /// (the simulator's engine). The caller excludes the door that just
    /// failed so failover never retries the same address first.
    nonisolated func candidates(excluding failed: URL? = nil) -> [URL] {
        var doors: [URL] = (UserDefaults.standard.stringArray(forKey: Self.knownURLsKey) ?? [])
            .compactMap(URL.init(string:))
        // Nothing baked in: the only doors are the ones this phone was told
        // about by an engine it actually paired with, plus localhost for the
        // simulator. See `APIClient.defaultBaseURL` for what a shipped
        // default address cost.
        doors.append(URL(string: "http://127.0.0.1:8000")!)
        var seen = Set<String>()
        return doors.filter { url in
            guard url.absoluteString != failed?.absoluteString else { return false }
            return seen.insert(url.absoluteString).inserted
        }
    }

    // MARK: - resolution

    /// The first door that answers, or nil. Knocks on the known list first
    /// (cheap, no permission prompt), then asks Bonjour — which may show
    /// iOS's local-network dialog, so it runs only after the list came up
    /// empty and only when the engine is genuinely lost.
    func firstHealthy(excluding failed: URL? = nil) async -> URL? {
        for door in candidates(excluding: failed) where await alive(door) {
            return door
        }
        for door in await discover() where door.absoluteString != failed?.absoluteString {
            if await alive(door) { return door }
        }
        return nil
    }

    /// Alive means *this phone's* engine answered — not merely that something
    /// answered.
    ///
    /// A 401 used to count, on the reasoning that an unpaired engine is still
    /// the right door. It isn't: any stranger's engine on the network returns
    /// 401 too, and adopting one persists a wrong address that the next
    /// pairing code is then sent to. When we hold a token, the door has to
    /// accept it. Only a phone with no token at all may settle for "answers".
    private func alive(_ base: URL) async -> Bool {
        var request = URLRequest(url: base.appendingPathComponent("health"),
                                 timeoutInterval: 3)
        let token = TokenStore.token
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        guard let (_, response) = try? await session.data(for: request),
              let http = response as? HTTPURLResponse else { return false }
        return token == nil ? http.statusCode < 500 : http.statusCode == 200
    }

    // MARK: - bonjour

    /// A short browse for `_loose-ends._tcp` — the zero-typing path home.
    /// Two seconds is enough for mDNS on a healthy network, and giving up
    /// quietly is fine: discovery is one source of doors, never the only one.
    func discover(wait seconds: Double = 2.0) async -> [URL] {
        let browser = NWBrowser(
            for: .bonjour(type: "_loose-ends._tcp", domain: nil),
            using: NWParameters(tls: nil, tcp: NWProtocolTCP.Options()))
        browser.start(queue: .global())
        try? await Task.sleep(for: .seconds(seconds))
        let endpoints = browser.browseResults.map(\.endpoint)
        browser.cancel()

        var doors: [URL] = []
        for endpoint in endpoints {
            if let url = await Self.resolve(endpoint) { doors.append(url) }
        }
        return doors
    }

    /// Bonjour names a service; HTTP needs a host and port. A TCP connection
    /// resolves one to the other — connect, read the remote address, hang up.
    private static func resolve(_ endpoint: NWEndpoint) async -> URL? {
        let queue = DispatchQueue(label: "app.looseends.locator.resolve")
        return await withCheckedContinuation { continuation in
            let connection = NWConnection(to: endpoint, using: .tcp)
            let resumed = OSAllocatedUnfairLock(initialState: false)
            let finish: @Sendable (URL?) -> Void = { url in
                let first = resumed.withLock { done -> Bool in
                    if done { return false }
                    done = true
                    return true
                }
                guard first else { return }
                connection.cancel()
                continuation.resume(returning: url)
            }
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    guard case .hostPort(let host, let port)? =
                        connection.currentPath?.remoteEndpoint else { return finish(nil) }
                    switch host {
                    case .ipv4(let address):
                        finish(URL(string: "http://\(address):\(port)"))
                    case .ipv6(let address):
                        // Scope suffixes (%en0) are meaningless in a URL.
                        let bare = "\(address)".split(separator: "%").first.map(String.init) ?? "\(address)"
                        finish(URL(string: "http://[\(bare)]:\(port)"))
                    case .name(let name, _):
                        finish(URL(string: "http://\(name):\(port)"))
                    @unknown default:
                        finish(nil)
                    }
                case .failed, .cancelled:
                    finish(nil)
                default:
                    break
                }
            }
            connection.start(queue: queue)
            queue.asyncAfter(deadline: .now() + 3) { finish(nil) }
        }
    }
}
