"""Own Soulseek accounts through the API (RFC #137, S1 and S3).

The rule that matters most: someone who saved their own account never
reaches the shared slskd, even when their account fails.
"""
import sqlite3

import pytest

from src.acquire import own_soulseek
from tests.test_own_soulseek import FakeClient, hit

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _fresh_fake():
    FakeClient.instances = []
    FakeClient.refuse_next = None


@pytest.fixture
def web(client, monkeypatch):
    web = client.web
    monkeypatch.setattr(web.OWN_SOULSEEK, "client_factory", FakeClient)
    monkeypatch.setattr(own_soulseek, "SEARCH_WAIT", 0)
    return web


@pytest.fixture
def no_slskd(web, monkeypatch):
    """slskd is configured, and touching it fails the test."""
    import src.acquire.slskd as slskd

    monkeypatch.setenv("SLSKD_URL", "http://slskd")
    calls = []

    async def request(self, method, path, **kwargs):
        calls.append((method, path))
        raise AssertionError(f"slskd was called: {method} {path}")

    monkeypatch.setattr(slskd.SlskdClient, "_request", request)
    return calls


async def save(client, password="hunter2-secret"):
    return await client.put("/api/profile/soulseek",
                            json={"username": "dj_alice", "password": password})


# ── Credentials (AC-1) ──────────────────────────────────────────────────────

async def test_nothing_is_saved_until_a_login_succeeds(client, web):
    FakeClient.refuse_next = "INVALIDPASS"
    response = await save(client)
    assert response.status_code == 400
    assert "INVALIDPASS" in response.json()["detail"]
    assert (await client.get("/api/profile/soulseek")).json()["configured"] is False


async def test_saving_checks_the_login_and_never_echoes_the_password(
        client, web, tmp_path):
    response = await save(client)
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True and body["username"] == "dj_alice"
    assert "hunter2-secret" not in response.text
    assert FakeClient.instances[0].logged_in and FakeClient.instances[0].stopped

    got = await client.get("/api/profile/soulseek")
    assert "hunter2-secret" not in got.text
    raw = sqlite3.connect(tmp_path / "data" / "accounts.db").execute(
        "SELECT password FROM soulseek_credentials").fetchone()[0]
    assert b"hunter2-secret" not in bytes(raw)


async def test_removing_closes_the_session_and_forgets(client, web):
    await save(client)
    session = await web.OWN_SOULSEEK.open(
        client.user_id, await web.soulseek_credentials.get(client.user_id))
    response = await client.delete("/api/profile/soulseek")
    assert response.status_code == 204
    assert session.client.stopped
    assert (await client.get("/api/profile/soulseek")).json()["configured"] is False


async def test_saving_is_rate_limited(client, web):
    FakeClient.refuse_next = "INVALIDPASS"
    codes = [(await save(client, f"pw{i}")).status_code for i in range(6)]
    assert codes[:5] == [400] * 5 and codes[5] == 429


# ── Routing, no fallback (AC-3) ─────────────────────────────────────────────

async def test_an_own_account_searches_through_itself(client, web, no_slskd):
    await save(client)
    session = await web.OWN_SOULSEEK.open(
        client.user_id, await web.soulseek_credentials.get(client.user_id))
    session.client.results = [hit("peer", "Music\\Artist - Title.mp3")]

    response = await client.post("/api/acquire/soulseek/search",
                                 json={"query": "artist title"})
    assert response.status_code == 200, response.text
    assert [c["username"] for c in response.json()["candidates"]] == ["peer"]
    assert no_slskd == []


async def test_a_failing_own_account_never_falls_back(client, web, no_slskd):
    await save(client)
    FakeClient.refuse_next = "INVALIDPASS"      # the password changed since
    response = await client.post("/api/acquire/soulseek/search",
                                 json={"query": "artist title"})
    assert response.status_code == 503
    assert "INVALIDPASS" in response.json()["detail"]
    assert no_slskd == []
    status = (await client.get("/api/profile/soulseek")).json()
    assert "INVALIDPASS" in status["last_error"]


async def test_own_account_downloads_stay_out_of_the_worker(
        client, web, no_slskd, monkeypatch):
    await save(client)
    enqueued = []

    async def enqueue(*args, **kwargs):
        enqueued.append(args)
        return True

    monkeypatch.setattr(web.jobs, "enqueue", enqueue)
    web.SOULSEEK_QUOTA.user_downloads_per_hour = 0   # the shared budget is spent

    response = await client.post("/api/acquire/track", json={
        "key": "a::x", "artist": "Artist", "title": "Title"})
    assert response.status_code == 200, response.text
    assert response.json()["queued"] is False
    assert enqueued == []
    for task in list(web._own_downloads):
        await task
    row = await web.library.get_download(response.json()["download_id"],
                                         user_id=client.user_id)
    assert row["status"] == "failed"            # the fake has no peers
    assert "No peer" in row["message"]
    assert no_slskd == []


async def test_an_own_account_enables_soulseek_without_slskd(client, web):
    before = await client.get("/api/acquire/sources?artist=A&title=X")
    assert before.json()["soulseek_configured"] is False
    await save(client)
    after = await client.get("/api/acquire/sources?artist=A&title=X")
    assert after.json()["soulseek_configured"] is True
    status = (await client.get("/api/acquire/soulseek/status")).json()
    assert status["configured"] is True and status["own_account"] == "dj_alice"
    # Answered from the last outcome, not by logging in again.
    assert len(FakeClient.instances) == 1


async def test_users_without_their_own_account_are_unchanged(client, web,
                                                             monkeypatch):
    client_for = await web.soulseek_for(client.user_id)
    assert isinstance(client_for, web.SlskdClient)


# ── After a restart ─────────────────────────────────────────────────────────

async def test_a_restart_fails_own_account_downloads_left_in_flight(client, web):
    await save(client)
    mine = await web.library.start_download("a::x", "A", "X",
                                            user_id=client.user_id)
    other = await web.library.start_download("b::y", "B", "Y",
                                             user_id="someone-else")
    await web.library.update_download(mine, status="downloading")
    await web.library.update_download(other, status="downloading")

    n = await web.library.fail_unfinished_downloads(
        await web.soulseek_credentials.user_ids(), "Interrupted")
    assert n == 1
    assert (await web.library.get_download(mine, user_id=None))["status"] == "failed"
    assert (await web.library.get_download(other, user_id=None))["status"] \
        == "downloading"


# ── Sharing your downloads ──────────────────────────────────────────────────

@pytest.fixture
def downloads(web, tmp_path, monkeypatch):
    directory = tmp_path / "library-downloads"
    directory.mkdir()
    monkeypatch.setattr(web, "DOWNLOAD_DIR", directory)
    return directory


async def _ready(web, user_id, path):
    path.write_bytes(b"audio")
    download_id = await web.library.start_download("k::t", "A", "T",
                                                   user_id=user_id)
    await web.library.update_download(download_id, status="ready",
                                      local_path=str(path))


async def _shared(web, client):
    session = await web.OWN_SOULSEEK.open(
        client.user_id, await web.soulseek_credentials.get(client.user_id))
    directories = session.client.settings.shares.directories
    if not directories:
        return []
    from pathlib import Path
    return sorted(p.resolve().name for p in Path(directories[0].path).iterdir())


async def test_your_own_account_shares_your_downloads_only(client, web,
                                                           downloads, tmp_path):
    await save(client)
    await _ready(web, client.user_id, downloads / "Mine.mp3")
    await _ready(web, "someone-else", downloads / "Theirs.mp3")
    outside = tmp_path / "elsewhere.mp3"
    await _ready(web, client.user_id, outside)        # not in the library
    assert await _shared(web, client) == ["Mine.mp3"]


async def test_sharing_can_be_turned_off(client, web, downloads):
    await save(client)
    await _ready(web, client.user_id, downloads / "Mine.mp3")
    assert (await _shared(web, client)) == ["Mine.mp3"]
    share_dir = web.OWN_SOULSEEK.share_dir(client.user_id)

    response = await client.patch("/api/profile/soulseek",
                                  json={"share_downloads": False})
    assert response.status_code == 200
    assert response.json()["share_downloads"] is False
    assert not share_dir.exists()
    assert await _shared(web, client) == []       # the next session


async def test_turning_sharing_off_does_not_cut_a_download(client, web,
                                                           downloads):
    await save(client)
    session = await web.OWN_SOULSEEK.open(
        client.user_id, await web.soulseek_credentials.get(client.user_id))
    await session.client.transfers.download("peer", "a\\b.mp3")
    await client.patch("/api/profile/soulseek", json={"share_downloads": False})
    assert not session.client.stopped


async def test_sharing_needs_an_account(client, web):
    response = await client.patch("/api/profile/soulseek",
                                  json={"share_downloads": False})
    assert response.status_code == 404


async def test_a_finished_download_joins_the_share(client, web, downloads,
                                                   monkeypatch):
    import src.acquire.runner as runner

    await save(client)
    refreshed = []

    async def acquire_track(*args, **kwargs):
        return {"status": "ready"}

    async def refresh(user_id):
        refreshed.append(user_id)

    monkeypatch.setattr(runner, "acquire_track", acquire_track)
    monkeypatch.setattr(web.OWN_SOULSEEK, "refresh_shares", refresh)
    await client.post("/api/acquire/track", json={
        "key": "a::x", "artist": "Artist", "title": "Title"})
    for task in list(web._own_downloads):
        await task
    assert refreshed == [client.user_id]


async def test_removing_the_account_removes_the_share(client, web, downloads):
    await save(client)
    await _ready(web, client.user_id, downloads / "Mine.mp3")
    await _shared(web, client)
    await client.delete("/api/profile/soulseek")
    assert not web.OWN_SOULSEEK.share_dir(client.user_id).exists()
