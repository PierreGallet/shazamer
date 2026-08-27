"""HTTP surface: validation, the library, exports, and the hardening fixes."""
import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

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


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """A fresh app instance per test, with all state under tmp_path."""
    import importlib

    for name in ("UPLOAD_DIR", "MEDIA_DIR", "DATA_DIR", "TMP_DIR"):
        (tmp_path / name.lower()).mkdir(exist_ok=True)

    monkeypatch.setenv("SLSKD_URL", "")
    import src.web as web
    importlib.reload(web)

    web.UPLOAD_DIR = tmp_path / "uploads"
    web.MEDIA_DIR = tmp_path / "media"
    for directory in (web.UPLOAD_DIR, web.MEDIA_DIR):
        directory.mkdir(exist_ok=True)
    web.library = web.Library(tmp_path / "library.db")
    web.tasks = web.TaskManager(tmp_path / "tasks")

    transport = ASGITransport(app=web.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.web = web  # type: ignore[attr-defined]
        yield c


async def test_health(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_head_on_root_is_not_a_405(client):
    """Uptime monitors send HEAD; a 405 there reads as 'site down'."""
    assert (await client.head("/")).status_code == 200


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
                                      uploader="Someone", quality="opus 160")

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
                                      audio_path="/srv/secret/audio.mp3")
    assert "audio_path" not in (await client.get("/api/sets")).text
    assert "audio_path" not in (await client.get("/api/sets/s1")).text


@pytest.mark.parametrize("fmt,marker", [
    ("json", '"tracks"'), ("txt", "Loose Lips"), ("csv", "#,start,artist"),
    ("m3u", "#EXTM3U"), ("rekordbox", "DJ_PLAYLISTS"),
])
async def test_exports(client, fmt, marker):
    await client.web.library.save_set("s1", "My Set", RESULT)
    response = await client.get(f"/api/sets/s1/export/{fmt}")
    assert response.status_code == 200
    assert marker in response.text
    assert "attachment" in response.headers["content-disposition"]


async def test_unknown_export_format_is_rejected(client):
    await client.web.library.save_set("s1", "My Set", RESULT)
    response = await client.get("/api/sets/s1/export/wav")
    assert response.status_code == 400


async def test_audio_requires_a_kept_file(client):
    await client.web.library.save_set("s1", "My Set", RESULT, audio_path="")
    assert (await client.get("/api/sets/s1/audio")).status_code == 404


async def test_audio_outside_the_media_directory_is_refused(client, tmp_path):
    """A path escaping the media roots must never be served."""
    outsider = tmp_path / "etc-passwd"
    outsider.write_bytes(b"root:x:0:0")
    await client.web.library.save_set("s1", "My Set", RESULT,
                                      audio_path=str(outsider))
    assert (await client.get("/api/sets/s1/audio")).status_code == 400


async def test_audio_range_requests(client):
    audio = client.web.MEDIA_DIR / "set.mp3"
    audio.write_bytes(bytes(range(256)) * 40)          # 10240 bytes
    await client.web.library.save_set("s1", "My Set", RESULT,
                                      audio_path=str(audio))

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
    await client.web.library.save_set(
        "s1", "My Set", RESULT,
        audio_path=str(client.web.MEDIA_DIR / "gone.mp3"))
    response = await client.get("/api/sets/s1/audio")
    assert response.status_code == 410
    assert "tracklist is still here" in response.json()["detail"]


async def test_crate_round_trip(client):
    await client.web.library.save_set("s1", "My Set", RESULT)
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
    await client.web.library.save_set("s1", "Set One", RESULT)
    assert (await client.get("/api/library/recurring")).json() == []

    await client.web.library.save_set("s2", "Set Two", RESULT)
    recurring = (await client.get("/api/library/recurring")).json()
    assert len(recurring) == 1
    assert recurring[0]["set_count"] == 2
    assert recurring[0]["title"] == "Loose Lips"


async def test_library_search_filters(client):
    await client.web.library.save_set("s1", "My Set", RESULT)

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
    await client.web.library.save_set("s1", "My Set", RESULT)
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
