"""Reusing an analysis of the same source (RFC #130).

A 2-hour mix costs about 47 minutes of analysis. When the same media was
already analysed — by you or by anyone — the requester gets a copy at once:
their own row, the audio hard-linked, and nothing that names whose analysis
it was. Identity is yt-dlp's extractor and media id, not the URL string, so
two ways of writing one link are the same source and two slides of one
Instagram post are not.

The download and the pipeline are faked: what is under test is the decision
to skip them, so each test counts how often they ran.
"""
import os
from pathlib import Path

import pytest

import src.sources.download as dl
from tests.conftest import TEST_USER, _seed_account

pytestmark = pytest.mark.anyio

BOB, BOB_EMAIL = "bob-account", "bob@example.com"


class _Result:
    def __init__(self):
        self.stats = {"identified": 1}

    def to_dict(self):
        return {"duration": 60.0, "waveform": [1, 2], "stats": self.stats,
                "tracks": [{"index": 1, "start": 0, "end": 60,
                            "identified": True, "key": "a::x",
                            "title": "X", "artist": "A"}]}


@pytest.fixture
def world(client, monkeypatch):
    """Fake yt-dlp and pipeline, with counters."""
    web = client.web
    state = {"downloads": 0, "analyses": 0, "probes": 0,
             "keys": {}, "probe_fails": False}

    async def media_key(url):
        state["probes"] += 1
        if state["probe_fails"]:
            return None
        return state["keys"].get(url)

    async def download_audio(url, dest_dir, stem, on_progress=None, **_):
        state["downloads"] += 1
        path = Path(dest_dir) / f"{stem}.m4a"
        path.write_bytes(b"\x00" * 2048)
        extractor, _, media_id = (state["keys"].get(url) or "Youtube:zzz").partition(":")
        return dl.DownloadedMedia(path=path, title="A Mix", duration=60.0,
                                  extractor=extractor, webpage_url=url,
                                  media_id=media_id)

    class _Pipeline:
        async def run(self, path, on_progress=None):
            state["analyses"] += 1
            return _Result()

    monkeypatch.setattr(dl, "media_key", media_key)
    monkeypatch.setattr(dl, "download_audio", download_audio)
    monkeypatch.setattr(web, "make_pipeline", lambda on_work=None: _Pipeline())

    async def no_enqueue(*a, **k):
        return False
    monkeypatch.setattr(web.jobs, "enqueue", no_enqueue)
    return state


async def _analyse(client, url, user_id=TEST_USER, *, replaces=None,
                   force=False):
    """Run one URL analysis to the end, as the worker would."""
    web = client.web
    import uuid
    task_id = str(uuid.uuid4())
    task = web.tasks.create(task_id, filename="Resolving...", source_url=url,
                            user_id=user_id)
    await web.run_url_analysis(task_id, url, set_id=replaces, force=force)
    return web.tasks.get(task_id)


async def _sets(client, user_id):
    return await client.web.library.list_sets(user_id=user_id)


# ── S1: a source key on every set ──────────────────────────────────────────

async def test_an_analysis_records_its_source_key(client, world):
    world["keys"]["https://youtu.be/abc"] = "Youtube:abc"
    task = await _analyse(client, "https://youtu.be/abc")

    assert task.status == "completed"
    stored = await client.web.library.get_set(task.set_id, user_id=TEST_USER)
    assert stored["source_key"] == "Youtube:abc"


# ── S2: look before analysing ─────────────────────────────────────────────

async def test_another_way_of_writing_the_link_reuses_it(client, world):
    world["keys"]["https://youtu.be/abc"] = "Youtube:abc"
    world["keys"]["https://www.youtube.com/watch?v=abc&t=60"] = "Youtube:abc"
    await _analyse(client, "https://youtu.be/abc")
    await _seed_account(client.web.accounts, BOB, BOB_EMAIL)

    task = await _analyse(client, "https://www.youtube.com/watch?v=abc&t=60",
                          user_id=BOB)

    assert task.status == "completed"
    assert "reused" in task.message.lower()
    assert world["analyses"] == 1, "the pipeline ran again"
    assert world["downloads"] == 1, "the audio was downloaded again"


async def test_a_reused_copy_belongs_to_the_requester_and_names_nobody(
        client, world):
    world["keys"]["https://youtu.be/abc"] = "Youtube:abc"
    first = await _analyse(client, "https://youtu.be/abc")
    await _seed_account(client.web.accounts, BOB, BOB_EMAIL)

    task = await _analyse(client, "https://youtu.be/abc", user_id=BOB)

    assert task.set_id != first.set_id
    copy = await client.web.library.get_set(task.set_id, user_id=BOB)
    assert copy["title"] == "A Mix" and len(copy["tracks"]) == 1
    assert copy["shared_by"] == "", "the copy names whose analysis it was"
    assert copy["reused"] is True


async def test_the_reused_copy_plays_without_extra_bytes(client, world):
    world["keys"]["https://youtu.be/abc"] = "Youtube:abc"
    first = await _analyse(client, "https://youtu.be/abc")
    await _seed_account(client.web.accounts, BOB, BOB_EMAIL)
    task = await _analyse(client, "https://youtu.be/abc", user_id=BOB)

    origin = await client.web.library.get_set(first.set_id, user_id=TEST_USER)
    copy = await client.web.library.get_set(task.set_id, user_id=BOB)
    assert copy["audio_path"] and copy["audio_path"] != origin["audio_path"]
    assert os.stat(copy["audio_path"]).st_ino == os.stat(origin["audio_path"]).st_ino


async def test_your_own_analysis_is_returned_not_copied(client, world):
    world["keys"]["https://youtu.be/abc"] = "Youtube:abc"
    first = await _analyse(client, "https://youtu.be/abc")
    again = await _analyse(client, "https://youtu.be/abc")

    assert again.set_id == first.set_id
    assert len(await _sets(client, TEST_USER)) == 1
    assert world["analyses"] == 1


async def test_two_slides_of_one_post_are_two_sources(client, world):
    post = "https://www.instagram.com/p/DeMHG9qkb5V/"
    world["keys"][f"{post}?img_index=3"] = "Instagram:SLIDE3"
    world["keys"][f"{post}?img_index=16"] = "Instagram:SLIDE16"
    await _analyse(client, f"{post}?img_index=3")
    await _analyse(client, f"{post}?img_index=16")
    assert world["analyses"] == 2


async def test_force_and_replace_always_analyse(client, world):
    world["keys"]["https://youtu.be/abc"] = "Youtube:abc"
    first = await _analyse(client, "https://youtu.be/abc")

    await _analyse(client, "https://youtu.be/abc", force=True)
    await _analyse(client, "https://youtu.be/abc", replaces=first.set_id)
    assert world["analyses"] == 3


async def test_a_failed_probe_falls_back_to_analysing(client, world):
    world["probe_fails"] = True
    task = await _analyse(client, "https://youtu.be/abc")
    assert task.status == "completed"
    assert world["analyses"] == 1


async def test_a_copy_is_never_an_origin(client, world):
    """Reused copies and share copies are not reused from: only originals."""
    world["keys"]["https://youtu.be/abc"] = "Youtube:abc"
    first = await _analyse(client, "https://youtu.be/abc")
    await _seed_account(client.web.accounts, BOB, BOB_EMAIL)
    await _analyse(client, "https://youtu.be/abc", user_id=BOB)

    origin = await client.web.library.find_by_source_key("Youtube:abc")
    assert origin == first.set_id


# ── Q-B: the origin's audio is gone ──────────────────────────────────────

async def test_missing_origin_audio_downloads_audio_only(client, world):
    world["keys"]["https://youtu.be/abc"] = "Youtube:abc"
    first = await _analyse(client, "https://youtu.be/abc")
    origin = await client.web.library.get_set(first.set_id, user_id=TEST_USER)
    Path(origin["audio_path"]).unlink()            # swept for disk space
    await _seed_account(client.web.accounts, BOB, BOB_EMAIL)

    task = await _analyse(client, "https://youtu.be/abc", user_id=BOB)

    assert world["analyses"] == 1, "re-analysed instead of fetching audio"
    assert world["downloads"] == 2
    copy = await client.web.library.get_set(task.set_id, user_id=BOB)
    assert copy["audio_path"] and Path(copy["audio_path"]).exists()


async def test_a_task_persisted_before_force_still_resumes(client, monkeypatch):
    """Inline dispatch used to unpack exactly three arguments."""
    web = client.web
    seen = {}

    async def fake(task_id, url, set_id=None, force=False):
        seen.update(task_id=task_id, set_id=set_id, force=force)

    monkeypatch.setattr(web, "run_url_analysis", fake)
    await web._run_inline(fake, "t-old", "https://youtu.be/abc", None)
    assert seen == {"task_id": "t-old", "set_id": None, "force": False}
    await web._run_inline(fake, "t-new", "https://youtu.be/abc", None, True)
    assert seen["force"] is True


async def test_a_reused_copy_says_when_it_was_analysed(client, world):
    world["keys"]["https://youtu.be/abc"] = "Youtube:abc"
    first = await _analyse(client, "https://youtu.be/abc")
    await _seed_account(client.web.accounts, BOB, BOB_EMAIL)
    task = await _analyse(client, "https://youtu.be/abc", user_id=BOB)

    origin = await client.web.library.get_set(first.set_id, user_id=TEST_USER)
    copy = await client.web.library.get_set(task.set_id, user_id=BOB)
    assert copy["analysed_at"] == origin["created_at"]
    assert origin["analysed_at"] == origin["created_at"]


async def test_re_analysing_a_reused_copy_makes_it_an_original(client, world):
    """The Re-analyse button posts the set's own URL with `replaces`."""
    world["keys"]["https://youtu.be/abc"] = "Youtube:abc"
    await _analyse(client, "https://youtu.be/abc")
    await _seed_account(client.web.accounts, BOB, BOB_EMAIL)
    reused = await _analyse(client, "https://youtu.be/abc", user_id=BOB)

    again = await _analyse(client, "https://youtu.be/abc", user_id=BOB,
                           replaces=reused.set_id)

    assert world["analyses"] == 2
    redone = await client.web.library.get_set(again.set_id, user_id=BOB)
    assert again.set_id == reused.set_id and redone["reused"] is False
