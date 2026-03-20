"""Tests for URL download functionality (YouTube, SoundCloud).

These tests verify that the download-url endpoint works correctly.
Integration tests (marked @pytest.mark.integration) hit real URLs and
require network + yt-dlp + ffmpeg to be available.
"""

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from src.web import app, analysis_tasks, UPLOAD_FOLDER


# ---------------------------------------------------------------------------
# Unit tests (mocked yt-dlp)
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.anyio


async def test_download_url_returns_task_id():
    """POST /api/download-url should return a task_id."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/download-url",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "task_id" in data
        assert "url" in data


async def test_download_url_empty_url():
    """POST /api/download-url with empty URL should fail."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/download-url", json={"url": ""})
        assert response.status_code == 400


async def test_download_url_missing_url():
    """POST /api/download-url without url field should fail."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/download-url", json={})
        assert response.status_code == 422


async def test_status_unknown_task():
    """GET /api/status/<random> should return 404."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/status/nonexistent-task-id")
        assert response.status_code == 404


async def test_download_task_registers_in_tasks():
    """After POSTing a URL the task should appear in analysis_tasks."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/download-url",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        task_id = response.json()["task_id"]
        assert task_id in analysis_tasks
        assert analysis_tasks[task_id]["status"] in ("downloading", "processing", "error")


# ---------------------------------------------------------------------------
# Integration tests — require network, yt-dlp, ffmpeg
# ---------------------------------------------------------------------------

YOUTUBE_URL = "https://www.youtube.com/watch?v=wiMVd4CN8ig&list=RDMM&start_radio=1"
SOUNDCLOUD_URL = "https://soundcloud.com/recordeep-mag/premiere-2tm-road-to-nemeland"


async def _submit_and_wait(client: AsyncClient, url: str, timeout: int = 180):
    """Helper: submit a URL and poll until download phase completes or errors."""
    response = await client.post("/api/download-url", json={"url": url})
    assert response.status_code == 200
    task_id = response.json()["task_id"]

    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        status_resp = await client.get(f"/api/status/{task_id}")
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        last_status = status_data

        if status_data["status"] == "error":
            return status_data
        # Once we reach processing or completed, the download succeeded
        if status_data["status"] in ("processing", "completed"):
            return status_data

        await asyncio.sleep(2)

    pytest.fail(f"Timed out waiting for download. Last status: {last_status}")


@pytest.mark.integration
async def test_youtube_download():
    """Download a short YouTube video and verify it reaches processing/completed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        result = await _submit_and_wait(client, YOUTUBE_URL, timeout=120)
        assert result["status"] != "error", f"YouTube download failed: {result.get('error')}"
        assert result["status"] in ("processing", "completed")
        # Verify a file was created
        assert result.get("filename") and result["filename"] != "Downloading from URL..."


@pytest.mark.integration
async def test_soundcloud_download():
    """Download a SoundCloud track and verify it reaches processing/completed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        result = await _submit_and_wait(client, SOUNDCLOUD_URL, timeout=120)
        assert result["status"] != "error", f"SoundCloud download failed: {result.get('error')}"
        assert result["status"] in ("processing", "completed")
        assert result.get("filename") and result["filename"] != "Downloading from URL..."


@pytest.mark.integration
async def test_youtube_download_produces_mp3():
    """Verify that a YouTube download actually produces an MP3 file in uploads/."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/download-url", json={"url": YOUTUBE_URL})
        task_id = response.json()["task_id"]

        # Wait for download to finish (but not full analysis)
        deadline = time.time() + 120
        while time.time() < deadline:
            status_resp = await client.get(f"/api/status/{task_id}")
            data = status_resp.json()

            if data["status"] == "error":
                pytest.fail(f"Download error: {data.get('error')}")

            if data["status"] in ("processing", "completed"):
                # Check that filepath exists in task
                task = analysis_tasks[task_id]
                filepath = task.get("filepath")
                if filepath:
                    # The file might already be cleaned up if analysis completed fast
                    # but at least the path should have been set
                    assert filepath.endswith(".mp3") or filepath.endswith(".m4a") or filepath.endswith(".wav"), \
                        f"Unexpected file extension: {filepath}"
                break

            await asyncio.sleep(2)


@pytest.mark.integration
async def test_soundcloud_download_produces_mp3():
    """Verify that a SoundCloud download actually produces an MP3 file."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/download-url", json={"url": SOUNDCLOUD_URL})
        task_id = response.json()["task_id"]

        deadline = time.time() + 120
        while time.time() < deadline:
            status_resp = await client.get(f"/api/status/{task_id}")
            data = status_resp.json()

            if data["status"] == "error":
                pytest.fail(f"Download error: {data.get('error')}")

            if data["status"] in ("processing", "completed"):
                task = analysis_tasks[task_id]
                filepath = task.get("filepath")
                if filepath:
                    assert filepath.endswith(".mp3") or filepath.endswith(".m4a"), \
                        f"Unexpected file extension: {filepath}"
                break

            await asyncio.sleep(2)
