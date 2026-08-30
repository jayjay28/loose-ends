"""Claude API classifier (§5, §9).

Called from the backend rather than the device so the API key never ships in
the app bundle. Falls back to the heuristic classifier when no key is
configured, which keeps milestones 4-9 testable offline.

**No assistant prefill.** Every call here used to end its message list with
``{"role": "assistant", "content": "{"}`` to nudge the model straight into
JSON. That is rejected with a 400 on Claude Opus 4.6 and later — including
`claude-opus-5`, this project's default extraction model — so all three call
sites had been failing and falling through to the heuristic or to Gemini.
The supported replacement is structured outputs (`output_config.format`),
used where the shape is known; elsewhere the prompts already demand JSON and
`_parse_json` recovers an object from prose or fences.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from ..config import get_config
from . import prompts

log = logging.getLogger(__name__)

MAX_TOKENS = 4096
# How many times a `pause_turn` will be resumed before the turn is taken as
# finished. A bound, not a target: without one a server-side tool loop that
# keeps pausing would bill forever.
_MAX_CONTINUATIONS = 5
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


class ClassifierError(RuntimeError):
    pass


def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ClassifierError("the `anthropic` package is not installed") from exc
    return anthropic.Anthropic(api_key=get_config().anthropic_api_key)


def _parse_json(text: str) -> Dict[str, Any]:
    """Models occasionally wrap JSON in prose or fences; recover what we can."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if not match:
            raise ClassifierError(f"classifier returned no JSON: {text[:200]}")
        return json.loads(match.group(0))


def classify_batch(batch: List[Dict[str, Any]], draft_replies: bool = True) -> Dict[str, Any]:
    """Classify a batch. Returns {"items": [...], "entities": [...]}, both
    unnormalised — §v2.8 phase 3 added the second key to the same read."""
    if not batch:
        return {"items": [], "entities": []}
    cfg = get_config()
    client = _client()
    response = client.messages.create(
        model=cfg.extraction_model,
        max_tokens=MAX_TOKENS,
        system=prompts.SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompts.build_user_prompt(batch, draft_replies)}],
    )
    _record_usage(response, cfg.extraction_model)
    text = "".join(block.text for block in response.content if block.type == "text")
    payload = _parse_json(text)
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ClassifierError("classifier returned a non-list `items`")
    claims = payload.get("entities")
    return {"items": items, "entities": claims if isinstance(claims, list) else []}


def link_followup(new_item: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Optional[str]:
    if not candidates:
        return None
    cfg = get_config()
    try:
        client = _client()
        response = client.messages.create(
            model=cfg.extraction_model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompts.build_link_prompt(new_item, candidates)}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {"links_to_item_id": {"type": ["string", "null"]}},
                        "required": ["links_to_item_id"],
                        "additionalProperties": False,
                    },
                }
            },
        )
        _record_usage(response, cfg.extraction_model)
        text = "".join(block.text for block in response.content if block.type == "text")
        return _parse_json(text).get("links_to_item_id")
    except (ClassifierError, Exception) as exc:  # linking is best-effort
        log.warning("follow-up linking failed: %s", exc)
        return None


def compose_batch_reply(person_name: str, items: List[Dict[str, Any]]) -> Optional[str]:
    """Phase D — fold several owed items to one person into a single reply."""
    text = complete_json(
        prompts.build_batch_reply_prompt(person_name, items),
        system=prompts.BATCH_REPLY_SYSTEM,
        max_tokens=400,
    )
    return _parse_json(text).get("reply")


def complete_with_tools(
    messages: List[Dict[str, Any]],
    *,
    tools: List[Dict[str, Any]],
    system: Optional[str] = None,
    max_tokens: int = MAX_TOKENS,
) -> Dict[str, Any]:
    """One turn of an agentic tool loop (§v1.4).

    Takes the loop's neutral message format and returns
    ``{"text": str, "tool_calls": [{"id", "name", "input"}]}`` — an empty
    ``tool_calls`` means the model concluded. Tool schemas are Claude-native
    ({name, description, input_schema}), which the loop treats as canonical.
    """
    cfg = get_config()
    # The loop's model, not the extraction model — tool routing is high-volume
    # and doesn't need the big model (see config.loop_model).
    kwargs: Dict[str, Any] = {"model": cfg.loop_model, "max_tokens": max_tokens}
    if system:
        kwargs["system"] = _system_blocks(system)

    convo = [_to_claude_message(m) for m in messages]
    _cache_the_conversation(convo)
    text_parts: List[str] = []
    calls: List[Dict[str, Any]] = []
    # Every block the assistant produced this turn, kept verbatim.
    #
    # This is the carrier for server-side tool results, and dropping it was the
    # bug that made the whole web-search feature notional. A `web_search_result`
    # block only ever exists inside the assistant turn that searched; the loop
    # used to rebuild that turn from `text` alone, so by the time the model
    # called record_finding on the next turn the prices and urls it had just
    # been shown were gone from the conversation. It wrote what it could
    # remember, which is how "Amazon Ekouaer: typically $25-50 each" — a
    # guessed range with no product and no link — reached the phone on a day
    # that paid for four real searches.
    raw: List[Any] = []

    # Server-side tools (web search, web fetch) run inside the request, and the
    # API stops with `pause_turn` when that inner loop hits its own iteration
    # cap. It is not a conclusion — it means "ask again and I'll carry on".
    # Treated as one, a search that needed three rounds silently became a
    # thread report written from one.
    for _ in range(_MAX_CONTINUATIONS):
        response = _client().messages.create(messages=convo, tools=tools, **kwargs)
        _record_usage(response, cfg.loop_model)
        text_parts.append("".join(b.text for b in response.content if b.type == "text"))
        calls = [
            {"id": b.id, "name": b.name, "input": dict(b.input)}
            for b in response.content
            if b.type == "tool_use"
        ]
        raw.extend(response.content)
        if response.stop_reason != "pause_turn":
            break
        # Resume by handing the paused turn straight back — the API reads its
        # own trailing server-tool block and continues. Anything added here
        # (a "continue" message) is a new instruction, not a resumption.
        convo.append({"role": "assistant", "content": response.content})

    return {
        "text": "".join(p for p in text_parts if p),
        "tool_calls": calls,
        "raw_content": raw,
    }


def _record_usage(response: Any, model: Optional[str] = None) -> None:
    """Log what the turn actually cost, in tokens.

    Kept here rather than in the loop because this is the only place that sees
    a response object, and a server-side tool's page content lands in
    `input_tokens` without ever passing through the loop as a tool result.

    Every `messages.create` in this module calls this. For a long time only the
    loop did, which meant the meter watched the cheap model and ignored the
    expensive one: `classify_batch` and `complete_json` run on
    `extraction_model`, and on 22 Aug that was Opus spending $10.23 against the
    loop's $1.35 — invisible here, and only findable in the billing console.
    An unmetered call site is worse than no meter, because the number it leaves
    behind looks complete.
    """
    from . import budget

    usage = getattr(response, "usage", None)
    if usage is None:
        return
    # Cache *creation* counts as input: writing an entry bills above the plain
    # input rate, not below it. Only reads are the cheap bucket.
    budget.record_tokens(
        int(getattr(usage, "input_tokens", 0) or 0)
        + int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
        cached_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        model=model or getattr(response, "model", None),
    )


_EPHEMERAL = {"type": "ephemeral"}


def _system_blocks(system: Any) -> Any:
    """Neutral system → Anthropic system, with the cache breakpoint placed.

    Caching renders `tools` → `system` → `messages` and matches on the prefix,
    so a breakpoint on the last stable system block caches the tool schemas
    with it. On this worker that is ~3,500 tokens of schemas plus ~1,900 of
    prompt — comfortably over Haiku's 4,096-token minimum, and byte-identical
    on every pass of every thread. Cached reads bill at a tenth of the input
    rate; the volatile date block after the breakpoint is ~60 tokens paid in
    full, which is the whole cost of knowing what day it is.
    """
    if isinstance(system, str):                       # no marked breakpoint
        return system
    blocks = []
    for part in system:
        if not part.get("text"):
            continue
        block: Dict[str, Any] = {"type": "text", "text": part["text"]}
        if part.get("cache"):
            block["cache_control"] = _EPHEMERAL
        blocks.append(block)
    return blocks or None


def _cache_the_conversation(convo: List[Dict[str, Any]]) -> None:
    """Put a rolling breakpoint on the newest turn, in place.

    This is the one that matters for an agentic loop. The conversation is
    resent whole on every iteration, so by turn ten the pass has paid for the
    same tool results nine times over — one live worker pass billed 369,000
    input tokens against a 5,000-token prompt, because searching four times
    and then reasoning over the results for six more turns re-sends those
    results on each of them.
    """
    if not convo:
        return
    last = convo[-1]
    content = last.get("content")
    if isinstance(content, str):
        # A plain-string turn can't carry cache_control; make it one block.
        content = [{"type": "text", "text": content}] if content else []
        last["content"] = content
    if not isinstance(content, list) or not content:
        return
    tail = content[-1]
    # Replayed provider blocks are SDK objects, not dicts — they are already
    # inside the cached prefix by the time the next turn is built, so the
    # breakpoint belongs on a turn we constructed rather than on one of those.
    if isinstance(tail, dict):
        tail["cache_control"] = _EPHEMERAL


def _to_claude_message(m: Dict[str, Any]) -> Dict[str, Any]:
    """Neutral loop message → Anthropic message shape."""
    # An assistant turn we produced ourselves is replayed exactly as the API
    # gave it to us, rather than reassembled from text + tool_use. Reassembling
    # is lossy in one specific and expensive way: it cannot express a
    # server-side tool result, so a turn that searched the web came back as a
    # turn that merely talked about searching. Verbatim replay keeps the
    # search results, their urls, and the tool_use/tool_result pairing the API
    # requires — the SDK's own block objects go back over the wire unchanged,
    # which is what the `pause_turn` continuation below already relies on.
    if m["role"] == "assistant" and m.get("raw_content"):
        return {"role": "assistant", "content": m["raw_content"]}
    if m["role"] == "assistant" and m.get("tool_calls"):
        content: List[Dict[str, Any]] = []
        if m.get("content"):
            content.append({"type": "text", "text": m["content"]})
        for c in m["tool_calls"]:
            content.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]})
        return {"role": "assistant", "content": content}
    if m["role"] == "tool":
        return {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            ],
        }
    return {"role": m["role"], "content": m["content"]}


def complete_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    max_tokens: int = MAX_TOKENS,
    schema: Optional[Dict[str, Any]] = None,
) -> str:
    """A raw JSON completion, for one-off prompts (e.g. topic titles).

    Pass `schema` when the caller knows the shape it wants: structured outputs
    constrain the response to it, which is the supported replacement for the
    assistant prefill this used to rely on. Without one, the system prompts
    here all say "return JSON only" and `_parse_json` recovers a JSON object
    from prose or fences.
    """
    cfg = get_config()
    kwargs: Dict[str, Any] = {"model": cfg.extraction_model, "max_tokens": max_tokens}
    if system:
        kwargs["system"] = system
    if schema:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    response = _client().messages.create(
        messages=[{"role": "user", "content": prompt}], **kwargs
    )
    _record_usage(response, cfg.extraction_model)
    return "".join(block.text for block in response.content if block.type == "text")
