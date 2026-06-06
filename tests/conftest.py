from unittest.mock import patch, AsyncMock

import pytest
from httpx import AsyncClient, ASGITransport

from src.web import app, analysis_tasks


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_real_downloads(request):
    """Stop non-integration tests from spawning the real yt-dlp subprocess.

    `/api/download-url` fires `asyncio.create_task(download_and_analyze(...))`,
    which shells out to yt-dlp against the submitted URL. The "unit" download
    tests POST a real YouTube link, so without this the background task tries
    to download from YouTube on the CI runner, hangs (bot-blocked, no
    timeout), and the event-loop teardown waits on the stuck subprocess —
    pytest then ran to the 6h job limit. Integration tests opt back in.
    """
    if request.node.get_closest_marker("integration"):
        yield
        return
    with patch("src.web.download_and_analyze", new=AsyncMock(return_value=None)):
        yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def clear_tasks():
    """Clear analysis tasks between tests."""
    analysis_tasks.clear()
    yield
    analysis_tasks.clear()
