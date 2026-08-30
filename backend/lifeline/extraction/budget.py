"""A soft daily ceiling on LLM calls, so a bug or a runaway loop can never
drain the API budget. Counts attempts per UTC day in sync_state; the pipeline
checks it before spending and silently falls back to the heuristic once the
cap is hit.

Tune with LIFELINE_LLM_DAILY_CALL_CAP (0 disables the cap).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from .. import db

log = logging.getLogger(__name__)

DEFAULT_CAP = 1500  # ~cents/day on flash-lite; well under any small budget.

# §v2 step 4 — per-trigger ceilings, inside the global one.
#
# Until now this was a single counter shared by everything. The audit's warning
# was precise: the worker loop draws from the same pool as extraction, so a
# heavy ingest day silently starves autonomous work, and a runaway worker
# silently starves extraction. Neither failure announces itself — the loser
# just quietly falls back to heuristics.
#
# The worker is also the first thing here that spends money while the user is
# asleep, so it gets a ceiling of its own that a busy inbox cannot eat and that
# cannot eat the inbox. Anything with no named ceiling shares the global cap as
# before.
TRIGGER_CAPS = {
    "worker": 300,     # ~20 thread-passes/day at typical depth, on the loop model
    "draft": 120,      # user-initiated, so generous — but not unbounded
}


def _cap() -> int:
    return int(os.environ.get("LIFELINE_LLM_DAILY_CALL_CAP", str(DEFAULT_CAP)))


def _trigger_cap(trigger: str) -> int:
    env = os.environ.get(f"LIFELINE_BUDGET_{trigger.upper()}")
    if env is not None:
        return int(env)
    return TRIGGER_CAPS.get(trigger, 0)      # 0 = no ceiling of its own


def _key(trigger: Optional[str] = None) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"llm_calls:{trigger}:{day}" if trigger else f"llm_calls:{day}"


def used_today(trigger: Optional[str] = None) -> int:
    return int(db.get_sync_state(_key(trigger)) or 0)


def record_tokens(
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    model: Optional[str] = None,
) -> None:
    """Daily token totals, beside the call counts.

    Counting calls alone hid the thing that actually costs money. A day of
    cheap routing calls and a day where one of them dragged a whole shop page
    into a conversation that is resent every turn look identical in
    `llm_calls`, and differ by an order of magnitude on the bill. Tokens are
    what the invoice is made of, so tokens are what gets counted.

    `cached` is separated from `in` because they are not the same money. A
    cached read bills at about a tenth of the input rate, so folding the two
    together — which this did — makes a fully cached day and an uncached one
    look identical in the only number anyone reads. The first pass after
    caching was switched on would have reported the same ~400k it always did.

    `model` is separated for the same reason one rung up, and it is the one
    that actually got us. A token is not a unit of money — it is a unit of
    money *times the model that spent it*, and this system runs two models
    that differ 5x in both directions. Totalled together, 22 Aug read as a
    quiet ~1.8M-token day; the invoice for it was $11.58, of which $10.23 was
    Opus doing message classification and $1.35 was the whole agentic loop.
    The expensive half was the half nobody was looking at, because there was
    only ever one number. Per-model keys are what make that visible without
    opening the billing console.
    """
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffixes = [""] if not model else ["", f":{_slug(model)}"]
    for kind, n in (("in", input_tokens), ("out", output_tokens), ("cached", cached_tokens)):
        if not n:
            continue
        for suffix in suffixes:
            key = f"llm_tokens_{kind}{suffix}:{day}"
            try:
                db.set_sync_state(key, str(int(db.get_sync_state(key) or 0) + int(n)))
            except Exception:   # accounting must never break the run it measures
                log.debug("could not record %s tokens", kind, exc_info=True)


def _slug(model: str) -> str:
    """`claude-haiku-4-5-20251001` → `haiku`. The family is what has a price;
    the dated snapshot just makes the key unreadable and splits the total the
    day the pin moves."""
    name = model.rsplit("/", 1)[-1].lower()
    for family in ("opus", "sonnet", "haiku", "fable", "mythos", "flash", "pro"):
        if family in name:
            return family
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-") or "unknown"


def allow(trigger: Optional[str] = None) -> bool:
    """Both ceilings must agree. The global cap still protects the API key;
    the per-trigger one protects each kind of work from the others."""
    cap = _cap()
    if cap > 0 and used_today() >= cap:
        return False
    if trigger:
        own = _trigger_cap(trigger)
        if own > 0 and used_today(trigger) >= own:
            return False
    return True


def record(trigger: Optional[str] = None) -> None:
    cap = _cap()
    n = used_today() + 1
    db.set_sync_state(_key(), str(n))
    if cap > 0 and n == cap:
        log.warning("LLM daily call cap (%d) reached — using heuristic until UTC reset", cap)
    if trigger:
        own_n = used_today(trigger) + 1
        db.set_sync_state(_key(trigger), str(own_n))
        own_cap = _trigger_cap(trigger)
        if own_cap > 0 and own_n == own_cap:
            log.warning("%s daily budget (%d calls) spent — pausing until UTC reset", trigger, own_cap)


# Published per-MTok rates, by model family, for the estimate below. Cache
# reads bill at ~0.1x input on Anthropic; cache writes at ~1.25x, and those are
# already folded into the `in` bucket by the callers.
_RATES = {
    "opus":   (5.00, 25.00),
    "fable":  (10.00, 50.00),
    "mythos": (10.00, 50.00),
    "sonnet": (3.00, 15.00),
    "haiku":  (1.00, 5.00),
    "flash":  (0.30, 2.50),
    "pro":    (1.25, 10.00),
}


def tokens_today() -> dict:
    """Today's tokens, split by model family, with an estimated dollar cost.

    An estimate and labelled as one — the invoice is the invoice. Its job is to
    make a 5x model swap visible on the day it happens instead of at the end of
    the month, which is the failure this whole function exists to answer for.
    """
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out: dict = {"day": day, "models": {}, "estimated_usd": 0.0}
    for family, (rate_in, rate_out) in _RATES.items():
        got = {}
        for kind in ("in", "out", "cached"):
            n = int(db.get_sync_state(f"llm_tokens_{kind}:{family}:{day}") or 0)
            if n:
                got[kind] = n
        if not got:
            continue
        usd = (
            got.get("in", 0) / 1e6 * rate_in
            + got.get("out", 0) / 1e6 * rate_out
            + got.get("cached", 0) / 1e6 * rate_in * 0.1
        )
        got["estimated_usd"] = round(usd, 4)
        out["models"][family] = got
        out["estimated_usd"] += usd
    out["estimated_usd"] = round(out["estimated_usd"], 4)
    return out


def spend_report() -> dict:
    """What each kind of work has cost today, in calls *and* in tokens.

    Calls alone were the whole problem. Extraction and the worker each make
    roughly a hundred calls a day and look identical here; on 22 Aug one of
    them cost $10.23 and the other $1.35. `tokens` is the half that says which.
    """
    report = {"total": used_today(), "cap": _cap()}
    for trigger in TRIGGER_CAPS:
        report[trigger] = {"used": used_today(trigger), "cap": _trigger_cap(trigger)}
    report["tokens"] = tokens_today()
    return report
