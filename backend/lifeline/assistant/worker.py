"""The worker loop (§v2 step 4) — the system working while you aren't asking.

This is the architectural shift the whole redesign exists for. v1.5 answers
when asked; everything before this step still only reacts. Here the system
picks up a thread on its own, asks *what do I know, what changed, what's worth
checking*, and writes back what it found.

Three constraints shape it, and each one is a decision rather than an accident:

**Cadence.** A thread is worked when evidence actually arrives for it, plus one
pass per thread per day so nothing goes stale. The poller runs every five
minutes; working every thread every cycle would be 4,320 passes a day against
a 1,500-call ceiling — ten times the budget, and it would starve extraction.
Evidence-triggered plus a daily floor is responsive when the user's world moves
and near-silent when it doesn't.

**Budget.** The worker is the first thing in this codebase that spends money
while the user is asleep, so it gets a ceiling of its own (`budget.TRIGGER_CAPS`)
inside the global one. A heavy ingest day cannot eat the worker's budget, and a
runaway worker cannot eat extraction's.

**Honesty.** "I looked and found nothing" is recorded as a finding, not
discarded. It is what the grey marks on the lane's activity track mean, and a
system that logs only its hits is misrepresenting its work.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .. import db
from .. import threads as threads_mod
from ..threads import watchers as watch_mod
from ..extraction import budget
from ..models import Autonomy, FindingKind, ThreadState, parse_iso
from . import loop as assistant_loop
from . import sweeps
from . import registry as reg

log = logging.getLogger(__name__)

# One pass per thread per day, plus whenever evidence lands. Below this a
# thread is left alone: re-reading the same evidence an hour later produces the
# same answer at the same price.
DAILY_FLOOR_HOURS = 20

# A ceiling per cycle, so a backlog drains gradually instead of spending the
# whole day's budget in one five-minute window.
MAX_THREADS_PER_CYCLE = 3

WORKER_SYSTEM = (
    "You are working one of the user's open threads on your own initiative. "
    "They did not ask; they are living their life and you are checking whether "
    "anything about this loop needs them.\n"
    "Read the thread's own state first with read_thread_state — what you "
    "already found, what it already claims — then look for what CHANGED or "
    "what the user is likely to be missing. Do not re-investigate what the "
    "thread already knows, and do not repeat a finding it already has.\n"
    "Use the read tools freely: messages, mail, calendar, timeline, history, "
    "people. Nothing you read costs the user anything.\n"
    "`sources_last_delivered` says when each source last brought anything in. "
    "You cannot see past those moments: do not describe silence, a lack of "
    "reply, or \"nothing has changed\" after them, because the absence you "
    "would be describing is the store's and not the world's.\n"
    "**web_search and web_fetch reach the actual world, and most threads need "
    "them.** A thread about buying something is not worked by noting that it "
    "is still unbought — it is worked by going and finding the thing: real "
    "options, real prices, in the range they said, with links. Same for a "
    "tradesperson to call, a venue's opening hours, whether tickets are still "
    "available, what a form actually requires. If you find yourself about to "
    "record that nothing has changed, ask first whether a search would have "
    "changed it.\n"
    "Two rules for a query. It leaves this machine, so write it about the "
    "TASK and never about the user's private life: not the user's name, not "
    "their family or contacts, no phone numbers or addresses, nothing from a "
    "message that identifies them. \"matching pyjama sets under $150\" — "
    "never the sentence the user typed. And search for what is checkable: "
    "prices, availability, dates, requirements. Do not search to form an "
    "opinion about their life.\n"
    "One carve-out, because outreach threads are impossible without it: when "
    "the thread's declared task is to CONTACT a public figure or business — a "
    "creator, a company, an author, someone the user has never exchanged a "
    "message with — you may search their public name for their public or "
    "business contact information, and only that. A YouTuber's booking email "
    "is public by intent; the user's mother is not. If you draft outreach and "
    "lack an address, search for it before recording the draft as blocked.\n"
    "**`what_you_told_me` is the user correcting you, and it outranks "
    "everything else in the brief.** Every other field is your own reading of "
    "their data; that one is them telling you the reading was wrong. It is "
    "empty on almost every thread, so when it is not, someone took the trouble "
    "to type it. Do not re-litigate it, do not ask again about something it "
    "settles, and do not produce a move that contradicts it. If it says the "
    "choice is theirs to make, make it — do not hand the question back.\n"
    "`what_today_looks_like` is the user's next two days. Read it before you "
    "decide anything. It is not background colour — it changes what a thread "
    "needs and when. A bill due in three weeks becomes urgent if they fly "
    "tomorrow; a reply that could wait cannot wait if the meeting it concerns "
    "is this afternoon; and a move that needs them at a desk is worth less "
    "while they are travelling. Say so explicitly when it applies, and say "
    "nothing about it when it does not.\n"
    "A pass has THREE jobs and you finish them in order before you stop.\n"
    "\n"
    "**1. Set what should be watched from here on.** A thread implies monitors "
    "nobody will ever ask for again: mail from the airline, the payment "
    "confirmation that hasn't arrived, the registration date itself coming "
    "closer. Call list_watchers to see what is already set, then add_watcher "
    "for what is missing. They are free — a watcher is database queries, not "
    "thinking — so the cost of setting one is nothing and the cost of missing "
    "one is that the user finds out too late. Do this BEFORE you record, "
    "because recording is what ends the pass.\n"
    "\n"
    "**2. Ask what would actually close this.** This is the job you exist "
    "for, and the one most easily skipped. The user did not upload this thread "
    "so it could be described back to them accurately — they uploaded it "
    "hoping to be rid of it. So before you write anything down, answer: what "
    "is the single next move that would advance or end this loop, and how much "
    "of it can you do right now?\n"
    "Naming the gap is not doing this job. \"No response has been sent\", "
    "\"no plan yet\", \"still waiting\" — those describe the hole. The move is "
    "what fills it.\n"
    "\n"
    "A move must be specific enough to act on without thinking. That is the "
    "test, and it is strict:\n"
    "- \"Look into ticket prices\" is NOT a move. \"$89-$140, on sale until "
    "Aug 22, here is the link\" is.\n"
    "- \"Reply to Guest Relations\" is NOT a move. A written draft saying you "
    "land at 12:04 and will arrive around 12:30 is.\n"
    "- \"Pay the bill\" is NOT a move. \"$385.40 total: $153.52 past due plus "
    "$231.88 by Aug 31, payable at <link>\" is.\n"
    "If you cannot get it to that standard, it is a finding, not a move. Do "
    "not gesture at a direction and call it initiative.\n"
    "\n"
    "**A move is made of work you already did, not work you described.** The "
    "body must contain the thing itself — the actual draft, the actual "
    "figures, the actual link — not a plan for producing it. If what you are "
    "writing reads like instructions for assembling the answer (\"compile a "
    "list of...\", \"link to each resource\", \"confirm that...\"), you have "
    "not made a move; you have written a note to yourself. Record a finding "
    "instead.\n"
    "In particular: a resource you cannot link to is not a resource. A price, "
    "vendor, course or article you did not look up is a guess, and the user "
    "cannot act on a guess — so look it up. You have web_search: use it, then "
    "name the thing with its price and its link. What you may not do is state "
    "a figure you did not verify. If the move needs information that searching "
    "cannot get you — something only the user knows — say that is what it "
    "needs.\n"
    "\n"
    "Most threads cannot be finished inside this app — a bill gets paid on "
    "someone else's website, documents get uploaded to a county office, a "
    "decision gets made by a human being. That does not excuse you from this "
    "job; it defines it. For those, the move is to assemble every number, "
    "date, link and document the user would otherwise have to go and find, so "
    "that what is left is only the part that genuinely needs them.\n"
    "\n"
    "**Some threads have no move, and saying so is a real answer.** If the "
    "next step belongs entirely to the user, or the thread is waiting on "
    "someone else, say that plainly and stop. Do NOT invent a move to justify "
    "the pass — a move the user rejects costs them more than a finding they "
    "ignore, because it asks them to overrule you rather than merely skim you.\n"
    "\n"
    "**3. Record what this pass produced.** Exactly one call:\n"
    "- record_finding(kind='action') — you have a move that passes the test "
    "above. Lead the headline with the move itself, not the gap. Put the work "
    "you already did in `steps` and `facts`.\n"
    "**Choose move_kind by what the thread needs, not by what is easiest to "
    "write:**\n"
    "- `decide` — they must choose, and you found the options. One real "
    "option per entry in `facts`: the thing, its price, its link.\n"
    "- `send` — a message is the deliverable, and you have written it.\n"
    "- `do` — they must act somewhere else, and you staged every figure and "
    "link so only their part is left.\n"
    "- `gather` — no action exists yet and the material is the value.\n"
    "**On any thread about buying or choosing, the shape is `decide`, and the "
    "options ARE the work.** Do not draft a message asking what they want. "
    "They asked you to find it, so finding it is the job, and a question sent "
    "back is the job returned unstarted. If you know enough to price the "
    "options then you know enough to pick one: name the one you would take and "
    "what it costs, and let them overrule you. A `decide` that lists four "
    "things and recommends none has not decided.\n"
    "`needs` is what only the user can supply, and it is at most ONE thing — "
    "with your best assumption stated, so their silence still moves the thread "
    "rather than stopping it. Three questions in `needs` is the same failure as "
    "a message asking what they want, wearing a different hat.\n"
    "If you can name the move but could not stage it, say why in "
    "`blocked_reason` instead of leaving steps empty. It sits there ready; "
    "you never send or spend anything.\n"
    "- record_finding(kind='finding') — you learned something they'd want to "
    "know but there is no move, or none you could stage. Be specific: names, "
    "dates, amounts, and what it means for them.\n"
    "- record_finding(kind='nothing') — you looked and there is nothing new. "
    "This is a real and useful result. Record it honestly rather than "
    "inventing something to justify the pass.\n"
    "\n"
    "A finding must be worth interrupting a busy person for. Restating what "
    "the thread already says is not a finding. When in doubt, record nothing — "
    "but still set the watchers.\n"
    "Your final message is a one-line note of what you did, for the log."
)

# What this thread's ceiling permits, appended to the system prompt. Without it
# the worker spends a pass composing a move the tool will refuse, and the
# refusal arrives after the thinking is already paid for.
CEILING_NOTE = {
    Autonomy.SILENT: (
        "\n\n**This thread is set to no writing.** You may say what is missing "
        "and what it would take, and you may not stage anything that fills it — "
        "no drafts, no moves of any shape. Record findings only. The user chose "
        "this; do not work around it."
    ),
    Autonomy.PREPARED: (
        "\n\n**This thread is set to prepare things.** You may propose send, "
        "decide and gather moves — everything reversible and free. You may NOT "
        "propose a 'do' move: anything that spends money or cannot be undone "
        "needs the user to raise this thread first. When the real next move is "
        "one of those, record a finding that says so and lays out what it would "
        "take, so they can decide with the work already done."
    ),
    Autonomy.ASK: (
        "\n\n**This thread is set to ask before acting.** You may propose any "
        "move, including 'do' moves that spend money or cannot be undone. You "
        "still never send or spend anything yourself — you stage it and the "
        "user acts."
    ),
}


def due(reference: Optional[datetime] = None) -> List[Any]:
    """Threads worth a pass right now, most pressing first.

    Two triggers, per the cadence decision: evidence arrived since the last
    pass, or the last pass is older than the daily floor. Quiet threads are
    still worked — quiet means "stop surfacing", not "stop thinking" — but they
    sort last.
    """
    now = reference or datetime.now(timezone.utc)
    floor = now - timedelta(hours=DAILY_FLOOR_HOURS)

    candidates = []
    for thread in db.list_threads(states=ThreadState.OPEN, reference=now):
        worked = parse_iso(thread.last_worked_at)
        if worked is None:
            candidates.append((0, thread))          # never worked — highest claim
            continue
        newest = _newest_evidence(thread.id)
        if newest and newest > worked:
            candidates.append((1, thread))          # something arrived since
        elif worked < floor:
            candidates.append((2, thread))          # gone stale
    candidates.sort(
        key=lambda pair: (pair[0], pair[1].state == ThreadState.QUIET, -pair[1].importance)
    )
    return [thread for _, thread in candidates]


def _newest_evidence(thread_id: str) -> Optional[datetime]:
    stamps = [parse_iso(e.linked_at) for e in db.thread_evidence(thread_id)]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


# How far ahead the worker is told to look. Two days rather than a week: the
# question this answers is "does the user's actual day change what this thread
# needs", and a meeting on Thursday changes nothing about a bill due in
# September. A longer window is more tokens for less signal.
HORIZON_HOURS = 48


def whats_coming(reference: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """The user's next two days, handed to every pass.

    This is the whole of §v2.1's situational awareness, and it needs no sensors.
    The worker already produced the most useful sentence in the database this
    way — "you're leaving for Puerto Rico tomorrow morning (flight 12:00 PM,
    Aug 9), so this may need to be handled before then or remotely" — by
    cross-referencing a bill against a calendar entry. Nothing was listening to
    a microphone; the situation was in the user's own data the whole time.

    The alternative design (detect a real-time state and fire tools as it
    changes) additionally cannot work on iOS, which grants background execution
    when it chooses rather than when someone walks through an airport.
    """
    now = reference or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=HORIZON_HOURS)

    coming = []
    # Deduped on what the user would actually perceive as one event. The live
    # calendar carries recurring instances as separate rows with distinct ids —
    # "Paper Recycling" appeared five times at the same minute — and without
    # this the window fills with repeats and the real day is pushed out of it.
    seen = set()
    for event in db.list_calendar_events():
        start = parse_iso(event.start_at)
        if start is None or not (now - timedelta(hours=12) <= start <= horizon):
            continue
        fingerprint = (event.summary, event.start_at)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        coming.append({
            # `summary`, not `title` — CalendarEvent mirrors Google's field
            # names. Writing `.title` here is the same slip this codebase
            # already fixed once in search_calendar.
            "what": event.summary,
            "when": event.start_at,
            "where": event.location or None,
            # Said plainly so the model doesn't have to do date arithmetic to
            # notice the thing that matters most: whether it has already begun.
            "status": "under way or just finished" if start < now else "ahead",
        })
    return sorted(coming, key=lambda e: e["when"])[:12]


def work(thread_id: str, reference: Optional[datetime] = None) -> Optional[Any]:
    """One pass over one thread. Returns the LoopResult, or None when no
    provider is available or the budget is spent."""
    thread = db.get_thread(thread_id)
    if not thread:
        return None

    recorded: List[Any] = []
    brief = threads_mod.draft_brief(thread.id)
    brief["watching"] = [
        {"kind": w.kind, "what": w.what, "times_fired": w.fire_count}
        for w in watch_mod.for_thread(thread.id)
    ]
    brief["findings_so_far"] = [
        {"kind": f.kind, "headline": f.headline, "when": f.created_at}
        for f in db.thread_findings(thread.id)[:10]
    ]
    brief["what_today_looks_like"] = whats_coming(reference)
    # What the store can see. A pass that says "no new updates" is only true
    # up to the last moment a source delivered anything, and for five days in
    # August that moment was five days old.
    brief["sources_last_delivered"] = sweeps.source_freshness()
    # §v2.8 phase 4 — what the store knows about the things this thread is
    # about, so a pass starts grounded instead of spending its first three
    # iterations rediscovering the address and the account.
    try:
        from .. import world
        world.bind_thread(thread.id, f"{thread.title} {thread.summary or ''}",
                          person_id=getattr(thread, "contact_person_id", None))
        brief["known_entities"] = world.thread_entities(thread.id)
    except Exception:
        log.exception("entity grounding failed for %s", thread.id)

    run = assistant_loop.run_loop(
        "Work this thread. What changed, and is there anything the user is "
        f"missing?\n\n{json.dumps(brief, indent=2, default=str)}",
        trigger="worker",
        tools=reg.scoped_for(thread, recorded_findings=recorded),
        system=WORKER_SYSTEM + CEILING_NOTE.get(
            getattr(thread, "autonomy", Autonomy.PREPARED),
            CEILING_NOTE[Autonomy.PREPARED],
        ),
        # A pass that reads state, searches two sources, opens a message in
        # full, sets a watcher and records a finding is eight turns. The first
        # live run used all six and stopped before it ever reached the watchers.
        max_iterations=10,
    )
    if run is None:
        return None      # no provider or out of budget — try again next cycle

    # Back-fill provenance. The tool saves as it goes (so a run that dies
    # doesn't lose what it already learned); the run id only exists once the
    # loop finishes, and a finding whose run can't be opened is an assertion.
    for finding in recorded:
        finding.loop_run_id = run.run_id
        db.save_finding(finding)

    # A pass that recorded nothing at all still happened, and the thread has to
    # know that or it will be picked again immediately.
    if not recorded:
        db.save_finding(
            threads_mod.make_finding(
                thread.id, kind=FindingKind.NOTHING,
                headline="Checked, nothing new",
                body=run.conclusion or "", loop_run_id=run.run_id,
            )
        )

    thread.last_worked_at = (reference or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    db.save_thread(thread)
    return run


def run(reference: Optional[datetime] = None) -> Dict[str, Any]:
    """The poller's entry point. Self-limiting on both count and budget, so it
    is safe to call every cycle even though it only acts occasionally."""
    now = reference or datetime.now(timezone.utc)
    if not budget.allow("worker"):
        return {"worked": 0, "skipped": "budget spent"}

    queue = due(now)[:MAX_THREADS_PER_CYCLE]
    worked, findings = 0, 0
    for thread in queue:
        if not budget.allow("worker"):
            log.info("worker budget spent mid-cycle; stopping")
            break
        before = len(db.thread_findings(thread.id))
        result = work(thread.id, now)
        if result is None:
            break        # no provider — nothing will work this cycle
        worked += 1
        findings += len(db.thread_findings(thread.id)) - before

    if worked:
        log.info("worker: %d thread(s), %d finding(s)", worked, findings)
    return {"worked": worked, "findings": findings, "queued": len(queue)}
