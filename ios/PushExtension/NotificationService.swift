import UserNotifications

/// §v3 workstream 5 — the moment a relayed knock becomes words.
///
/// The relay signs pushes it cannot read: a fixed placeholder, marked
/// `mutable-content`, carrying only opaque ids. iOS hands that payload here
/// before showing anything, and this extension asks the user's own engine —
/// bearer token from the shared Keychain, address recorded by the app —
/// what the notification actually says. The words travel engine → phone,
/// never through anyone's cloud.
///
/// Every path out of here delivers *something*: the fetched words when the
/// engine answers, the placeholder when it doesn't ("something moved" is
/// true either way), and iOS's expiry handler delivers whatever is best so
/// far. A push must never be swallowed by its own privacy machinery.
///
/// Build note: this target sets `ENABLE_DEBUG_DYLIB: NO`. Xcode's previews
/// machinery otherwise splits Debug builds into a stub executable plus a
/// .debug.dylib holding the real code — and the extension point resolves
/// NSExtensionPrincipalClass against the stub, finds nothing, and silently
/// shows the placeholder forever.
final class NotificationService: UNNotificationServiceExtension {

    private var handler: ((UNNotificationContent) -> Void)?
    private var best: UNMutableNotificationContent?

    override func didReceive(_ request: UNNotificationRequest,
                             withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        handler = contentHandler
        let mutable = request.content.mutableCopy() as? UNMutableNotificationContent
        best = mutable

        let info = request.content.userInfo
        guard let mutable,
              info["relay"] != nil,
              let base = TokenStore.serverURL,
              let token = TokenStore.token,
              var components = URLComponents(url: base.appendingPathComponent("push/card"),
                                             resolvingAgainstBaseURL: false)
        else {
            NSLog("LoosePush: no relay marker or no credentials — delivering placeholder")
            contentHandler(request.content)
            return
        }

        var query: [URLQueryItem] = []
        if let findingId = info["finding_id"] as? String {
            query.append(URLQueryItem(name: "finding_id", value: findingId))
        }
        if let threadId = info["thread_id"] as? String {
            query.append(URLQueryItem(name: "thread_id", value: threadId))
        }
        guard !query.isEmpty else {
            contentHandler(request.content)
            return
        }
        components.queryItems = query
        guard let url = components.url else {
            contentHandler(request.content)
            return
        }

        var fetch = URLRequest(url: url, timeoutInterval: 20)
        fetch.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        URLSession.shared.dataTask(with: fetch) { [weak self] data, response, error in
            defer { self?.finish() }
            let status = (response as? HTTPURLResponse)?.statusCode ?? -1
            guard let data, status == 200,
                  let card = try? JSONDecoder().decode(Card.self, from: data)
            else {
                NSLog("LoosePush: card fetch failed status=%d error=%@ — placeholder stands",
                      status, error.map(String.init(describing:)) ?? "none")
                return
            }
            mutable.title = card.title
            mutable.body = card.body
            if let threadId = card.thread_id {
                // The tap has to know which door — same key the app's
                // PushRouter already reads on direct pushes.
                var userInfo = mutable.userInfo
                userInfo["thread_id"] = threadId
                mutable.userInfo = userInfo
            }
        }.resume()
    }

    override func serviceExtensionTimeWillExpire() {
        finish()
    }

    private func finish() {
        guard let handler, let best else { return }
        self.handler = nil
        handler(best)
    }

    private struct Card: Decodable {
        let title: String
        let body: String
        let thread_id: String?
    }
}
