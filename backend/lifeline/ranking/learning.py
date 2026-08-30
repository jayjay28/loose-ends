"""The learning loop (§6, milestone 7).

Milestone 4 ships static weights. This is the step that makes the ranking
*learned*: observed behaviour moves the per-person, per-type and
per-person×type weights that the scorer reads back in.

Two rules keep it honest:

  * Only the input weights move. The blend coefficients in ``scorer`` stay
    fixed, so no feedback loop can amplify itself.
  * Deprioritization decays a weight; avoidance never does. Per the §6.3
    warning, an avoided item must not be quietly demoted into oblivion —
    that is precisely the failure mode the distinction exists to prevent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from .. import db
from ..models import BehaviorPattern, Item, parse_iso
from . import behavior

log = logging.getLogger(__name__)

LEARNING_RATE = 0.25
DECAY_RATE = 0.15          # applied when deprioritization is diagnosed
MIN_WEIGHT, MAX_WEIGHT = 0.05, 1.5
NEUTRAL = 0.5

POSITIVE_KINDS = {"acted", "completed_manual", "completed_auto", "replied"}
NEGATIVE_KINDS = {"dismissed"}


def _ema(current: float, target: float, rate: float = LEARNING_RATE) -> float:
    return max(MIN_WEIGHT, min(MAX_WEIGHT, current + rate * (target - current)))


def _nudge(key: str, target: float, prior: float, rate: float = LEARNING_RATE) -> float:
    row = db.get_weight_row(key)
    current = row["value"] if row else prior
    observations = (row["observations"] if row else 0) + 1
    updated = _ema(current, target, rate)
    db.set_weight(key, round(updated, 4), observations)
    return updated


# ------------------------------------------------------------------ events
def record(kind: str, item: Item, payload: Optional[Dict] = None) -> None:
    """Log a user behaviour and immediately fold it into the weights.

    Called from the API whenever the app reports an interaction (§8.3).
    """
    db.log_behavior(kind, item_id=item.id, person_id=item.person_id, item_type=item.type, payload=payload)

    if kind in POSITIVE_KINDS:
        _reinforce(item, positive=True)
    elif kind in NEGATIVE_KINDS:
        _reinforce(item, positive=False)

    if kind == "completed_manual":
        _record_manual_close(item)
    elif kind == "completed_auto":
        _record_auto_close(item)


def _reinforce(item: Item, positive: bool) -> None:
    from .signals import RELATIONSHIP_PRIOR, TYPE_PRIOR

    target = 1.0 if positive else 0.0
    if item.person_id:
        person = db.get_person(item.person_id)
        prior = RELATIONSHIP_PRIOR.get(person.relationship if person else None, RELATIONSHIP_PRIOR[None])
        _nudge(f"person:{item.person_id}", target, prior)
        _nudge(f"pair:{item.person_id}/{item.type}", target, NEUTRAL)
    _nudge(f"type:{item.type}", target, TYPE_PRIOR.get(item.type, 0.5))


# --------------------------------------------------- manual vs auto close
def _record_manual_close(item: Item) -> None:
    """§7 — a manual close says this item type doesn't reliably produce an
    external signal. Future items of the type lean on confirmation prompts
    instead of waiting for evidence that will never arrive."""
    _nudge(f"manual_rate:{item.type}", 1.0, 0.5, rate=0.2)


def _record_auto_close(item: Item) -> None:
    _nudge(f"manual_rate:{item.type}", 0.0, 0.5, rate=0.2)


def manual_close_rate(item_type: str) -> float:
    """0 = external signals always close this type; 1 = the user always does."""
    return db.get_weight(f"manual_rate:{item_type}", 0.5)


# ---------------------------------------------------------- batch passes
def apply_behavior_patterns(reference: Optional[datetime] = None) -> Dict[str, int]:
    """Run the §6.3 classifier over open items and act on the verdicts.

    Deprioritization decays the sender/type weight — "this is the actual
    learning step". Avoidance is recorded but never decays anything; the
    scorer gives it a gentle visibility boost instead.

    **§v2 step 7a — only a *change* of verdict is an event.** This ran on every
    poll cycle and logged plus re-nudged unconditionally, while the verdict
    itself only changes when the item does. At a five-minute cadence that is
    288 identical rows per item per day: the live database holds 18,492
    `pattern_deprioritized` rows — 97% of all behaviour events — with one item
    carrying 223 copies of the same verdict, and weights decayed 288 times for
    one unchanged fact. The transition guard already existed three lines down,
    where the item's own `behavior_pattern` field was being written; it simply
    was not applied to the logging and the learning.
    """
    now = reference or datetime.now(timezone.utc)
    counts = {"avoidance": 0, "deprioritized": 0, "neutral": 0, "changed": 0}

    for item in db.open_items():
        reading = behavior.classify(item, now)
        changed = item.behavior_pattern != reading.pattern

        if reading.is_deprioritized:
            counts["deprioritized"] += 1
        elif reading.is_avoidance:
            counts["avoidance"] += 1
        else:
            counts["neutral"] += 1

        if not changed:
            continue      # the same verdict about the same item is not news

        counts["changed"] += 1
        if reading.is_deprioritized:
            if item.person_id:
                _nudge(f"pair:{item.person_id}/{item.type}", 0.0, NEUTRAL, rate=DECAY_RATE)
            _nudge(f"type:{item.type}", 0.0, NEUTRAL, rate=DECAY_RATE / 2)
            db.log_behavior(
                "pattern_deprioritized", item_id=item.id, payload={"evidence": reading.evidence}
            )
        elif reading.is_avoidance:
            db.log_behavior(
                "pattern_avoidance", item_id=item.id, payload={"evidence": reading.evidence}
            )

        item.behavior_pattern = reading.pattern
        db.save_item(item)
    return counts


def learn_from_latency(reference: Optional[datetime] = None) -> int:
    """Turn observed reply speed into per-person weight movement (§6.2).

    **§v2 step 7a — only move a weight when the evidence moved.** This nudged
    every person on every poll cycle whether or not a single new message had
    arrived. Reply latencies do not change on their own, so the same fact was
    re-applied 288 times a day: `person:boooooby-carter` accumulated 1,041
    "observations" and saturated at 0.9996, while 19 of 41 people sat untouched
    at the 0.5125 prior because they never cleared the three-latency minimum.
    That is a has-enough-replies proxy, not learned importance.

    The fix is a fingerprint of the evidence itself. Same evidence, no nudge.
    """
    import statistics

    from .signals import RELATIONSHIP_PRIOR, person_reply_latencies

    updated = 0
    for person in db.list_people():
        latencies = person_reply_latencies(person.id)
        if len(latencies) < 3:
            continue
        median_hours = statistics.median(latencies)
        # What the nudge would be based on. If this hasn't moved, nothing has.
        fingerprint = f"{len(latencies)}:{round(median_hours, 2)}"
        if db.get_sync_state(f"latency_seen:{person.id}") == fingerprint:
            continue
        db.set_sync_state(f"latency_seen:{person.id}", fingerprint)

        # Sub-hour replies -> ~1.0, week-long silences -> ~0.1.
        target = max(0.1, min(1.0, 1.0 - (median_hours / 48.0)))
        prior = RELATIONSHIP_PRIOR.get(person.relationship, RELATIONSHIP_PRIOR[None])
        _nudge(f"person:{person.id}", target, prior, rate=0.1)
        updated += 1
    return updated


def repair(reference: Optional[datetime] = None) -> Dict[str, int]:
    """Undo the damage the two bugs above did to the weights.

    Recomputes rather than nudges. The latency evidence was always real — only
    its repeated application was wrong — so the honest correction is to set
    each person's weight to what the evidence says once, instead of letting an
    EMA crawl back from a saturated value at rate 0.1.

    `observations` is reset to the count of evidence actually behind the
    weight, so the number means what it claims. It was counting writes.
    """
    import statistics

    from .signals import RELATIONSHIP_PRIOR, person_reply_latencies

    fixed = 0
    for person in db.list_people():
        latencies = person_reply_latencies(person.id)
        if len(latencies) < 3:
            continue
        median_hours = statistics.median(latencies)
        target = max(0.1, min(1.0, 1.0 - (median_hours / 48.0)))
        prior = RELATIONSHIP_PRIOR.get(person.relationship, RELATIONSHIP_PRIOR[None])
        # One EMA step from the prior — the value a single honest observation
        # would have produced, which is exactly what the evidence supports.
        db.set_weight(
            f"person:{person.id}",
            round(_ema(prior, target, rate=0.1), 4),
            observations=len(latencies),
        )
        db.set_sync_state(
            f"latency_seen:{person.id}", f"{len(latencies)}:{round(median_hours, 2)}"
        )
        fixed += 1
    return {"people_recomputed": fixed}


def run(reference: Optional[datetime] = None) -> Dict[str, object]:
    """Full learning pass. Safe to run on every poll cycle."""
    from . import scorer

    patterns = apply_behavior_patterns(reference)
    people = learn_from_latency(reference)
    rescored = scorer.rescore_all(reference)
    return {"patterns": patterns, "people_updated": people, "items_rescored": rescored}


# ------------------------------------------------- §v2 step 7b: threads
#
# The audit's finding: every signal here is keyed on `person:{id}`,
# `type:{item_type}` or `pair:{person}/{type}`, and `behavior.classify()` takes
# an `Item`. **A thread has no person and no type.** There is nothing to key on,
# so a key space has to be designed before importance can be learned.
#
# Three keys, all of them things a thread genuinely has. Nothing invented:
#
#   thread_origin:{origin}   Did the user declare this, or did we propose it?
#                            A loop someone typed out themselves is a different
#                            kind of object from one a sweep opened, and that
#                            difference is learnable.
#   thread_person:{id}       Derived from evidence, and only when *all* of it
#                            points at one person. This is the bridge to the
#                            existing person weight space — the live database's
#                            15 threads are all single-person, but "Puerto Rico
#                            work trip" would not be, and guessing a person for
#                            it would be worse than having none.
#   thread_dated:{bool}      Whether it carries a deadline. Dated and undated
#                            loops are attended to differently.
#
# What feeds them is the swipe, per the spec's table. Resolve says *that
# mattered*; dig-in says *more than you judged*. Quiet says "right thread,
# wrong moment" — which is a statement about interruption, not importance, so
# it moves the bar in §7c rather than the weight here.

THREAD_PRIOR = 0.5


def thread_keys(thread) -> List[str]:
    keys = [
        f"thread_origin:{thread.origin}",
        f"thread_dated:{bool(thread.deadline)}",
    ]
    person = _sole_person(thread)
    if person:
        keys.append(f"thread_person:{person}")
    return keys


def _sole_person(thread) -> Optional[str]:
    """The person a thread is about, when there is exactly one. A thread that
    spans several has no person, and saying otherwise would key its learning on
    a coin flip."""
    people = set()
    for evidence in db.thread_evidence(thread.id):
        if evidence.kind == "item":
            item = db.get_item(evidence.ref_id)
            if item and item.person_id:
                people.add(item.person_id)
        elif evidence.kind == "message":
            message = db.get_message(evidence.ref_id)
            if message and message.person_id:
                people.add(message.person_id)
    return people.pop() if len(people) == 1 else None


def record_thread(kind: str, thread) -> None:
    """Fold a swipe into what the system believes about this kind of loop.

    `kind` is `resolved` or `dug_in`. Quiet is deliberately absent: it is a
    statement about timing, and it belongs to the interruption bar.
    """
    if kind not in ("resolved", "dug_in"):
        return
    target = 1.0 if kind == "dug_in" else 0.8
    rate = LEARNING_RATE if kind == "dug_in" else LEARNING_RATE / 2
    for key in thread_keys(thread):
        _nudge(key, target, THREAD_PRIOR, rate=rate)
    db.log_behavior(kind, payload={"thread_id": thread.id, "title": thread.title})


# ------------------------------------------------- §v2.1: move appetite
#
# What rejecting a move teaches, which the spec left open. Three readings were
# possible: this thread wants no initiative, this *kind* of move is unwanted,
# or the timing was wrong. The third is already spoken for — `record_thread`
# refuses to learn from `quiet` because timing belongs to the interruption bar,
# and the same rule holds here. The first is too broad to learn from a single
# tap: one bad proposal should not end initiative on a thread the user is still
# carrying.
#
# So a rejection is keyed on the **shape of move against the shape of thread**.
# Rejecting a `do` move on an undated bill teaches "stop proposing do-moves for
# loops like this one", which is narrow enough to be fair and general enough to
# be worth knowing.
MOVE_PRIOR = 0.6

# Below this the worker stops offering that shape and records a finding
# instead. Set above the floor a single rejection produces (0.6 -> 0.45), so
# one "no" narrows the odds without silencing the shape outright — it takes a
# pattern, which is what learning is for.
MOVE_FLOOR = 0.4


def move_keys(thread, move_kind: str) -> List[str]:
    """A move's keys: its shape crossed with the thread's.

    The bare `move:` key is included so a user who dislikes being handed
    irreversible things is heard across their whole stack, not only on loops
    that happen to share an origin.
    """
    return [f"move:{move_kind}"] + [
        f"move:{move_kind}|{key}" for key in thread_keys(thread)
    ]


def record_move(kind: str, thread, move_kind: Optional[str]) -> None:
    """Fold a verdict on a proposed move into what the system will offer next.

    Both directions are recorded on purpose. A signal that only ever falls is a
    ratchet: initiative would decay to nothing as rejections accumulated and
    nothing ever restored it, and the system would quietly stop trying without
    anyone deciding that it should.
    """
    if kind not in ("accepted", "rejected") or not move_kind:
        return
    target = 1.0 if kind == "accepted" else 0.0
    for key in move_keys(thread, move_kind):
        _nudge(key, target, MOVE_PRIOR)
    db.log_behavior(
        f"move_{kind}",
        payload={"thread_id": thread.id, "move_kind": move_kind, "title": thread.title},
    )


def move_appetite(thread, move_kind: str) -> float:
    """How welcome this shape of move is on a thread like this one.

    Averaged rather than multiplied, for the same reason `thread_importance`
    is: an unfamiliar key means no information, not bad news.
    """
    values = [db.get_weight(key, MOVE_PRIOR) for key in move_keys(thread, move_kind)]
    return round(sum(values) / len(values), 4) if values else MOVE_PRIOR


def may_propose(thread, move_kind: str) -> bool:
    """Whether the worker should still offer this shape here.

    Only ever narrows. The ceiling is the user's explicit rule and this is the
    learned one; learning may close a door the user left open and may never
    open one they closed — the same asymmetry the autonomy ladder has held
    since step 4.
    """
    return move_appetite(thread, move_kind) >= MOVE_FLOOR


def thread_importance(thread) -> float:
    """What the system has learned about loops shaped like this one.

    Averaged over the keys rather than multiplied: with three signals of
    similar strength, a product would let one unfamiliar key drag a thread to
    near zero, and an unfamiliar key means *no information*, not bad news.
    """
    values = [db.get_weight(key, THREAD_PRIOR) for key in thread_keys(thread)]
    return round(sum(values) / len(values), 4) if values else THREAD_PRIOR


def prune_duplicate_verdicts() -> int:
    """Delete the machine-generated pile, keeping the first row per item.

    These are not user behaviour. They are one verdict about one unchanged item
    written 288 times a day by the bug above, and while they sit in the table
    every future question asked of `behavior_events` gets a 97%-noise answer.
    Never run automatically — deleting history is the user's call, even when
    the history is an artifact.
    """
    conn = db.get_connection()
    cur = conn.execute(
        """DELETE FROM behavior_events
           WHERE kind IN ('pattern_deprioritized', 'pattern_avoidance')
             AND id NOT IN (
               SELECT MIN(id) FROM behavior_events
               WHERE kind IN ('pattern_deprioritized', 'pattern_avoidance')
               GROUP BY item_id, kind)"""
    )
    conn.commit()
    return cur.rowcount


def snapshot() -> Dict[str, Dict[str, float]]:
    """Inspectable view of everything the model has learned so far."""
    out: Dict[str, Dict[str, float]] = {"person": {}, "type": {}, "pair": {}, "manual_rate": {}, "signal": {}}
    for key, value in db.all_weights().items():
        prefix, _, rest = key.partition(":")
        out.setdefault(prefix, {})[rest] = value
    return out
