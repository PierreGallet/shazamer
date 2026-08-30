"""What a record is, measured from the audio: tempo, key, loudness, dynamics.

Essentia rather than librosa, for one reason that was measured rather than
assumed. Ten windows of a real 55-minute set, each the exact window the
pipeline feeds to `estimate_bpm`:

    pos   librosa   Essentia   note
      3     123.0      123.8
      8     129.2      127.9
     13     129.2      129.4
     17     103.4      130.0   title says "Farma Remix 132 Bpm"
     19     129.2      129.9
     24     129.2      130.3
     30      89.1      132.6
     32     136.0      132.9
     35     107.7      132.9
     38     107.7      133.0

Essentia puts every window between 123 and 133, which is what a beatmatched
mix looks like — tempo is the one thing a DJ holds still. librosa returns 89.1,
103.4 and 107.7 twice: four of ten wrong, all half-time or odd-ratio locks that
the 85–175 folding in `features.estimate_bpm` cannot catch because they already
land inside it. One window carries its own ground truth and Essentia is within
two of it.

Key stays on librosa. The two agree on seven of ten and there is no ground
truth among the three disagreements, which is not a reason to switch.

INSTALLATION IS PLATFORM-DEPENDENT AND THAT IS LOAD-BEARING
-----------------------------------------------------------
Essentia publishes a Linux x86-64 wheel and no arm64 macOS one, so this works
on the server and not on a mac dev box. Every entry point here answers None
rather than raising, and `available()` says so plainly — a developer must be
able to run the whole pipeline without it, and get librosa's answer, not a
stack trace.

SAMPLE RATE IS NOT NEGOTIABLE
-----------------------------
`RhythmExtractor2013` has no `sampleRate` parameter — its algorithm list is
`maxTempo`, `method`, `minTempo` and nothing else — so what it assumes about
the rate is not something a caller can state or check. Audio is therefore
resampled to 44.1 kHz before it is handed over, which is the rate the numbers
above were measured at. Feeding it the pipeline's 22.05 kHz buffer happened to
agree in one trial, and "happened to agree once" is not a basis for a tempo.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

# The rate every Essentia call here is given. See the module docstring.
ESSENTIA_SR = 44100

# Where the tempo search is allowed to look. Wider than house and techno need,
# because a set that drops to half time or opens at 90 must not fall off the
# end of the window and be reported at the edge.
MIN_TEMPO, MAX_TEMPO = 60, 200

_AVAILABLE: Optional[bool] = None

# Camelot is the notation DJs mix by; adjacent numbers are compatible, A is
# minor and B is major. Essentia names some roots with sharps and some with
# flats depending on the profile, so both spellings resolve to one wheel
# position rather than to a missing key.
_ENHARMONIC = {"C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb"}
_CAMELOT_MAJOR = {"B": "1B", "Gb": "2B", "Db": "3B", "Ab": "4B", "Eb": "5B",
                  "Bb": "6B", "F": "7B", "C": "8B", "G": "9B", "D": "10B",
                  "A": "11B", "E": "12B"}
_CAMELOT_MINOR = {"Ab": "1A", "Eb": "2A", "Bb": "3A", "F": "4A", "C": "5A",
                  "G": "6A", "D": "7A", "A": "8A", "E": "9A", "B": "10A",
                  "Gb": "11A", "Db": "12A"}


def available() -> bool:
    """Whether Essentia can be imported here. Cached; never raises."""
    global _AVAILABLE
    if _AVAILABLE is None:
        try:
            import essentia.standard  # noqa: F401
            _AVAILABLE = True
        except Exception as exc:        # noqa: BLE001 - see module docstring
            logger.info("Essentia is not available (%s); "
                        "audio descriptors will be skipped", exc)
            _AVAILABLE = False
    return _AVAILABLE


def camelot(key: str, scale: str) -> str:
    """Camelot position for a root and mode, or "" when it is not a key."""
    root = _ENHARMONIC.get(key, key)
    table = _CAMELOT_MINOR if scale == "minor" else _CAMELOT_MAJOR
    return table.get(root, "")


@dataclass(frozen=True)
class Descriptors:
    """What one clean audio file turns out to be.

    Every field optional and independently so: a truncated file can yield a
    tempo and no key, and storing the tempo is better than storing nothing
    because one algorithm gave up.
    """

    bpm: Optional[float] = None
    musical_key: str = ""
    camelot: str = ""
    key_strength: Optional[float] = None
    loudness_lufs: Optional[float] = None
    dynamic_range: Optional[float] = None

    @property
    def empty(self) -> bool:
        return not any((self.bpm, self.musical_key, self.loudness_lufs,
                        self.dynamic_range))

    def to_dict(self) -> Dict[str, Any]:
        return {"bpm": self.bpm, "musical_key": self.musical_key,
                "camelot": self.camelot, "key_strength": self.key_strength,
                "loudness_lufs": self.loudness_lufs,
                "dynamic_range": self.dynamic_range}


def tempo_of(samples: np.ndarray, sample_rate: int) -> Optional[float]:
    """Tempo of a passage, or None when Essentia is unavailable or unsure.

    `multifeature` rather than `degara`: it is the mode the ten-window result
    in the module docstring was measured with, and it is about six seconds a
    call against librosa's fraction of one. That cost was accepted knowingly —
    on a run already dominated by waiting for Shazam it is a few percent, and
    it buys a number that is currently wrong two times in five.
    """
    if not available() or samples.size < sample_rate * 5:
        return None
    try:
        import essentia.standard as es

        signal = _at_essentia_rate(samples, sample_rate)
        bpm = float(es.RhythmExtractor2013(
            method="multifeature", minTempo=MIN_TEMPO, maxTempo=MAX_TEMPO,
        )(signal)[0])
    except Exception as exc:            # noqa: BLE001
        logger.debug("Essentia tempo failed: %s", exc)
        return None
    return round(bpm, 1) if np.isfinite(bpm) and bpm > 0 else None


def describe(path: Path) -> Optional[Descriptors]:
    """Every descriptor for one file, or None when Essentia is unavailable.

    Six targeted algorithms rather than `MusicExtractor`. Measured on a real
    58-second file: `MusicExtractor` takes 22.7s and returns 110 descriptors of
    which about a hundred go unused, where these take 11.5s including load and
    return byte-identical values for tempo, loudness and dynamic range.

    Each algorithm is attempted separately. A file that defeats one still
    yields the others, because half an answer about a record beats none.
    """
    if not available():
        return None
    if not path.exists():
        logger.debug("Nothing to describe at %s", path)
        return None

    try:
        import essentia.standard as es

        mono = es.MonoLoader(filename=str(path), sampleRate=ESSENTIA_SR)()
    except Exception as exc:            # noqa: BLE001
        logger.warning("Could not decode %s: %s", path.name, exc)
        return None

    if mono.size < ESSENTIA_SR:
        logger.debug("%s is under a second; not describing it", path.name)
        return None

    def attempt(name: str, run):
        try:
            return run()
        except Exception as exc:        # noqa: BLE001
            logger.debug("%s failed on %s: %s", name, path.name, exc)
            return None

    bpm = attempt("tempo", lambda: float(es.RhythmExtractor2013(
        method="multifeature", minTempo=MIN_TEMPO, maxTempo=MAX_TEMPO)(mono)[0]))
    key = attempt("key", lambda: es.KeyExtractor(
        profileType="edma", sampleRate=ESSENTIA_SR)(mono))
    dynamic = attempt("dynamics", lambda: float(es.DynamicComplexity()(mono)[0]))
    loudness = attempt("loudness", lambda: _integrated_loudness(es, path))

    root, scale, strength = key if key else ("", "", None)
    return Descriptors(
        bpm=round(bpm, 1) if bpm and np.isfinite(bpm) and bpm > 0 else None,
        musical_key=f"{root} {'maj' if scale == 'major' else 'min'}" if root else "",
        camelot=camelot(root, scale) if root else "",
        key_strength=round(float(strength), 3) if strength is not None else None,
        loudness_lufs=round(loudness, 1) if loudness is not None else None,
        dynamic_range=round(dynamic, 2) if dynamic is not None else None,
    )


def _integrated_loudness(es, path: Path) -> Optional[float]:
    """EBU R128 integrated loudness, in LUFS.

    Its own loader because `LoudnessEBUR128` wants stereo and everything else
    here wants mono. The broadcast standard rather than a peak or an average:
    it is the only loudness number that means the same thing across two files
    mastered by different people, which is the whole point of putting it beside
    a hundred and ninety-nine others in a crate.
    """
    stereo = es.AudioLoader(filename=str(path))()[0]
    if stereo.size == 0:
        return None
    return float(es.LoudnessEBUR128()(stereo)[2])


def _at_essentia_rate(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """The signal at 44.1 kHz, resampling only when it has to."""
    signal = np.ascontiguousarray(samples, dtype=np.float32)
    if sample_rate == ESSENTIA_SR:
        return signal
    import librosa

    return np.ascontiguousarray(
        librosa.resample(signal, orig_sr=sample_rate, target_sr=ESSENTIA_SR),
        dtype=np.float32)
