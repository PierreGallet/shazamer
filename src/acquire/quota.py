"""How much one shared Soulseek account may be asked to do.

Every user of an instance searches and downloads through the one account
slskd is logged in with. The Soulseek server bans an account for 30 minutes
after "too many operations at once" — searches above all ("Do not quickly
repeat a search", says its own message) — and publishes no threshold.

So the numbers are a margin under what the community runs without bans,
not a measured limit. sockseek, a Soulseek download tool, defaults to 34
searches per 220 seconds; the global budget here is 20 in the same window,
searches at least 5 s apart, so one account never looks like a burst. On top
of it, per-user limits keep one person from spending everyone's budget, and
a ban — if one still lands — pauses everything for 35 minutes instead of
being prolonged by the searches queued behind it.

The ledger is a small SQLite file because the API and the worker are two
processes on the same account: an in-memory counter would give each of them
the whole budget. `BEGIN IMMEDIATE` makes check-then-record atomic across
both.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Callable

HOUR = 3600.0
DAY = 24 * HOUR


class QuotaExceeded(Exception):
    """Refused, with when to come back and whose limit it was.

    `scope` is "global" (everyone's shared budget), "user" (this person's
    share of it) or "paused" (a ban was seen; nobody searches until it
    passes).
    """

    def __init__(self, message: str, *, retry_after: float, scope: str):
        super().__init__(message)
        self.retry_after = max(1.0, retry_after)
        self.scope = scope


def _in(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} min"


class SoulseekQuota:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time,
                 global_searches: int = 20, global_window: float = 220.0,
                 min_gap: float = 5.0, user_searches_per_hour: int = 30,
                 user_downloads_per_hour: int = 20,
                 user_downloads_per_day: int = 60,
                 pause_minutes: float = 35.0) -> None:
        self.path = Path(path)
        self.clock = clock
        self.global_searches = global_searches
        self.global_window = global_window
        self.min_gap = min_gap
        self.user_searches_per_hour = user_searches_per_hour
        self.user_downloads_per_hour = user_downloads_per_hour
        self.user_downloads_per_day = user_downloads_per_day
        self.pause_minutes = pause_minutes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS events ("
                "  kind TEXT NOT NULL, user_id TEXT NOT NULL, at REAL NOT NULL);"
                "CREATE INDEX IF NOT EXISTS idx_events ON events(kind, at);"
                "CREATE TABLE IF NOT EXISTS state ("
                "  key TEXT PRIMARY KEY, value REAL NOT NULL, note TEXT);")
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    # ── Ban pause ────────────────────────────────────────────────────────

    def pause(self, reason: str) -> None:
        """Stop all Soulseek activity for `pause_minutes`."""
        until = self.clock() + self.pause_minutes * 60
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO state (key, value, note) VALUES ('paused', ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = MAX(value, excluded.value),"
                " note = excluded.note", (until, reason[:200]))

    def paused_for(self) -> float:
        with closing(self._connect()) as conn:
            return self._paused_for(conn)

    def _paused_for(self, conn: sqlite3.Connection) -> float:
        row = conn.execute(
            "SELECT value FROM state WHERE key = 'paused'").fetchone()
        return max(0.0, row[0] - self.clock()) if row else 0.0

    def _refuse_if_paused(self, conn: sqlite3.Connection) -> None:
        left = self._paused_for(conn)
        if left > 0:
            raise QuotaExceeded(
                f"Soulseek is paused for everyone after the server limited "
                f"this account. It resumes in {_in(left)}.",
                retry_after=left, scope="paused")

    # ── Searches ─────────────────────────────────────────────────────────

    def reserve_search(self, user_id: str) -> float:
        """Book a search; return how many seconds to wait before sending it.

        Spacing is waited out rather than refused: a few seconds' delay is
        better than an error. A full window or a spent personal budget is
        refused, with the time until it frees up.
        """
        now = self.clock()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._refuse_if_paused(conn)
                if user_id:
                    since = now - HOUR
                    n, oldest = conn.execute(
                        "SELECT COUNT(*), MIN(at) FROM events WHERE kind = 'search'"
                        " AND user_id = ? AND at > ?", (user_id, since)).fetchone()
                    if n >= self.user_searches_per_hour:
                        left = oldest + HOUR - now
                        raise QuotaExceeded(
                            f"You have run {n} Soulseek searches in the last "
                            f"hour, the most one person can. Try again in "
                            f"{_in(left)}.", retry_after=left, scope="user")
                n, oldest, last = conn.execute(
                    "SELECT COUNT(*), MIN(at), MAX(at) FROM events"
                    " WHERE kind = 'search' AND at > ?",
                    (now - self.global_window,)).fetchone()
                if n >= self.global_searches:
                    left = oldest + self.global_window - now
                    raise QuotaExceeded(
                        f"Soulseek is busy: everyone shares one account, and it "
                        f"has searched {n} times in the last "
                        f"{int(self.global_window)} s. Try again in {_in(left)}.",
                        retry_after=left, scope="global")
                start = max(now, (last or 0) + self.min_gap)
                conn.execute("INSERT INTO events (kind, user_id, at)"
                             " VALUES ('search', ?, ?)", (user_id or "", start))
                conn.execute("DELETE FROM events WHERE at < ?", (now - DAY,))
                conn.execute("COMMIT")
                return start - now
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # ── Downloads ────────────────────────────────────────────────────────

    def reserve_download(self, user_id: str) -> None:
        """Book a download for `user_id`, or refuse with when to retry."""
        now = self.clock()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._refuse_if_paused(conn)
                for window, limit, label in (
                        (HOUR, self.user_downloads_per_hour, "hour"),
                        (DAY, self.user_downloads_per_day, "day")):
                    n, oldest = conn.execute(
                        "SELECT COUNT(*), MIN(at) FROM events"
                        " WHERE kind = 'download' AND user_id = ? AND at > ?",
                        (user_id, now - window)).fetchone()
                    if n >= limit:
                        left = oldest + window - now
                        raise QuotaExceeded(
                            f"You have started {n} Soulseek downloads in the "
                            f"last {label}, the most one person can. Try again "
                            f"in {_in(left)}.", retry_after=left, scope="user")
                conn.execute("INSERT INTO events (kind, user_id, at)"
                             " VALUES ('download', ?, ?)", (user_id, now))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
