"""The background cycle that ties the layers together (§4, §7 step 1)."""
from __future__ import annotations

import asyncio
import threading
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .. import db
from .. import threads
from ..threads import closure as thread_closure
from ..threads import watchers as thread_watchers
from ..assistant import sweeps, worker
from ..completion import engine
from ..config import get_config
from ..extraction import pipeline, topics
from ..ingestion import applecal, gcal, gmail, google_auth, imessage
from ..notifications import scheduler
from ..ranking import learning, scorer
from . import health

log = logging.getLogger(__name__)

# Only one poll cycle may run at a time — a manual /sync/poll must not race the
# periodic poller (two concurrent cycles corrupt SQLite writes).
_cycle_lock = threading.Lock()


def poll_sources() -> Dict[str, Any]:
    """Pull new data from every available source. Each is isolated so one
    failing (or being unconfigured) never blocks the others."""
    result: Dict[str, Any] = {}

    # iMessage — local, needs no account, but needs Full Disk Access to read
    # the live Messages DB (a no-op returning 0 when it can't).
    try:
        result["imessage"] = imessage.poll()
    except Exception as exc:
        log.warning("imessage poll failed: %s", exc)
        result["imessage_error"] = str(exc)

    # Gmail + Calendar — only when a Google account is connected.
    if google_auth.is_connected():
        try:
            result["gmail"] = gmail.poll()
        except Exception as exc:
            log.warning("gmail poll failed: %s", exc)
            result["gmail_error"] = str(exc)
        if google_auth.has_scope("calendar"):
            try:
                result["calendar"] = gcal.poll()
            except Exception as exc:
                log.warning("calendar poll failed: %s", exc)
                result["calendar_error"] = str(exc)
        else:
            result["calendar"] = "scope not granted"
    else:
        result["google"] = "not connected"

    # Raw-sentence titles get their headline here when the declare-time pass
    # had no provider (`eval-titles-are-raw-sentences`).
    try:
        from ..threads import titles as thread_titles
        thread_titles.sweep()
    except Exception as exc:
        log.warning("title sweep failed: %s", exc)

    # §v2.8 — the local Calendar store, synced by the OS itself. Needs no
    # Google connection, works while the Calendar API 403s, and converges on
    # the same event ids as the API and .ics doors.
    try:
        result["applecal"] = applecal.poll()
    except Exception as exc:
        log.warning("applecal poll failed: %s", exc)

    return result


def cycle(reference: Optional[datetime] = None) -> Dict[str, Any]:
    """One full pass: ingest -> extract -> rank -> detect -> notify.
    Serialized: if a cycle is already running, this one is skipped."""
    if not _cycle_lock.acquire(blocking=False):
        log.info("poll cycle already running; skipping this trigger")
        return {"skipped": "cycle already running"}
    try:
        return _run_cycle(reference)
    finally:
        _cycle_lock.release()


def _run_cycle(reference: Optional[datetime] = None) -> Dict[str, Any]:
    now = reference or datetime.now(timezone.utc)
    sources = poll_sources()
    extracted = pipeline.run()
    topics.refresh_thread_topics()
    completion = engine.scan(now)
    discovered = sweeps.run_all(now)   # §v1.4 idle sweeps — silence detection etc.
    # §v2 step 2: a resolved thread stays visible (struck through) for a day so
    # the pile is seen to shrink, then archives itself. Nobody has to tidy.
    # §v2 step 5 — does anything that arrived close a thread? Runs before the
    # worker so a thread that just closed isn't worked one last time for nothing.
    # §v2 step 6 — standing monitors. Runs before closure and the worker: a
    # watcher that fires attaches evidence, which is exactly what makes the
    # thread due for the worker on this same cycle.
    watched = thread_watchers.sweep(now)
    thread_closures = thread_closure.scan(now)
    archived = threads.sweep_resolved(now)
    # §v2 step 4 — the system working while nobody asked. Self-limiting on both
    # count and its own budget, so calling it every cycle is safe even though
    # it only acts when a thread has new evidence or has gone a day unworked.
    worked = worker.run(now)
    scorer.rescore_all(now)   # recency/currency decay as time passes, not just on new data
    learned = learning.run(now)
    # Before the notification pass, not after: the health check *queues* a
    # notification, and `scheduler.run` is what flushes the queue. Running
    # it afterwards would leave an alert about a broken system sitting in
    # the database until the next cycle — thirty minutes of silence, which
    # is the exact failure this is here to end. Everything that does real
    # work has already run by this point, so nothing waits on it, and it
    # self-gates to once an hour.
    checked = health.run_safely(now)
    notified = scheduler.run(now)
    summary = {
        "at": now.isoformat(timespec="seconds"),
        "sources": sources,
        "extracted": len(extracted),
        "completion": completion.summary(),
        "discovered": discovered,
        "watchers": watched,
        "thread_closures": thread_closures,
        "threads_archived": archived,
        "worker": worked,
        "learning": learned,
        "notifications": notified,
        "health": checked,
    }
    log.info("poll cycle: %s", summary)
    return summary


# ------------------------------------------------------------- imessage watch
#
# The interval is five minutes, which is the right cadence for mail and
# calendar — they are polled over the network and nothing is lost by being a
# few minutes behind. iMessage is a local file, and being five minutes behind
# there means a message can land, be read on the phone, and be replied to,
# before the system that is supposed to be watching it has noticed.

_WATCH_TIMEOUT = 60.0      # wake up periodically even when nothing is written
_WATCH_DEBOUNCE = 2.0      # let a burst of messages settle before importing
_WATCH_LOCK_WAIT = 45.0    # long enough to outlast a normal cycle


def _imessage_once() -> int:
    """One watched import, holding the same lock a full cycle takes.

    `imessage.poll()` writes to SQLite, and `_cycle_lock` exists precisely
    because two concurrent writers corrupt it. The watcher is a second writer,
    so it queues behind a running cycle rather than racing it — and gives up if
    that cycle is somehow still going, since the cycle would have imported the
    same messages itself anyway.
    """
    if not _cycle_lock.acquire(timeout=_WATCH_LOCK_WAIT):
        log.debug("imessage watch: a cycle is still running, leaving it to that")
        return 0
    try:
        return imessage.poll()
    finally:
        _cycle_lock.release()


async def watch_imessage() -> None:
    """Import iMessages when they arrive rather than up to an interval later.

    Deliberately silent about failure: on a machine without Full Disk Access —
    or without kqueue — `wait_for_change` returns False immediately, and this
    settles into a slow idle loop while the interval poll goes on doing the
    work. It is an optimisation, and it is never the only path to the data.
    """
    log.info("imessage watch started (kqueue available: %s)", imessage.HAS_WATCH)
    while True:
        try:
            changed = await asyncio.to_thread(imessage.wait_for_change, _WATCH_TIMEOUT)
            if not changed:
                if not imessage.HAS_WATCH:
                    # Nothing to watch with; don't spin.
                    await asyncio.sleep(_WATCH_TIMEOUT)
                continue
            # A conversation arrives as a burst of writes. Import once, after.
            await asyncio.sleep(_WATCH_DEBOUNCE)
            found = await asyncio.to_thread(_imessage_once)
            if found:
                log.info("imessage watch: imported %s new message(s)", found)
        except asyncio.CancelledError:
            log.info("imessage watch stopped")
            raise
        except Exception:
            log.exception("imessage watch failed; continuing")
            await asyncio.sleep(_WATCH_TIMEOUT)


async def run_forever(interval_seconds: Optional[int] = None) -> None:
    interval = interval_seconds or get_config().poll_interval_seconds
    log.info("poller started, interval %ss", interval)
    watcher = asyncio.create_task(watch_imessage())
    try:
        while True:
            try:
                await asyncio.to_thread(cycle)
            except asyncio.CancelledError:
                log.info("poller stopped")
                raise
            except Exception:
                log.exception("poll cycle failed; continuing")
            await asyncio.sleep(interval)
    finally:
        watcher.cancel()
