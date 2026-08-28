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
    votes: int = 0          # probes that named this track
    probes: int = 0         # probes attempted anywhere in the segment
    matched: int = 0        # probes that named *something*

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
    def agreement(self) -> float:
        """Agreement among the probes that named anything at all.

        Silence is not dissent. A probe that came back empty says nothing about
        whether the track is right — fingerprinting a mix fails constantly, on
        breakdowns, filter sweeps and passages the database does not have. So
        the probes that did speak are what agreement is measured over.

        The distinction matters most where the evidence is thinnest: a track
        established by three probes across seven minutes, with silence in
        between, is well established, not contested.
        """
        deciding = self.matched or self.votes
        if deciding <= 0:
            return 0.0
        return round(self.votes / deciding, 3)

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
        if self.votes >= 3 and self.agreement >= 0.99:
            return "strong"
        if self.votes >= 2 and self.agreement >= 0.6:
            return "medium"
        return "weak"


def auto_edge_margin(duration: float) -> float:
    """How much of each end to skip, as a function of length.

    Eight seconds is right for a set: the opening is applause, a fade-in or a
    DJ talking, and fingerprinting it wastes a request. It is badly wrong for
    a reel. On a thirty-five second clip an eight-second margin at each end
    puts nearly half the content outside any probe window — and unlike a set,
    a reel has no intro to skip, the music starts on the first frame.

    Capped at eight so nothing changes above about four minutes.
    """
    return min(8.0, max(1.0, duration * 0.03))


def grid_probes(duration: float, interval: float = 25.0,
                edge_margin: Optional[float] = None) -> List[float]:
    """Probe positions on a regular grid.

    `edge_margin` keeps the first and last probe away from fade-ins and
    applause, which fingerprint poorly and waste a request. Derived from the
    duration when not given, for the same reason the cadence is.
    """
    if edge_margin is None:
        edge_margin = auto_edge_margin(duration)
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


# How far the curve must rise above its own surroundings before the peak is
# treated as a real change rather than as noise.
#
# Measured on real music with known hard cuts: a genuine cut peaks at 1.19 to
# 1.33 times the median of the window it sits in, and a window with no cut in
# it reaches 1.03 to 1.10. The separation is clean and this sits between them.
#
# Below it the curve is flat and `argmax` returns whichever sample happens to
# be highest — which near the start of a file is biased towards the beginning,
# because the features are still settling. That produced a 1.1-second opening
# "track" on a real reel and shifted every boundary after it.
BOUNDARY_PROMINENCE = 1.15

# A boundary must not land on a probe. The probe heard the track *there*, so
# saying it ended there contradicts the only evidence in play. Trimmed from
# each end of the window before looking for a peak.
BOUNDARY_EDGE_MARGIN = 0.15


def refine_boundary(features: FeatureSet, curve: np.ndarray,
                    lo: float, hi: float) -> float:
    """Place a boundary at the strongest change between two probe times.

    Called only once we know the track changed somewhere in `[lo, hi]`, which
    turns an unreliable global search into a reliable local one — but only
    where the curve has something to say. Where it does not, the midpoint is
    the honest answer: knowing a change happened somewhere in five seconds and
    guessing the middle is better than pointing confidently at noise.
    """
    midpoint = (lo + hi) / 2
    if curve.size == 0 or hi <= lo:
        return midpoint

    frames_per_second = features.sample_rate / features.hop_length
    span = hi - lo
    inner_lo = lo + span * BOUNDARY_EDGE_MARGIN
    inner_hi = hi - span * BOUNDARY_EDGE_MARGIN
    i_lo = max(0, int(inner_lo * frames_per_second))
    i_hi = min(curve.size, int(inner_hi * frames_per_second))
    if i_hi - i_lo < 2:
        return midpoint

    # The baseline comes from the whole window and the peak only from its
    # interior. Measuring both on the trimmed window moves the median with the
    # trim and cost a boundary that had been landing within 0.02 s of a real
    # cut — the level to clear is a property of the surroundings, not of the
    # part being searched.
    outer = curve[max(0, int(lo * frames_per_second)):
                  min(curve.size, int(hi * frames_per_second))]
    window = curve[i_lo:i_hi]
    baseline = float(np.median(outer)) if outer.size else 0.0
    if baseline <= 0 or float(window.max()) / baseline < BOUNDARY_PROMINENCE:
        return midpoint                 # nothing stands out; do not pretend

    peak = i_lo + int(np.argmax(window))
    return round(peak / frames_per_second, 3)


# The longest silence that may be crossed between two matches of the same
# track. The measure is the *gap*, not the total span: a segment's start is
# where the previous track ended, which for the first track is the start of the
# file, so a span-based cap rejects perfectly ordinary bridges for a reason
# that has nothing to do with the music.
#
# Four minutes covers a long breakdown or a passage the database does not have.
# Past that, the same title twice is more plausibly two plays.
MAX_BRIDGE_GAP = 240.0


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
            matched=sum(1 for p in group if p.key),
        ))

    return coalesce(segments, min_segment)


def coalesce(segments: List[Segment], min_segment: float = 20.0) -> List[Segment]:
    """Bridge gaps, then absorb slivers. Safe to run more than once.

    Split out of `merge_probes` because merging is not only that function's
    business. Confirmation can *overturn* a segment — its extra probes vote,
    and the majority can name a different record than the original probe did.
    When the new name matches a neighbour, two adjacent segments now carry the
    same track and nothing was putting them back together.

    That is how a track came back as three consecutive plays of itself, at
    06:55, 08:04 and 08:33, with no gap between them: three touching segments,
    identical artist and title. The gap-bridging fix could not have caught it,
    because by then there was no gap left to bridge.
    """
    return _absorb_slivers(_bridge_gaps(segments), min_segment)


def _bridge_gaps(segments: List[Segment]) -> List[Segment]:
    """Close an unidentified gap when the same track sits on both sides.

    Fingerprinting a mix fails constantly and unevenly: a breakdown, a filter
    sweep, two records overlapping, a passage the database simply does not
    have. On a set where only a quarter of probes match, that shreds a track
    into pieces — one Axwell record came back as three thirty-second segments
    separated by gaps of one and three minutes, and read as three plays.

    A track heard before a silence and again after it is almost always the same
    track still playing. Bridging is capped at twelve minutes, past which two
    matches are more plausibly two separate plays.

    Only *unidentified* gaps are crossed. A different track in between means
    the record really did change, and merging across that would invent a play
    that never happened.
    """
    if len(segments) < 3:
        return segments

    out: List[Segment] = [segments[0]]
    i = 1
    while i < len(segments):
        current = segments[i]
        following = segments[i + 1] if i + 1 < len(segments) else None
        previous = out[-1]

        bridgeable = (
            following is not None
            and not current.identified              # the gap
            and previous.identified
            and previous.key == following.key       # same record either side
            and current.duration <= MAX_BRIDGE_GAP
        )
        if bridgeable:
            previous.end = following.end
            previous.votes += following.votes
            # The gap's probes were attempts that found nothing: they count as
            # attempted, so `confidence` still reports what share of the segment
            # was actually recognised — but not as disagreement, which is what
            # `agreement` and `strength` are measured over.
            previous.probes += current.probes + following.probes
            previous.matched += following.matched
            i += 2
            continue

        out.append(current)
        i += 1

    return out


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
    #
    # The leading segment is folded *forwards* instead, into its successor.
    # Nothing else could absorb it: pass 1 needs a predecessor and this has
    # none, and the rule below needs one too. So a too-short opening segment
    # survived at any length — a real reel came back with a 1.1-second first
    # track, which is not a track, it is the tail of the probe window.
    #
    # Same blind spot as the gap-bridging cap had: rules written as "look at
    # the one before" quietly exempt the first of anything.
    # Not when the successor is a gap and the head has a name: that would let
    # an unidentified stretch swallow a real finding, and keeping those
    # visible is the point of the whole merge step.
    if (len(kept) > 1 and kept[0].duration < min_segment
            and (kept[1].identified or not kept[0].identified)):
        head, nxt = kept[0], kept[1]
        nxt.start = head.start
        nxt.probes += head.probes
        if nxt.key == head.key:
            nxt.votes += head.votes
            nxt.matched += head.matched
        kept = kept[1:]

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
            out[-1].matched += seg.matched
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

    Deliberately sparse. A track runs three to six minutes, so a probe every
    forty seconds still puts four to nine of them inside it — several times
    what is needed to name it. The earlier cadence of twenty seconds fired
    nine to eighteen per track, which was five-fold redundancy paid for in
    requests to a service that rate-limits.

    Density was originally there to place boundaries accurately and to avoid
    missing a short track. Neither argument survives: boundaries are refined
    against the novelty curve rather than snapped to the nearest probe, and
    thin segments now get extra probes aimed at them by the confirmation pass.
    Starting sparse and adding where it matters beats blanketing the set.

    Short input inverts the argument. Below about ten minutes the content is
    not a set at all — it is a reel, a radio edit, a promo cut — and the
    tracks in it are seconds long with hard cuts between them. Sparse probing
    does not merely lose precision there, it loses whole tracks: a
    sixty-second clip of four fifteen-second tracks got two probes, and half
    of it was attributed to a track that had stopped playing thirty seconds
    earlier. Measured with an identifier that answered correctly every time,
    so nothing about it was Shazam's doing.

    The cost of being fine on short input is nothing, because the input is
    short. Twelve probes for a minute, thirty-six for a three-minute reel,
    against a hundred and eighteen for a set.
    """
    minutes = duration / 60
    if minutes < 2:
        return 5.0
    if minutes < 5:
        return 8.0
    if minutes < 10:
        return 12.0

    hours = duration / 3600
    if hours < 0.5:
        return 25.0
    if hours < 1.5:
        return 35.0
    if hours < 3:
        return 45.0
    return 60.0


def auto_min_segment(duration: float) -> float:
    """The shortest stretch worth reporting as its own track.

    Derived from the cadence rather than set beside it, because the two answer
    the same question and drift apart if they are maintained separately. A
    twenty-second floor is right for a set, where anything shorter is a stray
    probe or a sampled loop; it is wrong for a reel, where a twelve-second
    snippet is the entire point and the floor would swallow it.

    Capped at twenty so nothing changes for long input, where the current
    behaviour is what we want.
    """
    # Below the cadence, not at it. A track can only be caught by the probes
    # that land inside it, so on a reel probed every five seconds a real track
    # may legitimately show up as three or four seconds — and a floor of four
    # deleted exactly that: the opening record of a real reel, correctly
    # identified, thrown away for being shorter than the rule expected.
    return min(20.0, max(2.5, auto_interval(duration) * 0.6))
