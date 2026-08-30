"""Runtime configuration.

Everything that needs a credential degrades to an offline mode so each
milestone stays independently testable against sample data (§10).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "lifeline.db"

# §v3 workstream 2 — credentials get a home that isn't somebody's .zshrc.
# The setup wizard writes KEY=value lines here; anything already in the
# process environment wins, so a developer's shell exports keep working and
# the file is only ever a floor. Loaded once, at import, before any Config
# is constructed.
ENV_FILE = Path(os.environ.get("LIFELINE_ENV_FILE", str(Path.home() / ".lifeline" / "env")))


def load_env_file(path: Path = ENV_FILE) -> int:
    """Read KEY=value lines into os.environ (existing keys win). Returns how
    many keys were adopted, because the wizard reports what it did."""
    if not path.exists():
        return 0
    adopted = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"')
        if key and key not in os.environ:
            os.environ[key] = value
            adopted += 1
    return adopted


load_env_file()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("LIFELINE_DB", str(DEFAULT_DB))))

    # --- Claude, for extraction (§5) --------------------------------------
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    # Extraction is classification: given a message, name the commitment in it.
    # It ran on Opus for months on the reasoning that the *quality* of what
    # gets extracted sets the ceiling for everything downstream — which is true,
    # and was still the wrong trade at 5x the price. On 22 Aug it billed $10.23
    # against the entire agentic loop's $1.35, and no counter in this codebase
    # was watching it (see `claude._record_usage`). Haiku is the same tier the
    # loop already runs on, and the loop does strictly harder work: multi-step
    # tool routing over the user's whole world, not one labelled paragraph.
    #
    # Set LIFELINE_MODEL=claude-opus-5 to put it back if extraction quality
    # visibly drops — `llm_tokens_*:opus` vs `:haiku` now says what that costs.
    extraction_model: str = field(default_factory=lambda: os.environ.get("LIFELINE_MODEL", "claude-haiku-4-5"))
    # The agentic loop makes many small tool-routing calls — an order of
    # magnitude more volume than extraction, and none of it needs the big
    # model. Haiku keeps a maxed-out day in single-digit dollars, not $150+.
    loop_model: str = field(default_factory=lambda: os.environ.get("LIFELINE_LOOP_MODEL", "claude-haiku-4-5-20251001"))
    # When no key is present the pipeline falls back to a deterministic
    # heuristic classifier so the rest of the system still runs end to end.
    offline_extraction: bool = field(default_factory=lambda: _env_bool("LIFELINE_OFFLINE"))

    # Check that a link the worker staged actually resolves before it is stored.
    #
    # The EMES thread recorded five product urls and every one was a 404. The
    # search had genuinely run — all five products were real and two slugs were
    # exactly right — but Gemini hands back grounding *redirects* rather than
    # destinations, so the model had no citable address and rebuilt one from the
    # product name, guessing the wrong url shape. `_is_search_url` checks a
    # url's shape and a well-formed invention passes it, so the only thing that
    # catches this is asking the web whether the page is there.
    verify_urls: bool = field(default_factory=lambda: not _env_bool("LIFELINE_NO_VERIFY_URLS"))

    # --- Gemini, an alternative extraction provider (§5) -------------------
    # Google AI Studio keys have a free tier. Used only when no Claude key is
    # set; falls back to the heuristic if neither is configured.
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.environ.get("LIFELINE_GEMINI_MODEL", "gemini-flash-latest"))

    # --- Google OAuth (§3, §9) --------------------------------------------
    google_client_id: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLIENT_SECRET", ""))
    google_redirect_uri: str = field(
        default_factory=lambda: os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback")
    )

    # --- APNs (§8.4) -------------------------------------------------------
    # §v3 — engines without their own APNs key knock through the relay.
    # Empty disables; the developer's machine has the key and never knocks.
    push_relay_url: str = field(default_factory=lambda: os.environ.get("LIFELINE_PUSH_RELAY_URL", ""))
    apns_key_path: str = field(default_factory=lambda: os.environ.get("APNS_KEY_PATH", ""))
    apns_key_id: str = field(default_factory=lambda: os.environ.get("APNS_KEY_ID", ""))
    apns_team_id: str = field(default_factory=lambda: os.environ.get("APNS_TEAM_ID", ""))
    # The topic is the *bundle id*, and it has to match exactly — Apple rejects
    # a mismatch with a 400 that says nothing about which field was wrong. The
    # old default said com.lifeline.app; the app ships as com.lifelinecly.app.
    apns_topic: str = field(default_factory=lambda: os.environ.get("APNS_TOPIC", "com.lifelinecly.app"))
    apns_sandbox: bool = field(default_factory=lambda: _env_bool("APNS_SANDBOX", True))

    # --- Behaviour knobs ---------------------------------------------------
    poll_interval_seconds: int = field(default_factory=lambda: int(os.environ.get("LIFELINE_POLL_SECONDS", "300")))
    # §v2 step 3: drafts moved to write-time, so extraction stops writing them.
    # A reply composed during extraction sees one message and knows nothing
    # about the loop it belongs to; in the live database that produced 33 items
    # drafted "yep, I'll take care of it", four of them addressed to billing
    # robots at American Water, Capital One, Fidelity and a Marriott
    # front desk. `POST /threads/{id}/draft` writes with the thread in hand and
    # is allowed to say a message isn't the work at all. Set
    # LIFELINE_DRAFT_REPLIES=1 to restore the old behaviour.
    draft_replies: bool = field(default_factory=lambda: _env_bool("LIFELINE_DRAFT_REPLIES", False))
    morning_window: tuple = (6, 11)   # §6.4 mid-morning analytical peak

    @property
    def has_claude(self) -> bool:
        return bool(self.anthropic_api_key) and not self.offline_extraction

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key) and not self.offline_extraction

    @property
    def has_google(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def has_apns(self) -> bool:
        return bool(self.apns_key_path and self.apns_key_id and self.apns_team_id)


GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def set_config(cfg: Config) -> None:
    """Test hook."""
    global _config
    _config = cfg
