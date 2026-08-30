"""Gemini API classifier (§5, §9).

A drop-in alternative to the Claude classifier for people with a Google AI
Studio key (which has a free tier). Same prompts, same output contract, so the
pipeline can use either provider interchangeably. Uses the REST API over httpx
so no extra SDK dependency is needed.

Called from the backend, never the device — the key never ships in the app.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from ..config import get_config
from . import prompts
from .claude import ClassifierError, _parse_json

log = logging.getLogger(__name__)

MAX_TOKENS = 4096
_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _generate(prompt: str, *, system: Optional[str] = None, max_tokens: int = MAX_TOKENS) -> str:
    cfg = get_config()
    body: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0,
            # Force raw JSON out — no fences or prose to strip.
            "responseMimeType": "application/json",
        },
    }
    if system:
        body["system_instruction"] = {"parts": [{"text": _flatten_system(system)}]}

    url = f"{_BASE}/{cfg.gemini_model}:generateContent"
    headers = {"x-goog-api-key": cfg.gemini_api_key, "Content-Type": "application/json"}

    # Free-tier limits are low; a backfill burst hits 429. Back off and retry
    # (honoring Retry-After) instead of dropping the batch to the heuristic.
    resp = None
    for attempt in range(4):
        resp = httpx.post(url, headers=headers, json=body, timeout=60.0)
        if resp.status_code in (429, 503) and attempt < 3:
            wait = float(resp.headers.get("retry-after", 0) or 0) or 2 ** (attempt + 1)
            log.warning("Gemini %s, backing off %.0fs (attempt %d/3)", resp.status_code, wait, attempt + 1)
            time.sleep(min(wait, 30))
            continue
        break
    resp.raise_for_status()
    data = resp.json()
    _record_usage(data, cfg.gemini_model)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ClassifierError(f"Gemini returned no usable candidate: {str(data)[:200]}") from exc


# Resolved destinations, kept for the life of the process. One pass re-reads
# the same handful of sources across its turns.
_RESOLVED: Dict[str, str] = {}


def _grounded_sources(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """The real pages behind a googleSearch answer.

    Gemini does not hand back the urls it read. It hands back
    `vertexaisearch.cloud.google.com/grounding-api-redirect/…` — a redirect per
    source, in `groundingMetadata`, which sat next to `content` and was never
    read here. The model therefore answers with real knowledge of a page whose
    address it cannot state.

    That is not a cosmetic gap. `record_finding` refuses a priced finding with
    no link, so a worker that has just done good shopping is required to
    produce a url it was never given — and it does the only thing left, which
    is to reconstruct one from the product name. The EMES thread stored five of
    those. Every product was real, two slugs were exactly right, and all five
    urls were 404s because the site serves `/us/en/product/…` and the model
    guessed `/products/…`.

    So the redirects are followed here, once, and the destinations are handed
    to the model as text it can quote.
    """
    try:
        chunks = (
            data.get("candidates", [{}])[0]
            .get("groundingMetadata", {})
            .get("groundingChunks", [])
        ) or []
    except (AttributeError, IndexError, TypeError):
        return []

    out: List[Dict[str, str]] = []
    seen = set()
    for chunk in chunks[:10]:            # a bounded number of network calls
        web = (chunk or {}).get("web") or {}
        uri = (web.get("uri") or "").strip()
        if not uri or uri in seen:
            continue
        seen.add(uri)
        out.append({"title": (web.get("title") or "").strip(), "url": _resolve(uri)})
    return out


def _resolve(uri: str) -> str:
    """Follow one grounding redirect to the page it points at.

    Falls back to the redirect itself: a url that at least loads beats no url,
    and `registry._url_is_live` will judge whatever comes out of here.
    """
    if uri in _RESOLVED:
        return _RESOLVED[uri]
    destination = uri
    try:
        response = httpx.head(uri, follow_redirects=True, timeout=4.0)
        if str(response.url):
            destination = str(response.url)
    except Exception:                    # a source we cannot resolve is not fatal
        log.debug("could not resolve grounding redirect", exc_info=True)
    _RESOLVED[uri] = destination
    return destination


def _record_usage(data: Dict[str, Any], model: str) -> None:
    """Gemini's `usageMetadata` → the same daily counters Claude writes.

    Nothing here recorded a single token until now, and that gap is exactly as
    loud as it sounds: the moment the Anthropic key ran out of credit on 22
    Aug, every call fell to this provider and the token counters simply stopped
    growing. Three days of work looked, in `llm_tokens_*`, like three days of
    nothing happening — while `llm_calls` kept climbing past a hundred a day.
    A fallback that spends silently is how you find out from the invoice.

    `thoughtsTokenCount` is folded into output because that is how it bills.
    """
    from . import budget

    usage = data.get("usageMetadata") or {}
    if not usage:
        return
    cached = int(usage.get("cachedContentTokenCount") or 0)
    prompt_tokens = int(usage.get("promptTokenCount") or 0)
    budget.record_tokens(
        # promptTokenCount is the whole prompt, cached part included; only the
        # uncached remainder bills at the input rate.
        max(prompt_tokens - cached, 0),
        int(usage.get("candidatesTokenCount") or 0)
        + int(usage.get("thoughtsTokenCount") or 0),
        cached_tokens=cached,
        model=model,
    )


def classify_batch(batch: List[Dict[str, Any]], draft_replies: bool = True) -> Dict[str, Any]:
    """Classify a batch. Returns {"items": [...], "entities": [...]}, both
    unnormalised — §v2.8 phase 3 added the second key to the same read."""
    if not batch:
        return {"items": [], "entities": []}
    text = _generate(prompts.build_user_prompt(batch, draft_replies), system=prompts.SYSTEM_PROMPT)
    payload = _parse_json(text)
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ClassifierError("classifier returned a non-list `items`")
    claims = payload.get("entities")
    return {"items": items, "entities": claims if isinstance(claims, list) else []}


def link_followup(new_item: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Optional[str]:
    if not candidates:
        return None
    try:
        text = _generate(prompts.build_link_prompt(new_item, candidates), max_tokens=256)
        return _parse_json(text).get("links_to_item_id")
    except Exception as exc:  # linking is best-effort
        log.warning("follow-up linking failed: %s", exc)
        return None


def compose_batch_reply(person_name: str, items: List[Dict[str, Any]]) -> Optional[str]:
    """Phase D — fold several owed items to one person into a single reply."""
    text = _generate(
        prompts.build_batch_reply_prompt(person_name, items),
        system=prompts.BATCH_REPLY_SYSTEM,
        max_tokens=400,
    )
    return _parse_json(text).get("reply")


def complete_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    max_tokens: int = MAX_TOKENS,
    schema: Optional[Dict[str, Any]] = None,
) -> str:
    """A raw JSON completion, for one-off prompts (e.g. topic titles).

    `schema` is accepted so the two providers stay interchangeable through
    `providers.run`, and ignored: `_generate` already sets
    `responseMimeType: application/json`, so the response is raw JSON either
    way. Gemini's `responseSchema` uses a different dialect from JSON Schema
    and would need translating, which buys nothing here.
    """
    return _generate(prompt, system=system, max_tokens=max_tokens)


def _flatten_system(system):
    """Neutral system → one string.

    The loop hands the system prompt over as blocks so the Claude provider can
    put a cache breakpoint between the stable prompt and the volatile date
    stamp. Gemini has no equivalent, so the split is simply joined back up —
    the blocks are a caching hint, never a change to what the model reads.
    """
    if isinstance(system, str):
        return system
    return "\n\n".join(part["text"] for part in system if part.get("text"))


# Anthropic's server-side tools have Gemini equivalents, and they are native on
# both sides: the provider runs them and the loop never sees a call to
# dispatch. Matched on prefix because the Claude type carries a dated version
# (`web_search_20250305`, `web_search_20260209`) that Gemini has no analogue
# for — the capability is what maps, not the revision.
_NATIVE_EQUIVALENT = (
    ("web_search", {"googleSearch": {}}),
    ("web_fetch", {"urlContext": {}}),
)


def _native_for(tool: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind = tool.get("type") or tool.get("name") or ""
    for prefix, native in _NATIVE_EQUIVALENT:
        if kind.startswith(prefix):
            return native
    return None


def _to_gemini_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Claude-shaped tool list → Gemini's ``tools`` array.

    Two shapes arrive here and only one used to be handled. A normal tool
    carries ``input_schema`` and becomes a functionDeclaration. A *server*
    tool — ``{"type": "web_search_20250305", "name": "web_search"}`` — carries
    no schema at all, because Anthropic runs it rather than us.

    Indexing `input_schema` unconditionally is what killed the fallback: the
    worker offers web tools on every pass, so the moment Claude stopped
    answering (a dead key, a spent balance) Gemini raised ``KeyError:
    'input_schema'`` on its first turn, `loop.run` returned None, and the
    worker recorded `worked: 0` every cycle — silently, since there is no
    provider left to report the failure. A fallback that dies on the shape of
    its input is not a fallback.

    So server tools are translated to Gemini's own natives where one exists,
    and dropped with a log where one does not. Dropped is the right failure:
    losing a capability on a fallback pass costs a worse answer, and raising
    costs every answer.
    """
    declarations: List[Dict[str, Any]] = []
    natives: List[Dict[str, Any]] = []

    for t in tools:
        schema = t.get("input_schema")
        if schema is None:
            native = _native_for(t)
            if native is None:
                log.warning(
                    "gemini: no equivalent for server tool %r — dropped for this pass",
                    t.get("type") or t.get("name"),
                )
            elif native not in natives:
                natives.append(native)
            continue
        declaration: Dict[str, Any] = {
            "name": t["name"],
            "description": t.get("description", ""),
        }
        # A declaration with an empty `properties` is a 400 ("should be
        # non-empty for OBJECT type"), where Claude takes it happily. A
        # no-argument tool is declared with no parameters at all.
        if schema.get("properties"):
            declaration["parameters"] = schema
        declarations.append(declaration)

    out: List[Dict[str, Any]] = []
    if declarations:  # an empty functionDeclarations list is a 400, not a no-op
        out.append({"functionDeclarations": declarations})
    out.extend(natives)
    return out


def complete_with_tools(
    messages: List[Dict[str, Any]],
    *,
    tools: List[Dict[str, Any]],
    system: Optional[str] = None,
    max_tokens: int = 1024,
) -> Dict[str, Any]:
    """One turn of an agentic tool loop (§v1.4) — function calling over REST.

    Same neutral contract as the Claude version: neutral messages in,
    ``{"text", "tool_calls"}`` out. Tool schemas arrive Claude-shaped
    ({name, description, input_schema}) and are converted by `_to_gemini_tools`
    — including the server tools, which become Gemini's own natives.
    """
    cfg = get_config()
    body: Dict[str, Any] = {
        "contents": [_to_gemini_content(m) for m in messages],
        # No responseMimeType here — it conflicts with function calling.
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0},
    }
    gemini_tools = _to_gemini_tools(tools)
    if gemini_tools:
        body["tools"] = gemini_tools
    has_declarations = any("functionDeclarations" in t for t in gemini_tools)
    has_natives = len(gemini_tools) > int(has_declarations)
    if has_declarations and has_natives:
        # Mixing a built-in tool (googleSearch, urlContext) with custom
        # function declarations in one request 400s unless this is set —
        # confirmed against the live API, which names the exact field in its
        # error text ("Please enable tool_config.include_server_side_tool_
        # invocations to use Built-in tools with Function calling"). Without
        # it, every worker pass — which always offers local tools alongside
        # web tools — silently lost web search to the fallback below.
        body["tool_config"] = {"include_server_side_tool_invocations": True}
    if system:
        body["system_instruction"] = {"parts": [{"text": _flatten_system(system)}]}

    url = f"{_BASE}/{cfg.gemini_model}:generateContent"
    headers = {"x-goog-api-key": cfg.gemini_api_key, "Content-Type": "application/json"}
    resp = httpx.post(url, headers=headers, json=body, timeout=60.0)

    # Belt and suspenders: the flag above should make this branch dead, but
    # whether an older/other model still rejects the combination is not
    # something we can ask about — the answer only arrives as a 400 on the
    # whole turn. Since this provider only ever runs when the other one is
    # already down, drop the natives and ask again rather than failing the
    # pass: a worker turn with local tools and no web beats no worker turn.
    # Exactly 400, not `>= 400`: an unsupported combination is a bad *request*
    # and nothing else is. A 429 (depleted credits, which is how both keys on
    # this machine actually failed) or a 503 would otherwise buy a second
    # certain failure at the same price as the first.
    if resp.status_code == 400 and has_declarations and has_natives:
        log.warning(
            "gemini rejected natives alongside functions (%s); retrying without web: %s",
            resp.status_code, resp.text[:200],
        )
        body["tools"] = [t for t in gemini_tools if "functionDeclarations" in t]
        resp = httpx.post(url, headers=headers, json=body, timeout=60.0)

    if resp.status_code >= 400:
        # raise_for_status drops the body — which is where Google explains
        # *why* (bad model name, bad schema). Keep it for /health.
        raise ClassifierError(f"Gemini {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    _record_usage(data, cfg.gemini_model)
    try:
        parts = data["candidates"][0]["content"].get("parts", [])
    except (KeyError, IndexError, TypeError) as exc:
        raise ClassifierError(f"Gemini returned no usable candidate: {str(data)[:200]}") from exc

    text = "".join(p["text"] for p in parts if "text" in p)

    # Hand the searched pages back as quotable text.
    #
    # This lands in the assistant turn the loop replays, so the *next* turn can
    # cite an address instead of inventing one. It does not save a turn that
    # searches and records in one breath — `registry._url_is_live` is what
    # catches that — but it is the half that lets a correct citation exist at
    # all, rather than merely refusing the wrong one.
    sources = _grounded_sources(data)
    if sources:
        lines = "\n".join(
            f"- {s['title']} — {s['url']}" if s["title"] else f"- {s['url']}"
            for s in sources
        )
        text = (
            f"{text}\n\nSources you just read — quote these urls exactly, "
            f"do not rebuild an address from a product name:\n{lines}"
        ).strip()

    calls = []
    for i, p in enumerate(parts):
        if "functionCall" not in p:
            continue
        fc = p["functionCall"]
        calls.append(
            {
                "id": fc.get("id") or f"synth_{i}_{fc['name']}",
                "name": fc["name"],
                "input": fc.get("args", {}),
                # Thinking models require their thoughtSignature echoed back
                # verbatim when the turn is replayed — provider-opaque baggage
                # the neutral format carries and Claude simply ignores.
                "meta": {"sig": p.get("thoughtSignature"), "gid": fc.get("id")},
            }
        )
    return {"text": text, "tool_calls": calls}


def _to_gemini_content(m: Dict[str, Any]) -> Dict[str, Any]:
    """Neutral loop message → Gemini content shape."""
    if m["role"] == "assistant":
        parts: List[Dict[str, Any]] = []
        if m.get("content"):
            parts.append({"text": m["content"]})
        for c in m.get("tool_calls", []):
            meta = c.get("meta") or {}
            fc: Dict[str, Any] = {"name": c["name"], "args": c["input"]}
            if meta.get("gid"):
                fc["id"] = meta["gid"]
            part: Dict[str, Any] = {"functionCall": fc}
            if meta.get("sig"):
                part["thoughtSignature"] = meta["sig"]
            parts.append(part)
        return {"role": "model", "parts": parts or [{"text": ""}]}
    if m["role"] == "tool":
        fr: Dict[str, Any] = {"name": m["name"], "response": {"result": m["content"]}}
        call_id = m.get("tool_call_id") or ""
        if call_id and not call_id.startswith("synth_"):
            fr["id"] = call_id  # echo Gemini's own call id when it issued one
        return {"role": "user", "parts": [{"functionResponse": fr}]}
    return {"role": "user", "parts": [{"text": m["content"]}]}
