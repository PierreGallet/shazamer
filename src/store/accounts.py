"""Accounts, one-time login codes and sessions.

Passwordless on purpose. This is a personal tool with a handful of users, and
a password would be one more secret to store, hash, rotate and lose — for no
gain over proving you can read an inbox, which is what a password reset falls
back to anyway.

Three tables and three rules:

- **Codes are hashed**, like passwords, because a six-digit code read out of a
  stolen database is a working login for the next ten minutes.
- **Sessions are hashed too.** The cookie is the credential; the row is only
  how we recognise it. Someone with read access to the database must not be
  able to mint a session from it.
- **Nothing says whether an address is registered.** Requesting a code always
  answers the same way, so this cannot be used to find out who has an account.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    last_seen   TEXT
);

CREATE TABLE IF NOT EXISTS login_codes (
    email       TEXT PRIMARY KEY,
    code_hash   TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    sent_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    last_used   TEXT,
    user_agent  TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""

# Long enough to type from a phone, short enough that guessing is hopeless
# against the attempt limit below.
CODE_TTL = timedelta(minutes=10)
MAX_CODE_ATTEMPTS = 5
# One code at a time per address: asking again inside this window returns the
# code already in flight rather than sending a second one, so a mistyped
# address cannot be used to flood a real inbox.
RESEND_INTERVAL = timedelta(seconds=60)

# "quasi jamais être déco sauf si on le demande" — a year, refreshed on use.
SESSION_TTL = timedelta(days=365)
# How stale `last_used` may get before it is worth a write. Every request would
# otherwise touch the database to record something nobody reads that often.
SESSION_TOUCH_INTERVAL = timedelta(hours=6)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    # Rows written before this module set tzinfo, and SQLite's own defaults,
    # come back naive. Comparing those to an aware `now` raises, which would
    # turn every login into a 500.
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def normalise_email(email: str) -> str:
    """Lower-cased and trimmed. Not more than that.

    Deliberately no dot-stripping or plus-tag removal: those are Gmail
    conventions, not email ones, and applying them elsewhere merges two
    different people into one account.
    """
    return (email or "").strip().lower()


def looks_like_email(email: str) -> bool:
    if not email or len(email) > 254 or " " in email:
        return False
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".")


def _hash(value: str) -> str:
    """SHA-256, not a password KDF, and deliberately.

    Both things hashed here are high-entropy values we generated ourselves — a
    32-byte session token, and a six-digit code that is rate-limited, expiring
    and single-use. A slow KDF defends against offline guessing of *chosen*
    secrets; there is nothing here to guess. Paying bcrypt's cost on every
    authenticated request would buy nothing.
    """
    return hashlib.sha256(value.encode()).hexdigest()


def new_code() -> str:
    """Six digits, uniformly random, leading zeros kept."""
    return f"{secrets.randbelow(1_000_000):06d}"


@dataclass
class Accounts:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(
                None, fn, *args)

    # ── Login codes ──────────────────────────────────────────────────────

    async def start_login(self, email: str) -> Optional[str]:
        """Issue a code for `email`, or None if one was just sent.

        Returning None is not a failure — it means a live code already exists
        and the caller should stay quiet rather than send a second mail. The
        endpoint answers identically either way.
        """
        return await self._run(self._start_login_sync, normalise_email(email))

    def _start_login_sync(self, email: str) -> Optional[str]:
        now = _now()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT sent_at, expires_at FROM login_codes WHERE email = ?",
                (email,)).fetchone()
            if row and _parse(row["expires_at"]) > now \
                    and _parse(row["sent_at"]) > now - RESEND_INTERVAL:
                return None

            code = new_code()
            conn.execute(
                "INSERT INTO login_codes (email, code_hash, expires_at,"
                " attempts, sent_at) VALUES (?, ?, ?, 0, ?)"
                " ON CONFLICT(email) DO UPDATE SET code_hash = excluded.code_hash,"
                " expires_at = excluded.expires_at, attempts = 0,"
                " sent_at = excluded.sent_at",
                (email, _hash(code), _iso(now + CODE_TTL), _iso(now)))
            conn.commit()
            return code

    async def verify_login(self, email: str, code: str) -> Optional[Dict[str, Any]]:
        """Check a code and return the user it logs in, creating them if new.

        None covers every failure — wrong code, expired, too many attempts, no
        code outstanding — because telling them apart only helps someone
        guessing.
        """
        return await self._run(self._verify_login_sync,
                               normalise_email(email), (code or "").strip())

    def _verify_login_sync(self, email: str,
                           code: str) -> Optional[Dict[str, Any]]:
        now = _now()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT code_hash, expires_at, attempts FROM login_codes"
                " WHERE email = ?", (email,)).fetchone()
            if row is None:
                return None
            if _parse(row["expires_at"]) <= now or row["attempts"] >= MAX_CODE_ATTEMPTS:
                conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
                conn.commit()
                return None

            # Constant time, so a wrong code cannot be narrowed down by how
            # long the comparison took.
            if not secrets.compare_digest(row["code_hash"], _hash(code)):
                conn.execute(
                    "UPDATE login_codes SET attempts = attempts + 1"
                    " WHERE email = ?", (email,))
                conn.commit()
                return None

            # Single use, whatever happens next.
            conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))

            user = conn.execute("SELECT * FROM users WHERE email = ?",
                                (email,)).fetchone()
            if user is None:
                user_id = secrets.token_hex(8)
                conn.execute(
                    "INSERT INTO users (id, email, created_at, last_seen)"
                    " VALUES (?, ?, ?, ?)",
                    (user_id, email, _iso(now), _iso(now)))
                logger.info("Created account for %s", email)
            else:
                user_id = user["id"]
                conn.execute("UPDATE users SET last_seen = ? WHERE id = ?",
                             (_iso(now), user_id))
            conn.commit()
            return {"id": user_id, "email": email}

    # ── Sessions ─────────────────────────────────────────────────────────

    async def create_session(self, user_id: str, user_agent: str = "") -> str:
        """Mint a session and return the raw token. Stored only as a hash."""
        token = secrets.token_urlsafe(32)
        await self._run(self._create_session_sync, user_id, token,
                        (user_agent or "")[:200])
        return token

    def _create_session_sync(self, user_id: str, token: str,
                             user_agent: str) -> None:
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at,"
                " expires_at, last_used, user_agent) VALUES (?, ?, ?, ?, ?, ?)",
                (_hash(token), user_id, _iso(now), _iso(now + SESSION_TTL),
                 _iso(now), user_agent))
            conn.commit()

    async def user_for_session(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        return await self._run(self._user_for_session_sync, token)

    def _user_for_session_sync(self, token: str) -> Optional[Dict[str, Any]]:
        now = _now()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT s.token_hash, s.expires_at, s.last_used, u.id, u.email"
                " FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = ?", (_hash(token),)).fetchone()
            if row is None:
                return None
            if _parse(row["expires_at"]) <= now:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                             (row["token_hash"],))
                conn.commit()
                return None

            # Sliding expiry: using the app keeps you signed in. Written only
            # every few hours, because otherwise every request would take a
            # write lock to update a timestamp nobody reads.
            last = row["last_used"] and _parse(row["last_used"])
            if last is None or last < now - SESSION_TOUCH_INTERVAL:
                conn.execute(
                    "UPDATE sessions SET last_used = ?, expires_at = ?"
                    " WHERE token_hash = ?",
                    (_iso(now), _iso(now + SESSION_TTL), row["token_hash"]))
                conn.execute("UPDATE users SET last_seen = ? WHERE id = ?",
                             (_iso(now), row["id"]))
                conn.commit()
            return {"id": row["id"], "email": row["email"]}

    async def end_session(self, token: str) -> None:
        if token:
            await self._run(self._end_session_sync, token)

    def _end_session_sync(self, token: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                         (_hash(token),))
            conn.commit()

    async def end_all_sessions(self, user_id: str) -> int:
        """Sign out everywhere. The answer to a lost or stolen device."""
        return await self._run(self._end_all_sessions_sync, user_id)

    def _end_all_sessions_sync(self, user_id: str) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM sessions WHERE user_id = ?",
                               (user_id,))
            conn.commit()
            return cur.rowcount

    # ── Housekeeping ─────────────────────────────────────────────────────

    async def sweep(self) -> int:
        """Drop expired codes and sessions. Cheap, and run at startup."""
        return await self._run(self._sweep_sync)

    def _sweep_sync(self) -> int:
        now = _iso(_now())
        with closing(self._connect()) as conn:
            a = conn.execute("DELETE FROM login_codes WHERE expires_at <= ?",
                             (now,)).rowcount
            b = conn.execute("DELETE FROM sessions WHERE expires_at <= ?",
                             (now,)).rowcount
            conn.commit()
            return a + b

    async def count_users(self) -> int:
        return await self._run(self._count_users_sync)

    def _count_users_sync(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    async def first_user_id(self) -> Optional[str]:
        """The earliest account, used to adopt data that predates accounts."""
        return await self._run(self._first_user_id_sync)

    def _first_user_id_sync(self) -> Optional[str]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id FROM users ORDER BY created_at LIMIT 1").fetchone()
            return row["id"] if row else None
