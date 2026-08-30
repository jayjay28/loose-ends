"""Ingestion adapters — one per source in the Section 3 table."""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from ..config import REPO_ROOT
from . import gcal, gmail, google_auth, imessage, whatsapp
from .base import IdentityResolver, load_people

SAMPLE_DIR = REPO_ROOT / "sample_data"


def load_sample_corpus(sample_dir: Path = SAMPLE_DIR) -> Dict[str, int]:
    """Seed everything from the bundled realistic corpus (§10)."""
    sample_dir = Path(sample_dir)
    load_people(sample_dir / "people.json")
    resolver = IdentityResolver()
    counts = {
        "imessage": imessage.import_export(sample_dir / "imessage_export.json", resolver),
        "whatsapp": (
            whatsapp.import_export(sample_dir / "whatsapp_dev_shah.txt", "Dev Shah", resolver=resolver)
            + whatsapp.import_export(sample_dir / "whatsapp_priya.txt", "Priya Raman", resolver=resolver)
        ),
        "gmail": gmail.import_sample(sample_dir / "gmail_sample.json"),
        "calendar": gcal.import_sample(sample_dir / "calendar_sample.json"),
    }
    return counts


__all__ = [
    "IdentityResolver",
    "gcal",
    "gmail",
    "google_auth",
    "imessage",
    "load_people",
    "load_sample_corpus",
    "whatsapp",
]
