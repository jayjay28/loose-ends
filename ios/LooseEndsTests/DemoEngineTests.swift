import XCTest
@testable import Lifeline

/// §v3 ws6 — the crafted world speaks the real wire format. Every payload the
/// demo serves must decode through the same snake_case decoder the live
/// client uses, or a demo screen would fail in a way the real app never does.
final class DemoEngineTests: XCTestCase {
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    func testTheStackDecodesLikeTheRealThing() async throws {
        let data = await DemoEngine.shared.respond("GET", "/threads", body: nil)
        let stack = try decoder.decode(ThreadStack.self, from: XCTUnwrap(data))
        XCTAssertGreaterThanOrEqual(stack.threads.count, 5)
        XCTAssertTrue(stack.threads.contains { $0.lane == .hot },
                      "the demo must show a move waiting")
        XCTAssertTrue(stack.threads.contains { $0.deadline != nil },
                      "the demo must show a deadline chip")
        XCTAssertTrue(stack.threads.contains { $0.isResolved },
                      "the demo must show a tied-off lane")
    }

    func testEveryThreadOnTheStackHasADetail() async throws {
        let engine = DemoEngine()
        let data = await engine.respond("GET", "/threads", body: nil)
        let stack = try decoder.decode(ThreadStack.self, from: XCTUnwrap(data))
        for thread in stack.threads {
            let detail = await engine.respond("GET", "/threads/\(thread.id)", body: nil)
            XCTAssertNotNil(try decoder.decode(ThreadDetail.self, from: XCTUnwrap(detail)),
                            "no detail for \(thread.id)")
        }
    }

    func testResolveStrikesTheLaneAndTheDetailAgrees() async throws {
        let engine = DemoEngine()
        let data = await engine.respond("POST", "/threads/demo-bag/resolve", body: nil)
        let thread = try decoder.decode(LifeThread.self, from: XCTUnwrap(data))
        XCTAssertTrue(thread.isResolved)
        let detailData = await engine.respond("GET", "/threads/demo-bag", body: nil)
        let detail = try decoder.decode(ThreadDetail.self, from: XCTUnwrap(detailData))
        XCTAssertTrue(detail.thread.isResolved, "the detail screen must agree with the lane")
    }

    func testConfirmingTheClosureResolvesItsThread() async throws {
        let engine = DemoEngine()
        _ = await engine.respond("POST", "/threads/closures/demo-close-padel/confirm", body: nil)
        let data = await engine.respond("GET", "/threads", body: nil)
        let stack = try decoder.decode(ThreadStack.self, from: XCTUnwrap(data))
        XCTAssertTrue(stack.threads.first { $0.id == "demo-padel" }?.isResolved ?? false)
        let closures = await engine.respond("GET", "/threads/closures", body: nil)
        XCTAssertEqual(try decoder.decode([ThreadClosure].self, from: XCTUnwrap(closures)).count, 0)
    }

    func testDeclaringLandsOnTheStack() async throws {
        let engine = DemoEngine()
        let body = try JSONEncoder().encode(["title": "Return the library books"])
        let created = await engine.respond("POST", "/threads", body: body)
        let thread = try decoder.decode(LifeThread.self, from: XCTUnwrap(created))
        XCTAssertEqual(thread.title, "Return the library books")
        let data = await engine.respond("GET", "/threads", body: nil)
        let stack = try decoder.decode(ThreadStack.self, from: XCTUnwrap(data))
        XCTAssertEqual(stack.threads.first?.id, thread.id, "declared threads land on top")
    }

    func testTheAskDoorAnswers() async throws {
        let data = await DemoEngine.shared.respond("POST", "/ask", body: nil)
        let card = try decoder.decode(AskCard.self, from: XCTUnwrap(data))
        XCTAssertFalse(card.answer.isEmpty)
        XCTAssertFalse(card.receipts.isEmpty, "an answer without receipts is a vibe, not an answer")
    }

    func testUnstagedRoutesSayNothing() async {
        let data = await DemoEngine.shared.respond("GET", "/model-of-you", body: nil)
        XCTAssertNil(data)
    }
}
