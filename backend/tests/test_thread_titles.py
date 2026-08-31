"""Thread headlines (`eval-titles-are-raw-sentences`, filed 08-11).

The user's sentence is the declaration and survives as the summary; the feed
title is a generated headline. Declares stay instant (the retitle is async),
a manual rename is never overwritten, and system-made threads are untouched.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lifeline import db, threads
from lifeline.api.app import app
from lifeline.extraction import providers
from lifeline.threads import titles


@pytest.fixture
def client():
    return TestClient(app)


PAJAMAS = ("Nia is continuously asking me to find some sexy pajamas, "
           "or a matching set. So I wanna get like 3 sets for between $100-150.")


def _provider_says(monkeypatch, title):
    monkeypatch.setattr(providers, "run",
                        lambda fn, kind=None, **kw: json.dumps({"title": title}))


def test_the_sentence_becomes_the_summary_and_a_headline_takes_the_feed(monkeypatch):
    _provider_says(monkeypatch, "Pajama sets for Nia")
    thread = threads.create(title=PAJAMAS)

    assert titles.retitle(thread.id) is True
    after = db.get_thread(thread.id)
    assert after.title == "Pajama sets for Nia"
    assert after.summary == PAJAMAS, "the declaration survives, verbatim"


def test_declare_over_http_retitles_in_the_background(client, monkeypatch):
    """TestClient runs BackgroundTasks after the response — the declare
    response carries the typed words (the arrival animation flies them), and
    the stored thread already wears the headline by the next fetch."""
    _provider_says(monkeypatch, "Basketball hoop for Milo")
    body = client.post("/threads", json={
        "title": "I need to get Milo (my son) a basketball hoop."}).json()
    assert body["title"].startswith("I need to get Milo"), "response is instant"

    stored = db.get_thread(body["id"])
    assert stored.title == "Basketball hoop for Milo"
    assert stored.summary.startswith("I need to get Milo")


def test_a_manual_rename_is_never_overwritten(monkeypatch):
    _provider_says(monkeypatch, "Something else entirely")
    thread = threads.create(title=PAJAMAS)
    threads.update(thread.id, title="Pajamas")          # the user renamed it

    assert titles.retitle(thread.id) is False, "six clean words are left alone"
    assert db.get_thread(thread.id).title == "Pajamas"


def test_system_threads_and_headline_shaped_titles_are_untouched(monkeypatch):
    _provider_says(monkeypatch, "should never be used")
    from lifeline.models import ThreadOrigin

    silence = threads.create(title="Tess went quiet", origin=ThreadOrigin.SILENCE)
    assert titles.retitle(silence.id) is False

    assert titles.looks_raw("Pajama sets for Nia") is False
    assert titles.looks_raw(PAJAMAS) is True
    assert titles.looks_raw("I need to start my blog. Deploy it publicly.") is True


def test_no_provider_means_no_change_and_the_sweep_catches_up(monkeypatch):
    monkeypatch.setattr(providers, "run", lambda fn, kind=None, **kw: None)
    thread = threads.create(title=PAJAMAS)
    assert titles.retitle(thread.id) is False
    assert db.get_thread(thread.id).title == PAJAMAS, "raw beats wrong"

    # A provider comes back; the poller's sweep finishes the job.
    _provider_says(monkeypatch, "Pajama sets for Nia")
    assert titles.sweep() == 1
    assert db.get_thread(thread.id).title == "Pajama sets for Nia"


def test_garbage_headlines_are_refused(monkeypatch):
    thread = threads.create(title=PAJAMAS)
    _provider_says(monkeypatch, "")
    assert titles.retitle(thread.id) is False
    _provider_says(monkeypatch, "a headline that runs on far too long to ever fit a feed row at all")
    assert titles.retitle(thread.id) is False
    assert db.get_thread(thread.id).title == PAJAMAS
