"""End-to-end analysis: source file in, tracklist and waveform out.

The whole pipeline holds at most one 30 s block of audio at a time. Every
stage streams, and progress is reported from real work done rather than
guessed at, because the duration is known before a single sample is decoded.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

from ..identify.base import Identifier, TrackMatch
from . import audio as audio_io
from .features import StreamingFeatures, estimate_bpm, estimate_key
from .segment import (ProbeResult, Segment, auto_interval, auto_min_segment,
                      coalesce, confirmation_times, grid_probes, merge_probes,
                      spectral_boundaries)
from .timecode import format_timestamp

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, str], None]

# Progress is one shared 0-100 scale across stages that take wildly different
# amounts of time, so the boundaries live here rather than as literals at each
# report() call. They must not overlap: a bar that goes backwards reads as a
# bug even when the work is fine.
DECODE_FROM, DECODE_TO = 5, 35
IDENTIFY_FROM, IDENTIFY_TO = 36, 80
MERGE_AT = 81
CONFIRM_FROM, CONFIRM_TO = 82, 87
FEATURES_FROM, FEATURES_TO = 88, 95


@dataclass
class AnalyzeConfig:
    strategy: str = "grid"          # "grid" (default) or "spectral"
    probe_interval: Optional[float] = None   # None → derived from duration
    probe_duration: float = 12.0    # Shazam uses a centred 10 s of this
    # Minimum probes to stand behind a track before it is reported as solidly
    # identified. Segments thinner than this get extra probes spread across
    # them; see _confirm_segments. 1 disables the pass entirely.
    votes_per_segment: int = 3
    # See src/web.py: parallel slots now cost refusals, not just memory.
    concurrency: int = 4
    waveform_points: int = 1600
    # None → derived from duration, like probe_interval and for the same
    # reason: what counts as a track depends on what you fed it.
    min_segment: Optional[float] = None
    compute_musical_features: bool = True
    # Legacy spectral-strategy knobs, unused by the grid strategy.
    min_song_duration: Optional[float] = None
    peak_threshold: Optional[float] = None


class StageTimer:
    """How long each stage of an analysis took.

    Worth having because the stages are wildly unequal and the imbalance moves.
    A run that spent two hours identifying and ninety seconds decoding looks
    identical, from the outside, to one that split the time evenly — and the
    difference is the whole diagnosis. Finding that out once meant reading
    container logs.

    Fed from the progress callback, so a stage is timed by the same thing that
    announces it and the two cannot drift apart.
    """

    def __init__(self) -> None:
        self._started = time.monotonic()
        self._marks: List[tuple] = []       # (stage, monotonic time)

    def enter(self, stage: str) -> None:
        if self._marks and self._marks[-1][0] == stage:
            return                          # same stage reporting again
        self._marks.append((stage, time.monotonic()))

    def finish(self) -> Dict[str, float]:
        """Seconds per stage, in the order they ran."""
        if not self._marks:
            return {}
        end = time.monotonic()
        out: Dict[str, float] = {}
        for i, (stage, at) in enumerate(self._marks):
            until = self._marks[i + 1][1] if i + 1 < len(self._marks) else end
            # Summed rather than assigned: a stage can be re-entered, and
            # reporting it twice would hide half its cost.
            out[stage] = round(out.get(stage, 0.0) + (until - at), 1)
        return out


@dataclass
class Track:
    """A segment as the API and UI consume it."""
    index: int
    start: float
    end: float
    start_label: str
    duration: float
    identified: bool
    title: str = "ID ?"
    artist: str = ""
    url: str = ""
    cover_url: str = ""
    album: str = ""
    label: str = ""
    year: str = ""
    genre: str = ""
    isrc: str = ""
    key: str = ""
    confidence: float = 0.0         # share of all probes that named this
    agreement: float = 0.0          # share of *speaking* probes that agreed
    strength: str = "none"          # strong | medium | weak — see Segment
    votes: int = 0
    probes: int = 0
    bpm: Optional[float] = None
    camelot: Optional[str] = None
    musical_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AnalysisResult:
    duration: float
    tracks: List[Track]
    waveform: List[float]
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "duration": round(self.duration, 3),
            "tracks": [t.to_dict() for t in self.tracks],
            "waveform": self.waveform,
            "stats": self.stats,
        }

    @property
    def identified(self) -> List[Track]:
        return [t for t in self.tracks if t.identified]




class Pipeline:
    def __init__(self, identifier: Identifier,
                 config: Optional[AnalyzeConfig] = None,
                 on_work: Optional[Callable[[int, int], None]] = None) -> None:
        self.identifier = identifier
        self.config = config or AnalyzeConfig()
        # Reports (identify probes, confirmation probes) once both are known.
        # Separate from on_progress because it is not progress — it is what the
        # remaining progress is going to cost.
        self.on_work = on_work

    async def run(self, path: str, on_progress: Optional[ProgressFn] = None
                  ) -> AnalysisResult:
        started = time.monotonic()

        timer = StageTimer()

        def report(stage: str, pct: int, message: str) -> None:
            timer.enter(stage)
            if on_progress is not None:
                try:
                    on_progress(stage, pct, message)
                except Exception:
                    logger.debug("progress callback raised", exc_info=True)

        report("probing", 2, "Reading file information...")
        duration = await audio_io.probe_duration(path)
        if duration <= 0:
            logger.warning("Unknown duration for %s; progress will be coarse", path)

        # ── Stage 1: decode in blocks, keep only feature vectors ──────────
        features = await self._extract_features(path, duration, report)
        duration = features.duration or duration
        waveform = features.waveform_peaks(self.config.waveform_points)

        # ── Stage 2: fingerprint probes, in parallel ──────────────────────
        probes = self._plan_probes(duration, features)
        report("identifying", IDENTIFY_FROM,
               f"Identifying {len(probes)} probes across {format_timestamp(duration)}...")
        results = await self._run_probes(path, probes, report)

        # ── Stage 3: probes → segments ────────────────────────────────────
        report("merging", MERGE_AT, "Merging probes into tracks...")
        min_segment = (self.config.min_segment
                       if self.config.min_segment is not None
                       else auto_min_segment(duration))
        segments = merge_probes(results, duration, features,
                                min_segment=min_segment)

        # ── Stage 4: confirm the thin ones ────────────────────────────────
        segments = await self._confirm_segments(path, segments, results, report,
                                                min_segment)
        tracks = self._to_tracks(segments)

        # ── Stage 4: per-track BPM and key ────────────────────────────────
        if self.config.compute_musical_features and tracks:
            await self._add_musical_features(path, tracks, report)

        report("done", 100, f"Found {sum(1 for t in tracks if t.identified)} tracks")

        identified = [t for t in tracks if t.identified]
        return AnalysisResult(
            duration=duration,
            tracks=tracks,
            waveform=waveform,
            stats={
                "probes": len(probes),
                "probes_matched": sum(1 for r in results if r.key),
                "segments": len(tracks),
                "identified": len(identified),
                "unidentified": len(tracks) - len(identified),
                "coverage": round(
                    sum(t.duration for t in identified) / duration, 3
                ) if duration else 0.0,
                "elapsed_seconds": round(time.monotonic() - started, 1),
                # Where the time actually went. The stages are wildly unequal
                # and which one dominates moves with the set and the service.
                "stage_seconds": timer.finish(),
                "strategy": self.config.strategy,
                "concurrency": self.config.concurrency,
            },
        )

    async def _extract_features(self, path: str, duration: float,
                                report: ProgressFn):
        """Stream the file through the feature extractor.

        Peak memory here is one block (2.6 MB) plus the accumulating feature
        vectors (~1.2 MB per hour), regardless of how long the set is.
        """
        report("decoding", DECODE_FROM, "Decoding audio...")
        features = StreamingFeatures(sample_rate=audio_io.ANALYSIS_SR)
        loop = asyncio.get_running_loop()

        seconds_done = 0.0
        last_pct = 5
        async for block in audio_io.stream_blocks(path):
            # Offloaded, not awaited inline. push() is librosa doing an STFT —
            # hundreds of milliseconds of solid CPU — and blocks arrive as fast
            # as ffmpeg can decode, so running it here pins the event loop for
            # nearly the whole analysis. The server then stops answering: the
            # healthcheck times out and the container is killed as unhealthy,
            # taking the analysis with it.
            #
            # A thread is enough because numpy and librosa drop the GIL for the
            # heavy parts; only one push runs at a time, so the accumulator is
            # never touched concurrently.
            await loop.run_in_executor(None, features.push, block)
            seconds_done += len(block) / audio_io.ANALYSIS_SR
            if duration > 0:
                pct = 5 + int(30 * min(1.0, seconds_done / duration))
                if pct > last_pct:
                    last_pct = pct
                    report("decoding", pct,
                           f"Analysing audio... {format_timestamp(seconds_done)}"
                           f" / {format_timestamp(duration)}")

        report("decoding", DECODE_TO, "Audio analysed. Planning probes...")
        return await loop.run_in_executor(None, features.finish)

    def _plan_probes(self, duration: float, features) -> List[float]:
        if self.config.strategy == "spectral":
            min_song = self.config.min_song_duration or _auto_min_duration(duration)
            threshold = self.config.peak_threshold or _auto_threshold(duration)
            bounds = spectral_boundaries(features, min_song, threshold)
            # Probe the middle of each detected segment: the safest distance
            # from both crossfades.
            return [round((bounds[i] + bounds[i + 1]) / 2, 3)
                    for i in range(len(bounds) - 1)]

        interval = self.config.probe_interval or auto_interval(duration)
        return grid_probes(duration, interval=interval)

    async def _run_probes(self, path: str, times: List[float],
                          report: ProgressFn) -> List[ProbeResult]:
        """Fingerprint every probe position, a bounded number at a time.

        The bound covers **extraction as well as identification**, which is the
        whole point. The identifier has its own semaphore, but it only guards
        the HTTP call — so gathering over every probe spawned one ffmpeg per
        probe up front, all of them alive at once while they queued for a slot.

        That scales with set length, which is exactly the wrong thing: a 30
        minute set opens ~95 processes and survives, a three hour set opens
        ~430 and the container is killed. Bounding here keeps the process count
        flat regardless of duration, matching what the streaming decode already
        does for memory.
        """
        total = len(times) or 1
        done = 0
        lock = asyncio.Lock()
        slots = asyncio.Semaphore(self.config.concurrency)

        async def one(t: float) -> ProbeResult:
            nonlocal done
            try:
                async with slots:
                    wav = await audio_io.extract_probe(
                        path, t, self.config.probe_duration)
                    match = await self.identifier.identify(wav)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Probe at %.1fs failed: %s", t, exc)
                match = None

            async with lock:
                done += 1
                pct = IDENTIFY_FROM + int(
                    (IDENTIFY_TO - IDENTIFY_FROM) * done / total)
                report("identifying", pct,
                       f"Identifying... {done}/{total} probes{self._waiting_note()}")

            if match is None:
                return ProbeResult(time=t)
            return ProbeResult(time=t, key=match.key, payload=_match_payload(match))

        return list(await asyncio.gather(*(one(t) for t in times)))

    def _waiting_note(self) -> str:
        """Say so when the run is sitting out a rate limit.

        Without this the progress bar simply stops, and a set that is waiting
        exactly as designed is indistinguishable from one that has hung — a
        distinction that cost an hour of staring at "82%" once already.
        """
        gate = getattr(self.identifier, "_gate", None)
        pause = getattr(gate, "paused", 0.0) if gate else 0.0
        if pause <= 1:
            return ""
        return f" — Shazam is rate-limiting, waiting {int(pause)}s"

    async def _confirm_segments(self, path: str, segments: List[Segment],
                                probes: List[ProbeResult], report: ProgressFn,
                                min_segment: float) -> List[Segment]:
        """Re-probe segments that rest on too little evidence.

        A track found by a single probe reports 100% agreement, because it
        agrees with itself. That is the weakest possible finding dressed as the
        strongest, and on a grid it happens constantly: short tracks and the
        stretch either side of a transition often catch only one probe.

        Extra probes go in spread across the segment — a second probe near the
        first re-reads the same audio and confirms nothing. They can also
        overturn the result: if the newcomers agree with each other rather than
        with the original, the majority wins and the segment changes hands.

        Segments already carrying enough votes are left alone, so a well-probed
        set costs nothing here.
        """
        wanted = self.config.votes_per_segment
        if wanted <= 1:
            return segments

        probed_at = [p.time for p in probes]
        plan: List[tuple] = []          # (segment index, probe time)
        for index, segment in enumerate(segments):
            if not segment.identified:
                continue                # nothing to confirm; a gap is a gap
            for t in confirmation_times(segment, probed_at, wanted,
                                         self.config.probe_duration):
                plan.append((index, t))

        if not plan:
            return segments

        # Published before the stage runs, because this is the number that
        # makes the countdown honest: confirmation probes cost the same as any
        # other, but they are packed into a twentieth of the bar, so how many
        # there are decides whether this stage is a moment or a third of the
        # run. Guessing it from a table missed a real run by 215%.
        if self.on_work is not None:
            try:
                self.on_work(len(probes), len(plan))
            except Exception:
                logger.debug("work callback raised", exc_info=True)

        report("confirming", CONFIRM_FROM,
               f"Confirming {len({i for i, _ in plan})} thinly-probed tracks...")

        slots = asyncio.Semaphore(self.config.concurrency)

        async def one(index: int, t: float):
            async with slots:
                try:
                    wav = await audio_io.extract_probe(
                        path, t, self.config.probe_duration)
                    match = await self.identifier.identify(wav)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Confirmation probe at %.1fs failed: %s", t, exc)
                    return index, None
            return index, match

        outcomes = await asyncio.gather(*(one(i, t) for i, t in plan))

        # Tally per segment, counting the original votes for the incumbent.
        tallies: Dict[int, Dict[str, int]] = {}
        extra_probes: Dict[int, int] = {}
        payloads: Dict[str, Dict[str, Any]] = {}
        for index, match in outcomes:
            extra_probes[index] = extra_probes.get(index, 0) + 1
            if match is None:
                continue
            counts = tallies.setdefault(index, {})
            counts[match.key] = counts.get(match.key, 0) + 1
            payloads.setdefault(match.key, _match_payload(match))

        for index, added in extra_probes.items():
            segment = segments[index]
            counts = dict(tallies.get(index, {}))
            if segment.key:
                counts[segment.key] = counts.get(segment.key, 0) + segment.votes

            segment.probes += added
            if not counts:
                continue                # every extra probe came back empty

            winner = max(counts, key=lambda k: counts[k])
            if winner != segment.key:
                logger.info("Confirmation overturned %s -> %s at %.0fs",
                            segment.key, winner, segment.start)
                segment.key = winner
                segment.payload = payloads.get(winner, segment.payload)
            segment.votes = counts[winner]

        # Overturning a segment can leave it naming the same record as the
        # segment beside it. Merging again is what turns that back into one
        # track — without it the set reports the same record playing two or
        # three times in a row, which is what a DJ notices first.
        merged = coalesce(segments, min_segment)
        if len(merged) != len(segments):
            logger.info("Confirmation left %d adjacent duplicate(s), merged",
                        len(segments) - len(merged))

        report("confirming", CONFIRM_TO, "Confirmation complete")
        return merged

    def _to_tracks(self, segments: List[Segment]) -> List[Track]:
        tracks: List[Track] = []
        for i, seg in enumerate(segments):
            payload = seg.payload or {}
            tracks.append(Track(
                index=i + 1,
                start=seg.start,
                end=seg.end,
                start_label=format_timestamp(seg.start),
                duration=round(seg.duration, 3),
                identified=seg.identified,
                title=payload.get("title", "ID ?"),
                artist=payload.get("artist", ""),
                url=payload.get("url", ""),
                cover_url=payload.get("cover_url", ""),
                album=payload.get("album", ""),
                label=payload.get("label", ""),
                year=payload.get("year", ""),
                genre=payload.get("genre", ""),
                isrc=payload.get("isrc", ""),
                key=seg.key or "",
                confidence=seg.confidence,
                agreement=seg.agreement,
                strength=seg.strength,
                votes=seg.votes,
                probes=seg.probes,
            ))
        return tracks

    async def _add_musical_features(self, path: str, tracks: List[Track],
                                    report: ProgressFn) -> None:
        """BPM and Camelot key from a stable window inside each track.

        The window is taken from the middle of the segment, away from both
        transitions, where the tempo grid and harmony are cleanest.
        """
        report("features", FEATURES_FROM, "Detecting BPM and key...")
        sem = asyncio.Semaphore(max(2, self.config.concurrency // 2))
        loop = asyncio.get_running_loop()

        async def one(track: Track) -> None:
            if track.duration < 20:
                return
            window = min(30.0, track.duration * 0.6)
            start = track.start + (track.duration - window) / 2
            async with sem:
                try:
                    pcm = await audio_io.extract_pcm(path, start, window)
                except Exception as exc:
                    logger.debug("PCM extract failed for track %d: %s", track.index, exc)
                    return
                # librosa is CPU-bound and releases the GIL in its inner loops;
                # a thread keeps the event loop responsive for other probes.
                track.bpm = await loop.run_in_executor(
                    None, estimate_bpm, pcm, audio_io.ANALYSIS_SR)
                key = await loop.run_in_executor(
                    None, estimate_key, pcm, audio_io.ANALYSIS_SR)
            if key is not None:
                track.camelot = key.camelot
                track.musical_key = key.label

        await asyncio.gather(*(one(t) for t in tracks))
        report("features", FEATURES_TO, "BPM and key detected")


def _match_payload(match: TrackMatch) -> Dict[str, Any]:
    return {
        "title": match.title,
        "artist": match.artist,
        "url": match.url,
        "cover_url": match.cover_url,
        "album": match.album,
        "label": match.label,
        "year": match.year,
        "genre": match.genre,
        "isrc": match.isrc,
        "provider": match.provider,
    }


def _auto_min_duration(duration: float) -> float:
    hours = duration / 3600
    if hours < 1:
        return 30.0
    if hours < 2:
        return 45.0
    if hours < 3:
        return 60.0
    return 90.0


def _auto_threshold(duration: float) -> float:
    hours = duration / 3600
    if hours < 1:
        return 0.30
    if hours < 2:
        return 0.25
    if hours < 3:
        return 0.20
    return 0.15
