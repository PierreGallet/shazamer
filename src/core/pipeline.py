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
from .segment import (ProbeResult, Segment, auto_interval, grid_probes,
                      merge_probes, spectral_boundaries)
from .timecode import format_timestamp

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, str], None]


@dataclass
class AnalyzeConfig:
    strategy: str = "grid"          # "grid" (default) or "spectral"
    probe_interval: Optional[float] = None   # None → derived from duration
    probe_duration: float = 12.0    # Shazam uses a centred 10 s of this
    votes_per_segment: int = 1      # >1 adds confirmation probes per candidate
    concurrency: int = 8
    waveform_points: int = 1600
    min_segment: float = 20.0
    compute_musical_features: bool = True
    # Legacy spectral-strategy knobs, unused by the grid strategy.
    min_song_duration: Optional[float] = None
    peak_threshold: Optional[float] = None


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
    confidence: float = 0.0
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
                 config: Optional[AnalyzeConfig] = None) -> None:
        self.identifier = identifier
        self.config = config or AnalyzeConfig()

    async def run(self, path: str, on_progress: Optional[ProgressFn] = None
                  ) -> AnalysisResult:
        started = time.monotonic()

        def report(stage: str, pct: int, message: str) -> None:
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
        report("identifying", 36,
               f"Identifying {len(probes)} probes across {format_timestamp(duration)}...")
        results = await self._run_probes(path, probes, report)

        # ── Stage 3: probes → segments ────────────────────────────────────
        report("merging", 86, "Merging probes into tracks...")
        segments = merge_probes(results, duration, features,
                                min_segment=self.config.min_segment)
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
        report("decoding", 5, "Decoding audio...")
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

        report("decoding", 35, "Audio analysed. Planning probes...")
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
                pct = 36 + int(49 * done / total)
                report("identifying", pct, f"Identifying... {done}/{total} probes")

            if match is None:
                return ProbeResult(time=t)
            return ProbeResult(time=t, key=match.key, payload=_match_payload(match))

        return list(await asyncio.gather(*(one(t) for t in times)))

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
        report("features", 88, "Detecting BPM and key...")
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
        report("features", 95, "BPM and key detected")


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
