"""Soulseek through a user's own account, over aioslsk (RFC #137).

The shared account goes through slskd (`slskd.py`). A user who saved their own
credentials in Profile goes through this instead, and never through slskd: if
their account fails, they see that failure rather than a quiet fallback onto
everyone's budget.

aioslsk (https://github.com/JurgenR/aioslsk) is a library, not a daemon: the
client runs inside this process and talks to the Soulseek server and peers
itself. Three consequences shape this module:

- **One session per account, one process.** The Soulseek server evicts a
  second login of the same account. So sessions live in the API process only;
  the worker never opens one. And while we are logged in, the user's own
  desktop client is the one evicted — which is why a session is closed after
  a few idle minutes instead of being held.
- **No listening port.** Like slskd in docker-stack.yml, nothing is published:
  peers that cannot be reached from outside are reached outward instead. Ports
  0/0 with error mode ANY is how aioslsk spells "no listener".
- **Its sharp edges are filed off here**, not left to callers: its Settings
  would read unprefixed environment variables (DEBUG=1 broke validation), it
  logs the login password at DEBUG, UPnP is on by default, and `start()`
  replaces the event loop's exception handler for the whole process.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aioslsk.events import KickedEvent, SessionDestroyedEvent
from aioslsk.exceptions import AuthenticationError
from aioslsk.network.network import ListeningConnectionErrorMode
from aioslsk.protocol.primitives import AttributeKey
from aioslsk.settings import Settings
from aioslsk.transfer.state import TransferState

from .slskd import Candidate, SlskdClient, SlskdError, score_candidate

logger = logging.getLogger(__name__)

# At DEBUG, aioslsk logs every message it sends — Login.Request included, with
# the password in it. Pinned here so no log configuration can lower it.
logging.getLogger("aioslsk").setLevel(logging.WARNING)
# With no listening port, a firewalled peer answering a search asks us to
# connect to it and that fails — a normal outcome, logged by aioslsk as a
# warning with the peer's IP, by the hundred (257 in 40 minutes in
# production). Errors from the network layer still come through.
logging.getLogger("aioslsk.network.network").setLevel(logging.ERROR)

# Closed after this long without an operation, so the user's own client can
# log back in. A transfer in progress keeps it open.
IDLE_SECONDS = float(os.environ.get("OWN_SOULSEEK_IDLE_SECONDS", "300"))

# How long a search collects answers. Peers answer over several seconds and
# aioslsk has no notion of a search being "complete", unlike slskd.
SEARCH_WAIT = float(os.environ.get("OWN_SOULSEEK_SEARCH_WAIT", "15"))

KICKED = ("Soulseek logged your account out: it was logged in elsewhere "
          "(your own Soulseek client?).")


class OwnSoulseekError(SlskdError):
    """A failure of the user's own account, with a reason they can act on.

    A SlskdError so the existing handlers report it the same way; never a
    reason to retry on the shared account.
    """


class _IsolatedSettings(Settings):
    """aioslsk's Settings, fed only what it is given."""

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings,
                                   env_settings, dotenv_settings,
                                   file_secret_settings):
        return (init_settings,)


def build_settings(credentials, download_dir: Path) -> Settings:
    return _IsolatedSettings(
        credentials={"username": credentials.username,
                     "password": credentials.password},
        network={
            "listening": {"port": 0, "obfuscated_port": 0,
                          "error_mode": ListeningConnectionErrorMode.ANY},
            "upnp": {"enabled": False},
            "server": {"reconnect": {"auto": False}},
        },
        shares={"download": str(download_dir), "directories": [],
                "scan_on_start": False},
        rooms={"auto_join": False, "favorites": set()},
    )


@dataclass
class Session:
    user_id: str
    username: str
    client: Any
    last_used: float
    dead: Optional[str] = None
    busy: int = 0

    def transfers(self) -> List[Any]:
        return list(self.client.transfers.transfers)

    def transferring(self) -> bool:
        return any(not t.is_finalized() for t in self.transfers())


class OwnSoulseekSessions:
    """One logged-in aioslsk client per user, opened on demand."""

    def __init__(self, root: Path, *, client_factory: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic,
                 idle_seconds: float = IDLE_SECONDS,
                 on_login: Optional[Callable] = None,
                 on_failure: Optional[Callable] = None) -> None:
        """`on_login(user_id)` and `on_failure(user_id, reason)` are told
        how each login went, and of an eviction — for Profile to show the
        last outcome without logging in to find out."""
        if client_factory is None:
            from aioslsk.client import SoulSeekClient
            client_factory = SoulSeekClient
        self.root = Path(root)
        self.client_factory = client_factory
        self.clock = clock
        self.idle_seconds = idle_seconds
        self._sessions: Dict[str, Session] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._reaper: Optional[asyncio.Task] = None
        self.on_login = on_login
        self.on_failure = on_failure

    async def _tell(self, callback, *args) -> None:
        if callback is None:
            return
        try:
            await callback(*args)
        except Exception:
            logger.exception("Recording a Soulseek outcome failed")

    def download_dir(self, user_id: str) -> Path:
        return self.root / user_id

    def active(self, user_id: str) -> Optional[Session]:
        session = self._sessions.get(user_id)
        return session if session and not session.dead else None

    def _lock(self, user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def open(self, user_id: str, credentials) -> Session:
        async with self._lock(user_id):
            session = self._sessions.get(user_id)
            if session and not session.dead \
                    and session.username == credentials.username:
                session.last_used = self.clock()
                return session
            if session:
                await self._stop(session)
            session = await self._login(user_id, credentials)
            self._sessions[user_id] = session
            self._ensure_reaper()
            return session

    async def verify(self, user_id: str, credentials) -> None:
        """Log in once and out again: are these credentials good?"""
        await self.close(user_id)
        session = await self._login(user_id, credentials)
        await self._stop(session)

    async def close(self, user_id: str) -> None:
        async with self._lock(user_id):
            session = self._sessions.pop(user_id, None)
            if session:
                await self._stop(session)

    async def close_all(self) -> None:
        for user_id in list(self._sessions):
            await self.close(user_id)
        if self._reaper:
            self._reaper.cancel()
            self._reaper = None

    async def reap(self) -> None:
        """Close the sessions nobody has used for a while."""
        now = self.clock()
        for user_id, session in list(self._sessions.items()):
            if session.dead:
                await self.close(user_id)
            elif (now - session.last_used > self.idle_seconds
                  and not session.busy and not session.transferring()):
                logger.info("Closing idle Soulseek session for %s", user_id)
                await self.close(user_id)

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_forever())

    async def _reap_forever(self) -> None:
        while self._sessions:
            await asyncio.sleep(min(60.0, self.idle_seconds))
            try:
                await self.reap()
            except Exception:                      # never kill the loop
                logger.exception("Reaping Soulseek sessions failed")

    async def _login(self, user_id: str, credentials) -> Session:
        directory = self.download_dir(user_id)
        directory.mkdir(parents=True, exist_ok=True)
        client = self.client_factory(build_settings(credentials, directory))
        session = Session(user_id, credentials.username, client,
                          last_used=self.clock())

        async def on_kicked(event) -> None:
            session.dead = KICKED
            await self._tell(self.on_failure, user_id, KICKED)

        async def on_destroyed(event) -> None:
            session.dead = session.dead or "The Soulseek session ended."

        client.events.register(KickedEvent, on_kicked)
        client.events.register(SessionDestroyedEvent, on_destroyed)

        # start() installs aioslsk's own handler on the loop, for every task
        # in this process. Put ours back.
        loop = asyncio.get_running_loop()
        handler = loop.get_exception_handler()
        try:
            await client.start()
            await client.login()
        except AuthenticationError as exc:
            error = OwnSoulseekError(
                f"Soulseek refused the login for {credentials.username}: "
                f"{exc.reason or 'no reason given'}.")
        except Exception as exc:
            # The type only: a traceback here would carry the settings.
            error = OwnSoulseekError(
                f"Could not reach Soulseek ({type(exc).__name__}).")
        else:
            error = None
        finally:
            loop.set_exception_handler(handler)
        if error is not None:
            await self._stop(session)
            await self._tell(self.on_failure, user_id, str(error))
            raise error
        await self._tell(self.on_login, user_id)
        return session

    async def _stop(self, session: Session) -> None:
        loop = asyncio.get_running_loop()
        handler = loop.get_exception_handler()
        try:
            await session.client.stop()
        except Exception as exc:
            logger.warning("Stopping a Soulseek session failed: %s",
                           type(exc).__name__)
        finally:
            loop.set_exception_handler(handler)


# aioslsk's transfer states, spelled the way slskd spells them, so every
# caller — and the interface — reads one vocabulary.
_STATES = {
    TransferState.UNSET: "Queued",
    TransferState.VIRGIN: "Queued",
    TransferState.QUEUED: "Queued, Remotely",
    TransferState.PAUSED: "Queued, Paused",
    TransferState.INITIALIZING: "Initializing",
    TransferState.INCOMPLETE: "InProgress",
    TransferState.DOWNLOADING: "InProgress",
    TransferState.COMPLETE: "Completed, Succeeded",
    TransferState.FAILED: "Completed, Errored",
    TransferState.ABORTED: "Completed, Cancelled",
}


def _as_slskd_hit(result, item) -> tuple:
    """One aioslsk search hit, in the shape `score_candidate` reads."""
    attributes = item.get_attribute_map()
    raw = {"filename": item.filename, "size": item.filesize,
           "extension": (item.extension or "").lower(),
           "bitRate": attributes.get(AttributeKey.BITRATE),
           "length": attributes.get(AttributeKey.DURATION),
           "sampleRate": attributes.get(AttributeKey.SAMPLE_RATE),
           "bitDepth": attributes.get(AttributeKey.BIT_DEPTH)}
    user = {"username": result.username,
            "hasFreeUploadSlot": result.has_free_slots,
            "queueLength": result.queue_size,
            "uploadSpeed": result.avg_speed}
    return raw, user


class OwnSoulseekClient:
    """The slskd client's interface, over one user's own account.

    Only what the call sites use: `configured`, `search`, `enqueue`,
    `transfer_state`, `await_transfer`.
    """

    configured = True
    # The file is copied into the library by `collect`; the raw copy in the
    # user's session folder is not shared with anyone and can go.
    keeps_raw = False

    def __init__(self, sessions: OwnSoulseekSessions, user_id: str,
                 credentials, *, quota=None,
                 search_wait: Optional[float] = None) -> None:
        self.sessions = sessions
        self.user_id = user_id
        self.credentials = credentials
        self.quota = quota
        self.search_wait = search_wait

    async def _session(self) -> Session:
        return await self.sessions.open(self.user_id, self.credentials)

    async def search(self, query: str, *, wait: Optional[float] = None,
                     poll: float = 1.0, user_id: str = "") -> List[Candidate]:
        if self.quota is not None:
            delay = self.quota.reserve_own_search(self.user_id)
            if delay > 0:
                await asyncio.sleep(delay)
        session = await self._session()
        session.busy += 1
        try:
            request = await session.client.searches.search(query)
            if wait is None:
                wait = SEARCH_WAIT if self.search_wait is None \
                    else self.search_wait
            await asyncio.sleep(wait)
            if session.dead:
                raise OwnSoulseekError(session.dead)
            candidates = []
            for result in list(request.results):
                for item in result.shared_items:
                    candidate = score_candidate(*_as_slskd_hit(result, item),
                                                query)
                    if candidate is not None:
                        candidates.append(candidate)
            session.client.searches.remove_request(request)
        finally:
            session.busy -= 1
            session.last_used = self.sessions.clock()
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    async def enqueue(self, candidate: Candidate) -> Dict[str, Any]:
        session = await self._session()
        await session.client.transfers.download(candidate.username,
                                                candidate.filename)
        session.last_used = self.sessions.clock()
        return {"queued": True, "username": candidate.username,
                "filename": candidate.basename}

    def _find(self, username: str, filename: str):
        session = self.sessions._sessions.get(self.user_id)
        if session is None:
            return None, None
        for transfer in session.transfers():
            if transfer.username == username \
                    and transfer.remote_path == filename:
                return session, transfer
        return session, None

    async def transfer(self, username: str,
                       filename: str) -> Optional[Dict[str, Any]]:
        """One transfer as slskd's `downloads()` lists it — what
        `await_transfer` reads."""
        session, transfer = self._find(username, filename)
        if session is not None and session.dead:
            raise OwnSoulseekError(session.dead)
        if transfer is None:
            return None
        session.last_used = self.sessions.clock()
        size = int(transfer.filesize or 0)
        done = int(transfer.bytes_transfered or 0)
        state = _STATES.get(transfer.state.VALUE, "Queued")
        if transfer.state.VALUE == TransferState.FAILED \
                and transfer.fail_reason:
            state = f"{state} ({transfer.fail_reason})"
        return {"username": username, "full_path": filename,
                "filename": filename.replace("\\", "/").rsplit("/", 1)[-1],
                "state": state,
                "percent": round(done / size * 100, 1) if size else 0.0,
                "size": size, "transferred": done,
                "speed": transfer.get_speed(),
                "placeInQueue": transfer.place_in_queue,
                "local_path": transfer.local_path or ""}

    async def transfer_state(self, username: str,
                             filename: str) -> Optional[Dict[str, Any]]:
        try:
            entry = await self.transfer(username, filename)
        except OwnSoulseekError:
            return None
        if entry is None:
            return None
        state = entry["state"].lower()
        return {"state": entry["state"],
                "finished": "succeeded" in state,
                "failed": any(w in state for w in
                              ("errored", "cancelled", "rejected", "timedout")),
                "percent": entry["percent"],
                "transferred": entry["transferred"], "size": entry["size"],
                "speed": entry["speed"],
                "queue_position": entry["placeInQueue"],
                "remaining_seconds": None}

    async def await_transfer(self, username: str, filename: str,
                             **kwargs) -> Dict[str, Any]:
        """slskd's wait — stall and queue patience included — over this
        account's transfer list. Busy meanwhile, so it is never reaped.

        The session the transfer was queued on, never a new one: logging in
        again would start with an empty transfer list and report the download
        as vanished, instead of saying why it stopped.
        """
        session = self.sessions._sessions.get(self.user_id)
        if session is None or session.dead:
            raise OwnSoulseekError(
                session.dead if session else "The Soulseek session ended.")
        session.busy += 1
        try:
            return await SlskdClient.await_transfer(self, username, filename,
                                                    **kwargs)
        finally:
            session.busy -= 1
            session.last_used = self.sessions.clock()
