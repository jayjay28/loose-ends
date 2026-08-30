"""A staged link has to actually go somewhere.

The failure these cover shipped to the user's phone: the EMES thread recorded
five product urls and every one 404'd. The search had genuinely run — all five
products were real and two slugs were character-exact — but Gemini returns
grounding *redirects* rather than destinations, so the model had no citable
address and rebuilt one from the product name, guessing `/products/<slug>`
where the site serves `/us/en/product/<slug>`.

`_is_search_url` did not and could not catch it: a fabricated product url is
perfectly well-formed. Shape cannot detect a plausible invention.
"""
from __future__ import annotations

import httpx
import pytest

from lifeline.assistant import registry
from lifeline.config import Config, get_config, set_config
from lifeline.extraction import gemini


@pytest.fixture(autouse=True)
def verifying():
    """Turn the guard on.

    conftest runs the whole suite with `LIFELINE_OFFLINE=1`, which switches
    verification off so no test reaches the network. These tests are the
    exception — they need the guard live, and stub `httpx` themselves.
    """
    cfg = get_config()
    set_config(Config(db_path=cfg.db_path, offline_extraction=False, verify_urls=True))
    registry._PROBED.clear()
    registry._IS_IMAGE.clear()
    gemini._RESOLVED.clear()
    yield
    registry._PROBED.clear()
    registry._IS_IMAGE.clear()
    gemini._RESOLVED.clear()


PAGE = (
    b"<html><head>"
    b'<meta content="Rustic Off Sand Knit Sweater - Always Grateful" property="og:title"/>'
    b'<meta content="https://cdn.shopify.com/x.webp?v=1&amp;width=1200" property="og:image"/>'
    b"</head><body>...</body></html>"
)


class _Stream:
    """The bit of httpx.stream's context manager that `_probe_url` uses."""

    def __init__(self, status, body=b""):
        self.status_code = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self, n=8192):
        for i in range(0, len(self._body) or 1, n):
            yield self._body[i:i + n]


def _answers(monkeypatch, status=None, exc=None, body=b"", image_type="image/webp"):
    """Stub the two requests a probe makes, and count the page fetch.

    The page itself is streamed (`_probe_url` needs the body for the og tags);
    the image url is then HEADed to confirm it returns a picture
    (`_serves_an_image`). Only the stream is counted — that is the expensive
    one and the one whose count matters.
    """
    calls = {"n": 0}

    def fake_stream(method, url, **kw):
        calls["n"] += 1
        if exc is not None:
            raise exc
        return _Stream(status, body)

    monkeypatch.setattr(httpx, "stream", fake_stream)
    monkeypatch.setattr(
        httpx, "head",
        lambda url, **kw: httpx.Response(
            200, headers={"content-type": image_type},
            request=httpx.Request("HEAD", url)),
    )
    return calls


def _heads(monkeypatch, status=200, exc=None, final_url=None):
    """Stub `httpx.head` — how `gemini._resolve` follows a grounding redirect.

    That one genuinely wants HEAD: it needs where the redirect lands, not what
    the page says.
    """
    calls = {"n": 0}

    def fake_head(url, **kw):
        calls["n"] += 1
        if exc is not None:
            raise exc
        return httpx.Response(status, request=httpx.Request("HEAD", final_url or url))

    monkeypatch.setattr(httpx, "head", fake_head)
    return calls


# --------------------------------------------------------------- the predicate
def test_a_404_is_dead(monkeypatch):
    _answers(monkeypatch, status=404)
    assert registry._url_is_live("https://emestudios.com/products/rustic-knit") is False


def test_a_200_is_live(monkeypatch):
    _answers(monkeypatch, status=200)
    assert registry._url_is_live("https://emestudios.com/us/en/product/rustic-knit") is True


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_a_blocked_or_broken_host_is_treated_as_live(monkeypatch, status):
    """Bot walls and outages are not missing pages. Refusing these would throw
    away real research every time a shop dislikes a scripted request."""
    _answers(monkeypatch, status=status)
    assert registry._url_is_live("https://shop.example/p/thing") is True


def test_a_timeout_fails_open(monkeypatch):
    _answers(monkeypatch, exc=httpx.ConnectTimeout("slow"))
    assert registry._url_is_live("https://shop.example/p/thing") is True


def test_the_answer_is_cached(monkeypatch):
    calls = _answers(monkeypatch, status=404)
    for _ in range(4):
        registry._url_is_live("https://shop.example/gone")
    assert calls["n"] == 1, "one finding must not become four requests at a shop"


def test_verification_is_skipped_when_disabled(monkeypatch):
    cfg = get_config()
    set_config(Config(db_path=cfg.db_path, offline_extraction=False, verify_urls=False))
    calls = _answers(monkeypatch, status=404)
    assert registry._url_is_live("https://shop.example/gone") is True
    assert calls["n"] == 0


def test_offline_mode_never_touches_the_network(monkeypatch):
    """The guarantee the rest of the suite relies on."""
    cfg = get_config()
    set_config(Config(db_path=cfg.db_path, offline_extraction=True, verify_urls=True))
    calls = _answers(monkeypatch, status=404)
    assert registry._url_is_live("https://shop.example/gone") is True
    assert calls["n"] == 0, "offline must mean offline"


def test_non_http_is_left_alone(monkeypatch):
    calls = _answers(monkeypatch, status=404)
    assert registry._url_is_live("mailto:someone@example.com") is True
    assert calls["n"] == 0


# ------------------------------------------------------------------ the wiring
def test_a_dead_url_is_demoted_but_the_fact_survives(monkeypatch):
    """Demotion, not deletion — the text is still worth reading, and the empty
    url is what trips the priced guard downstream."""
    _answers(monkeypatch, status=404)
    facts = registry._as_facts(
        [{"label": "Rustic Knit", "value": "$109.00",
          "url": "https://emestudios.com/products/rustic-off-sand-knit-sweater"}]
    )
    assert facts[0]["label"] == "Rustic Knit"
    assert facts[0]["value"] == "$109.00"
    assert facts[0]["url"] == ""


def test_a_live_url_is_kept(monkeypatch):
    _answers(monkeypatch, status=200)
    facts = registry._as_facts(
        [{"label": "Rustic Knit", "value": "$109.00",
          "url": "https://emestudios.com/us/en/product/rustic-off-sand-knit-sweater"}]
    )
    assert facts[0]["url"].endswith("/us/en/product/rustic-off-sand-knit-sweater")


def test_search_urls_are_still_demoted_without_a_request(monkeypatch):
    """The cheap shape check must short-circuit the network one."""
    calls = _answers(monkeypatch, status=200)
    facts = registry._as_facts(
        [{"label": "hoops", "value": "$299", "url": "https://amazon.com/s?k=glass+hoop"}]
    )
    assert facts[0]["url"] == ""
    assert calls["n"] == 0


def test_the_priced_guard_fires_once_the_links_are_gone(monkeypatch):
    """The whole point of demoting rather than erroring: `_has_link` goes false
    and the existing refusal does the talking."""
    _answers(monkeypatch, status=404)
    facts = registry._as_facts(
        [{"label": "Rustic Knit", "value": "$109.00",
          "url": "https://emestudios.com/products/rustic-knit"}]
    )
    assert registry._has_link([], facts) is False


# ----------------------------------------------------------- images and labels
def test_the_page_image_is_carried_onto_the_fact(monkeypatch):
    _answers(monkeypatch, status=200, body=PAGE)
    facts = registry._as_facts(
        [{"label": "Rustic Off Sand Knit Sweater", "value": "$109.00",
          "url": "https://emestudios.com/us/en/product/rustic-off-sand-knit-sweater"}]
    )
    # `&amp;` in a meta tag would break the CDN url it appears in.
    assert facts[0]["image"] == "https://cdn.shopify.com/x.webp?v=1&width=1200"


def test_a_dead_page_contributes_no_image(monkeypatch):
    _answers(monkeypatch, status=404, body=PAGE)
    facts = registry._as_facts(
        [{"label": "Rustic Knit", "value": "$109.00",
          "url": "https://emestudios.com/products/rustic-knit"}]
    )
    assert facts[0]["url"] == ""
    assert facts[0]["image"] == ""


def test_the_image_never_comes_from_the_model(monkeypatch):
    """Trusting a model-supplied image url would rebuild the exact bug this
    guard exists to catch."""
    _answers(monkeypatch, status=200, body=PAGE)
    facts = registry._as_facts(
        [{"label": "Rustic Knit", "value": "$109.00",
          "url": "https://emestudios.com/us/en/product/rustic-off-sand-knit-sweater",
          "image": "https://emestudios.com/invented-by-the-model.jpg"}]
    )
    assert "invented" not in facts[0]["image"]


def test_the_page_corrects_a_compressed_label(monkeypatch):
    """The real failure: a hoodie and a crewneck are different garments."""
    body = PAGE.replace(
        b"Rustic Off Sand Knit Sweater - Always Grateful",
        b"Building Heather Grey Oversized Crewneck - Always Grateful",
    )
    _answers(monkeypatch, status=200, body=body)
    facts = registry._as_facts(
        [{"label": "Building Oversized Hoodie", "value": "$109.00",
          "url": "https://emestudios.com/us/en/product/building-heather-grey-oversized-crewneck"}]
    )
    assert facts[0]["label"] == "Building Heather Grey Oversized Crewneck"


@pytest.mark.parametrize("title,expected", [
    ("Cookie Preferences", "Rustic Knit"),                      # unrelated
    ("", "Rustic Knit"),                                        # no title
    ("Rustic Knit", "Rustic Knit"),                             # identical
    ("Rustic Knit Sweater - Always Grateful", "Rustic Knit Sweater"),
    ("Shop", "Rustic Knit"),                                    # generic
])
def test_a_label_is_only_replaced_by_a_title_about_the_same_thing(title, expected):
    assert registry._corrected_label("Rustic Knit", title) == expected


def test_a_sentence_is_not_a_product_name():
    long_title = "Rustic " + "very " * 30 + "long"
    assert registry._corrected_label("Rustic Knit", long_title) == "Rustic Knit"


def test_one_probe_serves_both_the_liveness_check_and_the_image(monkeypatch):
    calls = _answers(monkeypatch, status=200, body=PAGE)
    url = "https://emestudios.com/us/en/product/rustic-off-sand-knit-sweater"
    registry._as_facts([{"label": "Rustic", "value": "$1", "url": url}])
    assert calls["n"] == 1, "liveness, image and title must share one request"


def test_an_og_image_that_serves_html_is_dropped(monkeypatch):
    """Victoria's Secret answers its own og:image url with 4.5KB of HTML. A
    well-formed tag pointing at nothing renderable is still nothing."""
    _answers(monkeypatch, status=200, body=PAGE, image_type="text/html; charset=UTF-8")
    facts = registry._as_facts(
        [{"label": "Satin Set", "value": "$69", "url": "https://vs.example/us/vs/pajamas"}]
    )
    assert facts[0]["image"] == ""
    assert facts[0]["url"], "only the picture is refused, not the link"


def test_an_unreachable_image_fails_closed(monkeypatch):
    """The opposite of the liveness rule, on purpose: a missing picture costs a
    row nothing, a broken one costs it its credibility."""
    _answers(monkeypatch, status=200, body=PAGE)
    monkeypatch.setattr(
        httpx, "head",
        lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")),
    )
    facts = registry._as_facts(
        [{"label": "Knit", "value": "$1", "url": "https://shop.example/p/knit"}]
    )
    assert facts[0]["image"] == ""


def test_one_image_across_several_rows_is_the_site_not_the_product():
    """Five pyjama sets that all show the same picture read as one product
    listed five times — worse than no pictures at all."""
    facts = registry._drop_shared_images([
        {"label": "A", "value": "$1", "url": "u1", "image": "https://x/card.jpg"},
        {"label": "B", "value": "$2", "url": "u2", "image": "https://x/card.jpg"},
        {"label": "C", "value": "$3", "url": "u3", "image": "https://x/real.jpg"},
    ])
    assert [f["image"] for f in facts] == ["", "", "https://x/real.jpg"]


def test_a_bare_domain_is_not_a_product_link():
    """`https://www.amazon.com` offered as a product link. Two stored findings
    do exactly this."""
    facts = registry._drop_shared_images([
        {"label": "Ekouaer Long-Sleeved", "value": "$26", "url": "https://www.amazon.com", "image": "https://x/a.jpg"},
    ])
    assert facts[0]["url"] == ""
    assert facts[0]["image"] == ""


def test_a_link_two_different_products_share_is_neither_one_s():
    """The clearance-page failure: three named sets, one category url. It is
    live and it is not a search url, so nothing else catches it."""
    shared = "https://www.victoriassecret.com/us/vs/sale/clearance-sleep"
    facts = registry._drop_shared_images([
        {"label": "Signature Satin Long", "value": "$69", "url": shared, "image": "https://x/a.jpg"},
        {"label": "SoSoft Modal 3-Piece", "value": "$49", "url": shared, "image": "https://x/b.jpg"},
        {"label": "Rustic Knit", "value": "$109", "url": "https://shop.example/p/knit", "image": "https://x/c.jpg"},
    ])
    assert [f["url"] for f in facts] == ["", "", "https://shop.example/p/knit"]
    assert [f["image"] for f in facts] == ["", "", "https://x/c.jpg"]


def test_the_same_product_listed_twice_keeps_its_link():
    """Identical labels are a duplicated row, not evidence about the url."""
    same = "https://tickets.example/beres-hammond/1179841"
    facts = registry._drop_shared_images([
        {"label": "Beres Hammond Tickets", "value": "$85", "url": same, "image": ""},
        {"label": "Beres Hammond Tickets", "value": "$140", "url": same, "image": ""},
    ])
    assert all(f["url"] == same for f in facts)


def test_a_deep_path_on_a_shop_is_left_alone():
    facts = registry._drop_shared_images([
        {"label": "Rustic Knit", "value": "$109",
         "url": "https://emestudios.com/us/en/product/rustic-off-sand-knit-sweater", "image": ""},
    ])
    assert facts[0]["url"].endswith("rustic-off-sand-knit-sweater")


def test_distinct_images_all_survive():
    facts = registry._drop_shared_images([
        {"label": "A", "value": "$1", "url": "u1", "image": "https://x/a.jpg"},
        {"label": "B", "value": "$2", "url": "u2", "image": "https://x/b.jpg"},
    ])
    assert [f["image"] for f in facts] == ["https://x/a.jpg", "https://x/b.jpg"]


# ------------------------------------------------------- gemini grounding urls
def test_grounding_redirects_resolve_to_real_pages(monkeypatch):
    real = "https://emestudios.com/us/en/product/rustic-off-sand-knit-sweater"
    _heads(monkeypatch, status=200, final_url=real)
    sources = gemini._grounded_sources(
        {"candidates": [{"groundingMetadata": {"groundingChunks": [
            {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbC",
                     "title": "EME Studios"}}
        ]}}]}
    )
    assert sources == [{"title": "EME Studios", "url": real}]


def test_an_unresolvable_redirect_falls_back_to_itself(monkeypatch):
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbC"
    _heads(monkeypatch, exc=httpx.ConnectError("nope"))
    sources = gemini._grounded_sources(
        {"candidates": [{"groundingMetadata": {"groundingChunks": [
            {"web": {"uri": redirect, "title": "EME Studios"}}
        ]}}]}
    )
    assert sources[0]["url"] == redirect


def test_no_grounding_metadata_is_not_an_error():
    assert gemini._grounded_sources({"candidates": [{"content": {"parts": []}}]}) == []
    assert gemini._grounded_sources({}) == []


def test_duplicate_sources_are_resolved_once(monkeypatch):
    calls = _heads(monkeypatch, status=200, final_url="https://shop.example/p/1")
    uri = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/Same"
    sources = gemini._grounded_sources(
        {"candidates": [{"groundingMetadata": {"groundingChunks": [
            {"web": {"uri": uri, "title": "a"}},
            {"web": {"uri": uri, "title": "a"}},
        ]}}]}
    )
    assert len(sources) == 1
    assert calls["n"] == 1
