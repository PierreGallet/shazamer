"""Soulseek quotas: one shared account must not get banned.

Every user of this instance searches and downloads through one Soulseek
account. The server bans an account for 30 minutes after "too many
operations at once", searches in particular, and publishes no threshold. So
the budget is set well under what the community runs without bans (sockseek
defaults to 34 searches per 220 s), spread across every user, and a ban
pauses everything rather than prolonging itself.

The clock is injected: the windows are minutes to hours long and the tests
must not wait for them.
"""
import pytest

from src.acquire.quota import QuotaExceeded, SoulseekQuota

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def quota(tmp_path, clock):
    return SoulseekQuota(tmp_path / "soulseek.db", clock=clock,
                         global_searches=4, global_window=220, min_gap=5,
                         user_searches_per_hour=3,
                         user_downloads_per_hour=2, user_downloads_per_day=3,
                         pause_minutes=35)


def test_searches_are_spaced_rather_than_refused(quota, clock):
    """Two searches a second apart: the second waits out the gap."""
    assert quota.reserve_search("alice") == 0
    clock.t += 1
    assert quota.reserve_search("bob") == pytest.approx(4)


def test_the_global_window_is_shared_by_every_user(quota, clock):
    for user in ("a", "b", "c", "d"):
        quota.reserve_search(user)
        clock.t += 10
    with pytest.raises(QuotaExceeded) as caught:
        quota.reserve_search("e")
    assert caught.value.scope == "global"
    assert 0 < caught.value.retry_after <= 220


def test_the_window_frees_up_as_it_slides(quota, clock):
    for user in ("a", "b", "c", "d"):
        quota.reserve_search(user)
        clock.t += 10
    clock.t += 221
    assert quota.reserve_search("e") == 0


def test_one_user_cannot_spend_the_whole_budget(quota, clock):
    for _ in range(3):
        quota.reserve_search("alice")
        clock.t += 100
    with pytest.raises(QuotaExceeded) as caught:
        quota.reserve_search("alice")
    assert caught.value.scope == "user"
    clock.t += 1
    quota.reserve_search("bob")                    # others are unaffected


def test_downloads_are_capped_per_hour_and_per_day(quota, clock):
    quota.reserve_download("alice")
    quota.reserve_download("alice")
    with pytest.raises(QuotaExceeded):
        quota.reserve_download("alice")            # 2 per hour
    clock.t += 3601
    quota.reserve_download("alice")
    clock.t += 3601
    with pytest.raises(QuotaExceeded) as caught:
        quota.reserve_download("alice")            # 3 per day
    assert caught.value.scope == "user"


def test_a_ban_pauses_everyone_for_longer_than_the_ban(quota, clock):
    quota.pause("banned by the server")
    with pytest.raises(QuotaExceeded) as caught:
        quota.reserve_search("alice")
    assert caught.value.scope == "paused"
    assert caught.value.retry_after == pytest.approx(35 * 60)
    with pytest.raises(QuotaExceeded):
        quota.reserve_download("bob")
    clock.t += 35 * 60 + 1
    assert quota.reserve_search("alice") == 0


def test_the_budget_is_shared_across_processes(tmp_path, clock):
    """The API and the worker are two processes on one account."""
    path = tmp_path / "soulseek.db"
    api = SoulseekQuota(path, clock=clock, global_searches=2, min_gap=0)
    worker = SoulseekQuota(path, clock=clock, global_searches=2, min_gap=0)
    api.reserve_search("a")
    worker.reserve_search("b")
    with pytest.raises(QuotaExceeded):
        api.reserve_search("c")


def test_the_message_says_when_to_come_back(quota, clock):
    quota.pause("banned")
    with pytest.raises(QuotaExceeded) as caught:
        quota.reserve_search("alice")
    assert "35 min" in str(caught.value)


# ── Wired into slskd and the API ────────────────────────────────────────────

async def test_a_refused_search_never_reaches_slskd(tmp_path, clock,
                                                    monkeypatch):
    import src.acquire.slskd as slskd

    calls = []

    async def request(self, method, path, **kwargs):
        calls.append((method, path))
        return {}

    monkeypatch.setattr(slskd.SlskdClient, "_request", request)
    quota = SoulseekQuota(tmp_path / "q.db", clock=clock)
    quota.pause("banned")
    monkeypatch.setattr(slskd, "QUOTA", quota)

    client = slskd.SlskdClient(base_url="http://slskd")
    with pytest.raises(QuotaExceeded):
        await client.search("artist title", user_id="alice")
    assert calls == []


async def test_a_ban_reported_by_slskd_pauses_soulseek(tmp_path, clock,
                                                       monkeypatch):
    import src.acquire.slskd as slskd

    async def request(self, method, path, **kwargs):
        raise slskd.SlskdError("slskd 409 on /searches: You have been banned "
                               "for 30 minutes")

    monkeypatch.setattr(slskd.SlskdClient, "_request", request)
    quota = SoulseekQuota(tmp_path / "q.db", clock=clock)
    monkeypatch.setattr(slskd, "QUOTA", quota)

    with pytest.raises(slskd.SlskdError):
        await slskd.SlskdClient(base_url="http://slskd").search("x y")
    assert quota.paused_for() > 30 * 60


async def test_the_api_answers_429_with_a_reason(client, monkeypatch):
    import src.acquire.slskd as slskd

    monkeypatch.setenv("SLSKD_URL", "http://slskd")
    client.web.SOULSEEK_QUOTA.pause("banned")
    monkeypatch.setattr(slskd, "QUOTA", client.web.SOULSEEK_QUOTA)

    response = await client.post("/api/acquire/soulseek/search",
                                 json={"query": "artist title"})
    assert response.status_code == 429
    assert "paused" in response.json()["detail"].lower()
    assert int(response.headers["Retry-After"]) > 0


async def test_fetching_a_track_counts_against_the_download_quota(
        client, monkeypatch):
    import src.web as web_mod

    monkeypatch.setenv("SLSKD_URL", "http://slskd")
    client.web.SOULSEEK_QUOTA.user_downloads_per_hour = 1

    async def no_queue(*a, **k):
        return True                                # pretend the worker took it
    monkeypatch.setattr(client.web.jobs, "enqueue", no_queue)

    body = {"key": "a::x", "artist": "A", "title": "X"}
    assert (await client.post("/api/acquire/track", json=body)).status_code == 200
    second = await client.post("/api/acquire/track", json=body)
    assert second.status_code == 429
    assert "downloads" in second.json()["detail"].lower()
