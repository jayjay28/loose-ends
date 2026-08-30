import XCTest
@testable import Lifeline

/// §v3 ws4 — the door list's pure logic. Bonjour browsing and health probes
/// touch the network and are exercised on hardware, not here.
final class EngineLocatorTests: XCTestCase {
    override func setUp() {
        super.setUp()
        UserDefaults.standard.removeObject(forKey: EngineLocator.knownURLsKey)
    }

    override func tearDown() {
        UserDefaults.standard.removeObject(forKey: EngineLocator.knownURLsKey)
        super.tearDown()
    }

    func testLearnedDoorsComeFirstThenBakedDefaultThenLocalhost() async {
        await EngineLocator.shared.remember(urls: ["http://192.168.1.20:8000",
                                                   "https://mac.tailnet.ts.net"])
        let doors = EngineLocator.shared.candidates().map(\.absoluteString)
        XCTAssertEqual(Array(doors.prefix(2)),
                       ["http://192.168.1.20:8000", "https://mac.tailnet.ts.net"])
        XCTAssertEqual(doors.last, "http://127.0.0.1:8000")
    }

    func testTheFailedDoorIsNeverKnockedOnAgainFirst() async {
        await EngineLocator.shared.remember(urls: ["http://192.168.1.20:8000",
                                                   "https://mac.tailnet.ts.net"])
        let doors = EngineLocator.shared
            .candidates(excluding: URL(string: "http://192.168.1.20:8000")!)
            .map(\.absoluteString)
        XCTAssertFalse(doors.contains("http://192.168.1.20:8000"))
        XCTAssertEqual(doors.first, "https://mac.tailnet.ts.net")
    }

    func testDuplicateDoorsCollapse() async {
        await EngineLocator.shared.remember(urls: ["http://127.0.0.1:8000",
                                                   "http://127.0.0.1:8000"])
        let doors = EngineLocator.shared.candidates().map(\.absoluteString)
        XCTAssertEqual(doors.filter { $0 == "http://127.0.0.1:8000" }.count, 1)
    }

    func testAnEmptyListIsNeverRemembered() async {
        await EngineLocator.shared.remember(urls: ["http://192.168.1.20:8000"])
        await EngineLocator.shared.remember(urls: [])
        XCTAssertEqual(UserDefaults.standard.stringArray(forKey: EngineLocator.knownURLsKey),
                       ["http://192.168.1.20:8000"])
    }
}
