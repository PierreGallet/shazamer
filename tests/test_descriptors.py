"""Describing a downloaded file: tags, Discogs, and the audio itself."""
import asyncio
from pathlib import Path

import pytest

from src.acquire.describe import SweepReport, describe_one, genre_from_tags, sweep
from src.core import descriptors as audio_descriptors
from src.core.descriptors import Descriptors, camelot
from src.enrich.discogs import DiscogsEnricher, Style, clean

pytestmark = pytest.mark.asyncio


# ── Camelot ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key, scale, expected", [
    ("Ab", "minor", "1A"),
    ("G#", "minor", "1A"),        # Essentia spells this both ways
    ("C", "major", "8B"),
    ("F#", "minor", "11A"),
    ("Gb", "minor", "11A"),
    ("", "minor", ""),
    ("H", "minor", ""),           # not a note
])
def test_enharmonic_spellings_land_on_one_wheel_position(key, scale, expected):
    """A key spelled two ways is one key.

    Essentia names roots with sharps or flats depending on the profile, and a
    lookup table that knows only one spelling silently drops half the results —
    which reads as "no key found" rather than as a bug.
    """
    assert camelot(key, scale) == expected


# ── File tags ─────────────────────────────────────────────────────────────

def test_a_file_with_no_tags_says_so_rather_than_raising(tmp_path):
    empty = tmp_path / "not-really-audio.mp3"
    empty.write_bytes(b"\x00" * 64)
    assert genre_from_tags(empty) == ""


def test_id3_filler_is_not_a_genre(monkeypatch):
    """"Other", "Unknown" and bare numbers are what rippers leave behind.

    Storing them would put "Other" in a column meant for browsing a crate, and
    a wrong label is worse than a blank one because it can be filtered on.
    """
    import sys

    def with_genre(value):
        module = type("M", (), {})()
        module.File = staticmethod(
            lambda *a, **k: type("F", (), {"get": lambda _s, _n: [value]})())
        monkeypatch.setitem(sys.modules, "mutagen", module)

    for filler in ("Other", "unknown", "None", "17", ""):
        with_genre(filler)
        assert genre_from_tags(Path("anything.mp3")) == "", filler

    with_genre("Deep House")
    assert genre_from_tags(Path("anything.mp3")) == "Deep House"


# ── Discogs query shaping ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("Jansons - Boxed (Original Mix)", "Jansons Boxed"),
    ("Amino feat. Lee Burton", "Amino"),
    ("Cohérent", "Coherent"),
    # ø and æ are letters, not accented vowels — NFKD leaves them alone and
    # this would search for "Mller" without an explicit table.
    ("Møller & Æther", "Moller AEther"),
    ("", ""),
])
def test_a_search_term_keeps_its_words_whole(raw, expected):
    """Folding accents must not shorten words.

    The same lesson the Soulseek query learned: "cohérent" has to become
    "coherent", never "cohrent". A character removed from inside a word makes
    a different search, not a looser one.
    """
    assert clean(raw) == expected


async def test_discogs_prefers_a_release_carrying_the_artist():
    """Guards against a compilation's genre list landing on one record.

    Discogs often returns a hundred-track compilation that merely contains the
    track, ranked first. Its style list describes the compilation.
    """
    client = DiscogsEnricher(token="x")

    async def answer(_path, _params, attempts=3):
        return {"results": [
            {"title": "Various - Ibiza Anthems 2019",
             "genre": ["Electronic"], "style": ["House", "Trance", "Pop"]},
            {"title": "Jansons - Boxed",
             "genre": ["Electronic"], "style": ["Tech House"]},
        ]}

    client._get = answer
    found = await client.lookup("Jansons", "Boxed")
    assert found is not None
    assert found.style == "Tech House"
    assert found.source == "discogs"


async def test_discogs_saying_nothing_is_not_the_same_as_no_style():
    """A request that could not be made must not read as an answer."""
    client = DiscogsEnricher(token="x")

    async def silence(_path, _params, attempts=3):
        return None

    client._get = silence
    assert await client.lookup("Jansons", "Boxed") is None

    async def nothing_found(_path, _params, attempts=3):
        return {"results": []}

    client._get = nothing_found
    assert await client.lookup("Jansons", "Boxed") is None


# ── The sweep ─────────────────────────────────────────────────────────────

class FakeLibrary:
    def __init__(self, rows):
        self.rows = rows
        self.updates = {}

    async def undescribed_downloads(self, *, user_id=None, limit=500):
        return list(self.rows)

    async def update_download(self, download_id, **fields):
        self.updates.setdefault(download_id, {}).update(fields)

    async def recent_downloads(self, limit=50, *, user_id=""):
        return [{**r, **self.updates.get(r["id"], {})} for r in self.rows]


class SilentDiscogs:
    async def lookup(self, artist, title):
        return None


async def test_a_file_that_vanished_is_skipped_not_failed(tmp_path):
    """Downloads are swept for disk space; the row outlives the bytes.

    Counting that as a failure would put a permanent error beside a record
    whose only problem is that it was tidied away.
    """
    library = FakeLibrary([{"id": 1, "artist": "A", "title": "T",
                            "local_path": str(tmp_path / "gone.mp3"),
                            "track_key": "a::t"}])
    report = await sweep(library, use_discogs=False)
    assert (report.skipped, report.failed, report.described) == (1, 0, 0)
    assert library.updates == {}, "a vanished file must not be stamped analysed"


async def test_a_described_file_is_never_measured_twice(tmp_path, monkeypatch):
    """`analysed_at` is stamped even when nothing was found.

    Otherwise a record whose tempo genuinely cannot be detected is re-measured
    on every sweep for ever, at six seconds a go.
    """
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"\x00" * 1024)
    library = FakeLibrary([{"id": 7, "artist": "A", "title": "T",
                            "local_path": str(audio), "track_key": "a::t"}])

    monkeypatch.setattr(audio_descriptors, "describe", lambda _p: None)
    monkeypatch.setattr(audio_descriptors, "available", lambda: True)

    await sweep(library, use_discogs=False)
    assert library.updates[7]["analysed_at"], "nothing recorded the attempt"


async def test_discogs_silence_leaves_the_tag_alone(tmp_path, monkeypatch):
    """The whole reason `lookup` returns None instead of an empty Style."""
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"\x00" * 1024)
    library = FakeLibrary([{"id": 3, "artist": "A", "title": "T",
                            "local_path": str(audio), "track_key": "a::t"}])

    import src.acquire.describe as describe_module
    monkeypatch.setattr(describe_module, "genre_from_tags",
                        lambda _p: "Deep House")
    monkeypatch.setattr(audio_descriptors, "describe", lambda _p: None)

    await describe_one(library, library.rows[0], SilentDiscogs())
    assert library.updates[3]["genre"] == "Deep House"
    assert library.updates[3]["style_source"] == "tag"


async def test_discogs_overrides_a_tag_and_says_it_did(tmp_path, monkeypatch):
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"\x00" * 1024)
    library = FakeLibrary([{"id": 4, "artist": "Jansons", "title": "Boxed",
                            "local_path": str(audio), "track_key": "j::b"}])

    import src.acquire.describe as describe_module
    monkeypatch.setattr(describe_module, "genre_from_tags", lambda _p: "Dance")
    monkeypatch.setattr(audio_descriptors, "describe", lambda _p: None)

    class Found:
        async def lookup(self, artist, title):
            return Style(genre="Electronic", style="Tech House",
                         source="discogs")

    await describe_one(library, library.rows[0], Found())
    assert library.updates[4]["genre"] == "Electronic"
    assert library.updates[4]["style"] == "Tech House"
    assert library.updates[4]["style_source"] == "discogs"


async def test_a_measurement_is_stored_field_by_field(tmp_path, monkeypatch):
    """A truncated file yielding a tempo and no key stores the tempo."""
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"\x00" * 1024)
    library = FakeLibrary([{"id": 9, "artist": "A", "title": "T",
                            "local_path": str(audio), "track_key": "a::t"}])

    monkeypatch.setattr(audio_descriptors, "describe",
                        lambda _p: Descriptors(bpm=129.1, loudness_lufs=-14.3))
    await describe_one(library, library.rows[0], None)
    stored = library.updates[9]
    assert stored["bpm"] == 129.1
    assert stored["loudness_lufs"] == -14.3
    assert "musical_key" not in stored, "an empty key was stored as a value"


async def test_essentia_missing_is_reported_not_raised(tmp_path, monkeypatch):
    """Every mac dev box takes this path."""
    monkeypatch.setattr(audio_descriptors, "available", lambda: False)
    library = FakeLibrary([])
    report = await sweep(library, use_discogs=False)
    assert report.unavailable is True
    assert report.queued == 0


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
async def test_describe_returns_nothing_without_essentia(monkeypatch, tmp_path):
    monkeypatch.setattr(audio_descriptors, "_AVAILABLE", False)
    assert audio_descriptors.describe(tmp_path / "anything.mp3") is None
    import numpy as np
    assert audio_descriptors.tempo_of(np.zeros(44100 * 6, dtype=np.float32),
                                      44100) is None


# ── Against Essentia itself, where it is installed ────────────────────────
#
# Marked `integration` and skipped everywhere Essentia is absent, which is
# every mac. These are the criteria that cannot be checked with a stub: that
# the six targeted algorithms return what `MusicExtractor` returns, and that
# a tempo survives the resample the pipeline's 22.05 kHz buffer needs.

essentia_only = pytest.mark.skipif(
    not audio_descriptors.available(), reason="Essentia is not installed here")


def _click_track(bpm: float, seconds: float, sample_rate: int = 44100):
    """A click at an exact tempo. Ground truth that fits in a repository.

    The real validation ran on ten windows of a 55-minute set and is recorded
    in the PR — but that audio cannot be committed, and a test whose fixture
    lives on one laptop is not a test. A click track is unambiguous, and any
    estimator that cannot find 128 in it is broken in a way worth catching.
    """
    import numpy as np

    n = int(sample_rate * seconds)
    signal = np.zeros(n, dtype=np.float32)
    period = int(sample_rate * 60.0 / bpm)
    # A short decaying burst rather than an impulse: a true impulse has energy
    # at every frequency and onset detectors treat it as noise.
    tick = (np.exp(-np.linspace(0, 12, 900))
            * np.sin(2 * np.pi * 90 * np.arange(900) / sample_rate))
    for start in range(0, n - tick.size, period):
        signal[start:start + tick.size] += tick.astype(np.float32)
    return signal


@pytest.mark.integration
@essentia_only
async def test_a_known_tempo_survives_the_resample_the_pipeline_needs():
    """AC-4.1. `RhythmExtractor2013` has no `sampleRate` parameter.

    Its whole parameter list is `maxTempo`, `method`, `minTempo` — so what it
    assumes about the rate is not something a caller can state or verify, and
    the pipeline's buffer is 22.05 kHz. `tempo_of` resamples to 44.1 kHz for
    exactly this reason, and this is what says the resampling works rather
    than that it happens.
    """
    for rate in (44100, 22050):
        found = audio_descriptors.tempo_of(_click_track(128.0, 20.0, rate), rate)
        assert found is not None, f"no tempo at {rate} Hz"
        assert abs(found - 128.0) < 2.0, f"{found} at {rate} Hz, click is 128"


@pytest.mark.integration
@essentia_only
async def test_the_targeted_algorithms_agree_with_musicextractor(tmp_path):
    """AC-2.2. The equivalence the whole design rests on.

    `MusicExtractor` takes 22.7s on a 58-second file and returns 110
    descriptors, about a hundred of them unused. Six targeted algorithms take
    11.5s. That trade is only worth making if the numbers are the same ones —
    otherwise it is not a cheaper measurement, it is a different one.

    Measured on real audio when this shipped: 129.088 against 129.1 for tempo,
    -14.276 against -14.3 for LUFS, 5.498 against 5.5 for dynamic range.
    """
    import essentia.standard as es
    import soundfile as sf

    audio = tmp_path / "click.wav"
    sf.write(str(audio), _click_track(128.0, 20.0), 44100)

    ours = audio_descriptors.describe(audio)
    assert ours is not None and ours.bpm is not None

    reference, _ = es.MusicExtractor(
        lowlevelStats=["mean"], rhythmStats=["mean"], tonalStats=["mean"],
    )(str(audio))
    assert abs(ours.bpm - float(reference["rhythm.bpm"])) < 0.05
    assert abs(ours.dynamic_range
               - float(reference["lowlevel.dynamic_complexity"])) < 0.005


@pytest.mark.integration
@essentia_only
async def test_a_file_that_is_not_audio_yields_nothing_and_does_not_raise(tmp_path):
    """A truncated MP3 is ordinary in a peer-to-peer crate."""
    broken = tmp_path / "truncated.mp3"
    broken.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff" * 400)
    assert audio_descriptors.describe(broken) is None
