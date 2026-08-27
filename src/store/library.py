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
    track_key    TEXT PRIMARY KEY,
    title        TEXT DEFAULT '',
    artist       TEXT DEFAULT '',
    note         TEXT DEFAULT '',
    starred_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watches (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,
    title        TEXT DEFAULT '',
    kind         TEXT DEFAULT 'channel',
    created_at   TEXT NOT NULL,
    last_checked TEXT,
    seen_ids     TEXT DEFAULT '[]'
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
            conn.commit()

    # Columns added after the first release, as (table, column, definition).
    # SCHEMA above uses CREATE TABLE IF NOT EXISTS, which does nothing to a
    # database that already exists — so a new column has to be added here too
    # or it only appears on fresh installs and production keeps the old shape.
    MIGRATIONS = (
        ("tracks", "strength", "TEXT DEFAULT ''"),
        ("tracks", "catalog_number", "TEXT DEFAULT ''"),
        ("tracks", "mbid", "TEXT DEFAULT ''"),
    )

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
                       *, source_url: str = "", source_kind: str = "upload",
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
                            quality, created_at)

    def _save_set_sync(self, set_id, title, result, source_url, source_kind,
                       uploader, audio_path, quality, created_at=None) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM sets WHERE id = ?", (set_id,))
            conn.execute(
                "INSERT INTO sets (id, title, source_url, source_kind, uploader,"
                " audio_path, quality, duration, waveform, stats, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (set_id, title, source_url, source_kind, uploader, audio_path,
                 quality, result.get("duration", 0),
                 json.dumps(result.get("waveform", [])),
                 json.dumps(result.get("stats", {})), created_at or _now()),
            )
            conn.executemany(
                "INSERT INTO tracks (set_id, position, start, end, identified,"
                " track_key, title, artist, album, label, year, genre, isrc, url,"
                " cover_url, bpm, camelot, musical_key, confidence, strength)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (set_id, t.get("index", i + 1), t.get("start", 0), t.get("end", 0),
                     1 if t.get("identified") else 0, t.get("key", ""),
                     t.get("title", ""), t.get("artist", ""), t.get("album", ""),
                     t.get("label", ""), t.get("year", ""), t.get("genre", ""),
                     t.get("isrc", ""), t.get("url", ""), t.get("cover_url", ""),
                     t.get("bpm"), t.get("camelot"), t.get("musical_key"),
                     t.get("confidence", 0), t.get("strength", ""))
                    for i, t in enumerate(result.get("tracks", []))
                ],
            )
            conn.commit()

    async def list_sets(self, limit: int = 50) -> List[Dict[str, Any]]:
        return await self._run(self._list_sets_sync, limit)

    def _list_sets_sync(self, limit: int) -> List[Dict[str, Any]]:
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
                " FROM sets s ORDER BY s.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        summaries = []
        for row in rows:
            item = _set_row(row)
            item.pop("audio_path", None)
            summaries.append(item)
        return summaries

    async def get_set(self, set_id: str) -> Optional[Dict[str, Any]]:
        return await self._run(self._get_set_sync, set_id)

    def _get_set_sync(self, set_id: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM sets WHERE id = ?", (set_id,)).fetchone()
            if row is None:
                return None
            tracks = conn.execute(
                "SELECT * FROM tracks WHERE set_id = ? ORDER BY position", (set_id,)
            ).fetchall()
            starred = {r["track_key"] for r in conn.execute(
                "SELECT track_key FROM crate").fetchall()}
        data = _set_row(row)
        data["waveform"] = json.loads(row["waveform"] or "[]")
        data["tracks"] = [_track_row(t, starred) for t in tracks]
        return data

    async def delete_set(self, set_id: str) -> bool:
        async with self._lock:
            return await self._run(self._delete_set_sync, set_id)

    def _delete_set_sync(self, set_id: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM sets WHERE id = ?", (set_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Cross-set digging ────────────────────────────────────────────────

    async def recurring_tracks(self, min_sets: int = 2, limit: int = 100
                               ) -> List[Dict[str, Any]]:
        """Tracks appearing across several sets — the strongest digging signal."""
        return await self._run(self._recurring_sync, min_sets, limit)

    def _recurring_sync(self, min_sets: int, limit: int) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT track_key,"
                "  MAX(title) AS title, MAX(artist) AS artist,"
                "  MAX(url) AS url, MAX(cover_url) AS cover_url,"
                "  MAX(label) AS label, AVG(bpm) AS bpm, MAX(camelot) AS camelot,"
                "  COUNT(DISTINCT set_id) AS set_count"
                " FROM tracks WHERE identified = 1 AND track_key != ''"
                " GROUP BY track_key HAVING set_count >= ?"
                " ORDER BY set_count DESC, artist ASC LIMIT ?",
                (min_sets, limit),
            ).fetchall()
        return [{
            "key": r["track_key"], "title": r["title"], "artist": r["artist"],
            "url": r["url"], "cover_url": r["cover_url"], "label": r["label"],
            "bpm": round(r["bpm"], 1) if r["bpm"] else None,
            "camelot": r["camelot"], "set_count": r["set_count"],
        } for r in rows]

    async def search_tracks(self, query: str = "", *, bpm_min: Optional[float] = None,
                            bpm_max: Optional[float] = None,
                            camelot: Optional[str] = None,
                            starred_only: bool = False,
                            limit: int = 200) -> List[Dict[str, Any]]:
        return await self._run(self._search_sync, query, bpm_min, bpm_max,
                               camelot, starred_only, limit)

    def _search_sync(self, query, bpm_min, bpm_max, camelot, starred_only, limit):
        sql = ["SELECT t.*, s.title AS set_title FROM tracks t"
               " JOIN sets s ON s.id = t.set_id WHERE t.identified = 1"]
        params: List[Any] = []
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
            sql.append(" AND t.track_key IN (SELECT track_key FROM crate)")
        sql.append(" ORDER BY t.artist, t.title LIMIT ?")
        params.append(limit)

        with closing(self._connect()) as conn:
            rows = conn.execute("".join(sql), params).fetchall()
            starred = {r["track_key"] for r in conn.execute(
                "SELECT track_key FROM crate").fetchall()}
        out = []
        for r in rows:
            item = _track_row(r, starred)
            item["set_title"] = r["set_title"]
            out.append(item)
        return out

    # ── Downloads ────────────────────────────────────────────────────────

    async def start_download(self, track_key: str, artist: str, title: str,
                             username: str = "", remote_path: str = "",
                             quality: str = "", size: int = 0) -> int:
        """Record an attempt and return its id."""
        async with self._lock:
            return await self._run(self._start_download_sync, track_key, artist,
                                   title, username, remote_path, quality, size)

    def _start_download_sync(self, track_key, artist, title, username,
                             remote_path, quality, size) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT INTO downloads (track_key, artist, title, status,"
                " message, quality, username, remote_path, size, created_at,"
                " updated_at) VALUES (?,?,?,'queued','Queued',?,?,?,?,?,?)",
                (track_key, artist, title, quality, username, remote_path,
                 size, _now(), _now()),
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

    async def get_download(self, download_id: int) -> Optional[Dict[str, Any]]:
        return await self._run(self._get_download_sync, download_id)

    def _get_download_sync(self, download_id: int) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM downloads WHERE id = ?",
                               (download_id,)).fetchone()
        return _download_row(row) if row else None

    async def download_path(self, download_id: int) -> Optional[str]:
        """The file's location on disk, for serving it.

        Separate from `get_download` because that shape goes to the browser and
        a server filesystem path has no business there.
        """
        return await self._run(self._download_path_sync, download_id)

    def _download_path_sync(self, download_id: int) -> Optional[str]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT local_path FROM downloads WHERE id = ?",
                               (download_id,)).fetchone()
        return (row["local_path"] or None) if row else None

    async def downloads_for(self, track_key: str) -> List[Dict[str, Any]]:
        return await self._run(self._downloads_for_sync, track_key)

    def _downloads_for_sync(self, track_key: str) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM downloads WHERE track_key = ?"
                " ORDER BY created_at DESC", (track_key,)).fetchall()
        return [_download_row(r) for r in rows]

    async def recent_downloads(self, limit: int = 50) -> List[Dict[str, Any]]:
        return await self._run(self._recent_downloads_sync, limit)

    def _recent_downloads_sync(self, limit: int) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM downloads ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
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
                          artist: str = "") -> bool:
        """Star or unstar. Returns the resulting state."""
        async with self._lock:
            return await self._run(self._toggle_star_sync, track_key, title, artist)

    def _toggle_star_sync(self, track_key: str, title: str, artist: str) -> bool:
        with closing(self._connect()) as conn:
            existing = conn.execute(
                "SELECT 1 FROM crate WHERE track_key = ?", (track_key,)).fetchone()
            if existing:
                conn.execute("DELETE FROM crate WHERE track_key = ?", (track_key,))
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO crate (track_key, title, artist, starred_at)"
                " VALUES (?,?,?,?)", (track_key, title, artist, _now()))
            conn.commit()
            return True

    async def crate(self) -> List[Dict[str, Any]]:
        return await self._run(self._crate_sync)

    def _crate_sync(self) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT c.track_key, c.starred_at,"
                " MAX(t.title) AS title, MAX(t.artist) AS artist,"
                " MAX(t.url) AS url, MAX(t.cover_url) AS cover_url,"
                " MAX(t.label) AS label, AVG(t.bpm) AS bpm,"
                " MAX(t.camelot) AS camelot, COUNT(DISTINCT t.set_id) AS set_count"
                " FROM crate c LEFT JOIN tracks t ON t.track_key = c.track_key"
                " GROUP BY c.track_key ORDER BY c.starred_at DESC"
            ).fetchall()
        return [{
            "key": r["track_key"], "title": r["title"] or "", "artist": r["artist"] or "",
            "url": r["url"] or "", "cover_url": r["cover_url"] or "",
            "label": r["label"] or "", "camelot": r["camelot"],
            "bpm": round(r["bpm"], 1) if r["bpm"] else None,
            "set_count": r["set_count"], "starred_at": r["starred_at"], "starred": True,
        } for r in rows]

    # ── Watches ──────────────────────────────────────────────────────────

    async def add_watch(self, watch_id: str, url: str, title: str,
                        kind: str = "channel") -> None:
        async with self._lock:
            await self._run(self._add_watch_sync, watch_id, url, title, kind)

    def _add_watch_sync(self, watch_id, url, title, kind) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watches (id, url, title, kind, created_at)"
                " VALUES (?,?,?,?,?)", (watch_id, url, title, kind, _now()))
            conn.commit()

    async def list_watches(self) -> List[Dict[str, Any]]:
        return await self._run(self._list_watches_sync)

    def _list_watches_sync(self) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM watches ORDER BY created_at DESC").fetchall()
        return [{
            "id": r["id"], "url": r["url"], "title": r["title"], "kind": r["kind"],
            "created_at": r["created_at"], "last_checked": r["last_checked"],
            "seen_count": len(json.loads(r["seen_ids"] or "[]")),
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

    async def delete_watch(self, watch_id: str) -> bool:
        async with self._lock:
            return await self._run(self._delete_watch_sync, watch_id)

    def _delete_watch_sync(self, watch_id: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
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
        "track_count": r["track_count"] if "track_count" in keys else None,
        "identified_count": r["identified_count"] if "identified_count" in keys else None,
    }


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
        "starred": key in starred,
    }
