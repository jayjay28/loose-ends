"""Read the macOS Contacts (AddressBook) store to map phone/email handles to
real names, so an iMessage from "+15551234567" shows as "Dana Whitlock".

Read-only, best-effort: needs the backend process to have Full Disk Access
(or Contacts access). Returns an empty map when it can't read, and the caller
falls back to the raw handle.
"""
from __future__ import annotations

import glob
import logging
import sqlite3
from pathlib import Path
from typing import Dict

from .base import normalise_handle

log = logging.getLogger(__name__)

_ROOT = Path.home() / "Library" / "Application Support" / "AddressBook"


def _db_paths() -> list[str]:
    return glob.glob(str(_ROOT / "AddressBook-v22.abcddb")) + glob.glob(
        str(_ROOT / "Sources" / "*" / "AddressBook-v22.abcddb")
    )


def _full_name(first, last, org) -> str:
    name = " ".join(part for part in (first, last) if part).strip()
    return name or (org or "").strip()


def load_handle_names() -> Dict[str, str]:
    """{normalised handle -> display name} across every local Contacts source."""
    names: Dict[str, str] = {}
    for path in _db_paths():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            phones = conn.execute(
                """SELECT p.ZFULLNUMBER, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION
                   FROM ZABCDPHONENUMBER p JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
                   WHERE p.ZFULLNUMBER IS NOT NULL"""
            ).fetchall()
            emails = conn.execute(
                """SELECT e.ZADDRESS, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION
                   FROM ZABCDEMAILADDRESS e JOIN ZABCDRECORD r ON e.ZOWNER = r.Z_PK
                   WHERE e.ZADDRESS IS NOT NULL"""
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("Contacts read failed for %s: %s", path, exc)
            conn.close()
            continue
        conn.close()

        for value, first, last, org in phones + emails:
            name = _full_name(first, last, org)
            if name and value:
                names.setdefault(normalise_handle(value), name)

    if names:
        log.info("Contacts: resolved %d handles to names", len(names))
    return names


def photo_for(handles: list) -> "bytes | None":
    """Best-effort contact photo (JPEG/PNG bytes) for any of these handles.
    Schema-tolerant: finds an image column on ZABCDRECORD if one exists.
    Returns None on anything unexpected — the app then shows a monogram."""
    norm = {normalise_handle(h) for h in handles if h}
    if not norm:
        return None
    for path in _db_paths():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(ZABCDRECORD)")]
            img_col = next((c for c in cols if "IMAGE" in c.upper() and "DATA" in c.upper()), None)
            if not img_col:
                continue
            owners: set = set()
            for num, owner in conn.execute(
                "SELECT ZFULLNUMBER, ZOWNER FROM ZABCDPHONENUMBER WHERE ZFULLNUMBER IS NOT NULL"
            ):
                if normalise_handle(num) in norm:
                    owners.add(owner)
            for addr, owner in conn.execute(
                "SELECT ZADDRESS, ZOWNER FROM ZABCDEMAILADDRESS WHERE ZADDRESS IS NOT NULL"
            ):
                if normalise_handle(addr) in norm:
                    owners.add(owner)
            for owner in owners:
                row = conn.execute(
                    f"SELECT {img_col} FROM ZABCDRECORD WHERE Z_PK = ?", (owner,)
                ).fetchone()
                if row and row[0]:
                    return bytes(row[0])
        except sqlite3.Error as exc:
            log.warning("Contacts photo read failed for %s: %s", path, exc)
        finally:
            conn.close()
    return None
