"""Incremental feature extraction and per-track musical descriptors.

`StreamingFeatures` is the piece that replaces the one-shot
`librosa.feature.spectral_centroid(y=whole_set)` call. It consumes the blocks
produced by `core.audio.stream_blocks` and keeps only the resulting feature
vectors, which are tiny: at hop 512 and 22.05 kHz a two-hour set yields about
310 k frames, so ~1.2 MB per feature — against gigabytes for the full STFT.

Frames are carried across block boundaries so the analysis is bit-identical to
what a single-shot call would produce, minus the edge padding.

Three features come out of it: spectral centroid, RMS energy, and chroma.
Centroid and chroma share one STFT rather than computing two, so adding
harmony cost a filterbank projection and not a second transform.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)

N_FFT = 1024
HOP_LENGTH = 512

# Chroma is pooled down to roughly two frames a second before being kept.
#
# Harmony moves at the speed of chords, not of samples: at hop 512 and
# 22.05 kHz, 43 columns a second is 40 of them saying the same thing. Pooling
# by 22 costs nothing in resolution — a boundary is placed against a window
# tens of seconds wide — and keeps a two-hour set at about 700 kB of chroma
# instead of 15 MB, which is what makes this affordable to carry at all.
CHROMA_POOL = 22

# Pitch classes in semitone order, matching librosa's chroma bin order.
PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Krumhansl–Kessler key profiles: the perceived stability of each scale degree.
# Correlating a chroma vector against all 24 rotations gives the key.
_KK_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                      2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KK_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                      2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# Camelot wheel — the notation DJs actually mix by. Adjacent numbers are
# harmonically compatible; A is minor, B is major.
_CAMELOT_MAJOR = {"B": "1B", "F#": "2B", "C#": "3B", "G#": "4B", "D#": "5B",
                  "A#": "6B", "F": "7B", "C": "8B", "G": "9B", "D": "10B",
                  "A": "11B", "E": "12B"}
_CAMELOT_MINOR = {"G#": "1A", "D#": "2A", "A#": "3A", "F": "4A", "C": "5A",
                  "G": "6A", "D": "7A", "A": "8A", "E": "9A", "B": "10A",
                  "F#": "11A", "C#": "12A"}


@dataclass
class StreamingFeatures:
    """Accumulates spectral centroid and RMS energy across streamed blocks.

    Usage:
        feats = StreamingFeatures(sample_rate=22050)
        async for block in stream_blocks(path):
            feats.push(block)
        result = feats.finish()
    """

    sample_rate: int
    n_fft: int = N_FFT
    hop_length: int = HOP_LENGTH

    _centroid: List[np.ndarray] = field(default_factory=list, repr=False)
    _rms: List[np.ndarray] = field(default_factory=list, repr=False)
    _chroma: List[np.ndarray] = field(default_factory=list, repr=False)
    _carry: Optional[np.ndarray] = field(default=None, repr=False)
    # Chroma columns not yet forming a complete pool group. Held rather than
    # dropped so the pooled grid is continuous across block boundaries — the
    # same reason `_carry` exists one level down.
    _chroma_pending: Optional[np.ndarray] = field(default=None, repr=False)
    _samples_seen: int = 0

    def _absorb(self, analysed: np.ndarray) -> None:
        """Extract every feature from one complete run of frames."""
        import librosa

        spectrum = np.abs(librosa.stft(
            analysed, n_fft=self.n_fft, hop_length=self.hop_length,
            center=False,
        ))
        self._centroid.append(
            librosa.feature.spectral_centroid(
                S=spectrum, sr=self.sample_rate, n_fft=self.n_fft,
            )[0]
        )
        # RMS from the samples, not from `spectrum`. The spectral form is an
        # approximation, and this envelope is what the waveform in the UI is
        # drawn from — a picture of the set should be the set.
        self._rms.append(
            librosa.feature.rms(
                y=analysed, frame_length=self.n_fft,
                hop_length=self.hop_length, center=False,
            )[0]
        )
        # `tuning=0.0` rather than letting librosa estimate it. Left to
        # itself it estimates from whatever spectrogram it is handed, which
        # here is one decoder block — so the filterbank would shift between
        # blocks of the same file and the chroma would differ depending on how
        # the download happened to chunk. A measure of *change* cannot afford
        # that, and electronic music sits at A=440 anyway.
        self._pool_chroma(librosa.feature.chroma_stft(
            S=spectrum ** 2, sr=self.sample_rate, n_fft=self.n_fft,
            tuning=0.0,
        ))

    def _pool_chroma(self, chroma: np.ndarray) -> None:
        """Average chroma down to the pooled grid, carrying the remainder."""
        if self._chroma_pending is not None and self._chroma_pending.size:
            chroma = np.concatenate([self._chroma_pending, chroma], axis=1)
        groups = chroma.shape[1] // CHROMA_POOL
        if groups:
            head = chroma[:, : groups * CHROMA_POOL]
            self._chroma.append(
                head.reshape(12, groups, CHROMA_POOL).mean(axis=2))
        self._chroma_pending = chroma[:, groups * CHROMA_POOL:]

    def push(self, block: np.ndarray) -> None:
        """Feed one decoded block. The block is not retained."""
        self._samples_seen += len(block)

        buf = block if self._carry is None else np.concatenate([self._carry, block])

        # With center=False, frame i spans [i*hop, i*hop + n_fft). Anything
        # past the last complete frame is carried into the next block so no
        # sample is analysed twice and none is dropped.
        if len(buf) < self.n_fft:
            self._carry = buf
            return

        n_frames = 1 + (len(buf) - self.n_fft) // self.hop_length
        consumed = n_frames * self.hop_length

        self._absorb(buf[: consumed + self.n_fft - self.hop_length])
        self._carry = buf[consumed:]

    def finish(self) -> "FeatureSet":
        """Flush the tail and return the concatenated feature vectors."""
        if self._carry is not None and len(self._carry) >= self.n_fft:
            self._absorb(self._carry)
        self._carry = None

        # The last partial pool group is kept rather than discarded: on a short
        # clip it can be most of the file.
        if self._chroma_pending is not None and self._chroma_pending.size:
            self._chroma.append(self._chroma_pending.mean(axis=1, keepdims=True))
        self._chroma_pending = None

        centroid = (np.concatenate(self._centroid) if self._centroid
                    else np.zeros(0, dtype=np.float32))
        rms = (np.concatenate(self._rms) if self._rms
               else np.zeros(0, dtype=np.float32))
        chroma = (np.concatenate(self._chroma, axis=1) if self._chroma
                  else np.zeros((12, 0), dtype=np.float32))

        return FeatureSet(
            centroid=centroid,
            rms=rms,
            chroma=chroma,
            sample_rate=self.sample_rate,
            hop_length=self.hop_length,
            duration=self._samples_seen / self.sample_rate if self.sample_rate else 0.0,
        )


@dataclass
class FeatureSet:
    centroid: np.ndarray
    rms: np.ndarray
    sample_rate: int
    hop_length: int
    duration: float
    # (12, m) — energy per pitch class, on the pooled grid. Empty for a
    # FeatureSet built before chroma existed, which every reader must handle.
    chroma: np.ndarray = field(
        default_factory=lambda: np.zeros((12, 0), dtype=np.float32))

    def frame_to_time(self, frame: int) -> float:
        return frame * self.hop_length / self.sample_rate

    @property
    def frame_rate(self) -> float:
        return self.sample_rate / self.hop_length if self.hop_length else 0.0

    @property
    def chroma_rate(self) -> float:
        """Chroma frames per second."""
        return self.frame_rate / CHROMA_POOL

    def waveform_peaks(self, points: int = 1600) -> List[float]:
        """Downsample the RMS envelope for display.

        This is the whole cost of the waveform in the UI: the envelope is
        already computed for segmentation, so drawing it is free.
        """
        if self.rms.size == 0:
            return []
        n = min(points, self.rms.size)
        # Max within each bucket rather than mean — a mean envelope looks flat
        # and washes out exactly the transients a DJ is scanning for.
        buckets = np.array_split(self.rms, n)
        peaks = np.array([float(b.max()) if b.size else 0.0 for b in buckets])
        ceiling = peaks.max()
        if ceiling > 0:
            peaks = peaks / ceiling
        return [round(float(p), 4) for p in peaks]


def estimate_bpm(y: np.ndarray, sample_rate: int) -> Optional[float]:
    """Tempo of a window, rounded to one decimal. None when undetectable."""
    import librosa

    if y.size < sample_rate * 5:
        return None
    try:
        tempo, _ = librosa.beat.beat_track(y=y, sr=sample_rate)
        value = float(np.atleast_1d(tempo)[0])
    except Exception as exc:
        logger.debug("BPM estimation failed: %s", exc)
        return None
    if not np.isfinite(value) or value <= 0:
        return None
    # Fold into the range house/techno actually lives in, so a track at 124
    # never reports as 62 or 248.
    while value < 85:
        value *= 2
    while value > 175:
        value /= 2
    return round(value, 1)


def estimate_key(y: np.ndarray, sample_rate: int) -> Optional["KeyEstimate"]:
    """Krumhansl–Schmuckler key estimation, reported in Camelot notation."""
    import librosa

    if y.size < sample_rate * 5:
        return None
    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sample_rate)
    except Exception as exc:
        logger.debug("Key estimation failed: %s", exc)
        return None

    profile = chroma.mean(axis=1)
    if not np.any(profile):
        return None
    profile = (profile - profile.mean()) / (profile.std() or 1.0)

    best_score = -np.inf
    best: Optional[KeyEstimate] = None
    for mode, template, table in (
        ("major", _KK_MAJOR, _CAMELOT_MAJOR),
        ("minor", _KK_MINOR, _CAMELOT_MINOR),
    ):
        norm = (template - template.mean()) / template.std()
        for tonic in range(12):
            score = float(np.corrcoef(profile, np.roll(norm, tonic))[0, 1])
            if np.isfinite(score) and score > best_score:
                best_score = score
                name = PITCH_CLASSES[tonic]
                best = KeyEstimate(
                    key=name,
                    mode=mode,
                    camelot=table[name],
                    confidence=round(max(0.0, score), 3),
                )
    return best


@dataclass(frozen=True)
class KeyEstimate:
    key: str
    mode: str
    camelot: str
    confidence: float

    @property
    def label(self) -> str:
        return f"{self.key} {'maj' if self.mode == 'major' else 'min'}"
