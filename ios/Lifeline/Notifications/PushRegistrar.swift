import UIKit
import UserNotifications

/// The missing half of §7c. The server has decided since step 7 which findings
/// clear the interruption bar, queued them, and called `apns.send` — into a
/// dry run, because nothing ever asked iOS for a device token and so the
/// `devices` table was always empty. This is the piece that closes it.
///
/// Registration is deliberately not a launch side effect. Asking for push
/// permission before the user has seen a single thread is the surest way to
/// get a permanent no, and a denied prompt cannot be re-shown from inside the
/// app. `ThreadsView` calls this once the stack is on screen.
@MainActor
final class PushRegistrar: NSObject {
    static let shared = PushRegistrar()

    /// Set by the app delegate so the token can be posted with the same client
    /// every other screen uses.
    var api: APIClient?

    private var registered = false

    /// Ask for permission, then register. Safe to call repeatedly — the work
    /// happens once per launch, and iOS only shows the system prompt the first
    /// time ever.
    func start(api: APIClient) async {
        self.api = api
        guard !registered else { return }
        registered = true

        let granted = await NotificationScheduler.shared.requestPermission()
        // Local notifications still want the permission even when push is
        // refused, so this is not an early return on `granted == false` —
        // but there is no point holding a token we can never present.
        guard granted else { return }
        UIApplication.shared.registerForRemoteNotifications()
    }

    /// Called from the app delegate with the raw token.
    func submit(_ deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        guard let api else { return }
        Task {
            do {
                try await api.registerDevice(token: hex)
            } catch {
                // Not fatal and not worth a UI: the next launch re-registers,
                // and iOS hands back the same token unless the app is
                // reinstalled.
                print("push: device registration failed — \(error)")
            }
        }
    }
}

/// UIKit still owns the remote-notification callbacks; SwiftUI has no
/// equivalent, so the app keeps a delegate purely for these two.
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { @MainActor in PushRegistrar.shared.submit(deviceToken) }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        // The common cause is running in the simulator on a machine with no
        // paid-team push entitlement, which is expected and not an error the
        // user can act on.
        print("push: registration failed — \(error.localizedDescription)")
    }
}

/// Without this, iOS drops any notification that arrives while the app is
/// open — no banner, no sound, no error, nothing in the log. It is the single
/// most common reason a push that Apple accepted appears never to have been
/// sent, and it cost this project exactly one confused round trip.
extension AppDelegate: UNUserNotificationCenterDelegate {
    // `nonisolated` because the protocol hands over non-Sendable UserNotifications
    // types, which Swift 6 refuses to let cross into a main-actor implementation.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }

    /// The tap. Without this the notification is a dead end — it opens the app
    /// to whatever screen it was last on, which for someone who was just told
    /// their bill is past due is the app shrugging.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        // Pulled apart here rather than passed across: `userInfo` is
        // [AnyHashable: Any] and so not Sendable, and two Strings are all the
        // router ever wanted from it.
        let info = response.notification.request.content.userInfo
        let threadId = info["thread_id"] as? String
        let findingId = info["finding_id"] as? String
        await MainActor.run {
            if let threadId {
                PushRouter.shared.arrival = .init(threadId: threadId, findingId: findingId)
            } else {
                // No destination in the payload (a digest, or an engine that
                // couldn't resolve one). Landing on the stack is still a
                // deliberate landing — the old `return` here was the app
                // shrugging.
                PushRouter.shared.landHome()
            }
        }
    }
}
