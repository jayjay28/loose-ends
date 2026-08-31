import Foundation
import Security

/// §v3 — where the engine credentials live: the Keychain, and nowhere else.
///
/// Two items — the bearer token and the engine's URL — in a **shared access
/// group**, because two processes need them: the app, and the notification
/// service extension that turns a relayed knock into words. Both targets
/// list `dev.clyon.looseends.shared` first in their entitlements, so writes
/// land there by default and no team prefix is ever hardcoded — the same
/// project builds under a stranger's team untouched.
///
/// `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`: readable while the
/// phone is locked (pushes arrive locked; the extension must fetch), never
/// in an off-device backup.
enum TokenStore {
    private static let service = "app.looseends.api-token"
    private static let tokenAccount = "engine"
    private static let urlAccount = "engine-url"

    // MARK: the bearer token

    static var token: String? { read(tokenAccount) }
    static func save(_ token: String) { write(tokenAccount, token) }
    static func clear() { delete(tokenAccount); delete(urlAccount) }

    // MARK: the engine's address

    /// Recorded by the app each launch; read by the extension, which has no
    /// Info.plist override chain of its own to resolve.
    static var serverURL: URL? { read(urlAccount).flatMap(URL.init(string:)) }
    static func recordServerURL(_ url: URL) { write(urlAccount, url.absoluteString) }

    /// Items written before the shared group existed live in the app's
    /// private group, invisible to the extension. Rewriting them once moves
    /// them — reads search every accessible group, writes land in the
    /// shared one. Called at launch; a no-op ever after.
    static func migrateToSharedGroup() {
        if let token { save(token) }
    }

    // MARK: - keychain plumbing

    private static func read(_ account: String) -> String? {
        var query = base(account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func write(_ account: String, _ value: String) {
        var query = base(account)
        // Replace-not-update: simpler than SecItemUpdate's second dictionary,
        // and it is also what moves a private-group item into the shared one.
        SecItemDelete(query as CFDictionary)
        query[kSecValueData as String] = Data(value.utf8)
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(query as CFDictionary, nil)
    }

    private static func delete(_ account: String) {
        SecItemDelete(base(account) as CFDictionary)
    }

    private static func base(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }
}
