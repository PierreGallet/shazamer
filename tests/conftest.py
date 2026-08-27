"""Shared fixtures.

Nothing here touches the network or Shazam. The pipeline is exercised against
synthetic audio with a stub identifier, which is both faster and honest: what
we want to verify is our segmentation, merging and streaming, not that
Shazam's servers are up.
"""
import asyncio
from pathlib import Path
from typing import List, Optional

import numpy as np
import pytest
import soundfile as sf
from httpx import ASGITransport, AsyncClient

from src.identify.base import TrackMatch

SOURCE_SR = 44100


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_set(path: Path, plan: List[Optional[tuple]], seconds: float) -> Path:
    """Write a synthetic 'set': one tone per planned track, hard-cut between."""
    parts = []
    for i, entry in enumerate(plan):
        t = np.linspace(0, seconds, int(SOURCE_SR * seconds), endpoint=False)
        freq = 180 + i * 90
        beat = (np.sin(2 * np.pi * 2.1 * t) > 0.7).astype(np.float32)
        env = 0.6 + 0.4 * np.sin(np.linspace(0, np.pi, t.size))
        parts.append(((0.25 * np.sin(2 * np.pi * freq * t) + 0.3 * beat) * env
                      ).astype(np.float32))
    sf.write(str(path), np.concatenate(parts), SOURCE_SR)
    return path


@pytest.fixture
def synthetic_set(tmp_path: Path):
    """A 4-track set, 40 s each, with the third track unidentifiable."""
    plan = [("Artist A", "Track A"), ("Artist B", "Track B"),
            None, ("Artist D", "Track D")]
    seconds = 40.0
    path = _make_set(tmp_path / "set.wav", plan, seconds)
    return {"path": str(path), "plan": plan, "segment_seconds": seconds,
            "duration": seconds * len(plan)}


@pytest.fixture
def stub_identifier(synthetic_set, monkeypatch):
    """Identify by probe position, bypassing audio entirely.

    `extract_probe` is replaced with a tagger so the stub knows which part of
    the set it is being asked about — deterministic, and it keeps the test
    independent of ffmpeg's seek accuracy.
    """
    import src.core.audio as audio_io

    seconds = synthetic_set["segment_seconds"]
    plan = synthetic_set["plan"]

    async def tagged_probe(path, start, duration=12.0):
        return f"T={start:<28.3f}".encode()[:30]

    monkeypatch.setattr(audio_io, "extract_probe", tagged_probe)

    class StubIdentifier:
        name = "stub"

        def __init__(self):
            self.calls = 0
            self.concurrent = 0
            self.max_concurrent = 0

        async def identify(self, wav_bytes: bytes) -> Optional[TrackMatch]:
            self.calls += 1
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            try:
                await asyncio.sleep(0.01)
                start = float(wav_bytes[2:].decode().strip())
                index = min(int(start // seconds), len(plan) - 1)
                entry = plan[index]
                if entry is None:
                    return None
                return TrackMatch(title=entry[1], artist=entry[0],
                                  provider="stub", label="Test Label")
            finally:
                self.concurrent -= 1

    return StubIdentifier()


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """A fresh app instance per test, with all state under tmp_path."""
    import importlib

    for name in ("UPLOAD_DIR", "MEDIA_DIR", "DATA_DIR", "TMP_DIR"):
        (tmp_path / name.lower()).mkdir(exist_ok=True)

    monkeypatch.setenv("SLSKD_URL", "")
    import src.web as web
    importlib.reload(web)

    web.UPLOAD_DIR = tmp_path / "uploads"
    web.MEDIA_DIR = tmp_path / "media"
    for directory in (web.UPLOAD_DIR, web.MEDIA_DIR):
        directory.mkdir(exist_ok=True)
    web.library = web.Library(tmp_path / "library.db")
    web.tasks = web.TaskManager(tmp_path / "tasks")

    transport = ASGITransport(app=web.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.web = web  # type: ignore[attr-defined]
        yield c
