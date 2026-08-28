"""HTTP surface: validation, the library, exports, and the hardening fixes."""
import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import TEST_USER

pytestmark = pytest.mark.anyio

RESULT = {
    "duration": 600.0,
    "waveform": [0.1, 0.9, 0.5],
    "stats": {"identified": 1, "unidentified": 1, "coverage": 0.5},
    "tracks": [
        {"index": 1, "start": 0, "end": 300, "start_label": "00:00:00",
         "duration": 300, "identified": True, "title": "Loose Lips",
         "artist": "Chris Stussy", "key": "chris stussy::loose lips",
         "album": "", "label": "Stuss", "year": "2023", "genre": "Dance",
         "isrc": "", "url": "https://shazam/1", "cover_url": "",
         "bpm": 126.0, "camelot": "8A", "musical_key": "A min",
         "confidence": 1.0},
        {"index": 2, "start": 300, "end": 600, "start_label": "00:05:00",
         "duration": 300, "identified": False, "title": "ID ?", "artist": "",
         "key": "", "album": "", "label": "", "year": "", "genre": "",
         "isrc": "", "url": "", "cover_url": "", "bpm": None,
         "camelot": None, "musical_key": None, "confidence": 0.0},
    ],
}


async def test_health(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_head_on_root_is_not_a_405(client):
    """Uptime monitors send HEAD; a 405 there reads as 'site down'.

    Both outcomes are checked rather than assuming a built frontend: this test
    must not depend on whether `web/dist` happens to exist, or it passes
    locally after a build and fails in CI before one.
    """
    response = await client.head("/")
    assert response.status_code != 405, "HEAD must be routed, not rejected"

    if (client.web.FRONTEND_DIST / "index.html").exists():
        assert response.status_code == 200
    else:
        assert response.status_code == 503, "unbuilt frontend should say so"


async def test_root_explains_itself_when_the_frontend_is_not_built(client, tmp_path):
    """A 503 here should tell you what to run, not just fail."""
    client.web.FRONTEND_DIST = tmp_path / "absent"
    response = await client.get("/")
    assert response.status_code == 503
    assert "npm run build" in response.json()["detail"]


@pytest.mark.parametrize("url,detail", [
    ("", "URL is required"),
    ("not-a-url", "http"),
    ("ftp://example.com/x", "http"),
])
async def test_url_validation(client, url, detail):
    response = await client.post("/api/analyze/url", json={"url": url})
    assert response.status_code == 400
    assert detail.lower() in response.json()["detail"].lower()


async def test_upload_rejects_unsupported_formats(client):
    response = await client.post(
        "/api/analyze/upload",
        files={"file": ("notes.txt", b"not audio", "text/plain")})
    assert response.status_code == 400
    assert "unsupported" in response.json()["detail"].lower()


async def test_upload_over_the_size_cap_is_refused_and_cleans_up(client):
    client.web.MAX_UPLOAD_BYTES = 1024
    response = await client.post(
        "/api/analyze/upload",
        files={"file": ("big.mp3", b"\x00" * 5000, "audio/mpeg")})
    assert response.status_code == 413
    assert list(client.web.UPLOAD_DIR.iterdir()) == [], "partial upload left behind"


async def test_unknown_task_is_a_404(client):
    assert (await client.get("/api/tasks/does-not-exist")).status_code == 404


async def test_set_lifecycle(client):
    await client.web.library.save_set("s1", "My Set", RESULT, source_url="u",
                                      uploader="Someone", quality="opus 160", user_id=TEST_USER)

    listing = (await client.get("/api/sets")).json()
    assert len(listing) == 1
    assert listing[0]["identified_count"] == 1
    assert listing[0]["track_count"] == 2

    detail = (await client.get("/api/sets/s1")).json()
    assert detail["title"] == "My Set"
    assert len(detail["tracks"]) == 2
    assert detail["waveform"] == [0.1, 0.9, 0.5]
    assert detail["tracks"][1]["identified"] is False

    assert (await client.delete("/api/sets/s1")).status_code == 200
    assert (await client.get("/api/sets/s1")).status_code == 404


async def test_server_paths_never_reach_the_client(client):
    """`audio_path` is a filesystem path — it has no business in a response."""
    await client.web.library.save_set("s1", "My Set", RESULT,
                                      audio_path="/srv/secret/audio.mp3", user_id=TEST_USER)
    assert "audio_path" not in (await client.get("/api/sets")).text
    assert "audio_path" not in (await client.get("/api/sets/s1")).text


@pytest.mark.parametrize("fmt,marker", [
    ("json", '"tracks"'), ("txt", "Loose Lips"), ("csv", "#,start,artist"),
    ("m3u", "#EXTM3U"), ("rekordbox", "DJ_PLAYLISTS"),
])
async def test_exports(client, fmt, marker):
    await client.web.library.save_set("s1", "My Set", RESULT, user_id=TEST_USER)
    response = await client.get(f"/api/sets/s1/export/{fmt}")
    assert response.status_code == 200
    assert marker in response.text
    assert "attachment" in response.headers["content-disposition"]


async def test_unknown_export_format_is_rejected(client):
    await client.web.library.save_set("s1", "My Set", RESULT, user_id=TEST_USER)
    response = await client.get("/api/sets/s1/export/wav")
    assert response.status_code == 400


async def test_audio_requires_a_kept_file(client):
    await client.web.library.save_set("s1", "My Set", RESULT, audio_path="", user_id=TEST_USER)
    assert (await client.get("/api/sets/s1/audio")).status_code == 404


async def test_audio_outside_the_media_directory_is_refused(client, tmp_path):
    """A path escaping the media roots must never be served."""
    outsider = tmp_path / "etc-passwd"
    outsider.write_bytes(b"root:x:0:0")
    await client.web.library.save_set("s1", "My Set", RESULT,
                                      audio_path=str(outsider), user_id=TEST_USER)
    assert (await client.get("/api/sets/s1/audio")).status_code == 400


async def test_audio_range_requests(client):
    audio = client.web.MEDIA_DIR / "set.mp3"
    audio.write_bytes(bytes(range(256)) * 40)          # 10240 bytes
    await client.web.library.save_set("s1", "My Set", RESULT,
                                      audio_path=str(audio), user_id=TEST_USER)

    full = await client.get("/api/sets/s1/audio")
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"

    part = await client.get("/api/sets/s1/audio",
                            headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.headers["content-range"] == "bytes 100-199/10240"
    assert len(part.content) == 100

    suffix = await client.get("/api/sets/s1/audio",
                              headers={"Range": "bytes=-50"})
    assert suffix.status_code == 206
    assert len(suffix.content) == 50

    bad = await client.get("/api/sets/s1/audio",
                           headers={"Range": "bytes=99999-"})
    assert bad.status_code == 416


async def test_missing_audio_reports_why(client):
    await client.web.library.save_set("s1", "My Set", RESULT,
        audio_path=str(client.web.MEDIA_DIR / "gone.mp3"), user_id=TEST_USER)
    response = await client.get("/api/sets/s1/audio")
    assert response.status_code == 410
    assert "tracklist is still here" in response.json()["detail"]


async def test_crate_round_trip(client):
    await client.web.library.save_set("s1", "My Set", RESULT, user_id=TEST_USER)
    key = RESULT["tracks"][0]["key"]

    assert (await client.post("/api/library/star", json={
        "key": key, "title": "Loose Lips", "artist": "Chris Stussy"}
    )).json()["starred"] is True
    assert len((await client.get("/api/library/crate")).json()) == 1

    assert (await client.post("/api/library/star", json={"key": key}
                              )).json()["starred"] is False
    assert (await client.get("/api/library/crate")).json() == []


async def test_starring_without_a_key_is_rejected(client):
    assert (await client.post("/api/library/star", json={"key": ""})
            ).status_code == 400


async def test_recurring_tracks_need_more_than_one_set(client):
    await client.web.library.save_set("s1", "Set One", RESULT, user_id=TEST_USER)
    assert (await client.get("/api/library/recurring")).json() == []

    await client.web.library.save_set("s2", "Set Two", RESULT, user_id=TEST_USER)
    recurring = (await client.get("/api/library/recurring")).json()
    assert len(recurring) == 1
    assert recurring[0]["set_count"] == 2
    assert recurring[0]["title"] == "Loose Lips"


async def test_library_search_filters(client):
    await client.web.library.save_set("s1", "My Set", RESULT, user_id=TEST_USER)

    assert len((await client.get("/api/library/search?q=stussy")).json()) == 1
    assert len((await client.get("/api/library/search?q=nobody")).json()) == 0
    assert len((await client.get("/api/library/search?camelot=8A")).json()) == 1
    assert len((await client.get("/api/library/search?camelot=1A")).json()) == 0
    assert len((await client.get(
        "/api/library/search?bpm_min=120&bpm_max=130")).json()) == 1
    assert len((await client.get("/api/library/search?bpm_min=140")).json()) == 0


async def test_acquisition_puts_stores_first(client):
    payload = (await client.get(
        "/api/acquire/sources?artist=Overmono&title=So+U+Kno")).json()
    assert payload["soulseek_configured"] is False
    names = [s["name"] for s in payload["sources"]]
    assert names[0] == "Bandcamp"
    assert "Soulseek" not in names, "P2P must stay hidden until configured"
    assert all(s["kind"] in ("store", "stream") for s in payload["sources"])


async def test_soulseek_reports_when_unconfigured(client):
    payload = (await client.get("/api/acquire/soulseek/status")).json()
    assert payload["configured"] is False
    assert "SLSKD_URL" in payload["hint"]


async def test_watch_validation(client):
    assert (await client.post("/api/watches", json={"url": "nope"})
            ).status_code == 400
    assert (await client.delete("/api/watches/missing")).status_code == 404


async def test_a_set_read_back_has_the_same_shape_as_a_fresh_one(client):
    """Regression: exports crashed on sets loaded from the library.

    `start_label` was computed in the pipeline but not rebuilt when reading a
    set out of SQLite, so every text-ish export of a stored set raised
    KeyError. The shapes must stay interchangeable.
    """
    await client.web.library.save_set("s1", "My Set", RESULT, user_id=TEST_USER)
    stored = (await client.get("/api/sets/s1")).json()["tracks"]

    for original, roundtripped in zip(RESULT["tracks"], stored):
        assert roundtripped["start_label"] == original["start_label"]
        missing = set(original) - set(roundtripped)
        assert not missing, f"fields lost on the way through the store: {missing}"


async def test_progress_streams_over_sse(client):
    """The stream must deliver a first frame immediately and close on its own."""
    task = client.web.tasks.create("t1", filename="Set")
    client.web.tasks.finish(task, status="completed", message="done", set_id="s1")

    async with client.stream("GET", "/api/tasks/t1/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = ""
        async for chunk in response.aiter_text():
            body += chunk
            if "event: end" in body:
                break

    assert '"status": "completed"' in body
    assert '"set_id": "s1"' in body


async def test_cancelling_a_finished_task_is_rejected(client):
    task = client.web.tasks.create("t1")
    client.web.tasks.finish(task, status="completed", message="done")
    assert (await client.post("/api/tasks/t1/cancel")).status_code == 400


async def test_interrupted_tasks_are_reported_as_such(client):
    """A restart mid-analysis must not look like a network failure."""
    task = client.web.tasks.create("t1", filename="Set")
    client.web.tasks.update(task, status="processing", progress=42,
                            message="Identifying...")
    client.web.tasks.persist(task)

    fresh = client.web.TaskManager(client.web.tasks.dir)
    assert fresh.mark_interrupted() == 1

    reloaded = fresh.get("t1")
    assert reloaded.status == "error"
    assert "restarted" in reloaded.error


async def test_completed_tasks_are_evicted_but_running_ones_are_not(client):
    manager = client.web.TaskManager(client.web.tasks.dir, max_tasks=3)
    running = manager.create("live")
    manager.update(running, status="processing")
    for i in range(6):
        done = manager.create(f"done-{i}")
        manager.finish(done, status="completed", message="ok")

    assert "live" in manager._tasks, "an in-flight task must never be evicted"
    assert len(manager._tasks) <= 4


async def test_reanalysing_replaces_the_set_in_place(client, monkeypatch):
    """An imported set is re-analysed under its own id, not beside itself.

    Without this, recovering the audio and waveform of a legacy import would
    leave two entries for the same mix — the stub and the real one.
    """
    import asyncio

    await client.web.library.save_set("legacy-abc", "Old Set", RESULT,
                                      source_kind="legacy", user_id=TEST_USER)

    captured = {}

    async def fake_run(task_id, url, set_id=None):
        captured["task_id"] = task_id
        captured["url"] = url
        captured["set_id"] = set_id

    monkeypatch.setattr(client.web, "run_url_analysis", fake_run)

    response = await client.post("/api/analyze/url", json={
        "url": "https://youtube.com/watch?v=abc", "replaces": "legacy-abc"})
    assert response.status_code == 200
    assert response.json()["replaces"] == "legacy-abc"

    await asyncio.sleep(0)  # let the background task start
    assert captured["set_id"] == "legacy-abc"


async def test_reanalysing_a_missing_set_is_rejected(client):
    response = await client.post("/api/analyze/url", json={
        "url": "https://youtube.com/watch?v=abc", "replaces": "not-a-set"})
    assert response.status_code == 404
    assert "no longer exists" in response.json()["detail"]


async def test_a_plain_analysis_still_gets_its_own_id(client, monkeypatch):
    import asyncio

    captured = {}

    async def fake_run(task_id, url, set_id=None):
        captured["set_id"] = set_id

    monkeypatch.setattr(client.web, "run_url_analysis", fake_run)
    await client.post("/api/analyze/url", json={"url": "https://x/y"})
    await asyncio.sleep(0)
    assert captured["set_id"] is None


async def test_failed_analysis_does_not_leave_its_audio_behind(client, monkeypatch):
    """Audio is kept for a set that succeeded, discarded for one that did not.

    Seven failed attempts at a single 69-minute set left 503 MB on the
    production disk before this: no set was written, so nothing referenced the
    files and the boot sweep would not touch them for the whole retention
    window.
    """
    audio = client.web.MEDIA_DIR / "doomed.mp3"
    audio.write_bytes(b"\x00" * 2048)

    async def exploding_run(self, path, on_progress=None):
        raise RuntimeError("analysis blew up")

    monkeypatch.setattr(client.web.Pipeline, "run", exploding_run)

    task = client.web.tasks.create("t-fail", filename="Doomed")
    await client.web.run_analysis("t-fail", audio, "Doomed")

    assert task.status == "error"
    assert not audio.exists(), "audio survived an analysis that produced no set"


async def test_successful_analysis_keeps_its_audio(client, monkeypatch):
    """The waveform player streams it, so a completed set must retain its file."""
    from src.core.pipeline import AnalysisResult

    audio = client.web.MEDIA_DIR / "keeper.mp3"
    audio.write_bytes(b"\x00" * 2048)

    async def fake_run(self, path, on_progress=None):
        return AnalysisResult(duration=10.0, tracks=[], waveform=[],
                              stats={"identified": 0})

    monkeypatch.setattr(client.web.Pipeline, "run", fake_run)

    client.web.tasks.create("t-ok", filename="Keeper", user_id=TEST_USER)
    await client.web.run_analysis("t-ok", audio, "Keeper")

    assert audio.exists(), "audio for a saved set was discarded"
    assert await client.web.library.get_set("t-ok", user_id=TEST_USER) is not None


async def test_discard_refuses_paths_outside_the_media_directories(client, tmp_path):
    """A bad path must never delete something that is not ours."""
    outsider = tmp_path / "important.txt"
    outsider.write_text("do not delete")

    client.web.discard_media(outsider)
    assert outsider.exists(), "discard_media deleted a file outside its roots"


async def test_an_existing_library_gains_new_columns(tmp_path):
    """A column added after release must reach production, not just fresh installs.

    SCHEMA uses CREATE TABLE IF NOT EXISTS, which does nothing to a database
    that already exists. Without a migration the column appears only on a clean
    install and every real deployment keeps the old shape — then every read of
    it raises.
    """
    import sqlite3

    from src.store.library import Library

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE sets (id TEXT PRIMARY KEY, title TEXT NOT NULL,"
        " source_url TEXT DEFAULT '', source_kind TEXT DEFAULT 'upload',"
        " uploader TEXT DEFAULT '', audio_path TEXT DEFAULT '',"
        " quality TEXT DEFAULT '', duration REAL DEFAULT 0,"
        " waveform TEXT DEFAULT '[]', stats TEXT DEFAULT '{}',"
        " created_at TEXT NOT NULL);"
        "CREATE TABLE tracks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " set_id TEXT NOT NULL, position INTEGER NOT NULL, start REAL NOT NULL,"
        " end REAL NOT NULL, identified INTEGER NOT NULL DEFAULT 0,"
        " track_key TEXT DEFAULT '', title TEXT DEFAULT '', artist TEXT DEFAULT '',"
        " album TEXT DEFAULT '', label TEXT DEFAULT '', year TEXT DEFAULT '',"
        " genre TEXT DEFAULT '', isrc TEXT DEFAULT '', url TEXT DEFAULT '',"
        " cover_url TEXT DEFAULT '', bpm REAL, camelot TEXT, musical_key TEXT,"
        " confidence REAL DEFAULT 0);"
    )
    conn.execute(
        "INSERT INTO sets VALUES ('s1','Old Set','','upload','','','',"
        "60,'[]','{}','2026-01-01')")
    conn.execute(
        "INSERT INTO tracks (set_id,position,start,end,identified,track_key,"
        "title,artist) VALUES ('s1',1,0,60,1,'a::b','Track','Artist')")
    conn.commit()
    conn.close()

    library = Library(path)
    columns = {r[1] for r in sqlite3.connect(path).execute(
        "PRAGMA table_info(tracks)")}
    assert "strength" in columns, "migration did not run"

    stored = await library.get_set("s1", user_id=TEST_USER)
    assert stored["title"] == "Old Set", "existing data was lost"
    assert stored["tracks"][0]["strength"] == ""


async def test_strength_survives_a_round_trip(client):
    payload = {**RESULT, "tracks": [{**RESULT["tracks"][0], "strength": "strong"}]}
    await client.web.library.save_set("s1", "Set", payload, user_id=TEST_USER)
    stored = (await client.get("/api/sets/s1")).json()
    assert stored["tracks"][0]["strength"] == "strong"


async def test_docs_are_not_swallowed_by_the_spa_catch_all(client):
    """/docs must reach the Docusaurus build, not the app shell.

    The catch-all that serves the frontend answers *every* unmatched path with
    index.html, so a docs route that is not mounted ahead of it comes back
    200 — with the wrong page. That failure is invisible to a status check,
    which is exactly how it got shipped the first time.
    """
    if not client.web.DOCS_DIST.exists():
        pytest.skip("docs not built (`make docs`)")

    response = await client.get("/docs/")
    assert response.status_code == 200
    body = response.text
    assert "docusaurus" in body.lower(), "served the app shell instead of the docs"

    # Its assets live under the same prefix; the app's own /assets mount must
    # not shadow them.
    response = await client.get("/docs/assets/css/", follow_redirects=True)
    assert response.status_code != 500


async def test_openapi_page_moved_out_of_the_way_of_the_docs(client):
    """Swagger lives under /api now, because /docs is the written manual."""
    assert (await client.get("/api/docs")).status_code == 200
    assert (await client.get("/api/openapi.json")).status_code == 200


def test_a_stale_extractor_failure_says_so():
    """"Login required" on a public post is usually an out-of-date extractor.

    Worth naming, because the message the site returns sends you the wrong
    way: Instagram answers "login required" for a post a logged-out browser
    opens fine, and the obvious next move — hunting for cookies — cannot work.
    The actual fix had shipped upstream six weeks before we hit it.
    """
    from src.sources import download as dl

    age = dl._installed_age_days()
    message = dl._clean_ytdlp_error(
        "ERROR: [Instagram] x: Requested content is not available, "
        "rate-limit reached or login required.")

    if age is not None and age >= dl.STALE_AFTER_DAYS:
        assert "days old" in message
    else:
        assert "days old" not in message, (
            "a fresh install must not blame itself")


def test_a_real_failure_is_not_blamed_on_staleness():
    """A private video is private however new the extractor is."""
    from src.sources import download as dl

    assert dl._clean_ytdlp_error(
        "ERROR: [youtube] abc: Private video") == "Private video"
