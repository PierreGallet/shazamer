#!/usr/bin/env python3
"""Two measurements the analysis is judged by, on real audio.

Both existed as throwaway scripts and produced the numbers that justified two
shipped changes. That is exactly the wrong place for them: the one figure a
decision rests on has to be reproducible by somebody else, later, without
taking it on trust.

    python scripts/benchmark_placement.py media/some-set.webm
    python scripts/benchmark_placement.py media/some-set.webm --boundaries
    python scripts/benchmark_placement.py media/some-set.webm --placement

BOUNDARY ACCURACY (--boundaries)
--------------------------------
A DJ set has no annotated cut times, so splicing two passages of one set puts
a cut at a time known to the sample, on audio that is real. Sine tones cannot
do this: they give the novelty curve nothing to read, and a boundary finder
measured on them is measured on nothing.

Reported when `harmonic_novelty` shipped: median error 1.81s -> 0.14s.

PROBE PLACEMENT (--placement)
-----------------------------
A Shazam probe is the scarcest thing in the pipeline — roughly one every ten
seconds under the rate limit. A probe landing where two records overlap hears
both at once, which is the one condition a fingerprinter cannot resolve.

So: of the probes a segment gets, what share land in the least stable quarter
of that segment? Measured for placement with and without the audio in hand.
The threshold is per-segment, because what counts as a busy passage in an
ambient set is not what counts in a peak-time one.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core import audio as audio_io                      # noqa: E402
from src.core.features import FeatureSet, StreamingFeatures  # noqa: E402
from src.core.segment import (Segment, confirmation_times,   # noqa: E402
                              novelty_curve, refine_boundary,
                              stability_curve)

SR = 22050
HALF = 30.0                 # seconds either side of a spliced cut

# Far apart enough that each pair is certainly two different records.
SPLICES: Sequence[Tuple[int, int]] = (
    (300, 1500), (600, 2100), (900, 2700), (1200, 3000), (1800, 2400),
)


def _spectral_only(features: FeatureSet) -> np.ndarray:
    """The novelty curve as it was before harmony was added.

    Kept here rather than in the library: it exists only to be the thing the
    current curve is compared against, and a comparison baseline living in
    production code is a baseline that will quietly drift.
    """
    from scipy.ndimage import gaussian_filter1d

    n = min(features.centroid.size, features.rms.size)
    if n == 0:
        return np.zeros(0, dtype=np.float32)

    def z(x: np.ndarray) -> np.ndarray:
        return (x - x.mean()) / (x.std() or 1.0)

    return gaussian_filter1d(
        np.abs(np.gradient(z(features.centroid[:n])))
        + np.abs(np.gradient(z(features.rms[:n]))), sigma=10.0)


async def _load(path: str) -> np.ndarray:
    chunks = []
    async for block in audio_io.stream_blocks(path, sample_rate=SR):
        chunks.append(block)
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def _features_of(samples: np.ndarray) -> FeatureSet:
    feats = StreamingFeatures(sample_rate=SR)
    for i in range(0, len(samples), SR * 10):
        feats.push(samples[i:i + SR * 10])
    return feats.finish()


def measure_boundaries(audio: np.ndarray) -> None:
    total = len(audio) / SR
    print(f"\nBoundary accuracy — cuts known to the sample\n")
    print(f"  {'splice':>18} {'spectral only':>15} {'with harmony':>14}")

    before: List[float] = []
    after: List[float] = []
    for a, b in SPLICES:
        if b + HALF > total:
            print(f"  {a:>6}s+{b:<6}s   skipped, set is only {total:.0f}s")
            continue
        clip = np.concatenate([audio[int(a * SR):int((a + HALF) * SR)],
                               audio[int(b * SR):int((b + HALF) * SR)]])
        features = _features_of(clip)
        lo, hi = HALF - 6, HALF + 6
        old = abs(refine_boundary(features, _spectral_only(features), lo, hi) - HALF)
        new = abs(refine_boundary(features, novelty_curve(features), lo, hi) - HALF)
        before.append(old)
        after.append(new)
        print(f"  {a:>6}s+{b:<6}s {old:14.2f}s {new:13.2f}s")

    if not before:
        print("  Nothing measurable — the set is too short for these splices.")
        return
    print(f"\n  {'mean':>18} {np.mean(before):14.2f}s {np.mean(after):13.2f}s")
    print(f"  {'median':>18} {np.median(before):14.2f}s {np.median(after):13.2f}s")


def measure_placement(features: FeatureSet, segments: Sequence[Segment],
                      probe_duration: float = 12.0) -> None:
    """Share of probes landing in the least stable quarter of their segment."""
    stability = stability_curve(features)
    if stability.size == 0:
        print("\n  No chroma in this FeatureSet — nothing to measure.")
        return
    rate = features.chroma_rate

    print(f"\nProbe placement — probes landing in unstable audio\n")
    print(f"  {'segment':>16} {'probes':>7} {'blind':>7} {'aware':>7}")

    blind_bad = blind_all = aware_bad = aware_all = 0
    for segment in segments:
        span = (int(segment.start * rate), int(segment.end * rate))
        window = stability[max(0, span[0]):max(span[0] + 1, span[1])]
        if window.size < 4:
            continue
        # Per-segment, because "unstable" is relative to the music around it.
        floor = float(np.quantile(window, 0.25))

        def unstable(times: Sequence[float]) -> int:
            return sum(1 for t in times
                       if stability[min(int(t * rate), stability.size - 1)] <= floor)

        blind = confirmation_times(segment, [], wanted=3,
                                   probe_duration=probe_duration)
        aware = confirmation_times(segment, [], wanted=3,
                                   probe_duration=probe_duration,
                                   features=features)
        if not blind and not aware:
            continue
        b_bad, a_bad = unstable(blind), unstable(aware)
        blind_bad += b_bad
        blind_all += len(blind)
        aware_bad += a_bad
        aware_all += len(aware)
        print(f"  {segment.start:6.0f}-{segment.end:<7.0f} {len(aware):7}"
              f" {b_bad:7} {a_bad:7}")

    if not blind_all:
        print("  No segment had room for a confirmation probe.")
        return
    print(f"\n  in the worst quarter:  blind {blind_bad}/{blind_all}"
          f" ({blind_bad / blind_all:.0%})"
          f"   aware {aware_bad}/{aware_all} ({aware_bad / aware_all:.0%})")
    if aware_all < blind_all:
        print("  REGRESSION: stability cost probes. It must never do that.")


def _even_segments(duration: float, span: float = 180.0) -> List[Segment]:
    """Stand-in segments when no tracklist is supplied.

    Placement is a question about where a probe goes *inside* a segment, so
    the segment boundaries only have to be plausible, not correct.
    """
    out = []
    at = 0.0
    while at + span <= duration:
        out.append(Segment(start=at, end=at + span, key="x", payload={},
                           votes=1, probes=1, matched=1))
        at += span
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", help="A real DJ set — the longer the better")
    parser.add_argument("--boundaries", action="store_true")
    parser.add_argument("--placement", action="store_true")
    parser.add_argument("--segment-span", type=float, default=180.0)
    args = parser.parse_args()

    if not Path(args.audio).exists():
        print(f"No such file: {args.audio}", file=sys.stderr)
        return 2
    both = not (args.boundaries or args.placement)

    audio = await _load(args.audio)
    duration = len(audio) / SR
    print(f"{args.audio} — {duration:.0f}s")
    if duration < 120:
        print("Too short to say anything. Use a real set.", file=sys.stderr)
        return 2

    if both or args.boundaries:
        measure_boundaries(audio)
    if both or args.placement:
        features = _features_of(audio)
        measure_placement(features, _even_segments(duration, args.segment_span))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
