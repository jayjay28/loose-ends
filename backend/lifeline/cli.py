"""Command line for operating Lifeline without the app.

    python -m lifeline.cli demo                 # load samples, run everything
    python -m lifeline.cli import imessage FILE
    python -m lifeline.cli poll
    python -m lifeline.cli today
    python -m lifeline.cli why ITEM_ID
    python -m lifeline.cli auth-url
    python -m lifeline.cli serve
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from . import db
from .completion import engine
from .config import get_config
from .extraction import pipeline
from .ingestion import gcal, gmail, google_auth, imessage, load_sample_corpus, whatsapp
from .ingestion.base import IdentityResolver
from .jobs import poller
from .models import InterruptionLevel
from .notifications import scheduler
from .ranking import learning, scorer
from .threads import bootstrap

LEVEL_LABEL = {
    InterruptionLevel.TIME_SENSITIVE: "NOW  ",
    InterruptionLevel.ACTIVE: "SOON ",
    InterruptionLevel.PASSIVE: "later",
}


def _print_today() -> None:
    items = scorer.ranked()
    if not items:
        print("Nothing open. Lifeline is watching the threads.")
        return
    current = None
    for item in items:
        if item.interruption_level != current:
            current = item.interruption_level
            print(f"\n── {LEVEL_LABEL.get(current, current).strip()} ──")
        flag = ""
        if item.behavior_pattern == "avoidance":
            flag = "  (looks avoided)"
        elif item.behavior_pattern == "deprioritized":
            flag = "  (deprioritised)"
        due = f"  due {item.entities.date[:10]}" if item.entities.date else ""
        print(f"  [{item.score:5.2f}] {item.person:<14} {item.suggested_action}{due}{flag}")
        print(f"           {item.id}")

    pending = engine.open_confirmations()
    if pending:
        print("\n── possible matches, confirm? ──")
        for c in pending:
            item = c["item"]
            print(f"  {item.person:<14} {item.suggested_action}")
            print(f"           {c['evidence_text'][:90]}")
            print(f"           {int(float(c['confidence']) * 100)}% · {'; '.join(c['reasons'])[:100]}")
            print(f"           confirm: {c['signal_id']}")


def _print_threads() -> None:
    """The stack, the way the user is meant to feel it: what you're carrying,
    soonest first, and how many proposals are waiting out of the way."""
    from .models import ThreadState

    stack = db.open_threads()
    if not stack:
        print("No open threads. Nothing running in the background.")
    for thread in stack:
        due = f"  due {thread.deadline[:10]}" if thread.deadline else ""
        src = f" ({thread.deadline_source})" if thread.deadline_source else ""
        quiet = "  · quiet" if thread.state == ThreadState.QUIET else ""
        n = len(db.thread_evidence(thread.id))
        print(f"  [{thread.importance:4.2f}] {thread.title}{due}{src}{quiet}")
        print(f"           {n} evidence · {thread.origin} · {thread.id}")

    waiting = db.list_threads(states=[ThreadState.PROPOSED])
    if waiting:
        print(f"\n── {len(waiting)} proposal(s), waiting where you can ignore them ──")
        for thread in waiting:
            print(f"  {thread.title}")
            print(f"           {thread.id}")


def _print_why(item_id: str) -> int:
    item = db.get_item(item_id)
    if not item:
        print(f"no item {item_id}", file=sys.stderr)
        return 1
    print(f"{item.person} · {item.type} · {item.interruption_level}")
    print(f"{item.suggested_action}")
    print(f'"{item.raw_text[:200]}"')
    print(f"\nscore {item.score}")
    for entry in item.score_explanation:
        print(f"  {entry['contribution']:+6.2f}  {entry['signal']:<20} {entry['detail']}")
    if item.suggested_reply:
        print(f"\nsuggested reply: {item.suggested_reply}")
    signals = db.signals_for_item(item.id)
    if signals:
        print("\ncompletion signals:")
        for s in signals:
            print(f"  [{s.resolution}] {s.source} {s.confidence:.2f} — {'; '.join(s.reasons)[:120]}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="lifeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="load the sample corpus and run the full pipeline")
    sub.add_parser("today", help="print the ranked briefing")
    sub.add_parser("poll", help="run one full poll cycle")
    sub.add_parser("extract", help="run extraction over un-processed messages")
    sub.add_parser("scan", help="run completion detection")
    sub.add_parser("learn", help="run the learning loop")
    sub.add_parser("notify", help="queue and flush notifications")
    sub.add_parser("model", help="dump learned weights")
    sub.add_parser("auth-url", help="print the Google authorization URL")
    sub.add_parser("pair", help="mint a pairing code for a new device (§v3)")
    sub.add_parser("purge", help="delete all extracted data")
    sub.add_parser("threads", help="print the thread stack (§v2)")

    p_repair = sub.add_parser(
        "learning-repair",
        help="recompute weights the per-cycle re-nudge bug corrupted (§v2 7a)",
    )
    p_repair.add_argument(
        "--prune", action="store_true",
        help="also delete duplicate machine-generated behaviour rows (keeps the first per item)",
    )

    p_boot = sub.add_parser("bootstrap-threads", help="turn open items into threads (§v2 step 1)")
    p_boot.add_argument(
        "--no-llm", action="store_true",
        help="skip the LLM clustering pass (it is the default; see bootstrap.py for why)",
    )
    p_boot.add_argument("--dry-run", action="store_true", help="show the clusters, write nothing")
    p_boot.add_argument("--compare", action="store_true", help="run both clusterers side by side")

    p_att = sub.add_parser(
        "attachments",
        help="attachment ingestion (phase 0: `scan` discovers metadata; fetch/parse land in 0.2)",
    )
    p_att.add_argument("action", choices=["scan", "backfill", "ics"])
    p_att.add_argument("--days", type=int, default=90, help="how far back to look (default 90)")
    p_att.add_argument("--limit", type=int, default=None, help="backfill: stop after this many carriers")

    p_world = sub.add_parser("world", help="the world model (§v2.8): entity facts")
    p_world.add_argument("action", choices=["backfill", "show"])
    p_world.add_argument("--days", type=int, default=90)
    p_world.add_argument("--limit", type=int, default=400)
    p_world.add_argument("--name", help="show: entity to inspect")

    p_eval = sub.add_parser("eval", help="run the answer-quality eval (docs/eval/questions.yaml)")
    p_eval.add_argument("--only", help="run a single question id")

    p_import = sub.add_parser("import", help="import a chat export")
    p_import.add_argument("source", choices=["imessage", "whatsapp", "chatdb", "gmail-sample", "calendar-sample"])
    p_import.add_argument("path")
    p_import.add_argument("--contact", default=None, help="display name for a WhatsApp export")
    p_import.add_argument("--group", action="store_true")
    p_import.add_argument("--since", default=None, help="ISO date (e.g. 2026-05-01); import only messages on/after it. chatdb only.")

    p_why = sub.add_parser("why", help="explain an item's rank")
    p_why.add_argument("item_id")

    p_done = sub.add_parser("done", help="mark an item complete")
    p_done.add_argument("item_id")

    p_confirm = sub.add_parser("confirm", help="accept a fuzzy completion match")
    p_confirm.add_argument("signal_id")

    p_reject = sub.add_parser("reject", help="reject a fuzzy completion match")
    p_reject.add_argument("signal_id")

    p_doctor = sub.add_parser(
        "doctor",
        help="check every integration by actually using it",
    )
    p_doctor.add_argument("--json", action="store_true",
                          help="machine-readable, for the Mac app's setup flow")

    p_serve = sub.add_parser("serve", help="run the API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    db.get_connection()

    if args.command == "demo":
        print("ingested:", load_sample_corpus())
        print("extracted:", len(pipeline.run()))
        print("completion:", engine.scan().summary())
        print("learning:", learning.run())
        print("notifications:", scheduler.run())
        print()
        _print_today()

    elif args.command == "today":
        _print_today()

    elif args.command == "poll":
        print(json.dumps(poller.cycle(), indent=2, default=str))

    elif args.command == "extract":
        created = pipeline.run()
        print(f"extracted {len(created)} items")

    elif args.command == "scan":
        print(json.dumps(engine.scan().summary(), indent=2))

    elif args.command == "learn":
        print(json.dumps(learning.run(), indent=2, default=str))

    elif args.command == "notify":
        print(json.dumps(scheduler.run(), indent=2, default=str))

    elif args.command == "model":
        print(json.dumps(learning.snapshot(), indent=2))

    elif args.command == "auth-url":
        try:
            print(google_auth.authorization_url())
        except google_auth.GoogleAuthError as exc:
            print(exc, file=sys.stderr)
            return 1

    elif args.command == "pair":
        from .api import auth as api_auth
        minted = api_auth.start_pairing()
        print(f"pairing code: {minted['code']}")
        print(f"valid for {minted['expires_in_minutes']} minutes, single use.")
        print("In the app: Pair this device -> type the code.")

    elif args.command == "purge":
        db.purge_all()
        print("purged")

    elif args.command == "threads":
        _print_threads()

    elif args.command == "learning-repair":
        print(json.dumps(learning.repair(), indent=2))
        if args.prune:
            print(f"pruned {learning.prune_duplicate_verdicts()} duplicate verdict rows")
        else:
            noise = db.get_connection().execute(
                "SELECT COUNT(*) FROM behavior_events WHERE kind LIKE 'pattern_%'"
            ).fetchone()[0]
            print(f"{noise} machine-generated verdict rows remain; --prune removes the duplicates")

    elif args.command == "bootstrap-threads":
        if args.compare:
            result = bootstrap.compare()
            items = bootstrap.open_items_to_thread()
            print(f"{result['items']} open items\n")
            print(f"── deterministic → {len(result['deterministic'])} threads ──")
            print(bootstrap.render(result["deterministic"], items))
            if result["llm_available"]:
                print(f"\n── llm → {len(result['llm'])} threads ──")
                print(bootstrap.render(result["llm"], items))
            else:
                print("\n── llm ── unavailable (no provider configured, or the pass was rejected)")
        else:
            print(json.dumps(
                bootstrap.run(use_llm=not args.no_llm, dry_run=args.dry_run), indent=2
            ))

    elif args.command == "attachments":
        if args.action == "scan":
            print(json.dumps(gmail.scan_attachments(days=args.days), indent=2))
        elif args.action == "backfill":
            from .ingestion import attachments as attachments_mod
            print(json.dumps(attachments_mod.backfill(days=args.days, limit=args.limit), indent=2))
        elif args.action == "ics":
            from .ingestion import attachments as attachments_mod
            print(json.dumps({"events": attachments_mod.import_stored_ics()}, indent=2))

    elif args.command == "world":
        from . import world
        from .extraction import pipeline as pipeline_mod
        if args.action == "backfill":
            print(json.dumps(pipeline_mod.world_backfill(days=args.days, limit=args.limit), indent=2))
        elif args.action == "show":
            entity = world.resolve(args.name or "")
            if not entity:
                print(f"nothing answers to {args.name!r}")
            else:
                print(f"{entity.name} ({entity.kind}, {entity.id})")
                for f in world.facts_for(entity.id):
                    print(f"  {f.predicate} = {f.value}   [conf {f.confidence}, msg {f.message_id}]")

    elif args.command == "eval":
        from .evals import run_eval
        report = run_eval(only=args.only)
        for row in report["results"]:
            mark = "PASS" if row["passed"] else "FAIL"
            print(f"  {mark}  {row['id']:<16} {row['answer'][:90]}")
        print(f"\n{report['passed']}/{report['total']} passed")

    elif args.command == "import":
        path = Path(args.path).expanduser()
        resolver = IdentityResolver()
        if args.source == "imessage":
            n = imessage.import_export(path, resolver)
        elif args.source == "chatdb":
            n, _ = imessage.import_chat_db(path, resolver, since=args.since)
        elif args.source == "whatsapp":
            n = whatsapp.import_export(path, args.contact, args.group, resolver)
        elif args.source == "gmail-sample":
            n = gmail.import_sample(path)
        else:
            n = gcal.import_sample(path)
        print(f"imported {n} records")
        print(f"extracted {len(pipeline.run())} items")

    elif args.command == "why":
        return _print_why(args.item_id)

    elif args.command == "done":
        item = engine.manual_close(args.item_id)
        print("closed" if item else "not found")

    elif args.command == "confirm":
        item = engine.confirm(args.signal_id)
        print(f"closed {item.suggested_action}" if item else "not found")

    elif args.command == "reject":
        item = engine.reject(args.signal_id)
        print("kept open" if item else "not found")

    elif args.command == "doctor":
        from . import doctor as doctor_mod

        report = doctor_mod.run()
        if args.json:
            print(json.dumps(report.as_dict(), indent=2))
        else:
            print(doctor_mod.render(report))
        # Non-zero on failure so a launchd job or CI can act on it.
        return 1 if report.failed else 0

    elif args.command == "serve":
        import uvicorn

        cfg = get_config()
        print(f"db: {cfg.db_path}")
        uvicorn.run("lifeline.api.app:app", host=args.host, port=args.port, log_level="info")

    return 0


if __name__ == "__main__":
    sys.exit(main())
