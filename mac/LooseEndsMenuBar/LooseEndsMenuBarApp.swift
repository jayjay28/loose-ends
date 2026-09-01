import SwiftUI

/// §v3 — the engine, made visible.
///
/// One icon in the menu bar that answers two questions without being asked:
/// *is it running*, and *how do I stop it*. Everything else in the panel is
/// there to make those two answers trustworthy.
@main
struct LooseEndsMenuBarApp: App {
    @State private var engine = EngineMonitor()

    var body: some Scene {
        MenuBarExtra {
            MenuPanel(engine: engine)
        } label: {
            // The icon carries the state. A closed loop when it's working, a
            // broken one when it isn't — legible at a glance and at 16pt,
            // which a coloured dot is not.
            Image(systemName: icon)
                .task { engine.start() }
        }
        .menuBarExtraStyle(.window)
    }

    private var icon: String {
        switch engine.state {
        case .running:      return "infinity"
        case .paused:       return "infinity"          // dimmed by the panel's copy
        case .unreachable:  return "exclamationmark.triangle"
        case .notInstalled: return "questionmark.circle"
        }
    }
}
