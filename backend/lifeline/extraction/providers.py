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

from ..config import get_config
from . import budget, claude, gemini

log = logging.getLogger(__name__)
T = TypeVar("T")


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
    for provider in available():
        if not budget.allow():
            break
        try:
            result = call(provider)
        except Exception as exc:  # network, rate limit, no credits, malformed
            log.warning("%s %s failed, trying next provider: %s", provider.__name__, what, exc)
            continue
        budget.record()
        return result
    return None
