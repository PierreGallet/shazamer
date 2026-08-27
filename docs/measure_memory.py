#!/usr/bin/env python3
"""Reproduce the memory table in architecture.md.

    PYTHONPATH=. ./venv/bin/python docs/measure_memory.py

Compares peak allocation across three paths at several durations:

  * n_fft=2048 — one-shot, what the deployed web path used
  * n_fft=1024 — one-shot, what the CLI used
  * streaming  — the current pipeline

The first two scale linearly with duration. The third does not, which is the
whole point of the rewrite.
"""
import asyncio
import gc
import os
import tempfile
import tracemalloc

import librosa
import numpy as np
import soundfile as sf

from src.core.audio import ANALYSIS_SR, stream_blocks
from src.core.features import HOP_LENGTH, N_FFT, StreamingFeatures

DURATIONS_MINUTES = (2, 5, 10, 20)
SOURCE_SR = 44100
tmp = tempfile.mkdtemp()


def write(minutes: int) -> str:
    path = os.path.join(tmp, f"{minutes}.wav")
    t = np.linspace(0, 60 * minutes, SOURCE_SR * 60 * minutes, endpoint=False)
    sf.write(path, (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), SOURCE_SR)
    del t
    gc.collect()
    return path


def oneshot_peak(path: str, n_fft: int) -> int:
    """Peak of loading the file whole, then taking its STFT in one call."""
    y = librosa.load(path, sr=ANALYSIS_SR, mono=True, res_type="soxr_hq")[0]
    tracemalloc.start()
    tracemalloc.reset_peak()
    librosa.feature.spectral_centroid(
        y=y, sr=ANALYSIS_SR, n_fft=n_fft, hop_length=HOP_LENGTH, center=False)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    total = peak + y.nbytes          # the decoded signal counts too
    del y
    gc.collect()
    return total


async def streaming_peak(path: str) -> int:
    features = StreamingFeatures(sample_rate=ANALYSIS_SR)
    tracemalloc.start()
    tracemalloc.reset_peak()
    async for block in stream_blocks(path):
        features.push(block)
    features.finish()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def main() -> None:
    warmup = write(1)
    asyncio.run(streaming_peak(warmup))   # let librosa allocate its caches
    os.remove(warmup)

    print(f"{'duration':>9} | {'n_fft=2048':>12} | {'n_fft=1024':>12} | {'streaming':>12}")
    print("-" * 58)

    rows = []
    for minutes in DURATIONS_MINUTES:
        path = write(minutes)
        wide = oneshot_peak(path, 2048)
        narrow = oneshot_peak(path, N_FFT)
        streamed = asyncio.run(streaming_peak(path))
        rows.append((minutes, wide, narrow, streamed))
        print(f"{minutes:>6} min | {wide/1e6:>9.1f} MB | {narrow/1e6:>9.1f} MB "
              f"| {streamed/1e6:>9.1f} MB")
        os.remove(path)

    minutes = np.array([r[0] for r in rows], dtype=float)
    print()
    for index, label in ((1, "n_fft=2048"), (2, "n_fft=1024"), (3, "streaming")):
        peaks = np.array([r[index] for r in rows], dtype=float)
        slope = float(np.polyfit(minutes, peaks, 1)[0])
        spread = peaks.std() / peaks.mean()
        verdict = "FLAT" if spread < 0.15 else f"+{slope/1e6:.1f} MB/min"
        extra = "" if spread < 0.15 else f"  →  2 h = {slope * 120 / 1e9:.2f} GB"
        print(f"  {label:<11} {verdict}{extra}")


if __name__ == "__main__":
    main()
