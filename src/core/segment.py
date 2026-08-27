"""Turning a set into segments.

Two strategies, and the choice between them matters more than any tuning
constant.

**Spectral** (the original approach) hunts for boundaries first, then asks
Shazam what sits between them. It assumes tracks are separated by a rupture —
which is exactly what a competent DJ never does. Beatmatched transitions
spread over 32 to 64 bars, so the detected "boundary" tends to land in the
middle of the blend, the one place where two tracks overlap and the
fingerprinter has the least chance of resolving either.

**Grid** (the default here) inverts it: probe at a fixed cadence, then merge
consecutive probes that name the same track. Boundaries become a *result*
rather than a hypothesis, unidentified stretches stay visible instead of being
silently swallowed, and every probe is independent — so the whole pass
parallelises perfectly.

The novelty curve is still computed, but it is used to *refine* a boundary
once we know one exists between two probes, which is a much easier question
than finding it blind.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

from .features import FeatureSet

logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    """One fingerprint attempt at one point in time."""
    time: float
    key: Optional[str] = None          # normalised "artist::title", None = no match
    payload: Optional[dict] = None     # the identifier's full result


@dataclass
class Segment:
    """A stretch of the set attributed to one track (or to nothing)."""
    start: float
    end: float
    key: Optional[str] = None
    payload: Optional[dict] = None
    votes: int = 0
    probes: int = 0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def identified(self) -> bool:
        return self.key is not None

    @property
    def confidence(self) -> float:
        """Share of probes in this segment that agreed on the winning track.

        This replaces the old `match_count` heuristic, which counted entries in
        Shazam's `matches` array — a number reflecting how many internal
        fingerprint hits came back, not how sure the answer is. Agreement across
        independent probes is a real signal.

        Read it with `strength`, never alone: a lone probe scores 1.0 here
        because it agrees with itself.
        """
        if self.probes <= 0:
            return 0.0
        return round(self.votes / self.probes, 3)

    @property
    def strength(self) -> str:
        """How much evidence stands behind the match: strong, medium or weak.

        Agreement on its own is misleading at small sample sizes — one probe
        matching is 100% agreement and almost no evidence. This weighs the
        count as well, so a track backed by four consistent probes reads
        differently from one backed by a single lucky hit.
        """
        if not self.identified or self.probes <= 0:
            return "none"
        agreement = self.votes / self.probes
        if self.votes >= 3 and agreement >= 0.99:
            return "strong"
        if self.votes >= 2 and agreement >= 0.6:
            return "medium"
        return "weak"


def grid_probes(duration: float, interval: float = 25.0,
                edge_margin: float = 8.0) -> List[float]:
    """Probe positions on a regular grid.

    `edge_margin` keeps the first and last probe away from fade-ins and
    applause, which fingerprint poorly and waste a request.
    """
    if duration <= 0:
        return []
    if duration <= edge_margin * 2:
        return [max(0.0, duration / 2)]

    start = edge_margin
    end = duration - edge_margin
    n = max(1, int((end - start) // interval) + 1)
    return [round(start + i * interval, 3) for i in range(n) if start + i * interval < end]


def novelty_curve(features: FeatureSet, smooth_sigma: float = 10.0) -> np.ndarray:
    """Combined rate-of-change of spectral centroid and RMS energy.

    Peaks mark where the sound is changing fastest. Useful for placing a
    boundary we already know exists; unreliable for discovering one.
    """
    from scipy.ndimage import gaussian_filter1d

    if features.centroid.size == 0 or features.rms.size == 0:
        return np.zeros(0, dtype=np.float32)

    n = min(features.centroid.size, features.rms.size)
    centroid = features.centroid[:n]
    rms = features.rms[:n]

    def z(x: np.ndarray) -> np.ndarray:
        std = x.std()
        return (x - x.mean()) / (std if std else 1.0)

    combined = np.abs(np.gradient(z(centroid))) + np.abs(np.gradient(z(rms)))
    return gaussian_filter1d(combined, sigma=smooth_sigma)


def refine_boundary(features: FeatureSet, curve: np.ndarray,
                    lo: float, hi: float) -> float:
    """Place a boundary at the strongest change between two probe times.

    Called only once we know the track changed somewhere in `[lo, hi]`, which
    turns an unreliable global search into a reliable local one.
    """
    if curve.size == 0 or hi <= lo:
        return (lo + hi) / 2

    frames_per_second = features.sample_rate / features.hop_length
    i_lo = max(0, int(lo * frames_per_second))
    i_hi = min(curve.size, int(hi * frames_per_second))
    if i_hi <= i_lo:
        return (lo + hi) / 2

    peak = i_lo + int(np.argmax(curve[i_lo:i_hi]))
    return round(peak / frames_per_second, 3)


def merge_probes(probes: Sequence[ProbeResult], duration: float,
                 features: Optional[FeatureSet] = None,
                 min_segment: float = 20.0) -> List[Segment]:
    """Collapse consecutive probes naming the same track into segments.

    Runs of unmatched probes survive as unidentified segments rather than
    vanishing: a stretch nobody can name is often a dub, an edit or an unsigned
    promo, which is precisely what makes a set worth digging through.
    """
    ordered = sorted(probes, key=lambda p: p.time)
    if not ordered:
        return []

    curve = novelty_curve(features) if features is not None else np.zeros(0)

    # Group consecutive probes sharing the same key (None groups with None).
    groups: List[List[ProbeResult]] = [[ordered[0]]]
    for probe in ordered[1:]:
        if probe.key == groups[-1][-1].key:
            groups[-1].append(probe)
        else:
            groups.append([probe])

    segments: List[Segment] = []
    for index, group in enumerate(groups):
        if index == 0:
            start = 0.0
        else:
            prev_time = groups[index - 1][-1].time
            start = (refine_boundary(features, curve, prev_time, group[0].time)
                     if features is not None else (prev_time + group[0].time) / 2)

        if index == len(groups) - 1:
            end = duration
        else:
            next_time = groups[index + 1][0].time
            end = (refine_boundary(features, curve, group[-1].time, next_time)
                   if features is not None else (group[-1].time + next_time) / 2)

        payload = next((p.payload for p in group if p.payload), None)
        segments.append(Segment(
            start=round(max(0.0, start), 3),
            end=round(min(duration, end), 3),
            key=group[0].key,
            payload=payload,
            votes=sum(1 for p in group if p.key == group[0].key),
            probes=len(group),
        ))

    return _absorb_slivers(segments, min_segment)


def _absorb_slivers(segments: List[Segment], min_segment: float) -> List[Segment]:
    """Remove segments too short to be real tracks.

    Two distinct cases, deliberately kept separate because conflating them
    would swallow legitimately short tracks:

    - A **stray**: a brief segment whose neighbours on *both* sides name the
      same track. A vocal break or a sampled loop that fingerprints as
      something else would otherwise chop one track into three. The
      neighbours agreeing is what makes this safe.
    - A **fragment**: a brief unidentified stretch, or one repeating its
      predecessor, which belongs to the segment before it.

    A short segment that is none of these — a genuine 20-second interlude
    between two different tracks — is left alone.
    """
    if len(segments) < 2:
        return segments

    # Pass 1: drop strays, splicing the surrounding track back together.
    kept: List[Segment] = []
    i = 0
    while i < len(segments):
        seg = segments[i]
        prev = kept[-1] if kept else None
        nxt = segments[i + 1] if i + 1 < len(segments) else None

        is_stray = (
            prev is not None and nxt is not None
            and seg.duration < min_segment
            and prev.key == nxt.key
            and prev.key is not None
        )
        if is_stray:
            prev.end = nxt.end
            prev.votes += nxt.votes
            prev.probes += seg.probes + nxt.probes
            i += 2  # the stray and the neighbour it was splitting
            continue

        kept.append(seg)
        i += 1

    # Pass 2: fold remaining fragments into the segment before them.
    out: List[Segment] = []
    for seg in kept:
        if out and seg.duration < min_segment and not seg.identified:
            out[-1].end = seg.end
            out[-1].probes += seg.probes
            continue
        if out and out[-1].key == seg.key:
            out[-1].end = seg.end
            out[-1].votes += seg.votes
            out[-1].probes += seg.probes
            continue
        out.append(seg)
    return out


def spectral_boundaries(features: FeatureSet, min_song_duration: float,
                        threshold: float) -> List[float]:
    """The original detector, kept as an alternative strategy.

    Retained so the two approaches can be compared on real sets rather than
    argued about, and because it is genuinely better on compilations and radio
    shows, where tracks *are* separated by hard cuts.
    """
    from scipy.signal import find_peaks

    curve = novelty_curve(features)
    if curve.size == 0:
        return [0.0, features.duration]

    frames_per_second = features.sample_rate / features.hop_length
    percentile = (1 - threshold) * 100
    peaks, _ = find_peaks(
        curve,
        height=float(np.percentile(curve, percentile)),
        distance=max(1, int(min_song_duration * frames_per_second)),
    )

    times = [0.0] + [round(p / frames_per_second, 3) for p in peaks] + [features.duration]

    filtered = [times[0]]
    for t in times[1:]:
        if t - filtered[-1] >= min_song_duration:
            filtered.append(t)
    if filtered[-1] != times[-1]:
        filtered[-1] = times[-1]
    return filtered


def confirmation_times(segment: Segment, already: Sequence[float],
                       wanted: int, probe_duration: float = 12.0,
                       edge_margin: float = 6.0) -> List[float]:
    """Extra probe positions inside a segment whose evidence is thin.

    The goal is independent evidence, so each position is chosen to sit as far
    as possible from every probe already taken — including the ones added by
    this call. Even spacing across the segment was tried first and placed
    probes badly whenever an existing one already sat near the middle: the
    obvious slot at the edge went unused and the segment kept its single vote.

    "Far enough" is defined by the probe window rather than a fixed number of
    seconds: two probes closer than half a window overlap by more than half, so
    they are not independent. Tying the rule to the window keeps it correct if
    the window size ever changes.

    Both ends are trimmed — a boundary is approximate, and audio there may
    belong to the neighbouring track.

    Returns fewer than requested, possibly none, when the segment has no room.
    A short track backed by one probe then stays weak, which is the honest
    outcome: there is nowhere independent left to look.
    """
    missing = wanted - segment.probes
    if missing <= 0:
        return []

    lo = segment.start + edge_margin
    hi = segment.end - edge_margin
    if hi <= lo:
        return []

    min_gap = probe_duration / 2
    # A candidate grid fine enough to find a good slot, capped so a long
    # segment does not turn this into a search problem.
    steps = max(2, min(200, int((hi - lo) / 2)))
    candidates = [lo + (hi - lo) * i / steps for i in range(steps + 1)]

    taken = list(already)
    out: List[float] = []
    for _ in range(missing):
        best: Optional[float] = None
        best_gap = 0.0
        for t in candidates:
            gap = min((abs(t - u) for u in taken + out), default=float("inf"))
            if gap > best_gap:
                best_gap, best = gap, t
        if best is None or best_gap < min_gap:
            break                       # nowhere independent left
        out.append(round(best, 3))

    return sorted(out)


def auto_interval(duration: float) -> float:
    """Probe cadence as a function of set length.

    Short mixes get dense probing; a six-hour set would otherwise generate
    close to a thousand requests for no extra resolution.
    """
    hours = duration / 3600
    if hours < 0.5:
        return 15.0
    if hours < 1.5:
        return 20.0
    if hours < 3:
        return 25.0
    return 35.0
