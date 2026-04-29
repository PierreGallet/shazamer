"""Tests for DJSetAnalyzer's audio pipeline.

These tests exercise the real librosa code path on a synthetic WAV. They
catch missing-resampler and similar dependency issues that would otherwise
only surface in production (e.g. the kaiser_fast → resampy regression).
"""
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from src.shazamer import DJSetAnalyzer


SAMPLE_RATE_SOURCE = 48000
DURATION_SECONDS = 60  # short — must still produce >=1 segment after detection


@pytest.fixture
def synthetic_wav(tmp_path: Path) -> Path:
    """Create a 60s 48kHz mono WAV with sine sweeps so spectral detection works."""
    n_samples = SAMPLE_RATE_SOURCE * DURATION_SECONDS
    t = np.linspace(0, DURATION_SECONDS, n_samples, endpoint=False)

    # Two sweeping sines with a transition halfway → at least one boundary
    half = n_samples // 2
    audio = np.empty(n_samples, dtype=np.float32)
    audio[:half] = 0.3 * np.sin(2 * np.pi * 440 * t[:half]).astype(np.float32)
    audio[half:] = 0.3 * np.sin(2 * np.pi * 880 * t[half:]).astype(np.float32)

    path = tmp_path / "synthetic.wav"
    sf.write(str(path), audio, SAMPLE_RATE_SOURCE)
    return path


def test_load_audio_resamples_to_target_sr(synthetic_wav: Path):
    """load_audio must resample to target_sr without missing-package errors.

    Regression guard: setting res_type to a value that requires `resampy` (e.g.
    "kaiser_fast") used to crash at runtime because resampy isn't pinned in
    requirements.txt. soxr_hq uses `soxr` which is a hard dep of librosa.
    """
    analyzer = DJSetAnalyzer(str(synthetic_wav), target_sr=22050)
    audio_data, sample_rate = analyzer.load_audio()

    assert sample_rate == 22050, f"Expected resample to 22050, got {sample_rate}"
    expected_samples = 22050 * DURATION_SECONDS
    # Allow ±1 sample tolerance from resampling rounding
    assert abs(len(audio_data) - expected_samples) <= 1
    assert audio_data.dtype == np.float32


def test_load_audio_native_sample_rate(synthetic_wav: Path):
    """target_sr=None should preserve the source rate."""
    analyzer = DJSetAnalyzer(str(synthetic_wav), target_sr=None)
    audio_data, sample_rate = analyzer.load_audio()

    assert sample_rate == SAMPLE_RATE_SOURCE
    assert len(audio_data) == SAMPLE_RATE_SOURCE * DURATION_SECONDS


def test_detect_song_boundaries_runs(synthetic_wav: Path):
    """detect_song_boundaries should produce at least 2 boundaries (start, end)."""
    analyzer = DJSetAnalyzer(
        str(synthetic_wav),
        target_sr=22050,
        min_song_duration=10,
        peak_threshold=0.3,
    )
    audio_data, sample_rate = analyzer.load_audio()
    boundaries = analyzer.detect_song_boundaries(audio_data, sample_rate)

    assert len(boundaries) >= 2
    assert boundaries[0] == 0
    assert boundaries[-1] == len(audio_data)


def test_probe_duration(synthetic_wav: Path):
    """probe_duration should read duration without loading the audio.

    Regression guard: this is what powers the MAX_AUDIO_DURATION_SECONDS guard
    in web.py. If pydub / ffprobe is missing or its API breaks, the guard
    silently disables itself. We assert it returns a sensible value here.
    """
    from src.web import probe_duration

    duration = probe_duration(str(synthetic_wav))
    assert abs(duration - DURATION_SECONDS) < 1.0, (
        f"Expected ~{DURATION_SECONDS}s, got {duration}"
    )
