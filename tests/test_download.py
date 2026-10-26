"""Downloading from a URL: which media yt-dlp is asked for.

An Instagram post can be a carousel mixing photos and videos. yt-dlp treats
it as a playlist whatever `--no-playlist` says, ignores the `img_index` the
browser put in the link, and fails the whole command on the first photo —
so a post holding perfectly good videos came back as "Download failed".
"""
import asyncio
import json
from pathlib import Path
from typing import List

import pytest

import src.sources.download as dl

pytestmark = pytest.mark.anyio

POST = "https://www.instagram.com/p/DeMHG9qkb5V/"


class FakeYtdlp:
    """Stands in for the yt-dlp process: records the command, plays a result."""

    def __init__(self, returncode=0, stderr="", writes=True):
        self.returncode = returncode
        self.stderr = stderr
        self.writes = writes
        self.cmd: List[str] = []

    async def __call__(self, *cmd, **_):
        self.cmd = list(cmd)
        if self.writes:
            template = cmd[cmd.index("-o") + 1]
            Path(template.replace("%(ext)s", "m4a")).write_bytes(b"audio")
            Path(template.replace("%(ext)s", "info.json")).write_text(
                json.dumps({"title": "Video by someone", "acodec": "mp4a"}))
        return _Process(self.returncode, self.stderr)


class _Process:
    def __init__(self, returncode, stderr):
        self.returncode = returncode
        self.stdout = _Stream("")
        self.stderr = _Stream(stderr)

    async def wait(self):
        return self.returncode


class _Stream:
    def __init__(self, text):
        self._lines = [l.encode() + b"\n" for l in text.splitlines()]

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


@pytest.fixture
def ytdlp(monkeypatch):
    def install(**kwargs):
        fake = FakeYtdlp(**kwargs)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
        return fake
    return install


def _option(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


async def test_the_slide_in_the_link_is_the_one_downloaded(tmp_path, ytdlp):
    """`img_index` is the slide you were looking at when you copied the link."""
    fake = ytdlp()

    await dl.download_audio(f"{POST}?hl=fr&img_index=16", tmp_path, "t1")

    assert _option(fake.cmd, "--playlist-items") == "16"


async def test_a_post_without_a_slide_takes_its_first_video(tmp_path, ytdlp):
    """Photos are skipped instead of failing the download."""
    # 101 is yt-dlp saying --max-downloads was reached: the success case here.
    fake = ytdlp(returncode=101)

    media = await dl.download_audio(POST, tmp_path, "t2")

    assert media.path.suffix == ".m4a"
    assert "--ignore-no-formats-error" in fake.cmd
    assert _option(fake.cmd, "--match-filter") == "formats"
    assert _option(fake.cmd, "--max-downloads") == "1"
    assert "--playlist-items" not in fake.cmd


async def test_a_photo_slide_says_it_is_a_photo(tmp_path, ytdlp):
    """Not "please report this issue", and not "yt-dlp is out of date"."""
    ytdlp(returncode=1, writes=False, stderr=(
        "ERROR: [Instagram] DeMG18jxPpR: No video formats found!; please "
        "report this issue on https://github.com/yt-dlp/yt-dlp/issues"))

    with pytest.raises(dl.DownloadError) as caught:
        await dl.download_audio(f"{POST}?img_index=3", tmp_path, "t3")

    message = str(caught.value).lower()
    assert "slide 3" in message and "photo" in message
    assert "days old" not in message


async def test_a_post_of_only_photos_says_so(tmp_path, ytdlp):
    ytdlp(returncode=0, writes=False)

    with pytest.raises(dl.DownloadError) as caught:
        await dl.download_audio(POST, tmp_path, "t4")

    assert "only photos" in str(caught.value)


async def test_the_post_metadata_does_not_overwrite_the_video_s(tmp_path,
                                                               ytdlp):
    """The carousel's own info.json would land on the same path and win."""
    fake = ytdlp()

    await dl.download_audio(f"{POST}?img_index=2", tmp_path, "t5")

    assert "--no-write-playlist-metafiles" in fake.cmd


async def test_other_sites_are_left_alone(tmp_path, ytdlp):
    fake = ytdlp()

    await dl.download_audio("https://www.youtube.com/watch?v=abc&img_index=4",
                            tmp_path, "t6")

    assert "--playlist-items" not in fake.cmd
    assert "--max-downloads" not in fake.cmd


async def test_a_failed_instagram_video_keeps_its_real_reason(tmp_path, ytdlp):
    """Only the no-formats failure is a photo; anything else is passed on."""
    ytdlp(returncode=1, writes=False,
          stderr="ERROR: [Instagram] x: This content is private")

    with pytest.raises(dl.DownloadError) as caught:
        await dl.download_audio(f"{POST}?img_index=2", tmp_path, "t7")

    assert str(caught.value) == "This content is private"
