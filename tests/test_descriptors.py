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


# ── Declared bitrate against the audio ────────────────────────────────────

@pytest.mark.parametrize("label, expected", [
    ("MP3 320 kbps", (320, False)),
    ("MP3 128 kbps", (128, False)),
    ("FLAC 16-bit 44.1 kHz", (0, True)),
    ("WAV 24-bit 48.0 kHz", (0, True)),
    ("MP3", (0, False)),
    ("", (0, False)),
])
def test_the_declared_quality_is_read_back_out_of_its_own_label(label, expected):
    """Parsed from the stored label rather than kept as a second column.

    The label is what the peer claimed and it is already recorded. A duplicate
    is one more thing that can disagree with the original.
    """
    from src.acquire.describe import _declared_quality

    assert _declared_quality(label) == expected


def test_a_quiet_recording_is_not_accused_of_being_a_transcode():
    """A ceiling, never a verdict.

    A recording with genuinely no high content is indistinguishable from a
    128 kbps transcode, and calling it one would be a confident lie. The
    message says "consistent with N at best", which is true of both.
    """
    from src.core.bitrate import assess

    import numpy as np
    import soundfile as sf
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        # A pure 200 Hz tone: no content anywhere near the cutoff region.
        t = np.linspace(0, 30, 44100 * 30, endpoint=False)
        path = Path(folder) / "quiet.wav"
        sf.write(str(path), (0.4 * np.sin(2 * np.pi * 200 * t)).astype("float32"),
                 44100)

        _found, note = assess(path, declared_bitrate=320, lossless=False)
        if note:
            assert "at best" in note, f"stated as a verdict, not a ceiling: {note}"
            assert "is a" not in note


def test_too_little_audio_is_not_measured(tmp_path):
    """Ten seconds is the floor.

    Less than that is one phrase, and a phrase with no cymbals in it looks
    exactly like a transcode.
    """
    import numpy as np
    import soundfile as sf
    from src.core.bitrate import cutoff_hz

    short = tmp_path / "short.wav"
    t = np.linspace(0, 5, 44100 * 5, endpoint=False)
    sf.write(str(short), (0.4 * np.sin(2 * np.pi * 440 * t)).astype("float32"),
             44100)
    assert cutoff_hz(short) is None


def test_an_unreadable_file_yields_nothing_and_raises_nothing(tmp_path):
    from src.core.bitrate import assess, cutoff_hz

    broken = tmp_path / "broken.mp3"
    broken.write_bytes(b"\x00" * 512)
    assert cutoff_hz(broken) is None
    assert assess(broken, 320, False) == (None, "")


def test_the_detector_finds_a_lowpass_that_was_actually_applied():
    """Tests the detector, not the encoder.

    A brick wall at 16 kHz, applied directly, is what a lossy codec leaves
    behind — and unlike an encode it is deterministic, so this runs everywhere
    and cannot drift with an ffmpeg version.

    Encoding a fixture and expecting a cutoff does not work: LAME's lowpass is
    content-dependent. On dense broadband material at 128 kbps it keeps the
    whole band. That belongs in an integration test against real music, and in
    the module's docstring as a limitation, not here.
    """
    import numpy as np
    import soundfile as sf
    import tempfile
    from pathlib import Path
    from src.core.bitrate import cutoff_hz

    rng = np.random.default_rng(3)
    seconds, sr = 30, 44100
    noise = rng.standard_normal(sr * seconds)

    with tempfile.TemporaryDirectory() as folder:
        for wall in (16_000, 20_000):
            spectrum = np.fft.rfft(noise)
            spectrum[np.fft.rfftfreq(noise.size, 1 / sr) > wall] = 0
            filtered = np.fft.irfft(spectrum, n=noise.size)
            filtered = (filtered / np.abs(filtered).max() * 0.8).astype("float32")

            path = Path(folder) / f"wall_{wall}.wav"
            sf.write(str(path), filtered, sr)
            found = cutoff_hz(path)
            assert found is not None
            assert abs(found - wall) < 400, (
                f"wall at {wall} Hz, detector says {found:.0f} Hz")


def test_a_declared_rate_above_what_the_audio_supports_is_flagged():
    """The rule, exercised without an encoder in the loop."""
    import numpy as np
    import soundfile as sf
    import tempfile
    from pathlib import Path
    from src.core.bitrate import assess

    rng = np.random.default_rng(4)
    sr, seconds = 44100, 30
    noise = rng.standard_normal(sr * seconds)
    spectrum = np.fft.rfft(noise)
    spectrum[np.fft.rfftfreq(noise.size, 1 / sr) > 16_000] = 0
    walled = np.fft.irfft(spectrum, n=noise.size)
    walled = (walled / np.abs(walled).max() * 0.8).astype("float32")

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "walled.wav"
        sf.write(str(path), walled, sr)

        _c, note = assess(path, declared_bitrate=320, lossless=False)
        assert "320" in note and "at best" in note, note

        # The same audio declaring 128 is telling the truth.
        _c, honest = assess(path, declared_bitrate=128, lossless=False)
        assert honest == "", honest

        # And as a lossless file it is a fake FLAC.
        _c, lossless_note = assess(path, 0, lossless=True)
        assert "lossy source" in lossless_note, lossless_note


@pytest.mark.integration
async def test_a_transcode_of_real_music_is_caught(tmp_path):
    """AC-1.2 against real music, which is the only place this is calibrated.

    Needs a real recording: a synthetic one does not make LAME lowpass. Set
    SHAZAMER_MUSIC_FIXTURE to any music file to run it. Skipped otherwise
    rather than replaced by something that would pass for the wrong reason.
    """
    import os
    import subprocess

    from src.core.bitrate import assess

    source = os.environ.get("SHAZAMER_MUSIC_FIXTURE", "")
    if not source or not Path(source).exists():
        pytest.skip("set SHAZAMER_MUSIC_FIXTURE to a real music file")

    master = tmp_path / "master.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-t", "60", "-i", source,
                    "-ar", "44100", "-ac", "2", str(master)], check=True)

    def encode(src, dst, kbps):
        subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-i", str(src),
                        "-codec:a", "libmp3lame", "-b:a", f"{kbps}k", str(dst)],
                       check=True)

    honest, low, fraud = (tmp_path / n for n in
                          ("h320.mp3", "l128.mp3", "f320.mp3"))
    encode(master, honest, 320)
    encode(master, low, 128)
    encode(low, fraud, 320)

    assert honest.stat().st_size == fraud.stat().st_size, (
        "the fixture no longer shows why a size check cannot do this")
    assert assess(honest, 320, False)[1] == "", "the honest 320 was flagged"
    assert "at best" in assess(fraud, 320, False)[1]
