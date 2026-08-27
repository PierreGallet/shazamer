"""Scheduled checking of followed channels.

What matters is restraint. A channel with years of back catalogue must not
turn into forty queued analyses, and a channel that has gone private must not
stop the others being checked.
"""
from typing import Any, Dict, List

import pytest

from src.jobs.watches import check_watches
from src.store.library import Library

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake_channel(monkeypatch):
    """Stands in for yt-dlp, and lets a channel be made to fail."""
    import src.sources.download as dl

    state: Dict[str, Any] = {"entries": {}, "broken": set()}

    async def list_channel(url, limit=20):
        if url in state["broken"]:
            raise dl.DownloadError("This channel is private")
        return state["entries"].get(url, [])[:limit]

    monkeypatch.setattr(dl, "list_channel", list_channel)
    return state


def _entry(n):
    return {"id": f"vid{n}", "title": f"Mix {n}", "url": f"https://x/{n}",
            "duration": 3600, "uploader": "Someone", "thumbnail": ""}


class Recorder:
    def __init__(self):
        self.urls: List[str] = []

    async def __call__(self, url: str) -> bool:
        self.urls.append(url)
        return True


async def test_the_first_check_records_without_analysing(tmp_path, fake_channel):
    """A back catalogue is not news.

    Following a channel with two hundred old mixes must not start two hundred
    analyses; only what appears afterwards is new.
    """
    library = Library(tmp_path / "lib.db")
    await library.add_watch("w1", "https://x/chan", "A Channel")
    fake_channel["entries"]["https://x/chan"] = [_entry(i) for i in range(8)]

    enqueue = Recorder()
    report = await check_watches(library, enqueue)

    assert enqueue.urls == [], "the back catalogue was queued"
    assert report["queued"] == 0
    assert len(await library.watch_seen_ids("w1")) == 8, "nothing was recorded"


async def test_uploads_after_the_first_check_are_queued(tmp_path, fake_channel):
    library = Library(tmp_path / "lib.db")
    await library.add_watch("w1", "https://x/chan", "A Channel")
    fake_channel["entries"]["https://x/chan"] = [_entry(1), _entry(2)]

    enqueue = Recorder()
    await check_watches(library, enqueue)          # first look, records only

    fake_channel["entries"]["https://x/chan"] = [_entry(3), _entry(1), _entry(2)]
    report = await check_watches(library, enqueue)

    assert enqueue.urls == ["https://x/3"]
    assert report["found"] == 1 and report["queued"] == 1


async def test_the_same_upload_is_not_queued_twice(tmp_path, fake_channel):
    library = Library(tmp_path / "lib.db")
    await library.add_watch("w1", "https://x/chan", "A Channel")
    fake_channel["entries"]["https://x/chan"] = [_entry(1)]

    enqueue = Recorder()
    await check_watches(library, enqueue)
    fake_channel["entries"]["https://x/chan"] = [_entry(2), _entry(1)]
    await check_watches(library, enqueue)
    await check_watches(library, enqueue)

    assert enqueue.urls == ["https://x/2"], "an upload was analysed twice"


async def test_a_burst_is_capped(tmp_path, fake_channel):
    """Each analysis takes an hour; a dozen at once blocks the queue all day."""
    library = Library(tmp_path / "lib.db")
    await library.add_watch("w1", "https://x/chan", "A Channel")
    fake_channel["entries"]["https://x/chan"] = [_entry(0)]

    enqueue = Recorder()
    await check_watches(library, enqueue)

    fake_channel["entries"]["https://x/chan"] = [_entry(i) for i in range(12)]
    report = await check_watches(library, enqueue, max_per_round=3)

    assert len(enqueue.urls) == 3
    assert report["found"] == 11, "the uncounted ones were hidden, not deferred"


async def test_a_broken_channel_does_not_stop_the_others(tmp_path, fake_channel):
    """A URL can rot, go private, or simply be down for the afternoon."""
    library = Library(tmp_path / "lib.db")
    await library.add_watch("w1", "https://x/dead", "Gone Private")
    await library.add_watch("w2", "https://x/alive", "Still Here")
    fake_channel["broken"].add("https://x/dead")
    fake_channel["entries"]["https://x/alive"] = [_entry(1)]

    enqueue = Recorder()
    await check_watches(library, enqueue)                 # records w2's entry
    fake_channel["entries"]["https://x/alive"] = [_entry(2), _entry(1)]
    report = await check_watches(library, enqueue)

    assert enqueue.urls == ["https://x/2"]
    assert report["watches"] == 2


async def test_entries_are_recorded_even_when_queueing_fails(tmp_path,
                                                             fake_channel):
    """Otherwise a failure makes the same uploads "new" again every round."""
    library = Library(tmp_path / "lib.db")
    await library.add_watch("w1", "https://x/chan", "A Channel")
    fake_channel["entries"]["https://x/chan"] = [_entry(1)]
    await check_watches(library, Recorder())

    fake_channel["entries"]["https://x/chan"] = [_entry(2), _entry(1)]

    async def refuses(url):
        return False

    await check_watches(library, refuses)
    assert "vid2" in await library.watch_seen_ids("w1")


async def test_nothing_followed_is_not_an_error(tmp_path, fake_channel):
    library = Library(tmp_path / "lib.db")
    report = await check_watches(library, Recorder())
    assert report == {"watches": 0, "found": 0, "queued": 0}
