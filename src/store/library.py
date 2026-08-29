"""The library: every track from every set, in one queryable place.

This is what turns the tool from a converter into something worth keeping. A
tracklist you download and forget is a one-shot; a library accumulates, and the
single most useful digging signal falls straight out of it — *this track shows
up in four of your sets*, which is how you find the records that actually
matter to the DJs you follow.

Plain `sqlite3` on purpose: no server to run, the file lives next to the
outputs, and it backs up by copying. Writes are serialised behind a lock and
every call is dispatched to a thread so the event loop is never blocked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.timecode import format_timestamp

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    source_url   TEXT DEFAULT '',
    source_kind  TEXT DEFAULT 'upload',
    uploader     TEXT DEFAULT '',
    audio_path   TEXT DEFAULT '',
    shared_by    TEXT DEFAULT '',
    shared_from  TEXT DEFAULT '',
    quality      TEXT DEFAULT '',
    duration     REAL DEFAULT 0,
    waveform     TEXT DEFAULT '[]',
    stats        TEXT DEFAULT '{}',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id       TEXT NOT NULL REFERENCES sets(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    start        REAL NOT NULL,
    end          REAL NOT NULL,
    identified   INTEGER NOT NULL DEFAULT 0,
    track_key    TEXT DEFAULT '',
    title        TEXT DEFAULT '',
    artist       TEXT DEFAULT '',
    album        TEXT DEFAULT '',
    label        TEXT DEFAULT '',
    year         TEXT DEFAULT '',
    genre        TEXT DEFAULT '',
    isrc         TEXT DEFAULT '',
    url          TEXT DEFAULT '',
    cover_url    TEXT DEFAULT '',
    preview_url  TEXT DEFAULT '',
    bpm          REAL,
    camelot      TEXT,
    musical_key  TEXT,
    confidence   REAL DEFAULT 0,
    strength     TEXT DEFAULT '',
    catalog_number TEXT DEFAULT '',
    mbid         TEXT DEFAULT ''
);

-- Enrichment is rate-limited to about one lookup per second, so the same
-- track appearing in five sets must not cost five lookups. Keyed by the same
-- normalised artist/title everything else uses.
CREATE TABLE IF NOT EXISTS enrichment (
    track_key    TEXT PRIMARY KEY,
    label        TEXT DEFAULT '',
    catalog_number TEXT DEFAULT '',
    year         TEXT DEFAULT '',
    album        TEXT DEFAULT '',
    genre        TEXT DEFAULT '',
    isrc         TEXT DEFAULT '',
    mbid         TEXT DEFAULT '',
    provider     TEXT DEFAULT '',
    confidence   REAL DEFAULT 0,
    -- A miss is cached too. Most unidentified-by-MusicBrainz tracks are white
    -- labels and dubs that will never be there, and re-asking every time is
    -- how you get rate-limited for nothing.
    found        INTEGER NOT NULL DEFAULT 0,
    checked_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracks_set ON tracks(set_id);
CREATE INDEX IF NOT EXISTS idx_tracks_key ON tracks(track_key);

-- Files fetched from Soulseek. One row per attempt, so a failure is visible
-- rather than silently absent: "nothing happened" and "the peer vanished at
-- 60%" need to look different to whoever clicked the button.
CREATE TABLE IF NOT EXISTS downloads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    track_key    TEXT NOT NULL,
    artist       TEXT DEFAULT '',
    title        TEXT DEFAULT '',
    status       TEXT NOT NULL,          -- queued|downloading|verifying|ready|failed
    message      TEXT DEFAULT '',
    quality      TEXT DEFAULT '',
    username     TEXT DEFAULT '',
    remote_path  TEXT DEFAULT '',
    local_path   TEXT DEFAULT '',
    size         INTEGER DEFAULT 0,
    verified     INTEGER DEFAULT 0,
    progress     REAL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_downloads_key ON downloads(track_key);

CREATE TABLE IF NOT EXISTS crate (
    track_key    TEXT NOT NULL,
    user_id      TEXT NOT NULL DEFAULT '',
    title        TEXT DEFAULT '',
    artist       TEXT DEFAULT '',
    note         TEXT DEFAULT '',
    starred_at   TEXT NOT NULL,
    PRIMARY KEY (track_key, user_id)
);

-- An invitation to take a copy of a set.
--
-- A copy, not a view: the recipient gets their own set with their own stars
-- and their own right to delete it, and the sender cannot see what they do
-- with it or take it back. That is the simplest thing to reason about, and
-- the only one with no way to surprise either side later.
CREATE TABLE IF NOT EXISTS shares (
    token       TEXT PRIMARY KEY,
    set_id      TEXT NOT NULL,
    from_user   TEXT NOT NULL,
    from_name   TEXT DEFAULT '',
    to_email    TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    claimed_at  TEXT,
    claimed_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_shares_set ON shares(set_id);

CREATE TABLE IF NOT EXISTS watches (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    user_id      TEXT NOT NULL DEFAULT '',
    title        TEXT DEFAULT '',
    kind         TEXT DEFAULT 'channel',
    created_at   TEXT NOT NULL,
    last_checked TEXT,
    seen_ids     TEXT DEFAULT '[]',
    UNIQUE (url, user_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Library:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            # After the additive pass, which is what puts user_id there.
            self._rebuild_for_ownership(conn)
            conn.commit()

    # Columns added after the first release, as (table, column, definition).
    # SCHEMA above uses CREATE TABLE IF NOT EXISTS, which does nothing to a
    # database that already exists — so a new column has to be added here too
    # or it only appears on fresh installs and production keeps the old shape.
    MIGRATIONS = (
        ("tracks", "strength", "TEXT DEFAULT ''"),
        ("tracks", "catalog_number", "TEXT DEFAULT ''"),
        ("tracks", "mbid", "TEXT DEFAULT ''"),
        # A ~30 s excerpt of the record, for checking the match by ear.
        ("tracks", "preview_url", "TEXT DEFAULT ''"),
        # Ownership, added when accounts arrived. Empty means "from before
        # accounts existed" — those rows are adopted by the first account to
        # sign in rather than deleted, because they are someone's work.
        ("sets", "user_id", "TEXT DEFAULT ''"),
        ("crate", "user_id", "TEXT DEFAULT ''"),
        ("watches", "user_id", "TEXT DEFAULT ''"),
        ("downloads", "user_id", "TEXT DEFAULT ''"),
        # Who passed this set on, kept on the copy so it can say so.
        ("sets", "shared_by", "TEXT DEFAULT ''"),
        # Which invitation produced this copy, so following the same link
        # twice returns the copy already made rather than a second one.
        ("sets", "shared_from", "TEXT DEFAULT ''"),
    )

    async def adopt_orphans(self, user_id: str) -> int:
        """Give everything that predates accounts to `user_id`.

        Called once, for the first account created. Without it the library
        built before this feature would be invisible to everybody — present in
        the database, owned by nobody, matching no query.
        """
        return await self._run(self._adopt_orphans_sync, user_id)

    def _adopt_orphans_sync(self, user_id: str) -> int:
        moved = 0
        with closing(self._connect()) as conn:
            for table in ("sets", "crate", "watches", "downloads"):
                moved += conn.execute(
                    f"UPDATE {table} SET user_id = ?"
                    " WHERE user_id IS NULL OR user_id = ''",
                    (user_id,)).rowcount
            conn.commit()
        if moved:
            logger.info("Adopted %d row(s) predating accounts", moved)
        return moved

    def _rebuild_for_ownership(self, conn: sqlite3.Connection) -> None:
        """Widen two keys that assumed a single user.

        `crate.track_key` was the primary key and `watches.url` was UNIQUE.
        Both are correct for one person and wrong the moment there are two:
        they would stop a second account from starring a track the first had
        starred, or following a channel the first follows — silently, as a
        constraint violation on someone else's row.

        Adding a column cannot fix a key, so these tables are rebuilt. This is
        the hand-written migration the note below asks for, and it is written
        to be safe to run twice: it checks the shape first and does nothing if
        the shape is already right.
        """
        rebuilds = (
            ("crate", "track_key",
             """CREATE TABLE crate_new (
                    track_key  TEXT NOT NULL,
                    user_id    TEXT NOT NULL DEFAULT '',
                    title      TEXT DEFAULT '',
                    artist     TEXT DEFAULT '',
                    note       TEXT DEFAULT '',
                    starred_at TEXT NOT NULL,
                    PRIMARY KEY (track_key, user_id)
                )""",
             "INSERT OR IGNORE INTO crate_new (track_key, user_id, title,"
             " artist, note, starred_at) SELECT track_key,"
             " COALESCE(user_id, ''), title, artist, note, starred_at FROM crate"),
            ("watches", "url",
             """CREATE TABLE watches_new (
                    id           TEXT PRIMARY KEY,
                    url          TEXT NOT NULL,
                    user_id      TEXT NOT NULL DEFAULT '',
                    title        TEXT DEFAULT '',
                    kind         TEXT DEFAULT 'channel',
                    created_at   TEXT NOT NULL,
                    last_checked TEXT,
                    seen_ids     TEXT DEFAULT '[]',
                    UNIQUE (url, user_id)
                )""",
             "INSERT OR IGNORE INTO watches_new (id, url, user_id, title, kind,"
             " created_at, last_checked, seen_ids) SELECT id, url,"
             " COALESCE(user_id, ''), title, kind, created_at, last_checked,"
             " seen_ids FROM watches"),
        )

        for table, column, create_sql, copy_sql in rebuilds:
            info = list(conn.execute(f"PRAGMA table_info({table})"))
            if not info:
                continue                    # fresh database; SCHEMA is current
            names = {row[1] for row in info}
            if "user_id" not in names:
                continue                    # the column migration runs first
            # Already widened? The old shape has the column as a lone primary
            # key (crate) or as a single-column unique index (watches).
            single_pk = any(row[1] == column and row[5] == 1 for row in info)
            lone_unique = any(
                idx[2] and len(list(conn.execute(
                    f"PRAGMA index_info({idx[1]})"))) == 1
                and next(iter(conn.execute(
                    f"PRAGMA index_info({idx[1]})")))[2] == column
                for idx in conn.execute(f"PRAGMA index_list({table})"))
            if not (single_pk or lone_unique):
                continue                    # already the wide shape

            logger.info("Rebuilding %s so two accounts can both use it", table)
            conn.execute(f"DROP TABLE IF EXISTS {table}_new")
            conn.execute(create_sql)
            conn.execute(copy_sql)
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add any column missing from an existing database.

        Deliberately additive only: SQLite cannot drop or retype a column
        without rebuilding the table, and a tracklist is worth more than a tidy
        schema. Anything beyond adding a column needs a real migration written
        by hand.
        """
        for table, column, definition in self.MIGRATIONS:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue                # table not created yet; SCHEMA handles it
            if column not in existing:
                logger.info("Adding %s.%s to the library", table, column)
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets the API read while an analysis is writing.
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)

    # ── Sets ──────────────────────────────────────────────────────────────

    async def save_set(self, set_id: str, title: str, result: Dict[str, Any],
                       *, user_id: str, source_url: str = "",
                       source_kind: str = "upload",
                       uploader: str = "", audio_path: str = "",
                       quality: str = "", created_at: Optional[str] = None) -> None:
        """Store a set and its tracks, replacing any set with the same id.

        `created_at` defaults to now. It is overridable so an import can keep
        the date the analysis actually happened rather than the date it was
        migrated — a library sorted by import time tells you nothing.
        """
        async with self._lock:
            await self._run(self._save_set_sync, set_id, title, result,
                            source_url, source_kind, uploader, audio_path,
                            quality, created_at, user_id)

    def _save_set_sync(self, set_id, title, result, source_url, source_kind,
                       uploader, audio_path, quality, created_at=None,
                       user_id="") -> None:
        with closing(self._connect()) as conn:
            # Scoped, so re-analysing an id cannot overwrite another account's
            # set of the same name — ids are ours, but an import supplies them.
            conn.execute("DELETE FROM sets WHERE id = ? AND user_id = ?",
                         (set_id, user_id))
            conn.execute(
                "INSERT INTO sets (id, title, source_url, source_kind, uploader,"
                " audio_path, quality, duration, waveform, stats, created_at,"
                " user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (set_id, title, source_url, source_kind, uploader, audio_path,
                 quality, result.get("duration", 0),
                 json.dumps(result.get("waveform", [])),
                 json.dumps(result.get("stats", {})), created_at or _now(),
                 user_id),
            )
            conn.executemany(
                "INSERT INTO tracks (set_id, position, start, end, identified,"
                " track_key, title, artist, album, label, year, genre, isrc, url,"
                " cover_url, bpm, camelot, musical_key, confidence, strength,"
                " preview_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (set_id, t.get("index", i + 1), t.get("start", 0), t.get("end", 0),
                     1 if t.get("identified") else 0, t.get("key", ""),
                     t.get("title", ""), t.get("artist", ""), t.get("album", ""),
                     t.get("label", ""), t.get("year", ""), t.get("genre", ""),
                     t.get("isrc", ""), t.get("url", ""), t.get("cover_url", ""),
                     t.get("bpm"), t.get("camelot"), t.get("musical_key"),
                     t.get("confidence", 0), t.get("strength", ""),
                     t.get("preview_url", ""))
                    for i, t in enumerate(result.get("tracks", []))
                ],
            )
            conn.commit()

    async def list_sets(self, *, user_id: str,
                        limit: int = 50) -> List[Dict[str, Any]]:
        return await self._run(self._list_sets_sync, limit, user_id)

    def _list_sets_sync(self, limit: int, user_id: str) -> List[Dict[str, Any]]:
        """Summaries for the library list.

        `audio_path` is deliberately dropped: it is a server filesystem path
        and the client has no use for it — playback goes through
        /api/sets/{id}/audio, which validates the path itself.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT s.*,"
                " (SELECT COUNT(*) FROM tracks t WHERE t.set_id = s.id) AS track_count,"
                " (SELECT COUNT(*) FROM tracks t WHERE t.set_id = s.id"
                "   AND t.identified = 1) AS identified_count"
                " FROM sets s WHERE s.user_id = ?"
                " ORDER BY s.created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        summaries = []
        for row in rows:
            item = _set_row(row)
            item.pop("audio_path", None)
            summaries.append(item)
        return summaries

    async def get_set(self, set_id: str, *,
                      user_id: str) -> Optional[Dict[str, Any]]:
        """A set, or None — including when it belongs to somebody else.

        Not 403: whether a set exists is itself information, and a tool that
        answers "that is not yours" tells you what other people have.
        """
        return await self._run(self._get_set_sync, set_id, user_id)

    def _get_set_sync(self, set_id: str,
                      user_id: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM sets WHERE id = ? AND user_id = ?",
                (set_id, user_id)).fetchone()
            if row is None:
                return None
            tracks = conn.execute(
                "SELECT * FROM tracks WHERE set_id = ? ORDER BY position", (set_id,)
            ).fetchall()
            starred = {r["track_key"] for r in conn.execute(
                "SELECT track_key FROM crate WHERE user_id = ?",
                (user_id,)).fetchall()}
        data = _set_row(row)
        data["waveform"] = json.loads(row["waveform"] or "[]")
        data["tracks"] = [_track_row(t, starred) for t in tracks]
        return data

    async def delete_set(self, set_id: str, *, user_id: str) -> bool:
        async with self._lock:
            return await self._run(self._delete_set_sync, set_id, user_id)

    def _delete_set_sync(self, set_id: str, user_id: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "DELETE FROM sets WHERE id = ? AND user_id = ?",
                (set_id, user_id))
            conn.commit()
            return cur.rowcount > 0

    # ── Cross-set digging ────────────────────────────────────────────────

    async def recurring_tracks(self, min_sets: int = 2, limit: int = 100,
                               *, user_id: str) -> List[Dict[str, Any]]:
        """Tracks appearing across several sets — the strongest digging signal.

        Across *your* sets. The whole value of the signal is that you keep
        hearing something; counting other people's sets would be noise.
        """
        return await self._run(self._recurring_sync, min_sets, limit, user_id)

    def _recurring_sync(self, min_sets: int, limit: int,
                        user_id: str) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT t.track_key,"
                "  MAX(t.title) AS title, MAX(t.artist) AS artist,"
                "  MAX(t.url) AS url, MAX(t.cover_url) AS cover_url,"
                "  MAX(t.label) AS label, AVG(t.bpm) AS bpm,"
                "  MAX(t.camelot) AS camelot,"
                "  COUNT(DISTINCT t.set_id) AS set_count"
                " FROM tracks t JOIN sets s ON s.id = t.set_id"
                " WHERE t.identified = 1 AND t.track_key != '' AND s.user_id = ?"
                " GROUP BY t.track_key HAVING set_count >= ?"
                " ORDER BY set_count DESC, artist ASC LIMIT ?",
                (user_id, min_sets, limit),
            ).fetchall()
        return [{
            "key": r["track_key"], "title": r["title"], "artist": r["artist"],
            "url": r["url"], "cover_url": r["cover_url"], "label": r["label"],
            "bpm": round(r["bpm"], 1) if r["bpm"] else None,
            "camelot": r["camelot"], "set_count": r["set_count"],
        } for r in rows]

    async def search_tracks(self, query: str = "", *, user_id: str,
                            bpm_min: Optional[float] = None,
                            bpm_max: Optional[float] = None,
                            camelot: Optional[str] = None,
                            starred_only: bool = False,
                            limit: int = 200) -> List[Dict[str, Any]]:
        return await self._run(self._search_sync, query, bpm_min, bpm_max,
                               camelot, starred_only, limit, user_id)

    def _search_sync(self, query, bpm_min, bpm_max, camelot, starred_only,
                     limit, user_id):
        sql = ["SELECT t.*, s.title AS set_title FROM tracks t"
               " JOIN sets s ON s.id = t.set_id"
               " WHERE t.identified = 1 AND s.user_id = ?"]
        params: List[Any] = [user_id]
        if query:
            sql.append(" AND (t.title LIKE ? OR t.artist LIKE ? OR t.label LIKE ?)")
            params += [f"%{query}%"] * 3
        if bpm_min is not None:
            sql.append(" AND t.bpm >= ?"); params.append(bpm_min)
        if bpm_max is not None:
            sql.append(" AND t.bpm <= ?"); params.append(bpm_max)
        if camelot:
            sql.append(" AND t.camelot = ?"); params.append(camelot)
        if starred_only:
            sql.append(" AND t.track_key IN"
                       " (SELECT track_key FROM crate WHERE user_id = ?)")
            params.append(user_id)
        sql.append(" ORDER BY t.artist, t.title LIMIT ?")
        params.append(limit)

        with closing(self._connect()) as conn:
            rows = conn.execute("".join(sql), params).fetchall()
            starred = {r["track_key"] for r in conn.execute(
                "SELECT track_key FROM crate WHERE user_id = ?",
                (user_id,)).fetchall()}
        out = []
        for r in rows:
            item = _track_row(r, starred)
            item["set_title"] = r["set_title"]
            out.append(item)
        return out

    # ── Downloads ────────────────────────────────────────────────────────

    async def start_download(self, track_key: str, artist: str, title: str,
                             username: str = "", remote_path: str = "",
                             quality: str = "", size: int = 0, *,
                             user_id: str) -> int:
        """Record an attempt and return its id."""
        async with self._lock:
            return await self._run(self._start_download_sync, track_key, artist,
                                   title, username, remote_path, quality, size,
                                   user_id)

    def _start_download_sync(self, track_key, artist, title, username,
                             remote_path, quality, size, user_id) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT INTO downloads (track_key, artist, title, status,"
                " message, quality, username, remote_path, size, created_at,"
                " updated_at, user_id)"
                " VALUES (?,?,?,'queued','Queued',?,?,?,?,?,?,?)",
                (track_key, artist, title, quality, username, remote_path,
                 size, _now(), _now(), user_id),
            )
            conn.commit()
            return int(cur.lastrowid)

    async def update_download(self, download_id: int, **fields) -> None:
        async with self._lock:
            await self._run(self._update_download_sync, download_id, fields)

    def _update_download_sync(self, download_id: int,
                              fields: Dict[str, Any]) -> None:
        allowed = {"status", "message", "local_path", "verified", "progress",
                   "quality", "size", "username", "remote_path"}
        writable = {k: v for k, v in fields.items() if k in allowed}
        if not writable:
            return
        writable["updated_at"] = _now()
        assignments = ", ".join(f"{k} = ?" for k in writable)
        with closing(self._connect()) as conn:
            conn.execute(f"UPDATE downloads SET {assignments} WHERE id = ?",
                         (*writable.values(), download_id))
            conn.commit()

    async def get_download(self, download_id: int, *, user_id: Optional[str]
                           ) -> Optional[Dict[str, Any]]:
        """One download, scoped — or unscoped when user_id is None.

        Scoped because a download id is a small sequential integer. Left
        unscoped in a request path, anyone could walk the numbers and read
        what everybody else has been fetching, and through `download_path`
        the files themselves.

        None is for the worker, which is already acting on a row it was handed
        and has no session to check against. It has to be passed explicitly:
        the argument stays required so that forgetting it raises rather than
        quietly returning somebody else's download.
        """
        return await self._run(self._get_download_sync, download_id, user_id)

    def _get_download_sync(self, download_id: int,
                           user_id: Optional[str]) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            if user_id is None:
                row = conn.execute("SELECT * FROM downloads WHERE id = ?",
                                   (download_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM downloads WHERE id = ? AND user_id = ?",
                    (download_id, user_id)).fetchone()
        return _download_row(row) if row else None

    async def download_path(self, download_id: int, *,
                            user_id: str) -> Optional[str]:
        """The file's location on disk, for serving it.

        Separate from `get_download` because that shape goes to the browser and
        a server filesystem path has no business there.
        """
        return await self._run(self._download_path_sync, download_id, user_id)

    def _download_path_sync(self, download_id: int,
                            user_id: str) -> Optional[str]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT local_path FROM downloads WHERE id = ? AND user_id = ?",
                (download_id, user_id)).fetchone()
        return (row["local_path"] or None) if row else None

    async def downloads_for(self, track_key: str, *,
                            user_id: str) -> List[Dict[str, Any]]:
        return await self._run(self._downloads_for_sync, track_key, user_id)

    def _downloads_for_sync(self, track_key: str,
                            user_id: str) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM downloads WHERE track_key = ? AND user_id = ?"
                " ORDER BY created_at DESC", (track_key, user_id)).fetchall()
        return [_download_row(r) for r in rows]

    async def recent_downloads(self, limit: int = 50, *,
                               user_id: str) -> List[Dict[str, Any]]:
        return await self._run(self._recent_downloads_sync, limit, user_id)

    def _recent_downloads_sync(self, limit: int,
                               user_id: str) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM downloads WHERE user_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (user_id, limit)).fetchall()
        return [_download_row(r) for r in rows]

    # ── Enrichment ───────────────────────────────────────────────────────

    async def cached_enrichment(self, track_key: str) -> Optional[Dict[str, Any]]:
        """What a provider last said about this track, hit or miss."""
        return await self._run(self._cached_enrichment_sync, track_key)

    def _cached_enrichment_sync(self, track_key: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM enrichment WHERE track_key = ?",
                               (track_key,)).fetchone()
        return dict(row) if row else None

    async def remember_enrichment(self, track_key: str,
                                  meta: Optional[Dict[str, Any]]) -> None:
        """Record a lookup result. `None` records that nothing was found."""
        async with self._lock:
            await self._run(self._remember_enrichment_sync, track_key, meta)

    def _remember_enrichment_sync(self, track_key: str,
                                  meta: Optional[Dict[str, Any]]) -> None:
        payload = meta or {}
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO enrichment (track_key, label,"
                " catalog_number, year, album, genre, isrc, mbid, provider,"
                " confidence, found, checked_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (track_key, payload.get("label", ""),
                 payload.get("catalog_number", ""), payload.get("year", ""),
                 payload.get("album", ""), payload.get("genre", ""),
                 payload.get("isrc", ""), payload.get("recording_id", ""),
                 payload.get("provider", ""), payload.get("confidence", 0.0),
                 1 if meta else 0, _now()),
            )
            conn.commit()

    async def apply_enrichment(self, track_key: str,
                               fields: Dict[str, Any]) -> int:
        """Write found metadata onto every track sharing this key.

        Across all sets, not just the one being enriched: the same record in
        four mixes is one lookup and four updated rows.
        """
        if not fields:
            return 0
        async with self._lock:
            return await self._run(self._apply_enrichment_sync, track_key, fields)

    def _apply_enrichment_sync(self, track_key: str,
                               fields: Dict[str, Any]) -> int:
        allowed = {"label", "year", "album", "genre", "isrc",
                   "catalog_number", "mbid"}
        writable = {k: v for k, v in fields.items() if k in allowed}
        if not writable:
            return 0
        assignments = ", ".join(f"{k} = ?" for k in writable)
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"UPDATE tracks SET {assignments} WHERE track_key = ?",
                (*writable.values(), track_key),
            )
            conn.commit()
            return cur.rowcount

    async def tracks_needing_enrichment(self, set_id: str) -> List[Dict[str, str]]:
        """Identified tracks in a set that have no label yet."""
        return await self._run(self._needing_enrichment_sync, set_id)

    def _needing_enrichment_sync(self, set_id: str) -> List[Dict[str, str]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT track_key, artist, title, isrc FROM tracks"
                " WHERE set_id = ? AND identified = 1 AND track_key != ''"
                "   AND (label IS NULL OR label = '')",
                (set_id,),
            ).fetchall()
        return [{"key": r["track_key"], "artist": r["artist"],
                 "title": r["title"], "isrc": r["isrc"] or ""} for r in rows]

    # ── Crate ────────────────────────────────────────────────────────────

    async def toggle_star(self, track_key: str, title: str = "",
                          artist: str = "", *, user_id: str) -> bool:
        """Star or unstar. Returns the resulting state."""
        async with self._lock:
            return await self._run(self._toggle_star_sync, track_key, title,
                                   artist, user_id)

    def _toggle_star_sync(self, track_key: str, title: str, artist: str,
                          user_id: str) -> bool:
        with closing(self._connect()) as conn:
            existing = conn.execute(
                "SELECT 1 FROM crate WHERE track_key = ? AND user_id = ?",
                (track_key, user_id)).fetchone()
            if existing:
                conn.execute(
                    "DELETE FROM crate WHERE track_key = ? AND user_id = ?",
                    (track_key, user_id))
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO crate (track_key, user_id, title, artist, starred_at)"
                " VALUES (?,?,?,?,?)", (track_key, user_id, title, artist, _now()))
            conn.commit()
            return True

    async def crate(self, *, user_id: str) -> List[Dict[str, Any]]:
        return await self._run(self._crate_sync, user_id)

    def _crate_sync(self, user_id: str) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT c.track_key, c.starred_at,"
                " MAX(t.title) AS title, MAX(t.artist) AS artist,"
                " MAX(t.url) AS url, MAX(t.cover_url) AS cover_url,"
                " MAX(t.label) AS label, AVG(t.bpm) AS bpm,"
                " MAX(t.camelot) AS camelot, COUNT(DISTINCT t.set_id) AS set_count"
                " FROM crate c LEFT JOIN tracks t ON t.track_key = c.track_key"
                " WHERE c.user_id = ?"
                " GROUP BY c.track_key ORDER BY c.starred_at DESC",
                (user_id,)
            ).fetchall()
        return [{
            "key": r["track_key"], "title": r["title"] or "", "artist": r["artist"] or "",
            "url": r["url"] or "", "cover_url": r["cover_url"] or "",
            "label": r["label"] or "", "camelot": r["camelot"],
            "bpm": round(r["bpm"], 1) if r["bpm"] else None,
            "set_count": r["set_count"], "starred_at": r["starred_at"], "starred": True,
        } for r in rows]

    async def remember_preview(self, track_key: str, url: str) -> None:
        """Cache a looked-up excerpt against every row for that track.

        Written back so the lookup happens once per record rather than once
        per row per page load — the same track appears in several sets, and
        each appearance would otherwise be its own request to Apple.
        """
        if track_key and url:
            await self._run(self._remember_preview_sync, track_key, url)

    def _remember_preview_sync(self, track_key: str, url: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE tracks SET preview_url = ? WHERE track_key = ?"
                " AND (preview_url IS NULL OR preview_url = '')",
                (url, track_key))
            conn.commit()

    async def preview_for(self, track_key: str) -> Optional[str]:
        """Any excerpt already known for this track."""
        return await self._run(self._preview_for_sync, track_key)

    def _preview_for_sync(self, track_key: str) -> Optional[str]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT preview_url FROM tracks WHERE track_key = ?"
                " AND preview_url != '' LIMIT 1", (track_key,)).fetchone()
        return row["preview_url"] if row else None

    async def track_names(self, track_key: str) -> Optional[Dict[str, str]]:
        """Artist, title and ISRC for a track key, for looking it up."""
        return await self._run(self._track_names_sync, track_key)

    def _track_names_sync(self, track_key: str) -> Optional[Dict[str, str]]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT artist, title, isrc FROM tracks WHERE track_key = ?"
                " LIMIT 1", (track_key,)).fetchone()
        if row is None:
            return None
        return {"artist": row["artist"] or "", "title": row["title"] or "",
                "isrc": row["isrc"] or ""}

    # ── Sharing ──────────────────────────────────────────────────────────

    async def create_share(self, set_id: str, *, user_id: str,
                           from_name: str, to_email: str = "") -> Optional[str]:
        """Mint an invitation to copy one of your sets. Returns its token."""
        import secrets

        if await self.get_set(set_id, user_id=user_id) is None:
            return None                 # not yours, or not there
        token = secrets.token_urlsafe(12)
        await self._run(self._create_share_sync, token, set_id, user_id,
                        from_name, to_email)
        return token

    def _create_share_sync(self, token, set_id, user_id, from_name,
                           to_email) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO shares (token, set_id, from_user, from_name,"
                " to_email, created_at) VALUES (?,?,?,?,?,?)",
                (token, set_id, user_id, from_name, to_email, _now()))
            conn.commit()

    async def peek_share(self, token: str) -> Optional[Dict[str, Any]]:
        """What is behind an invitation, without claiming it.

        So the page someone lands on can say what they are being offered
        before asking them to sign in for it.
        """
        return await self._run(self._peek_share_sync, token)

    def _peek_share_sync(self, token: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT s.token, s.set_id, s.from_name, s.claimed_at,"
                " t.title, t.duration,"
                " (SELECT COUNT(*) FROM tracks WHERE set_id = t.id) AS track_count"
                " FROM shares s JOIN sets t ON t.id = s.set_id"
                " WHERE s.token = ?", (token,)).fetchone()
        if row is None:
            return None
        return {"token": row["token"], "title": row["title"],
                "duration": row["duration"], "track_count": row["track_count"],
                "from_name": row["from_name"],
                "already_claimed": bool(row["claimed_at"])}

    async def claim_share(self, token: str, *,
                          user_id: str) -> Optional[Dict[str, Any]]:
        """Copy the shared set into `user_id`'s library.

        A copy rather than a grant of access: the recipient gets their own
        row, their own stars, and the right to delete it — and the sender
        cannot see what they do with it or take it back.

        The audio is deliberately *not* copied. It is a byproduct kept so the
        waveform can be scrubbed, it is swept on a timer anyway, and
        duplicating a 69 MB file for every share would fill the disk to give
        each person their own copy of the same bytes. The tracklist, the
        waveform and the timings — everything the set actually is — come
        across.

        Claiming twice returns the copy already made rather than a second one.
        """
        return await self._run(self._claim_share_sync, token, user_id)

    def _claim_share_sync(self, token: str,
                          user_id: str) -> Optional[Dict[str, Any]]:
        import secrets

        with closing(self._connect()) as conn:
            share = conn.execute("SELECT * FROM shares WHERE token = ?",
                                 (token,)).fetchone()
            if share is None:
                return None
            if share["from_user"] == user_id:
                return {"set_id": share["set_id"], "already_yours": True}

            existing = conn.execute(
                "SELECT id FROM sets WHERE user_id = ? AND shared_from = ?",
                (user_id, token)).fetchone() if _has_column(
                    conn, "sets", "shared_from") else None
            if existing:
                return {"set_id": existing["id"], "already_claimed": True}

            source = conn.execute("SELECT * FROM sets WHERE id = ?",
                                  (share["set_id"],)).fetchone()
            if source is None:
                return None             # the sender deleted it since

            new_id = secrets.token_hex(8)
            columns = [k for k in source.keys() if k != "id"]
            values = {k: source[k] for k in columns}
            values["user_id"] = user_id
            values["shared_by"] = share["from_name"] or ""
            # Not the audio: see the docstring. The copy plays nothing until
            # its owner re-analyses the source, which the UI offers.
            values["audio_path"] = ""
            values["created_at"] = _now()
            if "shared_from" in values:
                values["shared_from"] = token

            names = ", ".join(["id"] + list(values))
            marks = ", ".join(["?"] * (len(values) + 1))
            conn.execute(f"INSERT INTO sets ({names}) VALUES ({marks})",
                         (new_id, *values.values()))

            rows = conn.execute("SELECT * FROM tracks WHERE set_id = ?"
                                " ORDER BY position", (share["set_id"],)).fetchall()
            if rows:
                keep = [k for k in rows[0].keys() if k != "id"]
                names = ", ".join(keep)
                marks = ", ".join(["?"] * len(keep))
                conn.executemany(
                    f"INSERT INTO tracks ({names}) VALUES ({marks})",
                    [tuple(new_id if k == "set_id" else r[k] for k in keep)
                     for r in rows])

            conn.execute(
                "UPDATE shares SET claimed_at = ?, claimed_by = ?"
                " WHERE token = ? AND claimed_at IS NULL",
                (_now(), user_id, token))
            conn.commit()
            logger.info("Share %s copied to %s as %s", token, user_id, new_id)
            return {"set_id": new_id, "from_name": share["from_name"]}

    # ── Watches ──────────────────────────────────────────────────────────

    async def add_watch(self, watch_id: str, url: str, title: str,
                        kind: str = "channel", *, user_id: str) -> None:
        async with self._lock:
            await self._run(self._add_watch_sync, watch_id, url, title, kind,
                            user_id)

    def _add_watch_sync(self, watch_id, url, title, kind, user_id) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watches (id, url, user_id, title, kind,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (watch_id, url, user_id, title, kind, _now()))
            conn.commit()

    async def list_watches(self, *, user_id: Optional[str] = None
                           ) -> List[Dict[str, Any]]:
        """Watches for one account, or every account when user_id is None.

        The scheduled check runs for everybody at once and has no session, so
        it is the one caller allowed to ask unscoped — deliberately by passing
        None rather than by omitting the argument, so it cannot happen by
        accident in a request path.
        """
        return await self._run(self._list_watches_sync, user_id)

    def _list_watches_sync(self, user_id: Optional[str]) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            if user_id is None:
                rows = conn.execute(
                    "SELECT * FROM watches ORDER BY created_at DESC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM watches WHERE user_id = ?"
                    " ORDER BY created_at DESC", (user_id,)).fetchall()
        return [{
            "id": r["id"], "url": r["url"], "title": r["title"], "kind": r["kind"],
            "created_at": r["created_at"], "last_checked": r["last_checked"],
            "seen_count": len(json.loads(r["seen_ids"] or "[]")),
            # Carried because the scheduled check runs for everyone at once and
            # has to file each new upload under the account that follows the
            # channel. Without it those analyses land owned by nobody and are
            # invisible to the person who asked for them.
            "user_id": r["user_id"] if "user_id" in r.keys() else "",
        } for r in rows]

    async def mark_watch_checked(self, watch_id: str, seen_ids: List[str]) -> None:
        async with self._lock:
            await self._run(self._mark_watch_sync, watch_id, seen_ids)

    def _mark_watch_sync(self, watch_id: str, seen_ids: List[str]) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE watches SET last_checked = ?, seen_ids = ? WHERE id = ?",
                (_now(), json.dumps(seen_ids[-500:]), watch_id))
            conn.commit()

    async def watch_seen_ids(self, watch_id: str) -> List[str]:
        return await self._run(self._watch_seen_sync, watch_id)

    def _watch_seen_sync(self, watch_id: str) -> List[str]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT seen_ids FROM watches WHERE id = ?", (watch_id,)).fetchone()
        return json.loads(row["seen_ids"] or "[]") if row else []

    async def delete_watch(self, watch_id: str, *, user_id: str) -> bool:
        async with self._lock:
            return await self._run(self._delete_watch_sync, watch_id, user_id)

    def _delete_watch_sync(self, watch_id: str, user_id: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "DELETE FROM watches WHERE id = ? AND user_id = ?",
                (watch_id, user_id))
            conn.commit()
            return cur.rowcount > 0


def _download_row(r: sqlite3.Row) -> Dict[str, Any]:
    local = r["local_path"] or ""
    return {
        "id": r["id"], "track_key": r["track_key"], "artist": r["artist"],
        "title": r["title"], "status": r["status"], "message": r["message"],
        "quality": r["quality"], "username": r["username"],
        "filename": Path(local).name if local else "",
        # Whether the bytes are still here, not merely whether they once were:
        # downloads are swept on the same schedule as set audio.
        "available": bool(local and Path(local).exists()),
        "size": r["size"], "verified": bool(r["verified"]),
        "progress": r["progress"], "created_at": r["created_at"],
    }


def _set_row(r: sqlite3.Row) -> Dict[str, Any]:
    keys = r.keys()
    return {
        "id": r["id"], "title": r["title"], "source_url": r["source_url"],
        "source_kind": r["source_kind"], "uploader": r["uploader"],
        "quality": r["quality"], "duration": r["duration"],
        "has_audio": bool(r["audio_path"] and Path(r["audio_path"]).exists()),
        "audio_path": r["audio_path"],
        "stats": json.loads(r["stats"] or "{}"),
        "created_at": r["created_at"],
        # Who passed it on, when it came from somebody else. Guarded like the
        # rest: a database written before sharing existed has no column.
        "shared_by": r["shared_by"] if "shared_by" in keys else "",
        "track_count": r["track_count"] if "track_count" in keys else None,
        "identified_count": r["identified_count"] if "identified_count" in keys else None,
    }


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _track_row(r: sqlite3.Row, starred: set) -> Dict[str, Any]:
    key = r["track_key"]
    return {
        "index": r["position"], "start": r["start"], "end": r["end"],
        # Recomputed rather than stored: the exporters need it, and deriving
        # it here keeps a set read back from the library identical in shape to
        # one straight out of the pipeline.
        "start_label": format_timestamp(r["start"]),
        "duration": round(r["end"] - r["start"], 3),
        "identified": bool(r["identified"]), "key": key,
        "title": r["title"], "artist": r["artist"], "album": r["album"],
        "label": r["label"], "year": r["year"], "genre": r["genre"],
        "isrc": r["isrc"], "url": r["url"], "cover_url": r["cover_url"],
        "bpm": r["bpm"], "camelot": r["camelot"], "musical_key": r["musical_key"],
        "confidence": r["confidence"],
        "strength": r["strength"] if "strength" in r.keys() else "",
        "catalog_number": r["catalog_number"] if "catalog_number" in r.keys() else "",
        "mbid": r["mbid"] if "mbid" in r.keys() else "",
        # Whether an excerpt is known, not where it lives. The upstream URL is
        # a server-side cache key: handing it to the browser once meant the
        # page played Apple's copy directly, which Chrome refuses because
        # Apple labels it audio/x-m4p. One address for the audio, ours.
        "has_preview": bool(
            r["preview_url"] if "preview_url" in r.keys() else ""),
        "starred": key in starred,
    }
