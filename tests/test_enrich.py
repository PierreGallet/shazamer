"""Enrichment: choosing the right record, and not asking twice.

The MusicBrainz client is exercised against recorded responses rather than the
live API — the service is rate-limited and its data shifts, neither of which
belongs in a test suite. What is pinned here is the judgement: which recording
of a song to describe, which release to take it from, and when not to ask at
all.
"""
from pathlib import Path
from typing import Optional

import pytest

from src.enrich.base import TrackMeta
from src.enrich.musicbrainz import MusicBrainzEnricher
from src.enrich.runner import enrich_set
from src.identify.base import normalize_key
from src.store.library import Library

pytestmark = pytest.mark.anyio


def _release(date, label="", catalog="", secondary=None, title="Release"):
    return {
        "id": f"rel-{date}-{label}", "date": date, "title": title,
        "label-info": [{"label": {"name": label}, "catalog-number": catalog}]
        if label else [],
        "release-group": {"primary-type": "Album",
                          "secondary-types": secondary or []},
    }


def test_a_compilation_never_wins_over_the_original():
    """The catalogue number of a "Best Of" helps nobody find the 12-inch."""
    chosen = MusicBrainzEnricher._pick_release([
        _release("2019-01-01", "Big Comp Co", "COMP99", ["Compilation"]),
        _release("2015-06-01", "Rhythm Section", "RS008"),
    ])
    assert chosen["label-info"][0]["catalog-number"] == "RS008"


def test_a_dj_mix_never_wins_either():
    """A track lifted onto a fabric mix was not released by fabric."""
    chosen = MusicBrainzEnricher._pick_release([
        _release("2021-07-16", "fabric", "FABRIC01", ["Compilation", "DJ-mix"]),
        _release("2021-06-18", "Poly Kicks", "POLY015"),
    ])
    assert chosen["label-info"][0]["label"]["name"] == "Poly Kicks"


def test_the_earliest_labelled_release_wins():
    chosen = MusicBrainzEnricher._pick_release([
        _release("2020-01-01", "Reissue Records", "RE002"),
        _release("2003-05-05", "Original Records", "OG001"),
    ])
    assert chosen["date"] == "2003-05-05"


def test_an_unlabelled_release_is_a_last_resort():
    chosen = MusicBrainzEnricher._pick_release([
        _release("2001-01-01"),                       # earlier, but no label
        _release("2004-01-01", "Some Label", "SL1"),
    ])
    assert chosen["label-info"][0]["label"]["name"] == "Some Label"


def test_no_releases_means_no_choice():
    assert MusicBrainzEnricher._pick_release([]) is None


def test_the_recording_with_real_releases_wins():
    """A popular track has one released recording and several mix recordings.

    They routinely all score 100, so taking the first is a coin toss — and it
    produced different answers on consecutive runs, once naming a Boiler Room
    set as the release.
    """
    released = {"id": "real", "score": 100, "releases": [
        _release("2021-06-18", "Poly Kicks", "POLY015"),
        _release("2023-05-12", "XL Recordings", "XL1"),
    ]}
    broadcast = {"id": "mix", "score": 100, "releases": [
        _release("2021-07-16", "fabric", "F1", ["Compilation", "DJ-mix"]),
    ]}

    assert MusicBrainzEnricher._pick_recording([broadcast, released])["id"] == "real"
    # Order must not matter, or the result is a coin toss again.
    assert MusicBrainzEnricher._pick_recording([released, broadcast])["id"] == "real"


def test_a_weak_match_is_refused():
    """A wrong label is worse than none: it sends you hunting for a ghost."""
    assert MusicBrainzEnricher._pick_recording(
        [{"id": "x", "score": 40, "releases": []}]) is None


def test_metadata_never_overwrites_what_the_identifier_found():
    """Shazam read the audio; this read a name. The audio wins."""
    meta = TrackMeta(label="Guessed Records", year="1999", catalog_number="G1")
    written = meta.merged_over({"label": "Known Label", "year": ""})

    assert "label" not in written, "an existing label was overwritten"
    assert written["year"] == "1999"
    assert written["catalog_number"] == "G1"


class FakeEnricher:
    """Answers from a table, and counts how often it is asked."""

    name = "fake"

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def lookup(self, artist: str, title: str,
                     isrc: str = "") -> Optional[TrackMeta]:
        self.calls.append((artist, title))
        found = self.answers.get((artist, title))
        return TrackMeta(**found, provider="fake") if found else None


def _set_payload(tracks):
    return {"duration": 600, "waveform": [], "stats": {}, "tracks": [
        {"index": i + 1, "start": i * 100, "end": (i + 1) * 100,
         "start_label": "00:00:00", "duration": 100, "identified": True,
         "title": t, "artist": a, "key": normalize_key(a, t), "album": "",
         "label": "", "year": "", "genre": "", "isrc": "", "url": "",
         "cover_url": "", "bpm": None, "camelot": None, "musical_key": None,
         "confidence": 1.0, "strength": "strong"}
        for i, (a, t) in enumerate(tracks)]}


async def test_enrichment_fills_in_missing_metadata(tmp_path):
    library = Library(tmp_path / "lib.db")
    await library.save_set("s1", "Set", _set_payload([("Skee Mask", "Rev8617")]))

    enricher = FakeEnricher({("Skee Mask", "Rev8617"): {
        "label": "Ilian Tape", "catalog_number": "ITLP04", "year": "2018"}})
    report = await enrich_set(library, enricher, "s1")

    assert report.found == 1
    track = (await library.get_set("s1"))["tracks"][0]
    assert track["label"] == "Ilian Tape"
    assert track["catalog_number"] == "ITLP04"
    assert track["year"] == "2018"


async def test_a_track_in_several_sets_is_looked_up_once(tmp_path):
    """One lookup, every set updated — the point of keying on the track."""
    library = Library(tmp_path / "lib.db")
    shared = ("Skee Mask", "Rev8617")
    await library.save_set("s1", "One", _set_payload([shared, ("A", "B")]))
    await library.save_set("s2", "Two", _set_payload([shared]))

    enricher = FakeEnricher({shared: {"label": "Ilian Tape", "year": "2018"}})
    await enrich_set(library, enricher, "s1")
    asked_first = list(enricher.calls)
    await enrich_set(library, enricher, "s2")

    assert enricher.calls == asked_first, "the second set asked again"
    assert (await library.get_set("s2"))["tracks"][0]["label"] == "Ilian Tape"


async def test_a_miss_is_remembered_so_it_is_not_re_asked(tmp_path):
    """White labels and dubs will never be in MusicBrainz.

    Re-asking on every run is how you get rate-limited for nothing.
    """
    library = Library(tmp_path / "lib.db")
    await library.save_set("s1", "Set", _set_payload([("Unknown", "Dub Plate")]))

    enricher = FakeEnricher({})
    await enrich_set(library, enricher, "s1")
    assert len(enricher.calls) == 1

    await enrich_set(library, enricher, "s1")
    assert len(enricher.calls) == 1, "a known miss was looked up again"


async def test_tracks_that_already_have_a_label_are_skipped(tmp_path):
    library = Library(tmp_path / "lib.db")
    payload = _set_payload([("Artist", "Track")])
    payload["tracks"][0]["label"] = "Already Known"
    await library.save_set("s1", "Set", payload)

    enricher = FakeEnricher({("Artist", "Track"): {"label": "Something Else"}})
    await enrich_set(library, enricher, "s1")

    assert enricher.calls == []
    assert (await library.get_set("s1"))["tracks"][0]["label"] == "Already Known"


async def test_unidentified_tracks_are_not_looked_up(tmp_path):
    library = Library(tmp_path / "lib.db")
    payload = _set_payload([("", "")])
    payload["tracks"][0].update({"identified": False, "key": ""})
    await library.save_set("s1", "Set", payload)

    enricher = FakeEnricher({})
    report = await enrich_set(library, enricher, "s1")
    assert enricher.calls == [] and report.looked_up == 0
