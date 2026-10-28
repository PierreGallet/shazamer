"""A user's own Soulseek account, for acquiring through it (RFC #137).

The password is a secret people reuse elsewhere, so it is encrypted at rest
(Fernet) and no method hands it to the API: `describe` is the public view.

The key lives in a file next to the databases, created on first use with mode
0600. That protects against what is likely — a copy of accounts.db or of its
backups leaking on its own — not against someone who can read the whole data
volume, who has the key too. A new 1Password secret would protect more but
would make every self-hosted instance configure one before the feature works.

Same SQLite file as the accounts, so a row goes when its user does
(ON DELETE CASCADE).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS soulseek_credentials (
    user_id     TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    username    TEXT NOT NULL,
    password    BLOB NOT NULL,
    last_ok_at  TEXT,
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    share_downloads INTEGER NOT NULL DEFAULT 1
);
"""

# Columns added after the table shipped: CREATE TABLE IF NOT EXISTS leaves an
# existing table as it is.
MIGRATIONS = (("share_downloads", "INTEGER NOT NULL DEFAULT 1"),)

# What a row says when the key that encrypted it is gone.
LOST_KEY = ("This server can no longer read your Soulseek password: "
            "enter your password again.")


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:                    # never in a log line
        return f"Credentials(username={self.username!r}, password='***')"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SoulseekCredentials:
    def __init__(self, path: Path, *, key_path: Path) -> None:
        self.path = Path(path)
        self.key_path = Path(key_path)
        self._fernet: Optional[Fernet] = None
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            existing = {r[1] for r in conn.execute(
                "PRAGMA table_info(soulseek_credentials)")}
            for column, definition in MIGRATIONS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE soulseek_credentials"
                                 f" ADD COLUMN {column} {definition}")
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _key(self) -> Fernet:
        """The key, created on first use, readable by its owner only."""
        if self._fernet is None:
            if not self.key_path.exists():
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                # O_EXCL: two processes starting at once must not each write
                # a different key and leave the first one's rows unreadable.
                try:
                    fd = os.open(self.key_path,
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(Fernet.generate_key())
            self._fernet = Fernet(self.key_path.read_bytes().strip())
        return self._fernet

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(
                None, fn, *args)

    # ── Writing ──────────────────────────────────────────────────────────

    async def save(self, user_id: str, username: str, password: str) -> None:
        await self._run(self._save_sync, user_id, username, password)

    def _save_sync(self, user_id: str, username: str, password: str) -> None:
        token = self._key().encrypt(password.encode())
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO soulseek_credentials"
                " (user_id, username, password, last_ok_at, last_error,"
                "  updated_at) VALUES (?, ?, ?, ?, NULL, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET username = excluded.username,"
                " password = excluded.password, last_ok_at = excluded.last_ok_at,"
                " last_error = NULL, updated_at = excluded.updated_at",
                (user_id, username, token, _now(), _now()))
            conn.commit()

    async def forget(self, user_id: str) -> None:
        await self._run(self._exec, "DELETE FROM soulseek_credentials"
                                    " WHERE user_id = ?", (user_id,))

    async def note_ok(self, user_id: str) -> None:
        await self._run(self._exec, "UPDATE soulseek_credentials SET"
                        " last_ok_at = ?, last_error = NULL WHERE user_id = ?",
                        (_now(), user_id))

    async def note_error(self, user_id: str, message: str) -> None:
        await self._run(self._exec, "UPDATE soulseek_credentials SET"
                        " last_error = ? WHERE user_id = ?",
                        (message[:300], user_id))

    async def set_sharing(self, user_id: str, share: bool) -> None:
        await self._run(self._exec, "UPDATE soulseek_credentials SET"
                        " share_downloads = ? WHERE user_id = ?",
                        (1 if share else 0, user_id))

    async def shares_downloads(self, user_id: str) -> bool:
        row = await self._run(self._row_sync, user_id)
        return bool(row and row["share_downloads"])

    def _exec(self, sql: str, params: tuple) -> None:
        with closing(self._connect()) as conn:
            conn.execute(sql, params)
            conn.commit()

    # ── Reading ──────────────────────────────────────────────────────────

    async def user_ids(self) -> list:
        return await self._run(self._user_ids_sync)

    def _user_ids_sync(self) -> list:
        with closing(self._connect()) as conn:
            return [r[0] for r in conn.execute(
                "SELECT user_id FROM soulseek_credentials")]

    async def has(self, user_id: str) -> bool:
        return (await self._run(self._row_sync, user_id)) is not None

    async def get(self, user_id: str) -> Optional[Credentials]:
        """The usable credentials, or None if there are none or they cannot
        be decrypted any more."""
        row = await self._run(self._row_sync, user_id)
        if row is None:
            return None
        password = self._decrypt(row["password"])
        if password is None:
            return None
        return Credentials(row["username"], password)

    async def describe(self, user_id: str) -> Dict[str, Any]:
        """What the interface may show: never the password."""
        row = await self._run(self._row_sync, user_id)
        if row is None:
            return {"configured": False, "username": None,
                    "last_ok_at": None, "last_error": None,
                    "share_downloads": True}
        if self._decrypt(row["password"]) is None:
            return {"configured": False, "username": row["username"],
                    "last_ok_at": None, "last_error": LOST_KEY,
                    "share_downloads": bool(row["share_downloads"])}
        return {"configured": True, "username": row["username"],
                "last_ok_at": row["last_ok_at"],
                "last_error": row["last_error"],
                "share_downloads": bool(row["share_downloads"])}

    def _row_sync(self, user_id: str) -> Optional[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT username, password, last_ok_at, last_error,"
                " share_downloads FROM soulseek_credentials WHERE user_id = ?",
                (user_id,)).fetchone()

    def _decrypt(self, token: bytes) -> Optional[str]:
        try:
            return self._key().decrypt(bytes(token)).decode()
        except (InvalidToken, ValueError):
            logger.warning("A stored Soulseek password could not be decrypted:"
                           " the key changed or was lost")
            return None
