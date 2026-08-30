"""The agentic loop (§v1.4) — the spine everything rides on.

Given a goal, the model decides which tools to call, observes the results, and
keeps going until it concludes or hits the budget:

    GOAL -> think -> call a tool -> observe -> ... -> conclude -> write back

The loop speaks a neutral message format so any provider can drive it:
    {"role": "user"|"assistant", "content": str, ["tool_calls": [...]]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}

Every run is persisted to `loop_runs` — the provenance log that powers the
dossier's "why", debugging, and don't-re-litigate.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .. import db
from ..extraction import budget, providers
from ..models import new_id, now_iso
from . import registry as reg

log = logging.getLogger(__name__)

MAX_ITERATIONS = 5

# --------------------------------------------------------- the starting rule
#
# The v2 spec asks for a "stopping rule" because the loop averages 2.2
# iterations against a cap of 8. Measured against the real `loop_runs`, that
# framing is slightly off: the loop is not stopping early, it is **never
# starting**. Six of 34 runs called no tool at all, and every one of those
# concluded with a claim about the user's own data that was false:
#
#   "Who is booooby"                 -> "I don't have booooby on file"
#   "Who is Nia"                -> "I don't have Nia on file"
#   "What appointments did I have"   -> "I don't have access to historical
#                                        calendar data"
#
# Boooooby Carter, Nia Coleman and fourteen past calendar events are all in
# the database. The loop asserted absence without looking, which for a product
# whose thesis is "the system knows your world" is the worst answer available —
# worse than saying nothing.
#
# `CONVERSE_SYSTEM` already forbids this in prose ("NEVER ask permission to use
# a tool", "An empty result is a lead, not an answer"). It kept happening. So
# the rule is structural: a conclusion that claims absence, or asks the user
# for something a tool could supply, is not accepted from a run that never
# called a tool. The loop is handed back its own answer and told to look.

_ABSENCE = re.compile(
    r"\b(i (don'?t|do not) (have|see|find)|i can'?t (find|see|access)|"
    r"i couldn'?t find|not on file|no access to|don'?t have access|"
    r"i (don'?t|do not) have any (record|information))\b",
    re.I,
)

# "I would need to search your messages" — announcing the search instead of
# running it. Appeared the day the grounding block landed: the model reads
# "already known: …", treats it as the whole answer, and narrates the tool
# call it should be making (§v2.9, seen live on the first Ask card). Unlike
# _ABSENCE this is unearned even from a run that called tools — the loop has
# iterations left and the search it is describing is one it can run.
_NARRATED_SEARCH = re.compile(
    # Contractions included: the first live miss wrote "I'd need to check" —
    # and the guard, matching only "I would", waved it through.
    r"\b(i(?:'d|'ll| would| will)? (?:need|have) to (?:search|check|look)|"
    r"to find (?:out|where|when|this|that)[^.]{0,60}\bi(?:'d| would)\b|"
    r"would you like me to (?:search|look|check)|"
    r"(?:is |are )?not (?:stored|available) in (?:my|the|your) (?:knowledge|data))",
    re.I,
)

_NARRATED_NUDGE = (
    "Stop. You just described a search instead of running it. You have the "
    "tools and iterations to do exactly what you proposed — search_messages, "
    "search_mail, search_calendar, search_world — so run the search you "
    "named, read what comes back, and answer from that. The 'already known' "
    "facts are orientation, never the whole answer."
)

_ASKS_BACK = re.compile(
    r"\b(could you (tell|let) me|can you tell me|i need to know|i need a bit more|"
    r"who (is|are) (she|he|they|her|him)|what do you mean|which one)\b",
    re.I,
)

_NUDGE = (
    "Stop. You answered without calling a single tool, so you do not actually "
    "know whether that is true — you guessed. You have tools over the user's "
    "messages, people, calendar, mail, history and threads. Use them now:\n"
    "- A name you don't recognise: call find_person. It matches nicknames, "
    "misspellings and phone numbers. If it returns `alternatives`, name them "
    "and ask which one — never ask who the person is.\n"
    "- Anything that might have been said, sent, or scheduled: search_messages, "
    "search_mail, search_calendar, timeline.\n"
    "- A pronoun you can't resolve: look at the recent conversation and open "
    "items before asking.\n"
    "Then answer from what you found. Only say something isn't there after you "
    "have looked and can say where."
)


def _dated(system: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    """Tell the model what day it is — after the cache breakpoint.

    Nothing ever did. Asked "what appointments did I have in July?" the loop
    searched correctly and then reported "no events on file for July 2024" —
    with a July 2026 event in the table. It wasn't a retrieval failure; the
    model had no idea what year it was and guessed. Every relative date the
    user says — yesterday, last month, next week, "the 4th" — was being
    resolved against nothing.

    The stamp used to be prepended, and that placement quietly made the whole
    system prompt uncacheable: caching is a prefix match, the stamp carries
    minutes, so the first ~60 tokens of every request differed from the last
    one and invalidated everything after them. It goes last now, in its own
    block after the breakpoint — the 5k-token prefix in front of it is
    identical on every call and is what actually gets cached. `cache=True`
    marks where that breakpoint belongs; a provider without prompt caching
    ignores the flag and joins the text.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    stamp = (
        f"Today is {now.strftime('%A, %-d %B %Y')} ({now.date().isoformat()}), "
        f"{now.strftime('%H:%M')} UTC. Resolve every relative date the user "
        f"uses — yesterday, last week, next month, 'the 4th' — against that "
        f"date, and never guess the year."
    )
    if not system:
        return [{"text": stamp}]
    return [{"text": system, "cache": True}, {"text": stamp}]


def _looked_first(text: str, call_log: List[Dict[str, Any]]) -> bool:
    """Did this conclusion earn the right to be made?

    Only fires on runs that called *nothing*. A run that searched and came back
    empty is allowed to say so — that's an honest miss, and the whole point of
    the receipts is to show it.
    """
    if call_log:
        return True
    if not text:
        return True
    return not (_ABSENCE.search(text) or _ASKS_BACK.search(text) or text.strip().endswith("?"))


@dataclass
class LoopResult:
    conclusion: str                       # the model's final text
    run_id: str
    provider: str
    iterations: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)  # [{name, input, result}]


def run_loop(
    goal: str,
    *,
    trigger: str,
    tools: Optional[List[reg.Tool]] = None,
    system: Optional[str] = None,
    max_iterations: int = MAX_ITERATIONS,
    history: Optional[List[Dict[str, str]]] = None,
    session_id: Optional[str] = None,
) -> Optional[LoopResult]:
    """Run one investigation. Returns None when no tool-capable provider is
    configured or the budget is spent — the caller's signal to fall back to its
    non-agentic path (RAG-lite, heuristics)."""
    tools = tools if tools is not None else reg.READ_TOOLS
    by_name = reg.by_name(tools)
    schemas = [t.schema for t in tools]

    # No provider configured is a *louder* failure than a provider that broke,
    # and it used to be the quietest thing the system did. A scheduled poll
    # with no API key in its environment ran the whole cycle, worked zero
    # threads, wrote nothing, and exited 0 — indistinguishable in every log and
    # health check from a morning where there was simply nothing to do. It ran
    # that way twice before anyone noticed.
    if not providers.available():
        db.set_sync_state(
            "llm:last_error",
            "no LLM provider configured: ANTHROPIC_API_KEY (or GEMINI_API_KEY) "
            "is not set in this process's environment. The worker cannot run.",
        )
        log.error("no LLM provider configured — the worker did nothing this cycle")
        return None

    last_error: Optional[str] = None
    for provider in providers.available():
        if not hasattr(provider, "complete_with_tools"):
            continue
        if not budget.allow(trigger):
            break
        try:
            result = _drive(
                provider, goal, trigger, by_name, schemas, system, max_iterations,
                history=history, session_id=session_id,
            )
            # A working fallback is exactly how a dead primary key goes
            # unnoticed: the pass succeeds, the error is cleared, and nothing
            # anywhere says the preferred provider is down — the same shape as
            # the thirteen days of dead Gmail behind `google_connected: true`.
            # Success on a provider that isn't the first one is therefore
            # recorded as degradation, not as health.
            name = provider.__name__.rsplit(".", 1)[-1]
            db.set_sync_state(
                "llm:last_error",
                f"degraded: running on {name} because {last_error}"[:300] if last_error else "",
            )
            return result
        except Exception as exc:  # network, rate limit, no credits — try next
            last_error = f"{provider.__name__.rsplit('.', 1)[-1]}: {exc}"
            log.warning("%s loop failed, trying next provider: %s", provider.__name__, exc)
            continue
    if last_error:
        # Degradation must never be silent (QA round 2): keep the last provider
        # error where /health can show it, so a dead key is visible in one look.
        db.set_sync_state("llm:last_error", last_error[:300])
    return None


def _drive(
    provider: Any,
    goal: str,
    trigger: str,
    by_name: Dict[str, reg.Tool],
    schemas: List[Dict[str, Any]],
    system: Optional[str],
    max_iterations: int,
    history: Optional[List[Dict[str, str]]] = None,
    session_id: Optional[str] = None,
) -> LoopResult:
    """One provider drives the whole conversation (formats aren't mixable).

    Prior turns are replayed as plain text only — never their tool_use blocks.
    Replaying those would demand every matching tool_result (and Gemini's
    thoughtSignatures) held forever; the text carries what pronouns need.
    """
    messages: List[Dict[str, Any]] = [
        {"role": t["role"], "content": t["text"]}
        for t in (history or [])
        if t.get("text")
    ]
    messages.append({"role": "user", "content": goal})
    system = _dated(system)
    call_log: List[Dict[str, Any]] = []
    result: Dict[str, Any] = {"text": "", "tool_calls": []}

    iterations = 0
    nudged = False
    for iterations in range(1, max_iterations + 1):
        if not budget.allow(trigger):
            break
        result = provider.complete_with_tools(messages, tools=schemas, system=system)
        budget.record(trigger)
        if not result["tool_calls"]:
            # Concluded — but did it look? A claim about the user's data made
            # without touching a tool gets handed back exactly once. Once is
            # enough: the failure is not calling any tool, and a model that
            # still won't after being told isn't going to on the third ask.
            narrated = bool(schemas) and bool(_NARRATED_SEARCH.search(result["text"] or ""))
            if not nudged and schemas and (narrated or not _looked_first(result["text"], call_log)):
                nudged = True
                log.info("loop concluded by %s; sending it back to look",
                         "narrating a search" if narrated else "calling no tools")
                _count_nudge()
                if narrated:
                    messages.append({"role": "assistant", "content": result["text"],
                                     "raw_content": result.get("raw_content")})
                    messages.append({"role": "user", "content": _NARRATED_NUDGE})
                    continue
                messages.append({
                    "role": "assistant",
                    "content": result["text"],
                    # Carried here too: a turn can search the web and then
                    # conclude without calling a local tool, which is exactly
                    # the shape this nudge catches. Handing it back without its
                    # search results would make it look again from nothing.
                    "raw_content": result.get("raw_content"),
                })
                messages.append({"role": "user", "content": _NUDGE})
                continue
            break  # concluded

        # `raw_content` is the provider's own blocks for this turn, passed
        # straight back on the next one. The loop never inspects it — it is
        # opaque here by design, so a provider without server-side tools simply
        # omits it and the reassembled form below still applies.
        messages.append(
            {
                "role": "assistant",
                "content": result["text"],
                "tool_calls": result["tool_calls"],
                "raw_content": result.get("raw_content"),
            }
        )
        for call in result["tool_calls"]:
            tool = by_name.get(call["name"])
            output = (
                reg.execute(tool, call["input"])
                if tool
                else json.dumps({"error": f"unknown tool {call['name']}"})
            )
            # The FULL result stays in memory: the receipts extractor and the
            # trace parse this as JSON, and `output[:2000]` was cutting 45% of
            # tool results mid-object — every receipt on those answers was
            # silently dropped (audit F3). Truncation happens once, at
            # persistence, where a byte budget actually exists.
            call_log.append({"name": call["name"], "input": call["input"], "result": output})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": output,
                }
            )
    else:
        # Budget of iterations spent without a conclusion; ask for the best
        # answer from what's been gathered (no tools offered).
        messages.append(
            {"role": "user", "content": "Stop investigating. Conclude from what you have."}
        )
        if budget.allow(trigger):
            result = provider.complete_with_tools(messages, tools=[], system=system)
            budget.record(trigger)

    run = LoopResult(
        conclusion=result["text"].strip(),
        run_id=new_id(),
        provider=provider.__name__.rsplit(".", 1)[-1],
        iterations=iterations,
        tool_calls=call_log,
    )
    _persist(run, goal, trigger, session_id)
    return run


def _count_nudge() -> None:
    """Keep a running count so the rule's effect is measurable later without a
    migration — the whole reason this exists is that nobody could see the loop
    quitting early until `loop_runs` was read by hand."""
    try:
        current = int(db.get_sync_state("loop:nudges") or "0")
        db.set_sync_state("loop:nudges", str(current + 1))
    except Exception:      # a diagnostic must never break the run it measures
        pass


def _persist(run: LoopResult, goal: str, trigger: str, session_id: Optional[str] = None) -> None:
    conn = db.get_connection()
    with db.transaction(conn) as c:
        c.execute(
            "INSERT INTO loop_runs (id, trigger, session_id, goal, provider, status, iterations, "
            "tool_calls, conclusion, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run.run_id,
                trigger,
                session_id,
                goal,
                run.provider,
                "concluded" if run.conclusion else "inconclusive",
                run.iterations,
                json.dumps([
                    {**c, "result": (c.get("result") or "")[:4000]}
                    for c in run.tool_calls
                ]),
                run.conclusion,
                now_iso(),
            ),
        )
