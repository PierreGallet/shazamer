"""Streaming audio I/O built on ffmpeg.

The whole point of this module is that **the decoded set never lives in RAM**.

The previous implementation called `librosa.load()` on the full file, which
materialised the entire signal (635 MB for a 2 h set at 22.05 kHz float32,
before resampling copies) and then handed it to `spectral_centroid`, whose
peak was measured at 63.6 MB per minute of audio — 7.6 GB for two hours,
against a 6 GB container. Anything past ~1 h 20 was structurally doomed.

Here ffmpeg decodes to a pipe and we consume fixed-size blocks, so peak
memory is a function of the block size and nothing else. Duration becomes
irrelevant.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Analysis rate. High enough that the spectral centroid stays meaningful
# (Nyquist ~11 kHz), low enough to keep the frame count manageable.
ANALYSIS_SR = 22050

# Shazam's fingerprinter downsamples to 16 kHz mono internally, so handing it
# anything richer is wasted decoding on both sides.
PROBE_SR = 16000

# 30 s of float32 at the analysis rate = 2.6 MB. This is the entire memory
# budget of the decode stage, whatever the length of the set.
BLOCK_SECONDS = 30


class FFmpegError(RuntimeError):
    """ffmpeg exited non-zero. Carries the tail of stderr for diagnostics."""


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise FFmpegError(
            "ffmpeg not found on PATH. Install it (brew install ffmpeg / "
            "apt-get install ffmpeg) — it is required for all audio decoding."
        )
    return path


def ffprobe_bin() -> Optional[str]:
    return shutil.which("ffprobe")


async def probe_duration(path: str) -> float:
    """Return duration in seconds without decoding a single sample.

    Falls back to 0.0 when ffprobe is unavailable or the container carries no
    duration metadata; callers treat 0.0 as "unknown", never as "empty".
    """
    exe = ffprobe_bin()
    if exe is None:
        logger.warning("ffprobe not found; duration unknown for %s", path)
        return 0.0

    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return 0.0
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return 0.0


async def stream_blocks(
    path: str,
    sample_rate: int = ANALYSIS_SR,
    block_seconds: int = BLOCK_SECONDS,
) -> AsyncIterator[np.ndarray]:
    """Decode `path` to mono float32 and yield it block by block.

    Each yielded array is a *view over a fresh buffer* — it is safe to keep a
    reference to derived features, but the audio itself is dropped as soon as
    the consumer moves on, which is what keeps peak memory flat.
    """
    block_bytes = sample_rate * block_seconds * 4  # float32

    proc = await asyncio.create_subprocess_exec(
        ffmpeg_bin(), "-v", "error", "-nostdin",
        "-i", path,
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "f32le",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=block_bytes * 2,
    )
    assert proc.stdout is not None and proc.stderr is not None

    # Drain stderr concurrently. ffmpeg writes little at -v error, but a full
    # pipe buffer would deadlock the stdout reader.
    stderr_chunks: list[bytes] = []

    async def drain_stderr() -> None:
        while True:
            chunk = await proc.stderr.read(8192)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    drainer = asyncio.create_task(drain_stderr())
    try:
        while True:
            # readexactly raises IncompleteReadError on the final short block,
            # which is the normal way this loop ends.
            try:
                raw = await proc.stdout.readexactly(block_bytes)
            except asyncio.IncompleteReadError as exc:
                raw = exc.partial
                if raw:
                    yield np.frombuffer(raw, dtype=np.float32)
                break
            yield np.frombuffer(raw, dtype=np.float32)
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
        await drainer

    if proc.returncode not in (0, None):
        tail = b"".join(stderr_chunks).decode(errors="replace").strip()
        raise FFmpegError(f"ffmpeg failed decoding {path}: {tail[-500:]}")


async def extract_probe(path: str, start: float, duration: float = 12.0) -> bytes:
    """Return `duration` seconds of WAV starting at `start`, as bytes.

    `-ss` is placed *before* `-i` on purpose: that selects input seeking, so
    ffmpeg jumps straight to the timestamp instead of decoding from zero. On a
    3 h set the difference is seconds versus minutes.

    The result goes straight to the identifier as bytes — no temp file, so
    nothing to clean up and nothing to leak when a task is cancelled.
    """
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_bin(), "-v", "error", "-nostdin",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", path,
        "-ac", "1",
        "-ar", str(PROBE_SR),
        "-c:a", "pcm_s16le",
        "-f", "wav",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError(
            f"ffmpeg probe failed at {start:.1f}s: "
            f"{err.decode(errors='replace').strip()[-300:]}"
        )
    return out


async def extract_pcm(path: str, start: float, duration: float,
                      sample_rate: int = ANALYSIS_SR) -> np.ndarray:
    """Same seek trick, but returns float32 samples for feature extraction.

    Used for per-track BPM and key detection, where we want a window of a
    known track rather than a fingerprint probe.
    """
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_bin(), "-v", "error", "-nostdin",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", path,
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "f32le",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError(
            f"ffmpeg pcm extract failed at {start:.1f}s: "
            f"{err.decode(errors='replace').strip()[-300:]}"
        )
    return np.frombuffer(out, dtype=np.float32)


@dataclass(frozen=True)
class SourceInfo:
    """What we know about a media file before touching its samples."""
    path: str
    duration: float
    def __post_init__(self) -> None:
        if self.duration < 0:
            raise ValueError("duration cannot be negative")
