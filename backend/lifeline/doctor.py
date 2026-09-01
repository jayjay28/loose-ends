"""Does the work, rather than checking that the work is possible.

Every integration here is wrapped in defensive error handling so one broken
source cannot take down a poll cycle::

    except (OSError, sqlite3.Error):
        return 0                                    # imessage.poll
    except Exception as exc:
        log.warning("mail poll failed: %s", exc)    # poller.poll_sources

That is correct, and it is exactly what makes this system blind. It runs in the
background, nobody watches it work, and its only visible output is threads on a
phone — where *nothing new* is a legitimate answer. So a dead integration and a
genuinely quiet week look identical.

Three real failures found by accident rather than by alarm:

  * `record_finding` raised `NameError` on every pass for days. The fallback
    wrote "Checked, nothing new", so a dozen crashes a day read on screen as a
    dozen quiet threads.
  * Gmail stopped ingesting on 8 August and nobody knew until the 21st.
    `/health` reported ``google_connected: true`` the entire time, because it
    asks whether a token *row exists*, not whether it works.
  * iMessage returns 0 without Full Disk Access, which is indistinguishable
    from a week in which nobody texted.

Hence the rule this module exists to follow: **a check must attempt the thing.**
Refresh the token for real. Open `chat.db` and read a row. Make a real (free)
API call. Configuration presence is what `/health` already reports, and
configuration presence is precisely what was true throughout all three.

Written as a library first and a command second, because the same questions are
what onboarding has to ask — "can I read chat.db?" is both a diagnostic and a
setup gate.
"""
from __future__ import annotations

import json
import os
import logging
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import db
from .config import get_config

log = logging.getLogger(__name__)

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


@dataclass
class Check:
    """One question, asked by doing rather than by looking."""

    name: str
    status: str
    detail: str
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (OK, SKIP)


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    @property
    def failed(self) -> List[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> List[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def healthy(self) -> bool:
        return not self.failed

    def as_dict(self) -> Dict[str, Any]:
        return {
            "healthy": self.healthy,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
                for c in self.checks
            ],
        }


def _age(iso: Optional[str]) -> Optional[timedelta]:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when


def _said(delta: Optional[timedelta]) -> str:
    if delta is None:
        return "never"
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)}m ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


# --------------------------------------------------------------- the checks

def check_database() -> Check:
    """The one thing everything else needs."""
    try:
        conn = db.get_connection()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    except sqlite3.Error as exc:
        return Check("database", FAIL, f"cannot open the database: {exc}",
                     "check LIFELINE_DB and that the file is writable")
    return Check("database", OK,
                 f"{threads} threads, {messages} messages, journal={mode}")


def check_anthropic() -> Check:
    """Generate one token, because that is the thing that has to work.

    This used to call `messages.count_tokens`, which is free and unmetered —
    so it answered 200 on a key whose account had no credit, and reported a
    dead engine as healthy. `check_gemini` was rewritten for exactly this
    reason; this one kept the blind spot until the onboarding audit found it.
    """
    cfg = get_config()
    if not cfg.has_claude:
        return Check("anthropic", SKIP, "no ANTHROPIC_API_KEY set",
                     "paste the key into the setup wizard at localhost:8000/setup")
    try:
        import httpx

        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": cfg.anthropic_api_key,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5", "max_tokens": 1,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=20)
    except Exception as exc:                      # noqa: BLE001
        return Check("anthropic", FAIL, f"unreachable: {str(exc)[:120]}")
    if response.status_code in (200, 201):
        return Check("anthropic", OK, "key generates")
    body = (response.text or "").lower()
    if "credit balance" in body or "billing" in body:
        return Check("anthropic", FAIL, "the key works but the account has no credit",
                     "add a payment method at console.anthropic.com/settings/billing")
    if response.status_code == 401:
        return Check("anthropic", FAIL, "the key was rejected",
                     "paste a fresh key into localhost:8000/setup")
    if response.status_code in (429, 529):
        return Check("anthropic", WARN, "rate-limited right now, but the key authenticates")
    return Check("anthropic", FAIL,
                 f"{response.status_code}: {(response.text or '')[:100]}")


def check_gemini() -> Check:
    """The fallback, exercised — because a fallback nobody checks is a guess.

    This one was missing for the same reason it mattered least: nothing ran on
    Gemini while Claude worked. Then Claude's balance emptied, and the answer
    to "is there a second provider?" turned out to be no on two counts at once
    — a `KeyError` in the tool translation, and, underneath it, a depleted key
    of its own. Neither was visible anywhere, because the only thing that ever
    reported on Gemini was `gemini_configured: true`.

    It generates, rather than counting tokens, and that costs a fraction of a
    cent on purpose. `countTokens` is the free call and it is *unmetered*: it
    answered `OK — 2 tokens` on this machine's key at the same moment
    `generateContent` was returning 429 RESOURCE_EXHAUSTED, because the
    credits were gone. A check that passes on a key that cannot generate is
    the module's own failure mode, so this one asks the metered question and
    caps the answer at a token.
    """
    cfg = get_config()
    if not cfg.gemini_api_key:
        return Check("gemini", SKIP, "no GEMINI_API_KEY set",
                     "optional — it is the fallback for when the Claude key fails")
    try:
        import httpx
        from .extraction.gemini import _BASE
        resp = httpx.post(
            f"{_BASE}/{cfg.gemini_model}:generateContent",
            headers={"x-goog-api-key": cfg.gemini_api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generationConfig": {"maxOutputTokens": 1, "temperature": 0},
            },
            timeout=30.0,
        )
        if resp.status_code >= 400:
            # Google's error body is pretty-printed JSON, and a raw slice of it
            # puts newlines through the middle of a one-line report.
            why = " ".join(resp.text.split())[:110]
            return Check("gemini", FAIL, f"key rejected: {resp.status_code} {why}",
                         "check the key and its billing at ai.studio/projects")
        return Check("gemini", OK, f"key generates — {cfg.gemini_model} reachable")
    except Exception as exc:                      # noqa: BLE001 — any failure is the answer
        return Check("gemini", FAIL, f"unreachable: {str(exc)[:120]}",
                     "check the key and its billing at ai.studio/projects")


def check_thinking() -> Check:
    """Whether the engine is still reasoning, or has quietly fallen back.

    `check_anthropic` asks whether the key *can* work. This asks what actually
    happened on the last real call — the two differ exactly when a key dies
    mid-run, which is how an account's credit ran out one afternoon while
    every check reported healthy.
    """
    from .extraction import providers

    since = providers.degraded_since()
    if not since:
        return Check("thinking", OK, "extraction is using a model")
    detail = db.get_sync_state(providers.LAST_ERROR_KEY) or "no detail recorded"
    return Check("thinking", FAIL,
                 f"falling back to rules since {since[:16]} — {detail[:120]}",
                 "items are still being made, at much lower quality; "
                 "`lifeline doctor` names the provider that failed")


def check_mail() -> Check:
    """Open the local Mail store and read one message.

    Same three outcomes `check_imessage` distinguishes, for the same reason:
    `applemail.poll()` returns 0 both when Mail isn't set up and when Full
    Disk Access is missing, and only trying tells them apart.
    """
    from .ingestion import applemail

    if applemail.permission_denied():
        return Check("mail", FAIL, "Mail's store exists but this process can't read it",
                     "grant Full Disk Access to /bin/zsh — the same grant Messages needs")
    root = applemail.store_root()
    if root is None:
        return Check("mail", SKIP, "no mail store on this machine",
                     "add your account in Mail — the engine reads it from disk, "
                     "with no cloud project and no consent screen")
    if not applemail.available():
        return Check("mail", SKIP, f"{root.name} has no messages yet",
                     "let Mail finish syncing, then poll again")
    cursor = db.get_sync_state(applemail.CURSOR_KEY)
    return Check("mail", OK,
                 f"readable — {root.name}" + (" (incremental)" if cursor else " (first run pending)"))


def check_whatsapp() -> Check:
    """Open WhatsApp's own store and count what's in it.

    Unlike Messages and Mail this one needs no Full Disk Access — it lives in
    a group container — so the only two outcomes are "the desktop app is
    installed" and "it isn't".
    """
    from .ingestion import whatsapp

    if not whatsapp.LIVE_STORE.exists():
        return Check("whatsapp", SKIP, "WhatsApp Desktop isn't installed on this Mac",
                     "install it and sign in if WhatsApp is where your conversations are")
    rows = whatsapp.read_store(whatsapp.LIVE_STORE)
    if not rows:
        return Check("whatsapp", WARN, "the store is there but no messages came out",
                     "WhatsApp may have changed its schema — check the log for what was found")
    cursor = db.get_sync_state(whatsapp.CURSOR_KEY)
    return Check("whatsapp", OK,
                 f"{len(rows)} messages readable"
                 + (f", ingested through #{cursor}" if cursor else " (first run pending)"))


def check_notifications() -> Check:
    """Can we sample the notification store, and is anything in it?

    An empty window is a normal result, not a fault: the store is what
    Notification Center currently holds, and a user who clears their
    notifications leaves nothing behind to read.
    """
    from .ingestion import notifications as notif

    if os.environ.get("LIFELINE_NO_NOTIFICATIONS"):
        return Check("notifications", SKIP, "switched off (LIFELINE_NO_NOTIFICATIONS)")
    if not notif.STORE.exists():
        return Check("notifications", SKIP, "no notification store on this machine")
    if not notif.readable():
        return Check("notifications", FAIL, "the store exists but can't be read",
                     "grant Full Disk Access to /bin/zsh — the same grant Messages needs")
    window = notif.read_store(notif.STORE)
    apps = notif.seen_apps()
    if not window and not apps:
        return Check("notifications", SKIP, "nothing in the window yet",
                     "normal if notifications were just cleared — the next poll re-samples")
    return Check("notifications", OK,
                 f"{len(window)} in the window, {len(apps)} apps seen so far")


def check_imessage() -> Check:
    """Open the database and read a row.

    Without Full Disk Access `imessage.poll()` catches the error and returns 0,
    which looks exactly like a week in which nobody texted. The three outcomes
    — no file, no permission, works — are only distinguishable by trying.
    """
    from .ingestion import imessage

    src = imessage.LIVE_CHAT_DB
    if not src.exists():
        return Check("imessage", SKIP, "no ~/Library/Messages/chat.db on this machine",
                     "sign in to Messages, or run the backend on the Mac that has it")
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            total = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        import os as _os
        return Check("imessage", FAIL,
                     f"cannot read chat.db ({type(exc).__name__}) — this process "
                     f"(pid {_os.getpid()}) has no Full Disk Access",
                     "System Settings → Privacy & Security → Full Disk Access. "
                     "The grant is per-process, so granting Terminal does not grant "
                     "launchd — add whatever actually runs the poller, then restart it")
    size = src.stat().st_size / 1_000_000
    return Check("imessage", OK, f"readable — {total} messages, {size:.0f} MB")


def check_freshness() -> List[Check]:
    """Has each source produced anything lately?

    The read succeeding is not the same as the pipeline working. Gmail's token
    was dead for thirteen days while every other Google check would have passed.
    """
    out: List[Check] = []
    limits = {"imessage": timedelta(days=2), "mail": timedelta(days=3)}
    for source, limit in limits.items():
        row = db.get_connection().execute(
            "SELECT MAX(timestamp) FROM messages WHERE source = ?", (source,)
        ).fetchone()
        newest = row[0] if row else None
        age = _age(newest)
        if age is None:
            out.append(Check(f"{source} freshness", SKIP, "nothing ingested yet"))
        elif age > limit:
            out.append(Check(
                f"{source} freshness", WARN,
                f"newest message is {_said(age)} — ingestion may have stalled",
                f"run `lifeline doctor` after a poll; check the {source} check above",
            ))
        else:
            out.append(Check(f"{source} freshness", OK, f"newest message {_said(age)}"))
    return out


def check_attachments() -> Check:
    """Is the store holding documents it has never read?

    125 messages in a 90-day window carried an attachment and none was ever
    ingested — school forms, bills, the asthma action plan — while every check
    here said the pipeline was healthy, because reading a message's body counts
    as reading the message. This counts the files themselves.

    Parsed rows live in `attachments` (phase 0.2); until that table exists,
    everything discovered is unread by definition, which is the honest state.
    """
    conn = db.get_connection()
    row = conn.execute(
        """SELECT COUNT(*) FROM messages WHERE metadata LIKE '%"attachments"%'"""
    ).fetchone()
    carriers = row[0] if row else 0
    if carriers == 0:
        return Check(
            "attachments", SKIP,
            "no attachment metadata in the store",
            "run `lifeline attachments scan` to discover what mail carries",
        )
    try:
        parsed = conn.execute(
            "SELECT COUNT(DISTINCT message_id) FROM attachments WHERE parsed_at IS NOT NULL"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        parsed = 0      # the table arrives in phase 0.2
    unread = carriers - parsed
    if unread > 0:
        return Check(
            "attachments", WARN,
            f"{unread} of {carriers} attachment-bearing messages have never been read",
            "phase 0.2 fetches and parses them; until then this backlog is invisible everywhere else",
        )
    return Check("attachments", OK, f"all {carriers} attachment-bearing messages read")


def check_worker_recording(days: int = 7) -> Check:
    """Is the worker able to write down what it found?

    The signature of the dead `record_finding` is a pass that *tried* to record
    and was refused every time it tried. Some refusals are healthy — the guards
    exist to reject unlinked prices and menus-in-place-of-decisions — so a
    single failure is noise. A pass where *every* attempt failed is a pass whose
    work was thrown away, which is what happened here for days.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = db.get_connection().execute(
        "SELECT tool_calls FROM loop_runs WHERE trigger = 'worker' AND created_at >= ?",
        (since,),
    ).fetchall()
    if not rows:
        return Check("worker recording", SKIP, f"no worker passes in {days} days")

    lost = 0
    tried = 0
    for (raw,) in rows:
        calls = [c for c in json.loads(raw or "[]") if c.get("name") == "record_finding"]
        if not calls:
            continue
        tried += 1
        if all("error" in str(c.get("result", "")).lower()[:300] for c in calls):
            lost += 1

    if not tried:
        return Check("worker recording", WARN,
                     f"{len(rows)} passes in {days}d and none tried to record anything",
                     "the worker is running but never reaching record_finding")
    if lost:
        share = lost / tried
        status = FAIL if share > 0.3 else WARN
        return Check("worker recording", status,
                     f"{lost} of {tried} passes recorded nothing — every attempt was refused",
                     "read the refusal text in loop_runs.tool_calls; a guard may be "
                     "rejecting valid findings, or raising")
    return Check("worker recording", OK, f"{tried} of {len(rows)} passes recorded findings")


def check_caching() -> Check:
    """Cached reads bill at about a tenth of input. Zero of them is a bug.

    Prompt caching was counted from the day the counters were written and never
    actually enabled, and nothing reported the discrepancy — one live pass paid
    full price for 369,000 input tokens against a 5,000-token prompt.
    """
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fresh = int(db.get_sync_state(f"llm_tokens_in:{day}") or 0)
    cached = int(db.get_sync_state(f"llm_tokens_cached:{day}") or 0)
    if fresh + cached == 0:
        return Check("prompt caching", SKIP, "no model calls yet today")
    if cached == 0 and fresh > 50_000:
        return Check("prompt caching", WARN,
                     f"{fresh:,} input tokens today and none served from cache",
                     "something in the prompt prefix is changing between calls — "
                     "a timestamp above the cache breakpoint is the usual cause")
    hit = cached / max(fresh + cached, 1)
    return Check("prompt caching", OK,
                 f"{hit:.0%} of today's input served from cache ({cached:,} of {fresh + cached:,})")


def check_pollers() -> Check:
    """More than one poller is more than one SQLite writer.

    `_cycle_lock` is a `threading.Lock`, so it coordinates threads inside one
    process and nothing at all between processes. Two API servers and a launchd
    job were once running together, each on its own interval.
    """
    try:
        out = subprocess.run(["ps", "-eo", "command"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return Check("pollers", SKIP, "could not list processes on this platform")

    running = [
        line for line in out.splitlines()
        if ("lifeline.api.app" in line or "lifeline.cli poll" in line)
        and "grep" not in line
    ]
    # `--reload` runs a supervisor plus a worker; both match, one polls.
    servers = {line for line in running if "lifeline.api.app" in line}
    if len(servers) > 1:
        return Check("pollers", WARN,
                     f"{len(servers)} API processes are running, each polling on its own timer",
                     "keep one — concurrent writers are not coordinated by the cycle lock")
    return Check("pollers", OK, f"{len(running) or 'no'} poller process(es)")


# ------------------------------------------------------------------- runner

CHECKS: List[Callable[[], Any]] = [
    check_database,
    check_anthropic,
    check_gemini,
    check_thinking,
    check_mail,
    check_notifications,
    check_whatsapp,
    check_imessage,
    check_freshness,
    check_attachments,
    check_worker_recording,
    check_caching,
    check_pollers,
]


def run() -> Report:
    """Every check. One raising must never stop the rest — a doctor that dies
    on the first problem reports less than no doctor at all."""
    report = Report()
    for check in CHECKS:
        try:
            result = check()
        except Exception as exc:                  # noqa: BLE001
            log.exception("doctor check %s raised", getattr(check, "__name__", check))
            report.checks.append(Check(
                getattr(check, "__name__", "check").replace("check_", ""),
                FAIL, f"the check itself failed: {str(exc)[:120]}",
            ))
            continue
        report.checks.extend(result if isinstance(result, list) else [result])
    return report


_MARK = {OK: "ok  ", WARN: "warn", FAIL: "FAIL", SKIP: "--  "}


def render(report: Report) -> str:
    """For a terminal. The fix line only appears when there is something to fix."""
    width = max((len(c.name) for c in report.checks), default= 0)
    lines = []
    for c in report.checks:
        lines.append(f"  [{_MARK[c.status]}]  {c.name.ljust(width)}   {c.detail}")
        if c.fix and c.status in (WARN, FAIL):
            lines.append(f"           {' ' * width}   → {c.fix}")
    if report.failed:
        lines.append(f"\n{len(report.failed)} failing, {len(report.warned)} warning.")
    elif report.warned:
        lines.append(f"\nNothing broken. {len(report.warned)} worth a look.")
    else:
        lines.append("\nEverything works.")
    return "\n".join(lines)
