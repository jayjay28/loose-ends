import SwiftData
import SwiftUI

@main
struct LifelineApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    let container: ModelContainer
    let sync: SyncService

    init() {
        let container = LocalStore.makeContainer()
        self.container = container
        self.sync = SyncService(store: LocalStore(modelContainer: container))
        // §v3 — the notification extension resolves the engine from the
        // shared Keychain: record where we point, and move any token written
        // before the shared group existed.
        TokenStore.recordServerURL(APIClient.defaultBaseURL)
        TokenStore.migrateToSharedGroup()
#if DEBUG
        PushRouter.shared.seedFromLaunchArguments()
#endif
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(\.syncService, sync)
        }
        .modelContainer(container)
    }
}

/// A plain `SyncService` (an actor-backed class, not itself `Observable`)
/// injected via the environment so every screen shares one `APIClient` and
/// one `LocalStore` rather than standing up its own. The default value is a
/// real, working instance backed by an in-memory store — every network call
/// will simply fail offline-safe (cache-first still returns nil, refresh
/// throws and the view shows an error state) rather than crashing, which
/// keeps SwiftUI previews and any view forgetting to check for injection
/// both safe without scattering optional-handling through every screen.
private struct SyncServiceKey: EnvironmentKey {
    static let defaultValue = SyncService(store: LocalStore(modelContainer: LocalStore.makeContainer(inMemory: true)))
}

extension EnvironmentValues {
    var syncService: SyncService {
        get { self[SyncServiceKey.self] }
        set { self[SyncServiceKey.self] = newValue }
    }
}
