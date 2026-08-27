"""The streaming decode path — the fix that removed the memory ceiling."""
import asyncio

import librosa
import numpy as np
import pytest

from src.core.audio import (ANALYSIS_SR, extract_pcm, probe_duration,
                            stream_blocks)
from src.core.features import HOP_LENGTH, N_FFT, StreamingFeatures

pytestmark = pytest.mark.anyio


async def test_probe_duration_matches_source(synthetic_set):
    duration = await probe_duration(synthetic_set["path"])
    assert duration == pytest.approx(synthetic_set["duration"], abs=0.2)


async def test_probe_duration_unknown_file_returns_zero(tmp_path):
    missing = tmp_path / "nope.mp3"
    assert await probe_duration(str(missing)) == 0.0


async def test_stream_blocks_covers_the_whole_file(synthetic_set):
    total = 0
    blocks = 0
    async for block in stream_blocks(synthetic_set["path"]):
        assert block.dtype == np.float32
        total += block.size
        blocks += 1

    assert blocks > 1, "a 160s file must arrive in several blocks"
    assert total / ANALYSIS_SR == pytest.approx(synthetic_set["duration"], abs=0.5)


async def test_streaming_features_match_a_single_shot_computation(synthetic_set):
    """The whole refactor rests on this: streaming must not change the result.

    Blocks are fed through the accumulator, then the same samples are handed to
    librosa in one call. The two must agree exactly — any drift would mean the
    frame carry-over between blocks is wrong.
    """
    blocks = []
    features = StreamingFeatures(sample_rate=ANALYSIS_SR)
    async for block in stream_blocks(synthetic_set["path"]):
        blocks.append(block.copy())
        features.push(block)
    result = features.finish()

    y = np.concatenate(blocks)
    reference = librosa.feature.spectral_centroid(
        y=y, sr=ANALYSIS_SR, n_fft=N_FFT, hop_length=HOP_LENGTH, center=False)[0]

    assert result.centroid.size == reference.size
    np.testing.assert_allclose(result.centroid, reference, rtol=0, atol=0)


async def test_streaming_memory_does_not_grow_with_duration(tmp_path):
    """Peak memory must be flat in duration — that is the entire point.

    Two files, one four times longer than the other. The longer one may use a
    little more (the feature vectors themselves grow, ~1.2 MB per hour) but
    nothing close to proportionally.
    """
    import tracemalloc

    import soundfile as sf

    def write(seconds: float, name: str) -> str:
        t = np.linspace(0, seconds, int(44100 * seconds), endpoint=False)
        sf.write(str(tmp_path / name),
                 (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 44100)
        return str(tmp_path / name)

    async def peak_for(path: str) -> int:
        features = StreamingFeatures(sample_rate=ANALYSIS_SR)
        tracemalloc.start()
        tracemalloc.reset_peak()
        async for block in stream_blocks(path):
            features.push(block)
        features.finish()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return peak

    short = write(30, "short.wav")
    long = write(120, "long.wav")

    await peak_for(short)                      # warm up librosa's caches
    peak_short = await peak_for(short)
    peak_long = await peak_for(long)

    assert peak_long < peak_short * 1.5, (
        f"memory scaled with duration: {peak_short/1e6:.1f} MB for 30s vs "
        f"{peak_long/1e6:.1f} MB for 120s (4x the audio)"
    )


async def test_extract_pcm_seeks_to_the_requested_window(synthetic_set):
    window = await extract_pcm(synthetic_set["path"], start=45.0, duration=5.0)
    assert window.dtype == np.float32
    assert window.size / ANALYSIS_SR == pytest.approx(5.0, abs=0.3)


async def test_waveform_peaks_are_normalised(synthetic_set):
    features = StreamingFeatures(sample_rate=ANALYSIS_SR)
    async for block in stream_blocks(synthetic_set["path"]):
        features.push(block)
    peaks = features.finish().waveform_peaks(400)

    assert len(peaks) == 400
    assert all(0.0 <= p <= 1.0 for p in peaks)
    assert max(peaks) == pytest.approx(1.0)
