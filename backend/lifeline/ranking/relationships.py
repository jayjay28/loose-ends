"""Who matters to you — a data-driven relationship model (the first facet of the
continuously-built "model of you").

Tie strength is *inferred from the conversation itself*, not a manual label:
how much you exchange, whether it's two-way, how recently, and how fast you
reply. It updates every poll cycle and sharpens as new sources (Gmail, ...) join
the shared message store — no cold start, no configuration.

Grounded in tie-strength theory (Granovetter): frequency + reciprocity + recency
predict closeness. Reciprocity is the load-bearing term — a newsletter blasts at
you and you never reply (tie ~0); a person you actually text back scores high.
That makes this the same signal that keeps bulk mail out of the briefing.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Dict, Optional

from .. import db
from ..models import parse_iso

# A back-and-forth with a support desk isn't a relationship. Damp senders that
# look like service/automated accounts so a chatty support thread doesn't read
# as "one of your closest."
_SERVICE = re.compile(
    r"(support|service|helpdesk|no[-_.]?reply|notification|alerts?|billing|"
    r"team@|info@|admin|security|verify)",
    re.I,
)


def _is_service(person_id: str) -> bool:
    person = db.get_person(person_id)
    if not person:
        return False
    hay = " ".join([person.display_name or "", *(person.handles or [])])
    return bool(_SERVICE.search(hay))

_CACHE: Dict[str, object] = {"at": None, "value": {}}
_TTL_SECONDS = 300


def strengths(reference: Optional[datetime] = None, force: bool = False) -> Dict[str, float]:
    """{person_id: tie strength in 0..1}, cached briefly so a scoring pass is cheap."""
    now = reference or datetime.now(timezone.utc)
    cached_at = _CACHE["at"]
    if not force and isinstance(cached_at, datetime) and (now - cached_at).total_seconds() < _TTL_SECONDS:
        return _CACHE["value"]  # type: ignore[return-value]
    value = _compute(now)
    _CACHE["at"], _CACHE["value"] = now, value
    return value


def strength(person_id: str, reference: Optional[datetime] = None) -> float:
    return strengths(reference).get(person_id, 0.0)


# Messages of history needed before ratio-based signals are trusted at half
# weight. ~8 keeps a single exchange near zero while a real thread is unaffected.
_CONFIDENCE_K = 8


def _compute(now: datetime) -> Dict[str, float]:
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT conversation_id, person_id, is_from_user, timestamp FROM messages"
    ).fetchall()

    # Per thread, tally the counterpart's messages vs yours, and the last touch.
    # A 1:1 thread has a single counterpart person; that's who the exchange is
    # "with". (Group threads are noisier and simply contribute less signal.)
    inbound: Dict[str, int] = {}
    outbound: Dict[str, int] = {}
    last_seen: Dict[str, datetime] = {}
    conversation_person: Dict[str, str] = {}
    conversation_out: Dict[str, int] = {}

    for r in rows:
        pid, from_user, tid = r["person_id"], r["is_from_user"], r["conversation_id"]
        stamp = parse_iso(r["timestamp"])
        if from_user:
            conversation_out[tid] = conversation_out.get(tid, 0) + 1
        elif pid:
            conversation_person[tid] = pid
            inbound[pid] = inbound.get(pid, 0) + 1
            if stamp and (pid not in last_seen or stamp > last_seen[pid]):
                last_seen[pid] = stamp

    # Attribute your outgoing messages to the thread's counterpart.
    for tid, count in conversation_out.items():
        pid = conversation_person.get(tid)
        if pid:
            outbound[pid] = outbound.get(pid, 0) + count

    result: Dict[str, float] = {}
    for pid in inbound:
        i, o = inbound[pid], outbound.get(pid, 0)

        # Volume — log-scaled so a heavy thread doesn't dwarf everyone.
        volume = math.log1p(i + o) / math.log1p(400)          # ~1.0 near 400 msgs

        # Reciprocity — do you actually reply? The load-bearing term.
        reciprocity = (min(i, o) / max(i, o)) if o else 0.0

        # Recency — a relationship that's gone quiet for months decays.
        days = (now - last_seen[pid]).total_seconds() / 86400.0 if pid in last_seen else 999
        recency = math.exp(-days / 45.0)                       # ~half-life six weeks

        # Confidence — ratios and recency lie on tiny samples: one email with
        # one reply is "perfect reciprocity" and a recruiter scores like a best
        # friend. Shrink both toward zero until there's real history behind
        # them; volume needs no shrinking since it *is* the sample size.
        confidence = (i + o) / ((i + o) + _CONFIDENCE_K)

        raw = (
            0.30 * min(volume, 1.0)
            + 0.45 * reciprocity * confidence
            + 0.25 * recency * confidence
        )
        if _is_service(pid):
            raw *= 0.4
        result[pid] = round(min(raw, 1.0), 3)
    return result


def describe(person_id: str, reference: Optional[datetime] = None) -> str:
    """A human line for the "why this is here" panel."""
    s = strength(person_id, reference)
    if s >= 0.66:
        return "one of your closest — you two go back and forth constantly"
    if s >= 0.4:
        return "a real back-and-forth relationship"
    if s >= 0.2:
        return "you're in touch now and then"
    return "little history together"
