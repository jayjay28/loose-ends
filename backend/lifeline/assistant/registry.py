"""The loop's tool registry (§v1.4).

Each tool pairs a Claude-shaped schema ({name, description, input_schema} —
the loop's canonical format; the Gemini provider converts) with the Python
function that executes it. The loop dispatches a model's tool call by name and
feeds the JSON-serialised result back.

Read tools wrap `assistant.tools`; write tools (record_fact, ...) join the
registry as v1.4's pillars land.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .. import db
from . import tools

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    fn: Callable[..., Any]

    @property
    def schema(self) -> Dict[str, Any]:
        """The provider-facing declaration (no fn)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class ServerTool:
    """A tool the provider runs on its own infrastructure (§v2.3).

    Every other tool in this file reads or writes the user's own data, which is
    why the worker could only ever report that nothing had changed: a thread
    about finding pyjamas under $150 had no way to look at a shop. These are
    the first tools that reach the world.

    There is no `fn`. The provider executes the tool inside the same request
    and hands the model the result before it replies, so the loop never
    dispatches one — which is why `by_name` skips them. All this object
    carries is the declaration.
    """

    name: str
    type: str
    limits: Optional[Dict[str, Any]] = None

    @property
    def schema(self) -> Dict[str, Any]:
        return {"type": self.type, "name": self.name, **(self.limits or {})}


# The tool type depends on the model. Dynamic filtering (`_20260209`) runs the
# results through code execution before they reach the context window, and is
# Opus/Sonnet-tier only; the loop runs on Haiku by design (see
# `config.loop_model`), which takes the basic variants.
#
# The limits are the cost control, and they matter more than the call caps in
# `extraction.budget`. Those bound how many calls a day the worker makes; these
# bound how large each one gets. A fetched shop page is tens of thousands of
# tokens, and a loop resends its whole conversation every turn — so one
# unbounded fetch is paid for again on every turn that follows it. Capping the
# page is what keeps a thread pass in cents.
_SEARCH_LIMITS = {"max_uses": 4}
_FETCH_LIMITS = {"max_uses": 3, "max_content_tokens": 6000}

_WEB_TOOLS_FILTERED = (
    ServerTool(name="web_search", type="web_search_20260209", limits=_SEARCH_LIMITS),
    ServerTool(name="web_fetch", type="web_fetch_20260209", limits=_FETCH_LIMITS),
)
# Search only, and that is the cost decision, not an oversight.
#
# The basic fetch takes `max_uses` but not `max_content_tokens`, so a fetched
# page arrives whole — and the loop resends its entire conversation every turn,
# so one 80k-token shop page is paid for again on every turn that follows it.
# Measured on real threads that was the single largest line in a day's spend,
# an order of magnitude above the calls themselves. Search results carry
# titles, snippets and URLs, which is what naming three options with prices
# actually needs; whole pages were buying very little for what they cost.
_WEB_TOOLS_BASIC = (
    ServerTool(name="web_search", type="web_search_20250305", limits=_SEARCH_LIMITS),
)
_FILTERED_PREFIXES = ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
                      "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6",
                      "claude-fable-5", "claude-mythos-5")


def web_tools(model: Optional[str] = None) -> List[ServerTool]:
    """Search and fetch, picked to match the model actually driving the loop.

    Declaring a tool type the model doesn't support is a 400 on the whole
    request, so this is chosen rather than fixed — and it upgrades itself the
    day `LIFELINE_LOOP_MODEL` moves off Haiku.
    """
    from ..config import get_config

    name = model or get_config().loop_model
    filtered = any(name.startswith(p) for p in _FILTERED_PREFIXES)
    return list(_WEB_TOOLS_FILTERED if filtered else _WEB_TOOLS_BASIC)


_CITE = re.compile(r"</?cite[^>]*>", re.I)


def _plain(text: Optional[str]) -> str:
    """Text as a person should read it.

    Web search results carry citation markup, and the model quotes it back
    inside its prose: a real finding reached the phone reading `<cite
    index="41-1">Lifetime 8'-10' portable at $299.99</cite>`. The figures were
    right and the sentence was unreadable. Stripped here, at the point of
    record, so nothing downstream has to know the markup ever existed.
    """
    return _CALL_MARKUP.split(_CITE.sub("", text or ""))[0].strip()


# The model's own tool-call syntax, leaked INTO a string argument. Seen live:
# a record_finding body ending "…reviews all submissions.</body>
# <parameter name=\"facts\">[{…}]" — the whole facts array, serialized as
# text inside the body, rendered verbatim on the phone. Everything from the
# first marker on is machinery, not prose; `salvage_call_markup` recovers the
# facts it was carrying.
_CALL_MARKUP = re.compile(r"</?(?:body|parameter)\b[^>]*>|<parameter\s+name=", re.I)


def salvage_call_markup(text: Optional[str]) -> list:
    """The facts array a leaked `<parameter name="facts">` tail was carrying,
    or []. Best effort — garbage stays dropped."""
    match = re.search(r'<parameter\s+name="facts"\s*>\s*(\[.*?\])',
                      text or "", re.S)
    if not match:
        return []
    try:
        facts = json.loads(match.group(1))
        return facts if isinstance(facts, list) else []
    except ValueError:
        return []


# A URL that re-runs the search instead of landing on the thing found.
# `/s?k=…` (Amazon), `/search?q=…` (most of the web), `/results`, `/find`.
_SEARCH_PATH = re.compile(r"^/(s|search|results|find|sr)(/|$)", re.I)
_SEARCH_PARAMS = {"q", "k", "s", "query", "search", "keyword", "keywords",
                  "term", "field-keywords"}


def _is_search_url(url: str) -> bool:
    """Does this link hand the search back to the user?

    Verbatim from a pass that satisfied the price guard and still failed the
    user: `amazon.com/s?k=Spalding+The+Beast+54+glass`, with the fact's value
    reading "search for current price". That is not a found product — it is
    the query the worker was supposed to run, forwarded. It renders as a
    tappable row that dumps the user into a results page, which is exactly
    the work they opened the app to avoid.

    Product pages, review articles and category listings all pass: they are
    pages with content. Only the search endpoint itself is refused.
    """
    from urllib.parse import parse_qs, urlparse

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if _SEARCH_PATH.match(parsed.path or ""):
        return True
    return bool(_SEARCH_PARAMS & set(parse_qs(parsed.query or "")))


# Answers kept for the life of the process. A worker pass re-reads the same
# staged links across turns, and the guard must not turn one finding into a
# dozen requests at somebody else's shop.
_PROBED: Dict[str, "Probe"] = {}

# How much of a page to read. Open Graph tags live in `<head>`, and on the
# pages this actually runs against the whole block arrives inside the first
# chunk — measured at 4,096 bytes on emestudios.com. The cap is what keeps a
# liveness check from becoming a download.
_PROBE_BYTES = 24_576

_OG = {
    "image": re.compile(
        rb'<meta[^>]+property=["\']og:image["\'][^>]*>|'
        rb'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        re.I,
    ),
    "title": re.compile(
        rb'<meta[^>]+property=["\']og:title["\'][^>]*>|'
        rb'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        re.I,
    ),
}
_CONTENT_ATTR = re.compile(rb'content=["\']([^"\']+)["\']', re.I)


@dataclass(frozen=True)
class Probe:
    """What one look at a staged link found."""

    live: bool
    image: str = ""
    title: str = ""

# A url is refused only when the web says the page is not there. Everything
# else — a bot wall, a rate limit, a timeout, DNS trouble — is read as alive.
#
# The asymmetry is deliberate and it is the whole design of this check. A
# wrongly-kept dead link costs the user one tap into a 404. A wrongly-dropped
# live link destroys real research: it empties `facts`, which trips the priced
# guard, which refuses the whole finding and sends the worker back to redo work
# it already did correctly. Shops block scripted HEADs constantly, so failing
# closed here would reject good findings far more often than bad ones.
_DEAD_STATUS = {404, 410}


def _probe_url(url: str) -> Probe:
    """Open a staged link far enough to know it is real and what it shows.

    Two jobs, one request, because the request was already happening.

    **Is it there?** `_is_search_url` reads a url's *shape*, and the EMES
    failure sailed through it:
    `emestudios.com/products/rustic-off-sand-knit-sweater` is a perfectly
    well-formed product url, the product is real, and the slug is right. The
    site serves it at `/us/en/product/…` — singular, locale-prefixed — so the
    address was a 404. Shape cannot catch a plausible invention; only asking
    can.

    **What is on it?** The same first chunk carries `og:image` and `og:title`.
    The image is what the option row shows. The title is a free check on the
    model's own label: the finding that prompted all this called the garment a
    "Building Oversized Hoodie" and the page says *Building Heather Grey
    Oversized Crewneck*. Those are different clothes, and nothing else in this
    system would have noticed.

    Streamed and abandoned after `_PROBE_BYTES` — a liveness check must not
    become a download.
    """
    from ..config import get_config

    cfg = get_config()
    # `offline_extraction` means "this process does not talk to the network",
    # and it is what keeps the test suite hermetic (conftest sets it for every
    # test). Reaching out to a shop from a unit test would make the suite slow,
    # flaky, and dependent on somebody else's uptime.
    if not cfg.verify_urls or cfg.offline_extraction:
        return Probe(live=True)
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return Probe(live=True)          # not ours to judge; other guards apply
    if url in _PROBED:
        return _PROBED[url]

    import httpx

    probe = Probe(live=True)
    try:
        # A browser User-Agent, because the default one is refused by a good
        # share of retail sites — emestudios.com among them, which answers a
        # scripted request differently from a browser's.
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=6.0,
            headers={"User-Agent": _BROWSER_UA},
        ) as response:
            if response.status_code in _DEAD_STATUS:
                log.info("staged link is dead (%s): %s", response.status_code, url[:120])
                probe = Probe(live=False)
            else:
                probe = Probe(live=True, **_read_og(response, url))
    # Narrow on purpose. The first cut of this caught bare `Exception`, and a
    # NameError in the logging line above rode straight through it — the check
    # ran, found the 404, crashed, and reported the link healthy. A guard that
    # swallows its own bugs is worse than no guard, and this codebase has been
    # bitten by exactly that before (see `tools.py`, where a KeyError vanished
    # into `providers.run`). Network trouble fails open; a coding mistake does
    # not get to.
    except httpx.HTTPError:              # timeout, DNS, TLS, refused
        probe = Probe(live=True)

    _PROBED[url] = probe
    return probe


def _read_og(response: Any, base: str) -> Dict[str, str]:
    """Pull `og:image` and `og:title` out of the first chunks of a page."""
    from urllib.parse import urljoin

    buf = b""
    for chunk in response.iter_bytes(8192):
        buf += chunk
        if b"</head" in buf.lower() or len(buf) >= _PROBE_BYTES:
            break

    found: Dict[str, str] = {"image": "", "title": ""}
    for field, pattern in _OG.items():
        match = pattern.search(buf)
        if not match:
            continue
        # Either attribute order: `content` may precede `property` or follow it.
        raw = match.group(1) or (
            _CONTENT_ATTR.search(match.group(0)).group(1)
            if _CONTENT_ATTR.search(match.group(0))
            else b""
        )
        text = raw.decode("utf-8", "replace").strip()
        # Entity-escaped ampersands are normal in meta tags and break the CDN
        # urls they appear in.
        text = text.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
        found[field] = urljoin(base, text) if field == "image" and text else text
    if found["image"] and not _serves_an_image(found["image"]):
        found["image"] = ""
    return found


_IS_IMAGE: Dict[str, bool] = {}


def _serves_an_image(url: str) -> bool:
    """Does that address actually return a picture?

    Victoria's Secret answers its own `og:image` url with 4.5 KB of HTML. The
    tag is present and well-formed and points at nothing renderable, so a row
    trusting it would show a broken frame where a garment should be. Meta tags
    describe intent, not content; the only way to know is to ask what type
    comes back.
    """
    if url in _IS_IMAGE:
        return _IS_IMAGE[url]

    import httpx

    ok = False
    try:
        response = httpx.head(
            url, follow_redirects=True, timeout=4.0,
            headers={"User-Agent": _BROWSER_UA},
        )
        ok = response.headers.get("content-type", "").lower().startswith("image/")
        if not ok:
            log.info("og:image is not an image (%s): %s",
                     response.headers.get("content-type", "?"), url[:100])
    except httpx.HTTPError:
        ok = False               # unlike liveness, an image fails *closed* —
                                 # a missing picture costs a row nothing, and a
                                 # broken one costs it its credibility
    _IS_IMAGE[url] = ok
    return ok


def _url_is_live(url: str) -> bool:
    """Kept as its own name because the guard reads better at the call site."""
    return _probe_url(url).live


# Shops append their own name to `og:title`: "Rustic Off Sand Knit Sweater -
# Always Grateful". Everything from the last separator on is the shop, not the
# product.
_TITLE_TAIL = re.compile(r"\s+[|–—-]\s+[^|–—-]{2,40}$")
_WORD = re.compile(r"[a-z0-9]{3,}")


def _corrected_label(label: str, page_title: str) -> str:
    """Let the page name its own product.

    The finding that started this priced a "Building Oversized Hoodie". The page
    it linked says *Building Heather Grey Oversized Crewneck* — a different
    garment, at the same price, from the same brand. The model was not lying; it
    compressed a name it had read once, and nothing downstream could tell.

    Only replaces a label when the page title plainly describes the *same*
    thing: they must share a distinctive word. Without that guard a cookie
    banner or a generic "Shop" title would happily overwrite a good label, which
    is a worse failure than the one being fixed.
    """
    page = _TITLE_TAIL.sub("", (page_title or "").strip()).strip()
    if not label or not page or page.lower() == label.lower():
        return label
    if len(page) > 90:                   # a sentence, not a product name
        return label
    shared = set(_WORD.findall(label.lower())) & set(_WORD.findall(page.lower()))
    if not shared:
        return label                     # unrelated title — leave the label alone
    log.info("relabelled from the page: %r -> %r", label, page)
    return page


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _as_facts(value: Any) -> List[Dict[str, str]]:
    """Verified figures, normalised to `[{label, value, url}]`.

    Forgiving about shape for the same reason `_as_list` is: this arrives from
    a model, and rejecting a slightly-wrong shape means the research is lost
    rather than rendered. A bare string becomes a label-only fact, a dict is
    kept and cleaned, and anything unrecognisable is stringified rather than
    dropped — a fact the user can read beats a field the schema approved of.
    """
    if value is None:
        return []
    # Deliberately not routed through `_as_list`: that stringifies each
    # element, which would turn a perfectly good list of dicts into a list of
    # Python reprs and lose every field.
    items: List[Any]
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = json.loads(text)
        except ValueError:
            items = [text]
        else:
            items = parsed if isinstance(parsed, list) else [parsed]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]

    out: List[Dict[str, str]] = []
    for raw in items:
        item: Any = raw
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                item = json.loads(raw)
            except ValueError:
                item = raw
        if isinstance(item, dict):
            url = (str(item.get("url") or item.get("link") or "")).strip()
            # A search URL is demoted to no link rather than dropping the fact:
            # "Spalding makes a 54-inch glass model" is still worth reading as
            # text. What it must not do is render as a tappable row, or count
            # toward the price guard's link requirement — see `_has_link`.
            #
            # A dead URL is demoted the same way and for the same reason. The
            # demotion is what makes this work: an empty url fails `_has_link`,
            # which trips the priced-finding guard, which already knows how to
            # tell the worker to open the result and link the real page. One
            # predicate, no new refusal path.
            image = ""
            if url and _is_search_url(url):
                url = ""
            elif url:
                probe = _probe_url(url)
                if not probe.live:
                    url = ""
                else:
                    image = probe.image
            # Only a page about one product may rename it. A site root's
            # og:title is the shop's name, and letting it win turns a decent
            # label into a worse one — a live pass relabelled "EME Studios
            # Shipping & Promo Terms" to plain "Eme Studios", losing the only
            # part that said what the row was about.
            label = _corrected_label(
                _plain(str(item.get("label") or item.get("name") or "")),
                _probe_url(url).title if url and not _is_bare_domain(url) else "",
            )
            fact = {
                "label": label,
                "value": _plain(str(item.get("value") or item.get("detail") or "")),
                "url": url,
                # The row shows this; the model never supplies it. Asking the
                # model for an image url would reproduce the exact failure this
                # whole guard exists to catch — it would invent a plausible one.
                "image": image,
            }
        else:
            fact = {"label": _plain(str(item)), "value": "", "url": "", "image": ""}
        if fact["label"] or fact["value"] or fact["url"]:
            out.append(fact)
    return _drop_shared_images(out)


def _drop_shared_images(facts: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Strip links and pictures that describe a shop rather than a product.

    Two shapes, one tell, and neither needs an extra request.

    **A link two different products share is neither one's link.** Three
    pyjama sets pointing at `/us/vs/sale/clearance-sleep`, two Ekouaer sets
    pointing at `amazon.com` — the page exists, so the liveness check passes,
    and it is not a search url, so that check passes too. It still lands the
    user on a listing with the shopping left to do, which is the precise thing
    `_is_search_url` was written to prevent. Fourteen stored findings do this.

    **An image two rows share is the site's social card.** Victoria's Secret
    gave the same picture to five different sets. Five identical rows read as
    one product listed five times, which is worse than no pictures at all —
    telling the options apart was the entire reason for adding them.
    """
    # A bare domain is never a product page: `https://www.amazon.com` is a
    # shop, not a thing to buy.
    for fact in facts:
        url = fact.get("url") or ""
        if url and _is_bare_domain(url):
            log.info("dropping a bare-domain link: %s", url[:80])
            fact["url"] = ""
            fact["image"] = ""

    # One url, several *differently named* products. Identical labels are just
    # a duplicated row and say nothing about the link.
    by_url: Dict[str, set] = {}
    for fact in facts:
        if fact.get("url"):
            by_url.setdefault(fact["url"], set()).add(fact.get("label", ""))
    generic = {url for url, labels in by_url.items() if len(labels) > 1}
    if generic:
        log.info("dropping %d link(s) shared by different products", len(generic))
        for fact in facts:
            if fact.get("url") in generic:
                fact["url"] = ""
                fact["image"] = ""

    seen: Dict[str, int] = {}
    for fact in facts:
        if fact.get("image"):
            seen[fact["image"]] = seen.get(fact["image"], 0) + 1
    shared = {url for url, n in seen.items() if n > 1}
    if shared:
        log.info("dropping %d site-level image(s) repeated across a card", len(shared))
        for fact in facts:
            if fact.get("image") in shared:
                fact["image"] = ""
    return facts


def _is_bare_domain(url: str) -> bool:
    """`https://www.amazon.com` — a shop, offered as if it were a product."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return not parsed.query and (parsed.path or "/").strip("/") == ""


# An explicit currency marker, never a bare number. "$299.99", "£45", "1,200
# USD" are prices; "3-5 days", "54 inch backboard" and "Aug 22" are not, and a
# guard that fired on those would refuse most of the findings in the database.
_MONEY = re.compile(
    r"[$£€¥]\s?\d"
    r"|\b\d[\d,]*(?:\.\d+)?\s*(?:usd|eur|gbp|dollars?)\b",
    re.I,
)


def _haystack(steps: Any, body: Any, facts: Any) -> List[str]:
    """Every field a figure could be hiding in, as plain strings.

    Not a fact's `url` — a currency string inside a link is a product page,
    which is the thing we want found rather than flagged.
    """
    out = list(_as_list(steps))
    if body:
        out.append(str(body))
    for fact in _as_facts(facts):
        out.append(f"{fact.get('label', '')} {fact.get('value', '')}")
    return [t for t in out if t]


# The whole amount, not just its first digit — this one is used for counting
# distinct figures, where `$19.99` and `$19` must not collapse together.
_AMOUNT = re.compile(r"[$£€¥]\s?\d[\d,]*(?:\.\d+)?")


def _priced_options(steps: Any, body: Any, facts: Any) -> int:
    """How many distinct sums this finding names.

    One figure is a fact about a settled thing — a deposit, a bill, a budget.
    Several is a comparison, and a comparison is a decision someone has to
    make. Measured across every live move in the database, the split is clean:
    every legitimate `send` names nought or one, the one that should have been
    a `decide` names three, and the `decide` that got it right names seven.
    """
    text = " ".join(_haystack(steps, body, facts))
    return len({m.replace(" ", "") for m in _AMOUNT.findall(text)})


def _looks_priced(steps: Any, body: Any, facts: Any) -> bool:
    """Does this finding name money?

    The question the caller is really asking is "did this pass go shopping",
    and a price is the only reliable tell. A move that names a figure has
    either verified it somewhere or guessed it, and the two are
    indistinguishable on screen — which is how "Amazon Ekouaer Pajama Sets:
    typically $25-50 each" reached a phone as though it were research.

    Reads every field the figure could hide in: the staged steps, the prose,
    and a fact's own label and value. Not the fact's `url` — a currency string
    inside a link is a product page, which is exactly what we want to find.
    """
    return any(_MONEY.search(text) for text in _haystack(steps, body, facts))


_LINK = re.compile(r"https?://\S+", re.I)

# A headline offering a choice rather than making one. Word-bounded so "Order"
# and "for" don't trip it.
_MENU = re.compile(r"\bor\b", re.I)


def _has_link(steps: Any, facts: Any) -> bool:
    """Is there anything here the user could actually tap?

    Checks both places a staged link legitimately lives: a fact's `url`, which
    is where it belongs, and inside a step, which is where the worker put it
    before `facts` existed and still does. The finding is what matters, not
    which field carried it.
    """
    if any(f.get("url") for f in _as_facts(facts)):
        return True      # _as_facts has already dropped any search-query urls
    for step in _as_list(steps):
        match = _LINK.search(step or "")
        if match and not _is_search_url(match.group(0)):
            return True
    return False


def _as_list(value: Any) -> List[str]:
    """A list of strings, whatever shape the model sent.

    Models hand a list-typed argument over as a JSON *string* often enough that
    this cannot be assumed away, and a string is iterable: `[{"text": s} for s
    in value]` silently produced one step per character, so a staged move with
    three real options was stored as four hundred single-letter steps and the
    move card rendered as confetti. Nothing errored — the finding saved, the
    body was fine, and only the steps were destroyed.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                return [value]
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        return [value]           # one plain string is one item, not N letters
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return [str(value)]


def _obj(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _quiet_conversations(min_days: int = 2) -> List[dict]:
    """1:1 conversations where the user spoke last and nobody answered — the
    awaiting-reply data the silence sweep computes, exposed as a read tool so
    'who hasn't gotten back to me?' is answerable in one call.

    Named `conversations`, not `threads` (§v2 step 0): a thread is now the
    user's open loop. The model reads these names, and two objects sharing one
    word is the ambiguity the rename existed to remove."""
    from datetime import datetime, timezone

    from ..models import parse_iso

    now = datetime.now(timezone.utc)
    conn = db.get_connection()
    rows = conn.execute(
        """
        SELECT m.conversation_id, m.person_id, m.timestamp, substr(m.text, 1, 120) AS text,
               t.display_name
        FROM messages m
        JOIN conversations t ON t.id = m.conversation_id
        WHERE t.is_group = 0
          AND m.timestamp = (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.conversation_id = m.conversation_id)
          AND m.is_from_user = 1
        ORDER BY m.timestamp DESC
        """
    ).fetchall()
    out = []
    for row in rows:
        ts = parse_iso(row["timestamp"])
        if ts is None:
            continue
        days = (now - ts).days
        if days < min_days:
            continue
        person = db.get_person(row["person_id"]) if row["person_id"] else None
        name = person.display_name if person else row["display_name"]
        if not any(ch.isalpha() for ch in name or ""):
            continue  # unknown bare numbers aren't useful here
        out.append(
            {
                "person": name,
                "person_id": row["person_id"],
                "days_waiting": days,
                "your_last_word": row["text"],
            }
        )
    return out[:15]


def _list_open_items(person_id: Optional[str] = None) -> List[dict]:
    items = db.open_items()
    if person_id:
        items = [i for i in items if i.person_id == person_id]
    return [
        {
            "id": i.id,
            "person": i.person,
            "person_id": i.person_id,
            "type": i.type,
            "text": (i.raw_text or "")[:140],
            "timestamp": i.timestamp,
            "status": i.status,
        }
        for i in items[:20]
    ]


READ_TOOLS: List[Tool] = [
    Tool(
        name="search_messages",
        description=(
            "Search the user's ingested messages (iMessage + Gmail) by keyword, "
            "newest first. Every filter is optional and they combine: narrow by "
            "person, source, date range, direction, or links. Results carry a "
            "`message_id` — pass it to get_message to read one in full. Words "
            "must APPEAR in the message; search 'flight confirmation', never a "
            "description of the situation."
        ),
        input_schema=_obj(
            {
                "query": {"type": "string", "description": "keywords that appear in the text"},
                "person_id": {"type": "string", "description": "restrict to one person"},
                "source": {"type": "string", "enum": ["imessage", "mail", "whatsapp"]},
                "since": {"type": "string", "description": "ISO date/datetime lower bound"},
                "until": {"type": "string", "description": "ISO date/datetime upper bound"},
                "direction": {"type": "string", "enum": ["from_you", "from_them"]},
                "has_link": {"type": "boolean", "description": "only messages containing a URL"},
                "limit": {"type": "integer", "description": "max results (default 8)"},
            },
        ),
        fn=lambda query=None, person_id=None, source=None, since=None, until=None,
        direction=None, has_link=False, limit=8: tools.search_messages(
            query=query, person_id=person_id, source=source, since=since, until=until,
            direction=direction, has_link=has_link, limit=limit,
        ),
    ),
    Tool(
        name="read_attachment",
        description=(
            "The text INSIDE a file a message carried — a bill, form, "
            "statement, minutes. Search results with `via_attachment` matched "
            "inside a document; this is how you read it. Returns the parsed "
            "text (use offset to page through long files), or the honest "
            "error for a scan ('no extractable text'). Amounts, account "
            "numbers, requirements and dates usually live here, not in the "
            "covering email."
        ),
        input_schema=_obj(
            {
                "message_id": {"type": "string"},
                "filename": {"type": "string", "description": "one file, when the message carries several"},
                "offset": {"type": "integer", "description": "character offset for paging (default 0)"},
                "limit": {"type": "integer", "description": "max characters (default 6000)"},
            },
            required=["message_id"],
        ),
        fn=lambda message_id, filename=None, offset=0, limit=6000: tools.read_attachment(
            message_id, filename=filename, offset=offset, limit=limit
        ),
    ),
    Tool(
        name="search_world",
        description=(
            "Search the user's standing knowledge — who people are, where "
            "they're enrolled, addresses, accounts, institutions. Facts here "
            "were stated in past messages and persist; 'dentist' finds the "
            "dentist's practice and address even when no recent message says "
            "the word. Check here before concluding something about the "
            "user's life is unknown."
        ),
        input_schema=_obj({"query": {"type": "string"}}, required=["query"]),
        fn=lambda query: __import__("lifeline.world", fromlist=["search"]).search(query),
    ),
    Tool(
        name="find_person",
        description=(
            "Resolve a person by name, nickname, misspelling, email or phone "
            "number. ALWAYS call this before saying you don't know who someone "
            "is — it matches loosely, so 'booooby' finds 'Robbbbie Carter'. "
            "If the result has `alternatives`, several people match: name them "
            "and ask which, rather than picking one or asking who they are."
        ),
        input_schema=_obj({"name": {"type": "string"}}, required=["name"]),
        fn=lambda name: tools.find_person(name),
    ),
    Tool(
        name="get_message",
        description=(
            "One message in FULL. Every other read truncates — searches at 280 "
            "characters, read_conversation at 400 — which is exactly where a "
            "bill's amount, a due date or a flight number sits. Use the "
            "`message_id` from a search result."
        ),
        input_schema=_obj({"message_id": {"type": "string"}}, required=["message_id"]),
        fn=lambda message_id: tools.get_message(message_id),
    ),
    Tool(
        name="search_mail",
        description=(
            "Search email by its metadata: sender address or domain, Gmail "
            "label, direction, date. THE tool for bills, confirmations, "
            "receipts and statements — those are identified by who sent them, "
            "not by guessing keywords. sender accepts a bare domain "
            "('capitalone'); label takes IMPORTANT, SENT, INBOX, "
            "CATEGORY_PERSONAL and the like."
        ),
        input_schema=_obj(
            {
                "query": {"type": "string", "description": "keywords in the body (optional)"},
                "sender": {"type": "string", "description": "email address or domain fragment"},
                "label": {"type": "string", "description": "a Gmail label"},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "direction": {"type": "string", "enum": ["from_you", "from_them"]},
                "limit": {"type": "integer"},
            },
        ),
        fn=lambda query=None, sender=None, label=None, since=None, until=None,
        direction=None, limit=10: tools.search_mail(
            query=query, sender=sender, label=label, since=since, until=until,
            direction=direction, limit=limit,
        ),
    ),
    Tool(
        name="timeline",
        description=(
            "One person or one topic across EVERY channel, in the order things "
            "actually happened — messages, email, calendar and extracted items "
            "interleaved. Use it for 'what's going on with X' and 'catch me up', "
            "where reading one source at a time gives half the picture."
        ),
        input_schema=_obj(
            {
                "person_id": {"type": "string"},
                "query": {"type": "string", "description": "topic keywords"},
                "days": {"type": "integer", "description": "how far back (default 90)"},
                "limit": {"type": "integer"},
            },
        ),
        fn=lambda person_id=None, query=None, days=90, limit=30: tools.timeline(
            person_id=person_id, query=query, days=days, limit=limit
        ),
    ),
    Tool(
        name="search_history",
        description=(
            "Things the user already dealt with, and HOW they closed — the "
            "precedent for 'have I handled this before?'. Most closures carry "
            "the evidence that settled them, so this shows what the user "
            "actually does, not what they say they do."
        ),
        input_schema=_obj(
            {
                "query": {"type": "string"},
                "person_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
        ),
        fn=lambda query=None, person_id=None, limit=10: tools.search_history(
            query=query, person_id=person_id, limit=limit
        ),
    ),
    Tool(
        name="search_calendar",
        description=(
            "Calendar events. Upcoming by default — pass `since` (an ISO date) "
            "to reach into the past. The user's history IS here: never say you "
            "can't see past appointments, widen the window instead."
        ),
        input_schema=_obj({
            "query": {"type": "string", "description": "keyword filter (optional)"},
            "since": {"type": "string", "description": "ISO date lower bound; use for past events"},
            "until": {"type": "string", "description": "ISO date upper bound"},
        }),
        fn=lambda query=None, since=None, until=None: tools.search_calendar(
            query, since=since, until=until
        ),
    ),
    Tool(
        name="quiet_conversations",
        description=(
            "People the user messaged who never answered — each with days "
            "waiting and the user's last words. THE tool for 'who hasn't "
            "gotten back to me / who am I waiting on' questions."
        ),
        input_schema=_obj(
            {"min_days": {"type": "integer", "description": "minimum days of silence (default 2)"}}
        ),
        fn=_quiet_conversations,
    ),
    Tool(
        name="list_open_items",
        description=(
            "The user's open (surfaced, not yet done) items, optionally for one "
            "person. Use to answer 'what do I owe' questions."
        ),
        input_schema=_obj({"person_id": {"type": "string", "description": "restrict to one person (optional)"}}),
        fn=_list_open_items,
    ),
]


def _read_conversation(person_id: str, limit: int = 12) -> List[dict]:
    """The recent back-and-forth with one person, oldest last — what a draft
    has to be grounded in. Reads a *conversation*; `read_thread_state` reads
    one of the user's open loops. They are different objects and, since step 0,
    different words."""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT is_from_user, timestamp, substr(text, 1, 400) AS text FROM messages "
        "WHERE person_id = ? ORDER BY timestamp DESC LIMIT ?",
        (person_id, max(1, min(limit, 30))),
    ).fetchall()
    return [
        {
            "from": "you" if r["is_from_user"] else "them",
            "timestamp": r["timestamp"],
            "text": r["text"],
        }
        for r in reversed(rows)
    ]


def draft_tools(drafted: Optional[List[Any]] = None, thread: Any = None) -> List[Tool]:
    """The loop's ability to *act*, not just know (§v1.5): read a thread and
    hand back a ready message the user can send in one tap."""

    def draft_message(person_id: Optional[str] = None, text: str = "",
                      channel: Optional[str] = None) -> dict:
        # A thread the user declared carries no evidence, so there is no one
        # to infer from and the model would otherwise have to invent a
        # recipient. If the user named a contact, that is the answer.
        person_id = person_id or getattr(thread, "contact_person_id", None)
        if not person_id or not db.get_person(person_id):
            # Outreach to someone new — a YouTuber, a company, a stranger — has
            # no person row and no address, and this used to refuse, which
            # threw the finished draft away. The worker then recorded a
            # DESCRIPTION of the email it had just lost ("draft ready; need
            # his address"), and the user stared at a card describing a
            # message that existed nowhere. The draft is the deliverable:
            # tell the model exactly where it lives when the recipient is
            # unknown, so the text survives inside the move.
            return {"error": (
                "no known contact for that person, so this draft cannot be "
                "held here. If they are a public figure or business, "
                "web_search their public name for a public contact address "
                "FIRST — that is the carve-out outreach exists on. If no "
                "address turns up, record the draft: record_finding "
                "kind='action', move_kind='send', steps=[the COMPLETE "
                "message text you drafted — not a description of it], "
                "blocked_reason='no email address on file for <name>', "
                "needs=['<name>'s email address']. The card will show the "
                "draft and what is missing."
            )}
        person = db.get_person(person_id)
        handle = person.handles[0] if person.handles else None
        kind = channel or ("email" if handle and "@" in (handle or "") else "imessage")
        draft = {
            "person": person.display_name,
            "person_id": person.id,
            "handle": handle,
            "channel": kind,
            "text": text,
        }
        if drafted is not None:
            drafted.append(draft)
        return {"drafted": True, "to": person.display_name, "channel": kind}

    return [
        Tool(
            name="read_conversation",
            description=(
                "The recent messages exchanged with one person, oldest last. "
                "Read this BEFORE drafting anything to them — a follow-up must "
                "reference what was actually said."
            ),
            input_schema=_obj(
                {
                    "person_id": {"type": "string"},
                    "limit": {"type": "integer", "description": "messages to fetch (default 12)"},
                },
                required=["person_id"],
            ),
            fn=_read_conversation,
        ),
        Tool(
            name="draft_message",
            description=(
                "Hand the user a ready-to-send message to a person. Write it "
                "yourself in their voice — brief, natural, no salutations the "
                "thread wouldn't use. The user reviews and sends it in one tap; "
                "you never send anything."
            ),
            input_schema=_obj(
                {
                    "person_id": {
                        "type": "string",
                        "description": (
                            "Omit to use the thread's own contact, if the user set one."
                        ),
                    },
                    "text": {"type": "string", "description": "the message body, ready to send"},
                    "channel": {"type": "string", "enum": ["imessage", "email"]},
                },
                required=["text"],
            ),
            fn=draft_message,
        ),
    ]


def fact_tools(recorded: Optional[List[Any]] = None) -> List[Tool]:
    """Write tools for the model of you (§v1.4 pillar B). Pass a list as
    `recorded` to collect the Facts a run creates (the /tell response echoes
    them back to the user)."""
    from ..models import Fact  # local import to avoid cycles

    def record_fact(
        statement: str,
        subject_type: str = "self",
        subject_id: Optional[str] = None,
        predicate: Optional[str] = None,
        value: Optional[str] = None,
        confidence: float = 1.0,
    ) -> dict:
        fact = db.upsert_fact(
            Fact(
                subject_type=subject_type,
                subject_id=subject_id,
                statement=statement,
                predicate=predicate,
                value=value,
                source="user",
                confidence=confidence,
                provenance="tell",
            )
        )
        if recorded is not None:
            recorded.append(fact)
        return {"recorded": fact.id, "statement": fact.statement}

    def get_facts(subject_type: Optional[str] = None, subject_id: Optional[str] = None) -> List[dict]:
        return [
            {
                "id": f.id,
                "subject_type": f.subject_type,
                "subject_id": f.subject_id,
                "statement": f.statement,
                "confidence": f.confidence,
            }
            for f in db.list_facts(subject_type=subject_type, subject_id=subject_id)[:30]
        ]

    return [
        Tool(
            name="record_fact",
            description=(
                "Save one durable fact the user stated about themselves, a person, "
                "or a topic. Use one call per distinct fact. subject_type is "
                "'person' when the fact is about someone (resolve subject_id with "
                "find_person first), 'topic' for a project/company/theme, else 'self'."
            ),
            input_schema=_obj(
                {
                    "statement": {"type": "string", "description": "the fact, concise, third person"},
                    "subject_type": {"type": "string", "enum": ["self", "person", "topic"]},
                    "subject_id": {"type": "string", "description": "person_id or topic slug (optional)"},
                    "predicate": {"type": "string", "description": "structured key, e.g. priority (optional)"},
                    "value": {"type": "string", "description": "structured value, e.g. low (optional)"},
                    "confidence": {"type": "number", "description": "0-1; lower it when unsure"},
                },
                required=["statement"],
            ),
            fn=record_fact,
        ),
        Tool(
            name="get_facts",
            description="Facts already on file, to avoid duplicates and spot contradictions.",
            input_schema=_obj(
                {
                    "subject_type": {"type": "string", "enum": ["self", "person", "topic"]},
                    "subject_id": {"type": "string"},
                }
            ),
            fn=get_facts,
        ),
    ]


def information_tools() -> List[Tool]:
    """Write tool for surfacing information (§v1.4 pillar A): the loop's way to
    say 'you should know this' without inventing a to-do."""
    from ..models import Entities, Item

    def create_information_item(
        headline: str,
        body: str,
        category: str = "discovery",
        person_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        suggested_action: str = "",
    ) -> dict:
        person = db.get_person(person_id) if person_id else None
        item = Item(
            source="lifeline",
            conversation_id=conversation_id or f"lifeline:{category}",
            person=person.display_name if person else "Lifeline",
            person_id=person_id,
            type="followup",
            kind="information",
            category=category,
            raw_text=body,
            entities=Entities(item=headline),
            suggested_action=suggested_action,
        )
        db.save_item(item)
        return {"created": item.id, "headline": headline}

    return [
        Tool(
            name="create_information_item",
            description=(
                "Surface a piece of information to the user as a card — a "
                "discovery about their world ('X went quiet', 'loop closed'), "
                "added context, or relevant external info. Not for to-dos."
            ),
            input_schema=_obj(
                {
                    "headline": {"type": "string", "description": "short card title"},
                    "body": {"type": "string", "description": "what the user should know, with specifics"},
                    "category": {"type": "string", "enum": ["discovery", "context", "external"]},
                    "person_id": {"type": "string", "description": "the person it concerns (optional)"},
                    "conversation_id": {"type": "string", "description": "the thread it concerns (optional)"},
                    "suggested_action": {"type": "string", "description": "optional one-line next step"},
                },
                required=["headline", "body"],
            ),
            fn=create_information_item,
        )
    ]


def thread_tools(created: Optional[List[Any]] = None) -> List[Tool]:
    """Tier 4 — thread machinery (§v2 step 1).

    Two of these five are reads, and that is the point. The audit found tier 4
    specified as writes only, which meant the worker loop could create threads
    and record against them but never see what it already knew — it would
    re-investigate from zero on every pass, the exact v1.5 failure v2 exists to
    fix. `read_thread_state` and `search_threads` close that hole.

    Pass a list as `created` to collect the Threads a run opens (the endpoint
    echoes them back, the way /tell echoes facts).
    """
    from .. import threads as threads_mod
    from ..models import DeadlineSource, ThreadOrigin, ThreadState

    def create_thread(
        title: str,
        summary: str = "",
        state: str = ThreadState.PROPOSED,
        importance: float = 0.5,
        evidence_item_ids: Optional[List[str]] = None,
    ) -> dict:
        # The model proposes; the user promotes. A system-opened thread
        # defaults to `proposed` so nothing the user didn't acknowledge can
        # land on the main stack — the rule that makes the count mean anything.
        proposed = state != ThreadState.LIVE
        thread = threads_mod.create(
            title=title,
            summary=summary,
            origin=ThreadOrigin.SYSTEM_PROPOSED if proposed else ThreadOrigin.USER,
            state=ThreadState.PROPOSED if proposed else ThreadState.LIVE,
            importance=importance,
        )
        claimed = []
        for item_id in evidence_item_ids or []:
            try:
                threads_mod.claim(thread.id, item_id, kind="item")
                claimed.append(item_id)
            except threads_mod.ThreadError as exc:
                # A bad id must not lose the thread that was already created —
                # report the miss and keep the rest.
                claimed.append(f"skipped {item_id}: {exc}")
        if created is not None:
            created.append(thread)
        return {
            "created": thread.id,
            "title": thread.title,
            "state": thread.state,
            "evidence": claimed,
        }

    def update_thread(
        thread_id: str,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        state: Optional[str] = None,
        importance: Optional[float] = None,
    ) -> dict:
        thread = threads_mod.update(
            thread_id, title=title, summary=summary, state=state, importance=importance
        )
        return {
            "updated": thread.id,
            "title": thread.title,
            "state": thread.state,
            "importance": thread.importance,
        }

    def set_deadline(
        thread_id: str,
        date: str,
        evidence_item_ids: Optional[List[str]] = None,
        evidence_message_ids: Optional[List[str]] = None,
        reason: str = "",
    ) -> dict:
        refs = [{"kind": "item", "ref_id": i} for i in (evidence_item_ids or [])]
        refs += [{"kind": "message", "ref_id": m} for m in (evidence_message_ids or [])]
        thread = threads_mod.set_deadline(
            thread_id, date, source=DeadlineSource.INFERRED, evidence=refs, reason=reason
        )
        return {
            "thread_id": thread.id,
            "deadline": thread.deadline,
            "source": thread.deadline_source,
            "evidence": thread.deadline_evidence,
        }

    return [
        Tool(
            name="create_thread",
            description=(
                "Open a thread — one loop the user is carrying ('Puerto Rico "
                "work trip', 'water bill'), NOT a single message or to-do. "
                "Threads default to 'proposed': they wait in the proposals "
                "view for the user to accept. Only pass state='live' when the "
                "user asked for this thread in so many words. Attach the "
                "evidence that made you think it with evidence_item_ids. "
                "Check search_threads first — never open a second thread for a "
                "loop that already exists."
            ),
            input_schema=_obj(
                {
                    "title": {"type": "string", "description": "the loop, in the user's terms, under 60 chars"},
                    "summary": {"type": "string", "description": "what's open about it and why"},
                    "state": {"type": "string", "enum": ["proposed", "live"]},
                    "importance": {"type": "number", "description": "0-1, your read of how much it matters"},
                    "evidence_item_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "item ids that support this thread",
                    },
                },
                required=["title"],
            ),
            fn=create_thread,
        ),
        Tool(
            name="update_thread",
            description=(
                "Revise a thread: sharpen its title or summary, change its "
                "importance, or move its state (live | quiet | resolved | "
                "archived). Resolve it only when the loop is genuinely closed."
            ),
            input_schema=_obj(
                {
                    "thread_id": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "state": {"type": "string", "enum": ["live", "quiet", "resolved", "archived"]},
                    "importance": {"type": "number"},
                },
                required=["thread_id"],
            ),
            fn=update_thread,
        ),
        Tool(
            name="set_deadline",
            description=(
                "Give a thread the due date you INFERRED from evidence — a "
                "bill's due date, a flight time, a registration cutoff. You "
                "must name the evidence that implied it; a date with no source "
                "is a guess and will be refused. The user sees it as an "
                "editable chip and can overrule you, and once they have, you "
                "cannot change it back."
            ),
            input_schema=_obj(
                {
                    "thread_id": {"type": "string"},
                    "date": {"type": "string", "description": "ISO-8601, e.g. 2026-08-31T12:00:00+00:00"},
                    "evidence_item_ids": {"type": "array", "items": {"type": "string"}},
                    "evidence_message_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string", "description": "why this date, in one line"},
                },
                required=["thread_id", "date"],
            ),
            fn=set_deadline,
        ),
        Tool(
            name="read_thread_state",
            description=(
                "Everything one thread already knows about itself: its state, "
                "deadline and where that date came from, and every piece of "
                "evidence it has claimed. Read this BEFORE working a thread — "
                "it is how you avoid re-investigating what you already found."
            ),
            input_schema=_obj({"thread_id": {"type": "string"}}, required=["thread_id"]),
            fn=lambda thread_id: threads_mod.read_state(thread_id),
        ),
        Tool(
            name="search_threads",
            description=(
                "Find the thread something belongs to, by keyword over titles, "
                "summaries, and claimed evidence. Call with no query to list "
                "the user's open threads. THE tool for 'does this fit a loop "
                "the user is already carrying?' — ask it before create_thread."
            ),
            input_schema=_obj(
                {
                    "query": {"type": "string", "description": "keywords (optional)"},
                    "state": {
                        "type": "string",
                        "enum": ["live", "quiet", "proposed", "resolved", "archived", "all"],
                        "description": "default: the open stack (live + quiet)",
                    },
                }
            ),
            fn=lambda query=None, state=None: threads_mod.search(query, state=state),
        ),
    ]


def finding_tools(thread_id: str, recorded: Optional[List[Any]] = None,
                  ceiling: Optional[str] = None) -> List[Tool]:
    """`record_finding` — what the worker brings back (§v2 step 4).

    Absorbs the dead `create_information_item`, which was defined in v1.4 and
    never wired to anything. The difference is the thread: an information item
    had nowhere to belong, so it became another card in a deck; a finding
    belongs to the loop that wanted it.
    """
    from .. import threads as threads_mod
    from ..models import Autonomy, FindingKind, MoveKind

    # Which move shapes this thread's ceiling permits. `silent` permits none —
    # at that rung the worker may name what is missing and never stage it.
    ceiling = ceiling or Autonomy.PREPARED
    permitted = MoveKind.allowed_for(ceiling)

    def record_finding(
        headline: str,
        body: str = "",
        kind: str = FindingKind.FINDING,
        importance: float = 0.5,
        evidence_item_ids: Optional[List[str]] = None,
        evidence_message_ids: Optional[List[str]] = None,
        move_kind: Optional[str] = None,
        steps: Optional[List[str]] = None,
        needs: Optional[List[str]] = None,
        blocked_reason: Optional[str] = None,
        facts: Optional[List[Any]] = None,
    ) -> dict:
        # One recording per pass, enforced rather than requested.
        #
        # The prompt has said "Exactly one call" since the worker shipped, and
        # passes kept making two — the Kay-Dean thread recorded twice in one
        # run, and a live hoop pass recorded four times, leaving two findings
        # on screen disagreeing about the same hoops. `recorded` holds only
        # successful records (a refusal and a dedupe skip both leave it
        # untouched), so its length is the honest count of what this pass has
        # already written.
        if recorded:
            return {"error": (
                "this pass already recorded its finding — one call per pass, "
                "and you have made yours. If what you have now is better than "
                "what you wrote, say so in your final message; the next pass "
                "will supersede it. Do not record a second time."
            )}
        if kind not in FindingKind.ALL:
            return {"error": f"kind must be one of {FindingKind.ALL}"}
        if move_kind and move_kind not in MoveKind.ALL:
            return {"error": f"move_kind must be one of {MoveKind.ALL}"}
        if kind == FindingKind.ACTION and move_kind and move_kind in permitted:
            # The ceiling is the user's explicit rule; appetite is the learned
            # one. Both only ever narrow, and both route the worker to a
            # finding rather than discarding the thinking it already did.
            from ..ranking import learning

            thread_obj = db.get_thread(thread_id)
            if thread_obj is not None and not learning.may_propose(thread_obj, move_kind):
                return {"error": (
                    f"'{move_kind}' moves have been turned down repeatedly on "
                    "threads like this one, so this is not the moment to offer "
                    "another. Record what you found with kind='finding' "
                    "instead — the work is still worth having."
                )}
        if kind == FindingKind.ACTION and move_kind and move_kind not in permitted:
            # The ladder, enforced where the move is filed rather than by
            # withholding a tool: the worker still needs record_finding at
            # every rung, so the gate has to be on what it may file.
            if not permitted:
                return {"error": (
                    f"this thread is set to '{ceiling}', so it takes no moves at "
                    "all — say what is missing with kind='finding' instead."
                )}
            return {"error": (
                f"a '{move_kind}' move spends money or cannot be undone, and "
                f"this thread is set to '{ceiling}'. Only the user raises a "
                "thread to 'ask'. Record what you found with kind='finding', "
                "including what it would take, and let them decide."
            )}
        if kind == FindingKind.ACTION:
            # A move without a shape can't be rendered as one, and a move with
            # neither staged work nor a stated reason it couldn't be staged is
            # the failure mode the prompt spends a paragraph on: a note to
            # itself wearing the costume of prepared work.
            if not move_kind:
                return {"error": "kind='action' is a move and needs move_kind: "
                                 f"one of {MoveKind.ALL}"}
            # `facts` counts as staged work, and predating it here was a
            # real gap: a `decide` whose whole value is five priced products
            # with links carries its work entirely in facts, and this refused
            # it for having no steps — turning the best-shaped move the worker
            # can produce into the one shape it could not record.
            if not steps and not _as_facts(facts) and not blocked_reason:
                return {"error": "a move needs either steps (the work you "
                                 "actually did) or blocked_reason (why you "
                                 "could not do it). Describing what should be "
                                 "assembled is not staging it — record a "
                                 "finding instead."}
        elif move_kind or steps or blocked_reason:
            return {"error": "move_kind/steps/blocked_reason only apply to "
                             "kind='action'. Figures and links do not: put "
                             "them in `facts`, which is legal on a finding."}
        # Research that arrives as prose is research the user has to redo.
        #
        # The failure this catches, verbatim from a live thread: "Amazon
        # Ekouaer Pajama Sets: typically $25-50 each" — a category, a guessed
        # range, no link, nothing to buy. Recorded happily, rendered as text,
        # useless. If a pass names a price it must name what has that price and
        # where, and the only honest way to enforce that is to refuse the
        # finding and make the model record it properly.
        # A `decide` that offers a menu has not decided.
        #
        # "Decline or clarify the 2-year stalled deal", "Buy 3 pajama sets from
        # Amazon or Victoria's Secret" — the shape claims a decision was made
        # and the headline hands back a choice. Four of ten `decide` moves in
        # the database read like this, and the user's complaint about all of
        # them was the same: it did the research and then made them do the
        # deciding anyway.
        #
        # Only an explicit either/or is caught. A vague headline that names no
        # pick at all still passes, so this narrows the failure rather than
        # closing it — the prompt carries the rest.
        if kind == FindingKind.ACTION and move_kind == MoveKind.DECIDE:
            if _MENU.search(_plain(str(headline or ""))):
                return {"error": (
                    "a `decide` that says \"this or that\" has not decided. You "
                    "found the options, so you know more about them than the "
                    "user does — name the one you would take and what it costs, "
                    "and put the alternatives in `facts` underneath it. They can "
                    "overrule you in one tap; they cannot un-make a decision you "
                    "declined to offer."
                )}

        # A move may ask for one thing, not three.
        #
        # The pajamas card ended with "wait for Nia's specs, or proceed ·
        # her size if you don't already know it · which retailer you prefer" —
        # three questions handed back under a move that claimed to have decided
        # something. Whatever the shape says, a list of questions is the job
        # returned, and the screen renders `needs` as one italic line, so three
        # of them arrive as a run-on sentence nobody finishes.
        asked = [n for n in _as_list(needs) if n.strip()]
        if kind == FindingKind.ACTION and len(asked) > 1:
            return {"error": (
                f"this asks the user {len(asked)} things. A move may need at "
                "most one, and it must carry your best assumption so their "
                "silence still moves the thread: \"her size — M unless you say "
                "otherwise\", not \"her size\". Decide the rest yourself and "
                "say what you assumed. If you genuinely cannot proceed without "
                "all of them, this is not a move — record it as a finding."
            )}

        # A `send` move that priced several options is a `decide` in a send
        # costume, and it is the failure the user named out loud: "the pajamas
        # are enough for her — it's for me."
        #
        # The prompt has forbidden this since the worker shipped ("if you
        # searched and then recorded a message asking what they want, you threw
        # the pass away") and a live pass did it anyway — it shopped, found
        # Ekouaer at $12-35 and Victoria's Secret at $29.99, and then drafted
        # "do you have a preference on colors, style, or material?". Saying it a
        # third time in prose would not have helped; having priced the options
        # is the evidence that the shopping was already done.
        #
        # Counted rather than merely detected, because one figure is legitimate
        # on a send — "confirming the $500 deposit" is a message about a settled
        # thing. Two or more is a comparison, and a comparison is the user's to
        # make with the options in front of them.
        if kind == FindingKind.ACTION and move_kind == MoveKind.SEND:
            if _priced_options(steps, body, facts) >= 2:
                return {"error": (
                    "this is a `decide`, not a `send`. You priced more than one "
                    "option, which means the shopping is already done — and then "
                    "you drafted a message asking them what they want. That hands "
                    "the job back to the person who asked you to do it.\n"
                    "Record it as move_kind='decide' with one option per entry in "
                    "`facts` — the product, its price, its link — and lead the "
                    "headline with the one you would pick and what it costs. They "
                    "can overrule you; they cannot un-ask a question you sent."
                )}

        # Three carve-outs. Two turn on where the figure came from; the first
        # is that kind='nothing' makes no claim at all — it is the honest "I
        # looked and there was nothing", and `worker.py` writes one with the
        # run's own conclusion as the body. Refusing it because that prose
        # happens to quote a number would delete the pass's only record of
        # itself and leave the thread looking unworked.
        #
        # A price the worker read in the user's own mail — the water bill's
        # $385.40 — is already sourced. Demanding a web url for it would refuse
        # a true finding because its provenance is a message rather than a page,
        # and the evidence refs are that provenance.
        #
        # On a `send` move the deliverable is the draft, so a budget quoted
        # inside it ("I've got $100-150 for this") is the message's content, not
        # a claim about the world that needs a link behind it.
        sourced = bool(evidence_item_ids or evidence_message_ids)
        researched = (
            kind != FindingKind.NOTHING
            and move_kind != MoveKind.SEND
            and not sourced
        )
        priced = researched and _looks_priced(steps, body, facts)
        # A link is a link. `facts` is where one belongs and where the screen
        # looks first, but a pass that staged the striata payment url as a step
        # did the work — refusing that finding would destroy real research to
        # enforce a field preference. What this guard is for is a price with
        # nothing behind it anywhere.
        if priced and not _has_link(steps, facts):
            return {"error": (
                "this names prices but has no `facts` with urls. A price "
                "without a product and a link is something the user has to go "
                "and find again — which is the work you were supposed to do. "
                "Put one entry in `facts` per real option: label (the actual "
                "product), value (its actual price and availability), url "
                "(where you found it). Ranges like \"typically $25-50\" are "
                "guesses, not findings; search until you have specific items, "
                "or say plainly that you could not find any.\n"
                "A search url does not count and has been dropped from what "
                "you sent. `amazon.com/s?k=...` is the query you were asked "
                "to run, handed back — it lands the user on a results page "
                "with the work still to do. Open the result and link the "
                "product page itself, with the price you read on it.\n"
                "A url that 404s has been dropped too. Copy the address from "
                "the page you actually opened — do not rebuild it from the "
                "product name. A guessed url is wrong even when the product "
                "is right: `/products/rustic-knit` looks correct and the site "
                "serves `/us/en/product/rustic-knit`."
            )}

        # A headline that claims a draft exists must carry the draft. The
        # Marcus Reed card said "Draft email ready; need his email address to
        # send" over four bullets DESCRIBING the email — the text itself had
        # been thrown away by a refused draft_message call. A description of
        # work is not the work.
        claims_draft = re.search(r"\bdraft(ed)?\b.{0,40}\b(ready|written|prepared)\b",
                                 f"{headline} {body}", re.I)
        has_message_text = any(len(s_) > 150 for s_ in _as_list(steps))
        if claims_draft and not has_message_text:
            return {"error": (
                "this says a draft is ready but the steps don't contain it — "
                "they describe it. Put the COMPLETE message text as steps[0] "
                "(move_kind='send', blocked_reason if it cannot be sent yet). "
                "A description of a draft is not a draft."
            )}

        # The worker runs on a schedule, so without this the same observation
        # is re-recorded every pass and the thread starts repeating itself.
        if db.finding_exists(thread_id, headline):
            return {"skipped": "this thread already has that finding", "headline": headline}

        refs = [{"kind": "item", "ref_id": i} for i in (evidence_item_ids or [])]
        refs += [{"kind": "message", "ref_id": m} for m in (evidence_message_ids or [])]
        finding = threads_mod.make_finding(
            thread_id, kind=kind, headline=_plain(headline), body=_plain(body),
            importance=importance, evidence=refs,
            move_kind=move_kind,
            steps=[{"text": _plain(s)} for s in _as_list(steps)],
            needs=[_plain(n) for n in _as_list(needs)],
            # A leaked `<parameter name="facts">` tail in the body was
            # carrying exactly the structured facts the call's own `facts`
            # argument lost — salvage them rather than render them as prose.
            facts=_as_facts(facts) or _as_facts(salvage_call_markup(body)),
            blocked_reason=_plain(blocked_reason) or None,
        )
        # Saved now, not after the run. The worker back-fills `loop_run_id`
        # when it has one, but a finding that only exists in memory is lost if
        # the run dies — and the dedupe check above reads the table, so a
        # single pass could otherwise record the same headline twice.
        db.save_finding(finding)
        # The newest of each kind is the thread's current picture; everything
        # before it becomes history rather than another row competing for the
        # screen. Done here rather than in `save_finding` because that write
        # path is also used to back-fill `loop_run_id` on a finding that is
        # already current — superseding there would retire the thread's move
        # every time the worker filled in its provenance.
        retired = db.supersede_findings(thread_id, kind, finding.id)
        if recorded is not None:
            recorded.append(finding)
        return {"recorded": finding.id, "kind": kind, "headline": headline,
                "superseded": retired}

    return [
        Tool(
            name="record_finding",
            description=(
                "Write back what this pass produced. Exactly one call per pass.\n"
                "kind='finding' — something the user would want to know, with "
                "specifics (names, dates, amounts) and why it matters to them.\n"
                "kind='action' — a MOVE: the specific next thing that would "
                "advance or end this loop, with the work already done. Needs "
                "move_kind, and needs either steps (what you actually staged) "
                "or blocked_reason (why you could not). It sits ready and is "
                "never sent or spent.\n"
                "kind='nothing' — you looked and there is nothing new. A real "
                "result: record it rather than inventing something to justify "
                "the pass.\n"
                "Cite what you read with evidence_item_ids / evidence_message_ids."
            ),
            input_schema=_obj(
                {
                    "headline": {"type": "string", "description": "one line, specific"},
                    "body": {"type": "string", "description": "what it means for the user"},
                    "kind": {"type": "string", "enum": ["finding", "action", "nothing"]},
                    "importance": {"type": "number", "description": "0-1, is this worth interrupting for"},
                    "evidence_item_ids": {"type": "array", "items": {"type": "string"}},
                    "evidence_message_ids": {"type": "array", "items": {"type": "string"}},
                    "move_kind": {
                        "type": "string",
                        "enum": ["send", "decide", "gather", "do"],
                        "description": (
                            "kind='action' only. send = a message is the move. "
                            "decide = blocked on a choice, so lay out the real "
                            "options. gather = no action exists and the "
                            "material is the value. do = the user must act "
                            "somewhere else (pay, upload, buy) and your job is "
                            "to leave only the part that needs them."
                        ),
                    },
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "The work you ALREADY DID, one item each: the "
                            "figures, the link, the draft, the options. Not a "
                            "plan for producing them. If these read like "
                            "instructions to yourself, you have not made a "
                            "move — record a finding instead."
                        ),
                    },
                    "needs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "What only the user can supply — a card, a signature, a decision.",
                    },
                    "blocked_reason": {
                        "type": "string",
                        "description": (
                            "Set when you can name the move but not stage it. "
                            "Saying so plainly is a real answer."
                        ),
                    },
                    "facts": {
                        "type": "array",
                        "description": (
                            "What you verified, as data rather than prose — and "
                            "legal on BOTH kinds, unlike steps. Every figure you "
                            "looked up goes here: one entry per product, price, "
                            "date or requirement, each with its source URL. A "
                            "price buried in a paragraph makes the user read and "
                            "re-derive; the same price here is a row they can "
                            "compare and a link they can tap. If you searched and "
                            "recorded no facts, you threw the search away."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string",
                                          "description": "What it is: \"Spalding 54\\\" polycarbonate\"."},
                                "value": {"type": "string",
                                          "description": "The figure and its condition: \"$350-450, ships 3-5 days\"."},
                                "url": {"type": "string",
                                        "description": "Where you found it. Omit only if there is genuinely no link."},
                            },
                            "required": ["label"],
                        },
                    },
                },
                required=["headline"],
            ),
            fn=record_finding,
        )
    ]


def watcher_tools(thread_id: str) -> List[Tool]:
    """`add_watcher` (§v2 step 6) — the primitive that makes the system
    proactive rather than responsive.

    A thread implies monitors nobody asked for: a trip implies watching the
    airline's mail, a bill implies watching for the payment confirmation, a
    date implies watching the date itself. Setting one costs nothing per check
    — the watcher is deterministic SQL, and only the evidence it turns up ever
    reaches a model.
    """
    from ..threads import watchers as watch_mod

    def add_watcher(
        what: str,
        kind: str = watch_mod.WatchKind.MAIL,
        query: Optional[str] = None,
        sender: Optional[str] = None,
        person_id: Optional[str] = None,
        days_before: Optional[float] = None,
        every_hours: float = 3.0,
        until: Optional[str] = None,
    ) -> dict:
        spec: Dict[str, Any] = {}
        for key, value in (("query", query), ("sender", sender),
                           ("person_id", person_id), ("days_before", days_before)):
            if value is not None:
                spec[key] = value
        try:
            watcher = watch_mod.add(
                thread_id, kind=kind, what=what, spec=spec,
                cadence_minutes=int(max(0.25, every_hours) * 60), until=until,
            )
        except Exception as exc:
            return {"error": str(exc)}
        return {"watching": watcher.id, "kind": watcher.kind, "what": watcher.what,
                "every_minutes": watcher.cadence_minutes}

    def list_watchers() -> List[dict]:
        return [
            {"id": w.id, "kind": w.kind, "what": w.what,
             "every_minutes": w.cadence_minutes, "times_fired": w.fire_count}
            for w in watch_mod.for_thread(thread_id)
        ]

    def stop_watching(watcher_id: str) -> dict:
        return {"stopped": watch_mod.remove(watcher_id)}

    return [
        Tool(
            name="add_watcher",
            description=(
                "Set a standing monitor this thread implies — something to keep "
                "an eye on without being asked again. Costs nothing per check.\n"
                "kind='mail': new email, by sender/domain or phrase. THE one for "
                "bills, confirmations, delivery and flight changes — those all "
                "arrive by email.\n"
                "kind='messages': new messages from a person or about a topic.\n"
                "kind='calendar': an event appearing or MOVING (a flight time "
                "changing is the same event with a new time).\n"
                "kind='deadline' with days_before: fires as the thread's own "
                "date approaches. The one that notices time passing when "
                "nothing arrives.\n"
                "Set `until` when the watch has an end — a monitor that "
                "outlives its reason is waste. Check list_watchers first; don't "
                "duplicate one that already exists."
            ),
            input_schema=_obj(
                {
                    "what": {"type": "string", "description": "plain words: what you're watching for"},
                    "kind": {"type": "string", "enum": ["mail", "messages", "calendar", "deadline"]},
                    "query": {"type": "string"},
                    "sender": {"type": "string", "description": "email address or domain"},
                    "person_id": {"type": "string"},
                    "days_before": {"type": "number", "description": "deadline watchers: fire this many days out"},
                    "every_hours": {"type": "number", "description": "cadence (default 3)"},
                    "until": {"type": "string", "description": "ISO date to stop"},
                },
                required=["what"],
            ),
            fn=add_watcher,
        ),
        Tool(
            name="list_watchers",
            description="What this thread is already watching. Check before adding.",
            input_schema=_obj({}),
            fn=list_watchers,
        ),
        Tool(
            name="stop_watching",
            description="Retire a monitor whose reason has passed.",
            input_schema=_obj({"watcher_id": {"type": "string"}}, required=["watcher_id"]),
            fn=stop_watching,
        ),
    ]


def scoped_for(thread, recorded_findings: Optional[List[Any]] = None) -> List[Tool]:
    """The tools one thread may use, at its own autonomy ceiling.

    `execute(tool, args)` takes a tool and arguments with no thread context, and
    the tool list handed to `run_loop` is flat and global — which the audit
    flagged as the reason the autonomy ladder had nowhere to live. This is that
    missing piece: the tool set becomes a function of the thread.

    The ceiling is user-set and never learned. `PREPARED` already needs no
    permission, so the only tier learning could promote *into* is `ASK` —
    spending money, things that are hard to undo — which would turn unrelated
    draft approvals into consent for irreversible acts. Learning may lower a
    thread, never raise it.

    Step 8's web tools ride this same gate, which is why it is built once here
    rather than twice later.
    """
    from ..models import Autonomy

    ceiling = getattr(thread, "autonomy", Autonomy.PREPARED)
    rank = Autonomy.ORDER.get(ceiling, Autonomy.ORDER[Autonomy.PREPARED])

    # Silent: reading. Never needs permission, always available.
    #
    # The web tools sit here, with the other reads, because that is what they
    # are: searching and fetching take nothing from the user, send nothing as
    # them, and spend nothing. What they *do* is carry a query outward, so the
    # worker's prompt carries the rule about what a query may contain.
    allowed = (
        list(READ_TOOLS)
        + web_tools()
        + thread_tools()
        + finding_tools(thread.id, recorded_findings, ceiling=ceiling)
        + watcher_tools(thread.id)
    )

    # Prepared: may also write something the user can review. Never sends.
    if rank >= Autonomy.ORDER[Autonomy.PREPARED]:
        allowed += draft_tools(thread=thread)

    # Ask: the rung stopped being a placeholder in v2.1. It does not add a
    # tool — it widens what `record_finding` will accept, to include `do`
    # moves: the shape that reaches outside the app, spends money, or cannot
    # be undone. See `finding_tools`, which holds the gate because the worker
    # needs that tool at every rung.
    return allowed


def by_name(registry: List[Any]) -> Dict[str, Tool]:
    """Only the tools this process has to execute. A `ServerTool` runs on the
    provider's side and never comes back as a call to dispatch, so including
    one here would put a name in the table with nothing behind it."""
    return {t.name: t for t in registry if isinstance(t, Tool)}


def execute(tool: Tool, args: Dict[str, Any]) -> str:
    """Run a tool and serialise its result for the model. Errors come back as
    text so the loop can route around a failed call instead of dying."""
    try:
        result = tool.fn(**args)
    except TypeError as exc:  # bad/missing args from the model
        return json.dumps({"error": f"bad arguments: {exc}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(result, default=str)
