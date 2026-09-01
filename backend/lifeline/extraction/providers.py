"""The LLM provider chain (§5, §9).

Try Claude first, then Gemini, then let the caller fall back to heuristics — so
a dead or empty key on one provider slides to the next instead of dropping
straight to rules. One place that decides provider order, so every call site
(classification, follow-up linking, topic titles, batch replies) degrades the
same way.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional, TypeVar

from .. import db
from ..config import get_config
from ..models import now_iso
from . import budget, claude, gemini

log = logging.getLogger(__name__)
T = TypeVar("T")

# What the engine last failed at, and whether it is currently reduced to
# rules. Read by /health and by the menu bar icon.
LAST_ERROR_KEY = "llm:last_error"
DEGRADED_SINCE_KEY = "llm:degraded_since"


def available() -> List[Any]:
    """Configured providers, in preference order."""
    cfg = get_config()
    chain: List[Any] = []
    if cfg.has_claude:
        chain.append(claude)
    if cfg.has_gemini:
        chain.append(gemini)
    return chain


def run(call: Callable[[Any], T], what: str) -> Optional[T]:
    """Call each configured provider in order until one succeeds; budget is
    charged once, on the successful call. Returns None when none are configured,
    the budget is spent, or every provider failed — the signal to the caller to
    use its heuristic fallback."""
    chain = available()
    failures: List[str] = []
    for provider in chain:
        if not budget.allow():
            failures.append("today's spend cap is reached")
            break
        try:
            result = call(provider)
        except Exception as exc:  # network, rate limit, no credits, malformed
            log.warning("%s %s failed, trying next provider: %s", provider.__name__, what, exc)
            failures.append(f"{provider.__name__.rsplit('.', 1)[-1]}: {exc}")
            continue
        budget.record()
        _recovered()
        return result

    # Nothing answered. The caller will fall back to the heuristic rules and
    # carry on — which is the right behaviour and was, until now, completely
    # silent. An account whose credit ran out mid-afternoon kept extracting at
    # a fraction of the quality, stamped the messages processed forever, and
    # reported itself healthy. Degrading is fine; degrading invisibly is not.
    _degraded(what, failures if failures else ["no provider is configured"])
    return None


def _degraded(what: str, failures: List[str]) -> None:
    detail = f"{what} fell back to rules — " + "; ".join(failures)
    try:
        db.set_sync_state(LAST_ERROR_KEY, detail[:300])
        if not db.get_sync_state(DEGRADED_SINCE_KEY):
            db.set_sync_state(DEGRADED_SINCE_KEY, now_iso())
    except Exception:      # a status line must never break the pipeline
        log.debug("could not record degradation", exc_info=True)


def _recovered() -> None:
    """One success clears the flag: the interesting question is whether the
    engine is reduced *now*, not whether it ever was."""
    try:
        if db.get_sync_state(DEGRADED_SINCE_KEY):
            db.set_sync_state(DEGRADED_SINCE_KEY, "")
            db.set_sync_state(LAST_ERROR_KEY, "")
    except Exception:
        log.debug("could not clear degradation", exc_info=True)


def degraded_since() -> Optional[str]:
    """When the engine last stopped being able to think, or None."""
    return db.get_sync_state(DEGRADED_SINCE_KEY) or None
