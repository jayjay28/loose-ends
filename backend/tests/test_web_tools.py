"""§v2.3 — the tools that reach the world.

Every other tool reads or writes the user's own data, which is why a thread
about buying something could only ever be reported as still unbought. These
tests cover the wiring, not the searching: which tool type gets declared, that
the loop never tries to run one locally, and that a paused turn is resumed.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

from lifeline.assistant import registry as reg
from lifeline.extraction import claude


def test_haiku_gets_the_basic_search_variant():
    """Declaring a tool type the model doesn't support 400s the whole request,
    and the loop runs on Haiku by design."""
    tools = reg.web_tools("claude-haiku-4-5-20251001")
    assert [t.schema["type"] for t in tools] == ["web_search_20250305"]


def test_the_capable_models_get_dynamic_filtering():
    """...and it upgrades itself the day LIFELINE_LOOP_MODEL moves off Haiku."""
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8"):
        tools = reg.web_tools(model)
        assert [t.schema["type"] for t in tools] == [
            "web_search_20260209", "web_fetch_20260209"
        ], model


def test_a_server_tool_is_never_dispatched_locally():
    """It runs on the provider's side, so a name in the dispatch table would
    point at nothing."""
    table = reg.by_name(list(reg.READ_TOOLS) + reg.web_tools("claude-opus-5"))
    assert "web_search" not in table
    assert "search_messages" in table


def test_the_thread_tool_set_includes_the_web():
    """Whatever the configured model can afford — on the default (Haiku) that
    is search alone; see `test_haiku_gets_search_without_fetch`."""
    thread = type("T", (), {"id": "t1", "autonomy": None})()
    names = {t.name for t in reg.scoped_for(thread)}
    assert "web_search" in names


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


def test_a_paused_turn_is_resumed_not_taken_as_a_conclusion():
    """`pause_turn` means the server-side tool loop hit its own cap, not that
    the model is done. Treated as a conclusion, a search that needed three
    rounds becomes a report written from one."""
    responses = [
        _Response([_Block(type="text", text="Searching. ")], stop_reason="pause_turn"),
        _Response([_Block(type="text", text="Found three sets under $150.")]),
    ]
    seen: List[Dict[str, Any]] = []

    class _Messages:
        def create(self, **kwargs):
            seen.append(kwargs)
            return responses[len(seen) - 1]

    class _Client:
        messages = _Messages()

    with patch.object(claude, "_client", lambda: _Client()):
        out = claude.complete_with_tools(
            [{"role": "user", "content": "find pyjamas"}],
            tools=[t.schema for t in reg.web_tools("claude-opus-5")],
        )

    assert len(seen) == 2, "the paused turn was never resumed"
    # Resumed by handing the turn straight back — no invented "continue" message.
    assert seen[1]["messages"][-1]["role"] == "assistant"
    assert out["text"] == "Searching. Found three sets under $150."
    assert out["tool_calls"] == []


def test_resumption_is_bounded():
    """A bound, not a target: without one a tool loop that keeps pausing bills
    forever."""
    calls = {"n": 0}

    class _Messages:
        def create(self, **kwargs):
            calls["n"] += 1
            return _Response([_Block(type="text", text="x")], stop_reason="pause_turn")

    class _Client:
        messages = _Messages()

    with patch.object(claude, "_client", lambda: _Client()):
        claude.complete_with_tools([{"role": "user", "content": "hi"}], tools=[])

    assert calls["n"] == claude._MAX_CONTINUATIONS


# --------------------------------------------------- steps that survive
#
# Found while verifying the web tools: a staged move with three real options
# was stored as four hundred single-letter steps.

def test_a_json_string_of_steps_is_parsed_not_iterated():
    """The model hands list arguments over as JSON strings often enough that
    it cannot be assumed away — and a string is iterable, so the naive
    comprehension produced one step per character."""
    out = reg._as_list('["Spalding 54\\" glass, ~$700", "Lifetime 50\\", backordered"]')
    assert out == ['Spalding 54" glass, ~$700', 'Lifetime 50", backordered']


def test_a_plain_string_is_one_step():
    assert reg._as_list("Order the polycarbonate hoop") == ["Order the polycarbonate hoop"]


def test_a_real_list_is_untouched_and_none_is_empty():
    assert reg._as_list(["a", "b"]) == ["a", "b"]
    assert reg._as_list(None) == []


def test_malformed_json_is_kept_whole_rather_than_shredded():
    assert reg._as_list('[broken') == ['[broken']


# ------------------------------------------------------- what it costs
#
# Counting calls hid the thing that actually costs money: a fetched page is
# resent on every turn that follows it.

def test_haiku_gets_search_without_fetch():
    """The basic fetch cannot be capped with `max_content_tokens`, and an
    uncapped page is re-billed every turn. Search carries titles, snippets and
    URLs, which is what naming three options with prices needs."""
    names = [t.name for t in reg.web_tools("claude-haiku-4-5-20251001")]
    assert names == ["web_search"], "an uncappable fetch is back on the cheap model"


def test_the_capable_models_keep_fetch_because_it_can_be_bounded():
    tools = {t.name: t.schema for t in reg.web_tools("claude-opus-5")}
    assert tools["web_fetch"]["max_content_tokens"] == 6000
    assert tools["web_search"]["max_uses"] == 4


def test_tokens_are_counted_not_just_calls():
    from lifeline.extraction import budget
    from lifeline import db

    before = int(db.get_sync_state.__self__ is None) if False else None  # noqa: F841
    budget.record_tokens(1200, 300)
    budget.record_tokens(800, 100)
    import datetime as _dt
    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    assert int(db.get_sync_state(f"llm_tokens_in:{day}")) >= 2000
    assert int(db.get_sync_state(f"llm_tokens_out:{day}")) >= 400


# ------------------------------------------- findings supersede, not stack
#
# One real thread held six findings of which five were the same observation
# with a fresher number. The dedupe compared exact headlines, which never
# matched, because the number is exactly the part that changes.

def test_the_restatement_is_recognised_as_the_same_observation():
    from lifeline import db
    assert db._headline_shape("Deadline is 12 days away") == \
           db._headline_shape("Deadline is 3 days away")
    assert db._headline_shape("Basketball hoop deadline now critically tight") != \
           db._headline_shape("Glass backboard exceeds the $500 budget")


def test_a_newer_finding_retires_the_older_one_of_its_kind():
    from lifeline import db, threads
    from lifeline.models import Finding, FindingKind

    t = threads.create(title="a thread that gets worked twice")
    old = Finding(thread_id=t.id, kind=FindingKind.FINDING, headline="first pass")
    db.save_finding(old)
    new = Finding(thread_id=t.id, kind=FindingKind.FINDING, headline="second pass")
    db.save_finding(new)

    assert db.supersede_findings(t.id, FindingKind.FINDING, new.id) == 1
    by_id = {f.id: f for f in db.thread_findings(t.id)}
    assert by_id[old.id].superseded_at is not None
    assert by_id[new.id].superseded_at is None, "the newest is the current picture"


def test_a_move_and_an_observation_do_not_retire_each_other():
    """They are different kinds and both stay current — a thread can have a
    move ready and something else worth knowing at the same time."""
    from lifeline import db, threads
    from lifeline.models import Finding, FindingKind

    t = threads.create(title="a thread with both")
    obs = Finding(thread_id=t.id, kind=FindingKind.FINDING, headline="something true")
    db.save_finding(obs)
    move = Finding(thread_id=t.id, kind=FindingKind.ACTION, headline="something to do",
                   move_kind="decide", steps=[{"text": "option A"}])
    db.save_finding(move)

    db.supersede_findings(t.id, FindingKind.ACTION, move.id)
    assert {f.id for f in db.thread_findings(t.id) if f.superseded_at is None} == {obs.id, move.id}


def test_history_survives_superseding():
    """Superseded, never deleted: the thread's history is the receipt that the
    system was working."""
    from lifeline import db, threads
    from lifeline.models import Finding, FindingKind

    t = threads.create(title="a thread with a past")
    for n in range(3):
        f = Finding(thread_id=t.id, kind=FindingKind.FINDING, headline=f"pass {n}")
        db.save_finding(f)
        db.supersede_findings(t.id, FindingKind.FINDING, f.id)

    all_of_them = db.thread_findings(t.id)
    assert len(all_of_them) == 3, "history must still be readable"
    assert sum(1 for f in all_of_them if f.superseded_at is None) == 1


def test_a_missing_api_key_is_recorded_not_swallowed():
    """A scheduled poll with no key in its environment ran the whole cycle,
    worked zero threads, and exited 0 — indistinguishable from a quiet
    morning. It did that twice before anyone noticed."""
    from unittest.mock import patch
    from lifeline import db
    from lifeline.assistant import loop
    from lifeline.extraction import providers

    db.set_sync_state("llm:last_error", "")
    with patch.object(providers, "available", lambda: []):
        assert loop.run_loop("do something", trigger="worker") is None
    assert "no LLM provider configured" in (db.get_sync_state("llm:last_error") or "")


# --------------------------------------------- findings carry structure
#
# Thirteen web-enabled worker passes produced zero links. The model's only
# options were "make a move" or "write an essay", and the prompt tells it to be
# conservative about moves — so every price it paid to look up arrived as
# prose the user had to read and re-derive.

def test_facts_are_legal_on_a_plain_finding():
    """The whole point. `steps` belongs to a move, so research that concluded
    'no move yet' had nowhere to put what it learned."""
    from lifeline import db, threads
    from lifeline.assistant import registry as reg

    t = threads.create(title="a thread that got researched")
    tool = reg.by_name(reg.scoped_for(type("T", (), {"id": t.id, "autonomy": None})()))["record_finding"]
    # No thread_id: the tool is bound to one thread by `finding_tools`, which
    # is what keeps a worker pass from filing findings against its neighbours.
    out = reg.execute(tool, {
        "kind": "finding",
        "headline": "Glass backboards start above the budget",
        "body": "Checked the major retailers.",
        "facts": [
            {"label": 'Spalding 54" polycarbonate', "value": "$350-450, ships 3-5 days",
             "url": "https://example.com/spalding"},
            {"label": 'Lifetime 50" tempered glass', "value": "$959, backordered 3-6 weeks"},
        ],
    })
    assert "error" not in out, out

    f = db.thread_findings(t.id)[0]
    assert f.kind == "finding" and f.move_kind is None
    assert [x["label"] for x in f.facts] == ['Spalding 54" polycarbonate',
                                             'Lifetime 50" tempered glass']
    assert f.facts[0]["url"] == "https://example.com/spalding"
    assert f.facts[1]["url"] == ""


def test_facts_survive_the_shapes_a_model_actually_sends():
    from lifeline.assistant import registry as reg

    # `image` is filled from the linked page, never from the model, so it is
    # empty here — the suite runs offline and no page is ever fetched.
    # Paths, not bare domains: `http://x` is a shop rather than a thing to buy,
    # and `_drop_shared_images` now refuses one as a product link.
    # a JSON string instead of a list — the same failure that shredded steps
    out = reg._as_facts('[{"label": "A", "value": "$10", "url": "http://x/p/a"}]')
    assert out == [{"label": "A", "value": "$10", "url": "http://x/p/a", "image": ""}]
    # bare strings still land as readable facts rather than being dropped
    assert reg._as_facts(["just a note"]) == \
        [{"label": "just a note", "value": "", "url": "", "image": ""}]
    # alternate key names the model reaches for
    assert reg._as_facts([{"name": "B", "detail": "$20", "link": "http://y/p/b"}]) == \
        [{"label": "B", "value": "$20", "url": "http://y/p/b", "image": ""}]
    assert reg._as_facts(None) == []


def test_facts_reach_the_wire():
    from fastapi.testclient import TestClient
    from lifeline import threads, db
    from lifeline.api.app import app
    from lifeline.models import Finding

    t = threads.create(title="a researched thread")
    db.save_finding(Finding(thread_id=t.id, kind="finding", headline="found things",
                            facts=[{"label": "Hoop", "value": "$350", "url": "http://h"}]))
    body = TestClient(app).get(f"/threads/{t.id}").json()
    assert body["findings"][0]["facts"] == [{"label": "Hoop", "value": "$350", "url": "http://h"}]


# ------------------------------------- search results that survive the turn
#
# The leak that made the whole feature notional. A `web_search_result` block
# only exists inside the assistant turn that searched; the loop rebuilt that
# turn from `text` alone, so the prices and urls the model had just been shown
# were gone by the time it called record_finding on the next turn. Thirteen
# web-enabled passes produced zero links and every figure arrived as prose.

def test_the_turns_own_blocks_come_back_from_the_provider():
    """`raw_content` is the carrier. Without it there is nothing to replay."""
    search = _Block(type="web_search_tool_result", tool_use_id="srv_1", content=[])
    responses = [_Response([_Block(type="text", text="Looked."), search])]

    class _Messages:
        def create(self, **kwargs):
            return responses[0]

    class _Client:
        messages = _Messages()

    with patch.object(claude, "_client", lambda: _Client()):
        out = claude.complete_with_tools([{"role": "user", "content": "x"}], tools=[])

    assert search in out["raw_content"], "the search result never left the provider"


def test_a_paused_turn_keeps_every_rounds_blocks():
    """Continuations are one assistant turn to the loop, so all of their blocks
    belong to it — a search that needed three rounds must not come back as one."""
    first = _Block(type="web_search_tool_result", tool_use_id="srv_1", content=[])
    second = _Block(type="web_search_tool_result", tool_use_id="srv_2", content=[])
    responses = [
        _Response([_Block(type="text", text="a"), first], stop_reason="pause_turn"),
        _Response([_Block(type="text", text="b"), second]),
    ]
    seen = {"n": 0}

    class _Messages:
        def create(self, **kwargs):
            seen["n"] += 1
            return responses[seen["n"] - 1]

    class _Client:
        messages = _Messages()

    with patch.object(claude, "_client", lambda: _Client()):
        out = claude.complete_with_tools([{"role": "user", "content": "x"}], tools=[])

    assert first in out["raw_content"] and second in out["raw_content"]


def test_an_assistant_turn_is_replayed_verbatim_not_reassembled():
    """The fix itself. Reassembling from text + tool_use cannot express a
    server-side result, so the search silently became a claim about searching."""
    search = _Block(type="web_search_tool_result", tool_use_id="srv_1", content=[])
    blocks = [_Block(type="text", text="Found it."), search]

    out = claude._to_claude_message({
        "role": "assistant",
        "content": "Found it.",
        "tool_calls": [{"id": "t1", "name": "record_finding", "input": {}}],
        "raw_content": blocks,
    })

    assert out["content"] is blocks, "the turn was rebuilt and the search dropped"


def test_a_provider_without_server_tools_still_reassembles():
    """`raw_content` is opaque and optional — Gemini sends none, and that path
    has to keep working."""
    out = claude._to_claude_message({
        "role": "assistant",
        "content": "hi",
        "tool_calls": [{"id": "t1", "name": "search_messages", "input": {"query": "x"}}],
        "raw_content": None,
    })

    assert [b["type"] for b in out["content"]] == ["text", "tool_use"]


def test_the_loop_hands_the_blocks_to_the_next_turn():
    """End to end: what the model saw on turn one is still in front of it on
    turn two, which is when it records the finding."""
    from lifeline.assistant import loop as loop_mod

    search = _Block(type="web_search_tool_result", tool_use_id="srv_1", content=[])
    look = reg.Tool(
        name="look", description="", input_schema=reg._obj({}), fn=lambda: {"ok": True},
    )
    # The real shape of a worker pass: it searches the web and reads the thread
    # in one turn, then records on the next. The recording turn is the one that
    # needs the prices still to be there.
    turns = [{
        "text": "Searching.",
        "tool_calls": [{"id": "t1", "name": "look", "input": {}}],
        "raw_content": [search],
    }]
    sent: List[List[Dict[str, Any]]] = []

    class _Provider:
        __name__ = "lifeline.extraction.claude"

        @staticmethod
        def complete_with_tools(messages, tools=None, system=None):
            sent.append([dict(m) for m in messages])
            return turns.pop(0) if turns else {
                "text": "Three sets, $128 total.", "tool_calls": [], "raw_content": [],
            }

    with patch.object(loop_mod.providers, "available", lambda: [_Provider]), \
         patch.object(loop_mod.budget, "allow", lambda *_: True), \
         patch.object(loop_mod.budget, "record", lambda *_: None), \
         patch.object(loop_mod.db, "set_sync_state", lambda *_: None), \
         patch.object(loop_mod, "_persist", lambda *a, **k: None):
        loop_mod.run_loop("find pyjamas", trigger="worker", tools=[look], max_iterations=3)

    assert len(sent) >= 2, "the loop never took a second turn"
    replayed = [m for m in sent[-1] if m["role"] == "assistant"]
    assert replayed and replayed[0].get("raw_content") == [search], (
        "the search results were dropped between turns"
    )


# ------------------------------------------------------- what it costs, part 2
#
# One live worker pass billed 369,000 input tokens against a 5,000-token
# prompt. Nothing was cached — `_record_usage` counted cache tokens from day
# one and nothing ever set `cache_control` — and the date stamp sat at the
# front of the system prompt, where its minutes invalidated the prefix on
# every call even if caching had been on.

def test_the_stable_prompt_is_cached_and_the_date_is_not():
    """The breakpoint goes between them. Caching is a prefix match, so a
    timestamp ahead of the prompt makes the whole prompt uncacheable."""
    from lifeline.assistant import loop as loop_mod

    blocks = claude._system_blocks(loop_mod._dated("WORKER PROMPT"))

    assert blocks[0]["text"] == "WORKER PROMPT"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[-1], "the date stamp was cached"
    assert "UTC" in blocks[-1]["text"]


def test_a_plain_string_system_is_still_accepted():
    """Callers that pass a bare string get no breakpoint and no error."""
    assert claude._system_blocks("just a string") == "just a string"


def test_the_newest_turn_carries_the_rolling_breakpoint():
    """The conversation is resent whole every iteration; without this the
    same tool results are paid for again on each of them."""
    convo = [
        {"role": "user", "content": [{"type": "text", "text": "goal"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "big result"},
        ]},
    ]
    claude._cache_the_conversation(convo)

    assert convo[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in convo[0]["content"][-1]


def test_a_string_turn_becomes_a_block_so_it_can_be_cached():
    """The first turn's content is a plain string — the prefix it establishes
    is the one every later turn reads back."""
    convo = [{"role": "user", "content": "find pyjamas"}]
    claude._cache_the_conversation(convo)

    assert convo[0]["content"] == [
        {"type": "text", "text": "find pyjamas", "cache_control": {"type": "ephemeral"}}
    ]


def test_replayed_provider_blocks_are_left_alone():
    """A turn we didn't build carries SDK objects, not dicts — it is already
    inside the cached prefix by the time the next turn is constructed."""
    block = _Block(type="web_search_tool_result", tool_use_id="srv_1", content=[])
    convo = [{"role": "assistant", "content": [block]}]

    claude._cache_the_conversation(convo)  # must not raise

    assert not hasattr(block, "cache_control")


def test_the_request_actually_carries_the_breakpoints():
    """End to end: what reaches the wire is what gets billed."""
    from lifeline.assistant import loop as loop_mod

    seen: Dict[str, Any] = {}

    class _Messages:
        def create(self, **kwargs):
            seen.update(kwargs)
            return _Response([_Block(type="text", text="ok")])

    class _Client:
        messages = _Messages()

    with patch.object(claude, "_client", lambda: _Client()):
        claude.complete_with_tools(
            [{"role": "user", "content": "work this thread"}],
            tools=[],
            system=loop_mod._dated("WORKER PROMPT"),
        )

    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_cached_reads_are_counted_apart_from_fresh_input():
    """A cached read bills at about a tenth of the input rate. Summed into one
    number — which this did — a fully cached day and an uncached one look
    identical in the only figure anyone reads."""
    import datetime as _dt
    from lifeline import db
    from lifeline.extraction import budget

    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    before_in = int(db.get_sync_state(f"llm_tokens_in:{day}") or 0)
    before_cached = int(db.get_sync_state(f"llm_tokens_cached:{day}") or 0)

    usage = _Block(
        input_tokens=500,
        cache_creation_input_tokens=5000,   # a write bills ABOVE input rate
        cache_read_input_tokens=90000,      # a read bills at a tenth
        output_tokens=200,
    )
    claude._record_usage(_Block(usage=usage))

    assert int(db.get_sync_state(f"llm_tokens_in:{day}")) - before_in == 5500
    assert int(db.get_sync_state(f"llm_tokens_cached:{day}")) - before_cached == 90000


# ------------------------------------------- a search url is not a link
#
# Verbatim from a live pass that satisfied the price guard and still failed
# the user: `amazon.com/s?k=Spalding+The+Beast+54+glass`, with the fact's own
# value reading "search for current price". That is the query the worker was
# asked to run, handed back.

def test_a_search_query_url_is_not_a_link():
    assert reg._is_search_url("https://www.amazon.com/s?k=Spalding+54+glass")
    assert reg._is_search_url("https://www.google.com/search?q=basketball+hoop")
    assert reg._is_search_url("https://shop.example.com/results?query=hoop")


def test_a_real_page_is_a_link():
    """Product pages, review articles and category listings all have content
    on them — only the search endpoint itself is refused."""
    for url in (
        "https://www.costco.com/spalding-hybrid.product.4000059263.html",
        "https://www.lifetime.com/lifetime-91130-portable-basketball-hoop",
        "https://surprisesports.com/review/best-hoop-under-500/",
        "https://basketballhoopreview.net/category/500-to-700/",
        "https://secure7.striata.com/pay",
    ):
        assert not reg._is_search_url(url), url


def test_a_search_url_is_demoted_but_the_fact_survives():
    """"Spalding makes a 54-inch glass model" is worth reading as text. What
    it must not do is render as a tappable row that re-runs the search."""
    facts = reg._as_facts([
        {"label": "Spalding The Beast 54\"", "value": "glass backboard",
         "url": "https://www.amazon.com/s?k=Spalding+The+Beast"},
    ])
    assert facts[0]["label"] == 'Spalding The Beast 54"'
    assert facts[0]["url"] == "", "a search url rendered as a tappable link"


def test_search_urls_alone_do_not_satisfy_the_price_guard():
    priced = [
        {"label": "Spalding 54\" glass", "value": "$400-500",
         "url": "https://www.amazon.com/s?k=spalding+54+glass"},
    ]
    assert reg._looks_priced([], "", priced)
    assert not reg._has_link([], priced), "a search url passed as staged work"


def test_a_search_url_in_a_step_does_not_count_either():
    assert not reg._has_link(["Compare at https://www.amazon.com/s?k=hoop"], None)
    assert reg._has_link(["Pay at https://secure7.striata.com/pay"], None)


# --------------------------------------------- one recording per pass
#
# The prompt has said "Exactly one call" since the worker shipped. A live
# hoop pass recorded four times anyway, leaving two findings on screen
# disagreeing about the same hoops.

def _record_tool(thread_id, recorded):
    return reg.finding_tools(thread_id, recorded)[0]


def test_the_second_recording_of_a_pass_is_refused():
    from lifeline import threads
    from lifeline.models import FindingKind

    thread = threads.create(title="a thread worked once")
    recorded = []
    tool = _record_tool(thread.id, recorded)

    first = tool.fn(headline="What I found", kind=FindingKind.FINDING)
    assert "recorded" in first, first
    assert len(recorded) == 1

    second = tool.fn(headline="Actually, something else", kind=FindingKind.FINDING)
    assert "already recorded" in second.get("error", ""), second
    assert len(recorded) == 1, "a second finding was written anyway"


def test_a_refused_call_does_not_burn_the_pass():
    """A refusal leaves `recorded` untouched, so the model still gets to
    record properly once it fixes what was wrong."""
    from lifeline import threads
    from lifeline.models import FindingKind, MoveKind

    thread = threads.create(title="a thread whose first attempt is rejected")
    recorded = []
    tool = _record_tool(thread.id, recorded)

    rejected = tool.fn(
        headline="Three sets for about $120", kind=FindingKind.ACTION,
        move_kind=MoveKind.DECIDE, steps=["Set A, roughly $40"],
    )
    assert "no `facts` with urls" in rejected.get("error", ""), rejected
    assert recorded == []

    ok = tool.fn(
        headline="Three sets, $128 total", kind=FindingKind.ACTION,
        move_kind=MoveKind.DECIDE, steps=["Set A"],
        facts=[{"label": "Set A", "value": "$42.99",
                "url": "https://example.com/product/set-a"}],
    )
    assert "recorded" in ok, ok


# ------------------------------------- a send that priced options is a decide
#
# "The pajamas are enough for her. It's for me." A live pass shopped, found
# Ekouaer at $12-35 and Victoria's Secret at $29.99, and then drafted "do you
# have a preference on colors, style, or material?" — handing the job back to
# the person who asked for it. The prompt had forbidden exactly that since the
# worker shipped, so the fix is a gate rather than a third telling.

def test_counting_distinct_amounts():
    """One figure is a settled fact; several is a comparison."""
    assert reg._priced_options(["confirming the $500 deposit"], "", None) == 1
    # "$12-35" yields one match — the trailing 35 carries no symbol.
    assert reg._priced_options(["Ekouaer $12-35", "VS $29.99"], "", None) == 2
    assert reg._priced_options([], "no money here", None) == 0
    # The same sum written twice is still one sum.
    assert reg._priced_options(["$19.99 each", "totalling $19.99"], "", None) == 1


def test_a_send_that_priced_options_is_refused():
    from lifeline import threads
    from lifeline.models import FindingKind, MoveKind

    thread = threads.create(title="pick pyjamas")
    tool = reg.finding_tools(thread.id)[0]

    out = tool.fn(
        headline="Send final ask to Nia; execute purchase tonight if no response",
        body="Options include Ekouaer on Amazon ($12-35/set) or Victoria's Secret ($29.99/set).",
        kind=FindingKind.ACTION, move_kind=MoveKind.SEND,
        steps=["Message drafted: do you have a preference on colors or style?"],
    )
    assert "this is a `decide`, not a `send`" in out.get("error", ""), out


def test_an_ordinary_send_is_untouched():
    """Every legitimate send in the live database names nought or one amount."""
    from lifeline import threads
    from lifeline.models import FindingKind, MoveKind

    thread = threads.create(title="reply to the 646 number")
    tool = reg.finding_tools(thread.id)[0]

    out = tool.fn(
        headline="Decline, politely",
        kind=FindingKind.ACTION, move_kind=MoveKind.SEND,
        steps=["'Thanks for reaching out, but I can't help with this right now.'"],
    )
    assert "recorded" in out, out


def test_a_send_naming_one_settled_sum_is_fine():
    from lifeline import threads
    from lifeline.models import FindingKind, MoveKind

    thread = threads.create(title="confirm the deposit")
    tool = reg.finding_tools(thread.id)[0]

    out = tool.fn(
        headline="Confirm you sent the $500 deposit",
        kind=FindingKind.ACTION, move_kind=MoveKind.SEND,
        steps=["'Just sent the $500 over — let me know it landed.'"],
    )
    assert "recorded" in out, out


def test_the_decide_shape_is_still_allowed_to_price_freely():
    from lifeline import threads
    from lifeline.models import FindingKind, MoveKind

    thread = threads.create(title="buy the sets")
    tool = reg.finding_tools(thread.id)[0]

    out = tool.fn(
        headline="Buy these three — $103 total",
        kind=FindingKind.ACTION, move_kind=MoveKind.DECIDE,
        steps=["three sets, all shipping today"],
        facts=[
            {"label": "VS SoSoft Cami", "value": "$19.99", "url": "https://example.com/a"},
            {"label": "VS Satin Short", "value": "$39.99", "url": "https://example.com/b"},
            {"label": "Ekouaer Modal", "value": "$42.99", "url": "https://example.com/c"},
        ],
    )
    assert "recorded" in out, out


# --------------------------------------------- a decide has to decide
#
# "Buy 3 pajama sets now from Amazon or Victoria's Secret" — the shape claims a
# decision was made and the headline hands the choice back. Four of ten decide
# moves in the database read like this.

def _move_tool(title):
    from lifeline import threads
    thread = threads.create(title=title)
    return reg.finding_tools(thread.id)[0]


def test_a_decide_that_offers_a_menu_is_refused():
    from lifeline.models import FindingKind, MoveKind
    out = _move_tool("pick pyjamas").fn(
        headline="Buy 3 pajama sets now from Amazon or Victoria's Secret",
        kind=FindingKind.ACTION, move_kind=MoveKind.DECIDE,
        facts=[{"label": "Ekouaer", "value": "$42", "url": "https://example.com/a"}],
    )
    assert "has not decided" in out.get("error", ""), out


def test_a_decide_that_picks_is_recorded():
    from lifeline.models import FindingKind, MoveKind
    out = _move_tool("pick pyjamas properly").fn(
        headline="Buy these three — $103 total, here Thursday",
        kind=FindingKind.ACTION, move_kind=MoveKind.DECIDE,
        facts=[{"label": "Ekouaer", "value": "$42", "url": "https://example.com/a"}],
    )
    assert "recorded" in out, out


def test_the_menu_rule_does_not_reach_other_shapes():
    """Two live `send` moves say "or" legitimately — "clarify if you're
    interested or politely decline" is one message, not a menu."""
    from lifeline.models import FindingKind, MoveKind
    out = _move_tool("reply to 646").fn(
        headline="Send a response: clarify if you're interested or politely decline",
        kind=FindingKind.ACTION, move_kind=MoveKind.SEND,
        steps=["'Thanks — I don't think this is for me.'"],
    )
    assert "recorded" in out, out


def test_a_word_containing_or_is_not_a_menu():
    from lifeline.models import FindingKind, MoveKind
    out = _move_tool("order the hoop").fn(
        headline="Order the Spalding 54\" before Friday",
        kind=FindingKind.ACTION, move_kind=MoveKind.DECIDE,
        # Not `/s` — that is Amazon's search path, and `_is_search_url`
        # rightly demotes it. Product pages only.
        facts=[{"label": "Spalding 54\"", "value": "$528",
                "url": "https://example.com/product/spalding-54"}],
    )
    assert "recorded" in out, out


# ------------------------------------------------ one question, not three

def test_a_move_may_ask_one_thing():
    from lifeline.models import FindingKind, MoveKind
    out = _move_tool("three questions").fn(
        headline="Buy these three — $103 total",
        kind=FindingKind.ACTION, move_kind=MoveKind.DECIDE,
        facts=[{"label": "Ekouaer", "value": "$42", "url": "https://example.com/a"}],
        needs=["wait for her specs or proceed",
               "her size if you don't already know it",
               "which retailer you prefer"],
    )
    assert "asks the user 3 things" in out.get("error", ""), out


def test_one_need_is_fine():
    from lifeline.models import FindingKind, MoveKind
    out = _move_tool("one question").fn(
        headline="Buy these three — $103 total",
        kind=FindingKind.ACTION, move_kind=MoveKind.DECIDE,
        facts=[{"label": "Ekouaer", "value": "$42", "url": "https://example.com/a"}],
        needs=["her size — M unless you say otherwise"],
    )
    assert "recorded" in out, out


def test_a_finding_may_still_carry_several_needs():
    """The rule is about moves. A finding that lists what is unresolved is
    doing its job."""
    from lifeline.models import FindingKind
    out = _move_tool("a plain finding").fn(
        headline="Glass under $500 doesn't exist in four days",
        kind=FindingKind.FINDING,
        needs=["budget or glass", "delivery window", "who installs it"],
    )
    assert "recorded" in out, out
