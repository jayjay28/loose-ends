"""Every paid call site records what it spent, split by model.

The bug these cover cost real money: `_record_usage` existed and was wired into
exactly one of four Claude call sites, so the meter watched the cheap model and
ignored the expensive one. Gemini recorded nothing at all, which turned the
fallback into three days of invisible spend. Both failures were silent — the
counters kept returning plausible numbers, just not the ones that mattered.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lifeline.extraction import budget, claude, gemini

_SRC = Path(__file__).resolve().parent.parent / "lifeline" / "extraction"


def _usage(**kw):
    base = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    base.update(kw)
    return SimpleNamespace(usage=SimpleNamespace(**base))


# ----------------------------------------------------------------- recording
def test_claude_usage_is_attributed_to_the_model_that_spent_it():
    claude._record_usage(_usage(input_tokens=8000, output_tokens=1200), "claude-opus-5")
    claude._record_usage(_usage(input_tokens=3000, output_tokens=900), "claude-haiku-4-5")

    report = budget.tokens_today()
    assert report["models"]["opus"] == {"in": 8000, "out": 1200, "estimated_usd": 0.07}
    assert report["models"]["haiku"]["in"] == 3000

    # The whole point: Opus costs more than Haiku for *fewer* tokens.
    assert report["models"]["opus"]["estimated_usd"] > report["models"]["haiku"]["estimated_usd"]


def test_cache_creation_bills_as_input_and_reads_bill_as_cached():
    claude._record_usage(
        _usage(input_tokens=1000, cache_creation_input_tokens=500, cache_read_input_tokens=40000),
        "claude-haiku-4-5",
    )
    haiku = budget.tokens_today()["models"]["haiku"]
    assert haiku["in"] == 1500        # writes are input, not the cheap bucket
    assert haiku["cached"] == 40000


def test_a_cached_token_is_cheaper_than_an_uncached_one():
    """Folding these together is what made caching invisible in the old
    counter: the same 100k reads the same either way, and bills 10x apart."""
    claude._record_usage(_usage(input_tokens=100_000), "claude-opus-5")
    claude._record_usage(_usage(cache_read_input_tokens=100_000), "claude-haiku-4-5")

    models = budget.tokens_today()["models"]
    assert models["opus"]["estimated_usd"] == 0.5       # 100k × $5/MTok
    assert models["haiku"]["estimated_usd"] == 0.01     # 100k × $1/MTok × 0.1


def test_gemini_records_tokens_at_all():
    gemini._record_usage(
        {
            "usageMetadata": {
                "promptTokenCount": 12000,      # includes the cached part
                "cachedContentTokenCount": 5000,
                "candidatesTokenCount": 800,
                "thoughtsTokenCount": 200,      # bills as output
            }
        },
        "gemini-flash-latest",
    )
    flash = budget.tokens_today()["models"]["flash"]
    assert flash["in"] == 7000          # prompt minus the cached remainder
    assert flash["cached"] == 5000
    assert flash["out"] == 1000         # candidates + thoughts


def test_a_response_without_usage_is_survivable():
    claude._record_usage(SimpleNamespace(), "claude-opus-5")
    gemini._record_usage({}, "gemini-flash-latest")
    assert budget.tokens_today()["models"] == {}


def test_dated_snapshots_fold_into_one_family():
    claude._record_usage(_usage(input_tokens=10), "claude-haiku-4-5-20251001")
    claude._record_usage(_usage(input_tokens=10), "claude-haiku-4-5")
    assert budget.tokens_today()["models"]["haiku"]["in"] == 20


# ------------------------------------------------------------- no gaps left
def test_every_claude_request_is_followed_by_a_usage_record():
    """A new `messages.create` with no `_record_usage` after it is the exact
    shape of the bug — one existed for months and cost $10 a day."""
    src = (_SRC / "claude.py").read_text()
    creates = src.count("messages.create(")
    records = src.count("_record_usage(")
    # one `def _record_usage`, the rest are call sites
    assert records - 1 >= creates, (
        f"{creates} Claude requests but only {records - 1} usage records — "
        "an unmetered call site spends invisibly"
    )


def test_gemini_records_on_both_of_its_request_paths():
    src = (_SRC / "gemini.py").read_text()
    assert src.count("_record_usage(") - 1 >= 2, (
        "_generate and complete_with_tools must both record; the fallback "
        "provider spending nothing into the meter is how three days vanished"
    )
