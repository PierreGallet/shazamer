import pytest
from httpx import AsyncClient, ASGITransport

from src.web import app, analysis_tasks


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
