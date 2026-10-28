"""A user's own Soulseek account over aioslsk (RFC #137, S2 and S4).

aioslsk is faked at the client boundary — `SoulSeekClient` — and nothing
below it: no test touches the network. The fake mirrors the parts of the real
API this code uses (start, login, stop, searches.search, transfers.download,
transfers.transfers, events.register).
"""
import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from aioslsk.events import KickedEvent
from aioslsk.exceptions import AuthenticationError
from aioslsk.network.network import ListeningConnectionErrorMode
from aioslsk.protocol.primitives import Attribute, FileData
from aioslsk.transfer.state import TransferState

from src.acquire import own_soulseek
from src.acquire.own_soulseek import (OwnSoulseekClient, OwnSoulseekError,
                                      OwnSoulseekSessions, build_settings)
from src.acquire.quota import QuotaExceeded, SoulseekQuota
from src.store.soulseek_credentials import Credentials

pytestmark = pytest.mark.anyio

ALICE = Credentials("dj_alice", "hunter2-secret")


class Clock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeTransfer:
    def __init__(self, username, remote_path, download_dir):
        self.username = username
        self.remote_path = remote_path
        self.local_path = None
        self.filesize = 1000
        self.bytes_transfered = 0
        self.place_in_queue = None
        self.fail_reason = None
        self.state = SimpleNamespace(VALUE=TransferState.QUEUED)
        self._dir = download_dir

    def finish(self):
        self.local_path = os.path.join(self._dir, "track.mp3")
        with open(self.local_path, "wb") as handle:
            handle.write(b"x" * 1000)
        self.bytes_transfered = 1000
        self.state = SimpleNamespace(VALUE=TransferState.COMPLETE)

    def get_speed(self):
        return 0.0

    def is_finalized(self):
        return self.state.VALUE in (TransferState.COMPLETE,
                                    TransferState.FAILED,
                                    TransferState.ABORTED)


class FakeClient:
    """Stands in for aioslsk's SoulSeekClient."""
    instances = []

    def __init__(self, settings):
        self.settings = settings
        self.started = self.logged_in = self.stopped = False
        self.handlers = {}
        self.events = SimpleNamespace(register=self._register)
        self.refuse = None
        self.results = []
        self.searched = []
        self.removed = []
        self._transfers = []
        self.searches = SimpleNamespace(search=self._search,
                                        remove_request=self.removed.append)
        self.transfers = SimpleNamespace(download=self._download,
                                         transfers=self._transfers)
        self.scans = 0
        self.shares = SimpleNamespace(scan=self._scan)
        FakeClient.instances.append(self)

    def _register(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    async def start(self):
        # What the real one does, and what must not outlive it.
        asyncio.get_running_loop().set_exception_handler(lambda l, c: None)
        self.started = True

    async def login(self):
        logging.getLogger("aioslsk.network").debug(
            "sending Login.Request(username=%s, password=%s)",
            self.settings.credentials.username,
            self.settings.credentials.password)
        if FakeClient.refuse_next:
            raise AuthenticationError(FakeClient.refuse_next, "auth failed")
        self.logged_in = True

    async def stop(self):
        self.stopped = True

    async def _search(self, query):
        self.searched.append(query)
        return SimpleNamespace(query=query, results=list(self.results))

    async def _download(self, username, filename):
        transfer = FakeTransfer(username, filename,
                                self.settings.shares.download)
        self._transfers.append(transfer)
        return transfer

    async def _scan(self):
        self.scans += 1

    async def kick(self):
        for handler in self.handlers.get(KickedEvent, []):
            await handler(SimpleNamespace(raw_message=None))


@pytest.fixture(autouse=True)
def _fresh_fake():
    FakeClient.instances = []
    FakeClient.refuse_next = None


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sessions(tmp_path, clock):
    return OwnSoulseekSessions(tmp_path / "own", client_factory=FakeClient,
                               clock=clock, idle_seconds=300)


def hit(username, filename, *, bitrate=320, length=420, free=True):
    return SimpleNamespace(
        username=username, has_free_slots=free, avg_speed=500_000,
        queue_size=0, shared_items=[FileData(
            unknown=1, filename=filename, filesize=12_000_000,
            extension="mp3", attributes=[Attribute(0, bitrate),
                                         Attribute(1, length)])],
        locked_results=[])


# ── Settings (AC-2.3) ───────────────────────────────────────────────────────

def test_settings_open_no_port_no_upnp_and_share_nothing(tmp_path):
    settings = build_settings(ALICE, tmp_path / "dl")
    listening = settings.network.listening
    assert (listening.port, listening.obfuscated_port) == (0, 0)
    assert listening.error_mode == ListeningConnectionErrorMode.ANY
    assert settings.network.upnp.enabled is False
    assert settings.network.server.reconnect.auto is False
    assert settings.shares.directories == []
    assert settings.shares.scan_on_start is False
    assert settings.rooms.auto_join is False
    assert settings.shares.download == str(tmp_path / "dl")


def test_settings_ignore_stray_environment_variables(tmp_path, monkeypatch):
    """aioslsk's Settings reads unprefixed env vars: DEBUG=1 broke it."""
    monkeypatch.setenv("DEBUG", "1")
    monkeypatch.setenv("NETWORK", "not json")
    monkeypatch.setenv("CREDENTIALS", '{"username": "x", "password": "y"}')
    settings = build_settings(ALICE, tmp_path / "dl")
    assert settings.credentials.username == "dj_alice"


def test_aioslsk_never_logs_below_warning():
    assert logging.getLogger("aioslsk").level == logging.WARNING


def test_unreachable_peers_do_not_flood_the_log():
    """Without a listening port, every peer behind a firewall that answers a
    search fails to connect, logged as a warning with its IP: 257 lines in
    40 minutes in production, for a normal outcome."""
    network = logging.getLogger("aioslsk.network.network")
    assert network.getEffectiveLevel() == logging.ERROR
    assert not network.isEnabledFor(logging.WARNING)


# ── Sessions (AC-2.1, 2.2, 2.4) ─────────────────────────────────────────────

async def test_one_session_per_user_reused(sessions):
    first = await sessions.open("alice", ALICE)
    second = await sessions.open("alice", ALICE)
    assert first is second
    assert len(FakeClient.instances) == 1
    assert first.client.logged_in


async def test_new_credentials_replace_the_session(sessions):
    first = await sessions.open("alice", ALICE)
    second = await sessions.open("alice", Credentials("dj_alice2", "pw"))
    assert first.client.stopped and second is not first


async def test_a_refused_login_says_why_and_leaves_nothing_open(sessions):
    FakeClient.refuse_next = "INVALIDPASS"
    with pytest.raises(OwnSoulseekError) as caught:
        await sessions.open("alice", ALICE)
    assert "INVALIDPASS" in str(caught.value)
    assert "refused" in str(caught.value).lower()
    assert FakeClient.instances[0].stopped
    assert sessions.active("alice") is None


async def test_idle_sessions_are_closed(sessions, clock):
    session = await sessions.open("alice", ALICE)
    clock.t += 299
    await sessions.reap()
    assert not session.client.stopped
    clock.t += 2
    await sessions.reap()
    assert session.client.stopped
    assert sessions.active("alice") is None


async def test_a_transfer_in_progress_keeps_the_session_open(sessions, clock):
    session = await sessions.open("alice", ALICE)
    transfer = await session.client.transfers.download("peer", "a\\b.mp3")
    clock.t += 3600
    await sessions.reap()
    assert not session.client.stopped
    transfer.finish()
    await sessions.reap()
    assert session.client.stopped


async def test_the_loop_exception_handler_survives_a_session(sessions):
    loop = asyncio.get_running_loop()
    before = loop.get_exception_handler()
    await sessions.open("alice", ALICE)
    assert loop.get_exception_handler() is before


async def test_being_kicked_ends_the_session_with_a_reason(sessions):
    session = await sessions.open("alice", ALICE)
    await session.client.kick()
    assert session.dead
    assert "logged in elsewhere" in session.dead
    assert sessions.active("alice") is None


async def test_verify_logs_in_and_out(sessions):
    await sessions.verify("alice", ALICE)
    assert FakeClient.instances[0].logged_in
    assert FakeClient.instances[0].stopped


async def test_the_password_is_not_logged(sessions, caplog):
    caplog.set_level(logging.DEBUG)
    await sessions.open("alice", ALICE)
    assert "hunter2-secret" not in caplog.text


# ── Searching and downloading (AC-3.4) ──────────────────────────────────────

@pytest.fixture
def own(sessions, tmp_path, clock):
    quota = SoulseekQuota(tmp_path / "q.db", clock=clock)
    return OwnSoulseekClient(sessions, "alice", ALICE, quota=quota,
                             search_wait=0)


async def test_search_ranks_results_like_slskd(own, sessions):
    session = await sessions.open("alice", ALICE)
    session.client.results = [
        hit("peer1", "Music\\Artist - Title (Radio Edit).mp3", length=180),
        hit("peer2", "Music\\Artist - Title (Extended Mix).mp3", length=420),
        SimpleNamespace(username="p3", has_free_slots=True, avg_speed=0,
                        queue_size=0, locked_results=[], shared_items=[
                            FileData(1, "cover.jpg", 10, "jpg", [])]),
    ]
    candidates = await own.search("artist title")
    assert [c.username for c in candidates] == ["peer2", "peer1"]
    assert candidates[0].bitrate == 320 and candidates[0].length == 420
    assert session.client.removed                  # the request is cleaned up


async def test_transfer_state_has_the_shape_of_slskds(own, sessions):
    from src.acquire.slskd import Candidate

    candidate = Candidate("peer", "Music\\t.mp3", 1000, "mp3", 320, None,
                          None, 400, 0, True, 0, 1.0)
    await own.enqueue(candidate)
    state = await own.transfer_state("peer", "Music\\t.mp3")
    assert state["finished"] is False and state["failed"] is False
    assert "queued" in state["state"].lower()

    transfer = sessions.active("alice").client._transfers[0]
    transfer.finish()
    state = await own.transfer_state("peer", "Music\\t.mp3")
    assert state["finished"] is True and state["percent"] == 100.0


async def test_await_transfer_returns_where_the_file_is(own, sessions):
    from src.acquire.slskd import Candidate

    candidate = Candidate("peer", "Music\\t.mp3", 1000, "mp3", 320, None,
                          None, 400, 0, True, 0, 1.0)
    await own.enqueue(candidate)
    sessions.active("alice").client._transfers[0].finish()
    entry = await own.await_transfer("peer", "Music\\t.mp3", poll=0)
    assert os.path.isabs(entry["local_path"])
    assert os.path.exists(entry["local_path"])


async def test_a_kick_mid_transfer_fails_with_the_reason(own, sessions):
    from src.acquire.slskd import Candidate

    candidate = Candidate("peer", "Music\\t.mp3", 1000, "mp3", 320, None,
                          None, 400, 0, True, 0, 1.0)
    await own.enqueue(candidate)
    await sessions.active("alice").client.kick()
    with pytest.raises(OwnSoulseekError) as caught:
        await own.await_transfer("peer", "Music\\t.mp3", poll=0)
    assert "logged in elsewhere" in str(caught.value)


# ── Quotas on an own account (AC-4) ─────────────────────────────────────────

async def test_own_searches_leave_the_shared_budget_alone(tmp_path, clock):
    quota = SoulseekQuota(tmp_path / "q.db", clock=clock, global_searches=1,
                          min_gap=0)
    for _ in range(3):
        quota.reserve_own_search("alice")
        clock.t += 10
    assert quota.reserve_search("bob") == 0       # shared budget untouched


async def test_own_searches_are_spaced_and_capped(tmp_path, clock):
    quota = SoulseekQuota(tmp_path / "q.db", clock=clock,
                          user_searches_per_hour=2, min_gap=5)
    assert quota.reserve_own_search("alice") == 0
    clock.t += 1
    assert quota.reserve_own_search("alice") == pytest.approx(4)
    clock.t += 10
    with pytest.raises(QuotaExceeded) as caught:
        quota.reserve_own_search("alice")
    assert caught.value.scope == "user"
    # Someone else's own account is spaced on its own.
    assert quota.reserve_own_search("bob") == 0


async def test_a_shared_ban_does_not_stop_own_accounts(tmp_path, clock):
    quota = SoulseekQuota(tmp_path / "q.db", clock=clock)
    quota.pause("banned")
    assert quota.reserve_own_search("alice") == 0


# ── Sharing your downloads (RFC #137, Q-B) ──────────────────────────────────

from src.acquire.own_soulseek import sync_share_links  # noqa: E402


def _tracks(tmp_path, *names):
    library = tmp_path / "downloads"
    library.mkdir(exist_ok=True)
    paths = []
    for name in names:
        path = library / name
        path.write_bytes(b"audio")
        paths.append(path)
    return paths


def test_the_share_is_links_to_your_own_downloads(tmp_path):
    a, b = _tracks(tmp_path, "A - One.mp3", "B - Two.mp3")
    share = tmp_path / "share"
    assert sync_share_links(share, [a, b]) == 2
    linked = sorted(p.name for p in share.iterdir())
    assert linked == ["A - One.mp3", "B - Two.mp3"]
    assert all(p.is_symlink() and p.resolve() in (a, b)
               for p in share.iterdir())


def test_the_share_follows_what_you_still_have(tmp_path):
    a, b = _tracks(tmp_path, "A - One.mp3", "B - Two.mp3")
    share = tmp_path / "share"
    sync_share_links(share, [a, b])
    b.unlink()                                   # swept, or deleted
    c, = _tracks(tmp_path, "C - Three.mp3")
    assert sync_share_links(share, [a, b, c]) == 2
    assert sorted(p.name for p in share.iterdir()) == [
        "A - One.mp3", "C - Three.mp3"]


def test_two_files_with_one_name_are_both_shared(tmp_path):
    library = tmp_path / "downloads"
    (library / "x").mkdir(parents=True)
    (library / "y").mkdir()
    one = library / "x" / "Same.mp3"
    two = library / "y" / "Same.mp3"
    one.write_bytes(b"1")
    two.write_bytes(b"2")
    share = tmp_path / "share"
    assert sync_share_links(share, [one, two]) == 2
    assert {p.resolve() for p in share.iterdir()} == {one, two}


def test_the_share_holds_nothing_but_links(tmp_path):
    """Whatever is put there by hand is not ours to offer."""
    share = tmp_path / "share"
    share.mkdir()
    (share / "stray.mp3").write_bytes(b"?")
    sync_share_links(share, [])
    assert list(share.iterdir()) == []


def test_settings_share_the_folder_with_capped_uploads(tmp_path):
    settings = build_settings(ALICE, tmp_path / "dl", share_dir=tmp_path / "s")
    assert [d.path for d in settings.shares.directories] == [str(tmp_path / "s")]
    assert settings.shares.scan_on_start is True
    assert settings.transfers.limits.upload_slots == 2
    assert settings.network.limits.upload_speed_kbps > 0


async def test_a_session_shares_your_downloads(tmp_path, clock):
    a, = _tracks(tmp_path, "A - One.mp3")

    async def shares_for(user_id):
        return [a] if user_id == "alice" else None

    sessions = OwnSoulseekSessions(tmp_path / "own", client_factory=FakeClient,
                                   clock=clock, shares_for=shares_for)
    session = await sessions.open("alice", ALICE)
    directories = session.client.settings.shares.directories
    assert len(directories) == 1
    shared = list(Path(directories[0].path).iterdir())
    assert [p.resolve() for p in shared] == [a]


async def test_sharing_off_shares_nothing(tmp_path, clock):
    async def shares_for(user_id):
        return None

    sessions = OwnSoulseekSessions(tmp_path / "own", client_factory=FakeClient,
                                   clock=clock, shares_for=shares_for)
    session = await sessions.open("alice", ALICE)
    assert session.client.settings.shares.directories == []


async def test_a_new_download_joins_the_share(tmp_path, clock):
    files = _tracks(tmp_path, "A - One.mp3")

    async def shares_for(user_id):
        return list(files)

    sessions = OwnSoulseekSessions(tmp_path / "own", client_factory=FakeClient,
                                   clock=clock, shares_for=shares_for)
    session = await sessions.open("alice", ALICE)
    files.extend(_tracks(tmp_path, "B - Two.mp3"))
    await sessions.refresh_shares("alice")
    share = Path(session.client.settings.shares.directories[0].path)
    assert len(list(share.iterdir())) == 2
    assert session.client.scans == 1             # rescanned, so reported


async def test_checking_a_password_shares_nothing(tmp_path, clock):
    called = []

    async def shares_for(user_id):
        called.append(user_id)
        return []

    sessions = OwnSoulseekSessions(tmp_path / "own", client_factory=FakeClient,
                                   clock=clock, shares_for=shares_for)
    await sessions.verify("alice", ALICE)
    assert called == []
    assert FakeClient.instances[0].settings.shares.directories == []
