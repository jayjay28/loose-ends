import Foundation
import Observation

/// What the engine is doing, and the two verbs that change it.
///
/// The whole reason this app exists, from the first stranger's walkthrough:
/// *"I can't tell if the service is running, and I want to be able to close
/// it."* A background process reading your mail and messages with no visible
/// switch is unsettling no matter how true the privacy claim is — so the
/// answer is a switch you can see, not a longer promise.
@MainActor
@Observable
final class EngineMonitor {
    enum State: Equatable {
        case running(openItems: Int)
        /// Running, reading, and thinking with rules instead of a model —
        /// usually a key whose account ran out of credit. Items still appear,
        /// at a fraction of the quality, which is why this needs its own
        /// state rather than hiding inside `running`.
        case degraded(openItems: Int, why: String)
        case paused
        case unreachable          // installed, not answering — starting, or wedged
        case notInstalled

        var isRunning: Bool {
            switch self {
            case .running, .degraded: return true
            default: return false
            }
        }
    }

    private(set) var state: State = .unreachable
    private(set) var lastChecked: Date?
    /// The engine's own account of what it can read. Shown verbatim rather
    /// than summarised: "what is it reading?" deserves a literal answer.
    private(set) var sources: [(name: String, ok: Bool)] = []
    private(set) var spendToday: String?
    private(set) var lastError: String?

    /// The installer supports `PORT=` and `LABEL=` overrides for running an
    /// engine beside a live one, so neither can be assumed. Read the launch
    /// agent that is actually installed and take its word.
    @ObservationIgnored private lazy var label: String = Self.installedLabel()
    @ObservationIgnored private lazy var base: URL =
        URL(string: "http://127.0.0.1:\(Self.installedPort())")!

    private static func agents() -> [URL] {
        let dir = URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/LaunchAgents")
        return ((try? FileManager.default.contentsOfDirectory(at: dir,
                    includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.lastPathComponent.contains("looseends") }
    }

    private static func installedLabel() -> String {
        agents().first?.deletingPathExtension().lastPathComponent ?? "com.looseends.api"
    }

    private static func installedPort() -> Int {
        for agent in agents() {
            guard let text = try? String(contentsOf: agent, encoding: .utf8),
                  let range = text.range(of: "--port ") else { continue }
            let digits = text[range.upperBound...].prefix { $0.isNumber }
            if let port = Int(digits) { return port }
        }
        return 8000
    }
    private var timer: Timer?

    private var plistPath: URL {
        URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }

    func start() {
        Task { await refresh() }
        // Ten seconds is often enough to feel live and rare enough to cost
        // nothing — this is a status light, not a monitor.
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
    }

    // MARK: - reading the state

    func refresh() async {
        guard FileManager.default.fileExists(atPath: plistPath.path) else {
            state = .notInstalled
            return
        }
        // launchd knows whether the job is loaded; the socket knows whether it
        // is actually answering. Both, because "loaded but wedged" is a real
        // state and the difference is what the user is asking about.
        let loaded = jobIsLoaded()
        var request = URLRequest(url: base.appendingPathComponent("health"))
        request.timeoutInterval = 3

        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse, http.statusCode == 200,
              let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            state = loaded ? .unreachable : .paused
            lastChecked = Date()
            return
        }

        let open = body["open_items"] as? Int ?? 0
        lastError = body["llm_last_error"] as? String
        if body["degraded_since"] is String {
            state = .degraded(openItems: open,
                              why: lastError ?? "no model is answering")
        } else {
            state = .running(openItems: open)
        }
        lastChecked = Date()
        sources = [
            ("Mail", body["mail_readable"] as? Bool ?? false),
            ("Claude key", body["claude_configured"] as? Bool ?? false),
            ("Notifications", body["apns_configured"] as? Bool ?? false),
        ]
        if let spend = body["spend_today"] as? [String: Any],
           let tokens = spend["tokens"] as? [String: Any],
           let usd = tokens["estimated_usd"] as? Double {
            spendToday = String(format: "$%.2f today", usd)
        }
    }

    private func jobIsLoaded() -> Bool {
        let out = run("/bin/launchctl", ["print", "gui/\(getuid())/\(label)"])
        return out.status == 0
    }

    // MARK: - the two verbs

    /// Stop reading. This unloads the launchd job, so the engine is genuinely
    /// not running — not merely hidden. That distinction is the entire point:
    /// a switch that only hides the icon would be the dishonest version.
    func pause() {
        _ = run("/bin/launchctl", ["bootout", "gui/\(getuid())/\(label)"])
        Task { await refresh() }
    }

    func resume() {
        _ = run("/bin/launchctl", ["bootstrap", "gui/\(getuid())", plistPath.path])
        Task {
            // launchd needs a beat, and uvicorn a beat more.
            try? await Task.sleep(for: .seconds(2))
            await refresh()
        }
    }

    func openSetup() { open(base.appendingPathComponent("setup")) }

    func openLogs() {
        open(URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/Logs/loose-ends"))
    }

    // MARK: - plumbing

    private func open(_ url: URL) {
        _ = run("/usr/bin/open", [url.absoluteString])
    }

    @discardableResult
    private func run(_ path: String, _ arguments: [String]) -> (status: Int32, out: String) {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: path)
        task.arguments = arguments
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = Pipe()
        do {
            try task.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            task.waitUntilExit()
            return (task.terminationStatus, String(data: data, encoding: .utf8) ?? "")
        } catch {
            return (-1, "")
        }
    }
}
