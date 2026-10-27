"""Sharing a set between two accounts (RFC #123).

A share stays a copy: the recipient owns what they get. What these tests pin
down is everything around the copy that used to fail quietly — the copy
arriving mute, an addressed share invisible unless the email was read, and a
sender with no way to see or take back a link.

Two real accounts, real cookies, real files on disk. Hard links are the point
of the audio half, so they are checked with inodes rather than mocked.
"""
import os
from pathlib import Path

import pytest

from tests.conftest import TEST_EMAIL, TEST_USER, _seed_account

pytestmark = pytest.mark.anyio

BOB = "bob-account"
BOB_EMAIL = "bob@example.com"


def _set_payload():
    return {"duration": 60.0, "waveform": [1, 2], "stats": {},
            "tracks": [{"index": 1, "start": 0, "end": 60, "identified": True,
                        "key": "a::x", "title": "X", "artist": "A"}]}


async def _seed_set(client, set_id="s1", with_audio=True) -> Path:
    web = client.web
    audio = web.MEDIA_DIR / f"{set_id}.m4a"
    if with_audio:
        audio.write_bytes(b"\x00" * 4096)
    await web.library.save_set(set_id, "A Night Out", _set_payload(),
                               user_id=TEST_USER, audio_path=str(audio))
    return audio


async def _session(client, user_id, email) -> str:
    await _seed_account(client.web.accounts, user_id, email)
    return await client.web.accounts.create_session(user_id, "pytest")


class _As:
    """Run requests as another account, then switch back."""

    def __init__(self, client, token):
        self.client, self.token = client, token

    async def __aenter__(self):
        import src.auth as auth_mod
        self.name = auth_mod.COOKIE_NAME
        self.previous = self.client.cookies.get(self.name)
        self.client.cookies.set(self.name, self.token)
        return self.client

    async def __aexit__(self, *_):
        self.client.cookies.set(self.name, self.previous)


async def _share(client, set_id="s1", email=None) -> str:
    response = await client.post(f"/api/sets/{set_id}/share",
                                  json={"email": email} if email else {})
    assert response.status_code == 200, response.text
    return response.json()["token"]


# ── F2: the copy plays ─────────────────────────────────────────────────────

async def test_a_claimed_copy_plays(client):
    """The waveform without the sound is half a set."""
    await _seed_set(client)
    token = await _share(client)
    bob = await _session(client, BOB, BOB_EMAIL)

    async with _As(client, bob):
        claimed = (await client.post(f"/api/shares/{token}/claim")).json()
        assert claimed["audio"] is True
        response = await client.get(f"/api/sets/{claimed['set_id']}/audio",
                                    headers={"Range": "bytes=0-99"})

    assert response.status_code == 206
    assert len(response.content) == 100


async def test_the_copy_costs_no_extra_bytes(client):
    """A hard link, like the dedupe of re-analysed sets: same inode."""
    source = await _seed_set(client)
    token = await _share(client)
    bob = await _session(client, BOB, BOB_EMAIL)

    async with _As(client, bob):
        claimed = (await client.post(f"/api/shares/{token}/claim")).json()

    copy = await client.web.library.get_set(claimed["set_id"], user_id=BOB)
    copied = Path(copy["audio_path"])
    assert copied != source, "the copy must have its own path to delete"
    assert os.stat(copied).st_ino == os.stat(source).st_ino


async def test_deleting_the_source_keeps_the_copy_playable(client):
    await _seed_set(client)
    token = await _share(client)
    bob = await _session(client, BOB, BOB_EMAIL)
    async with _As(client, bob):
        claimed = (await client.post(f"/api/shares/{token}/claim")).json()

    assert (await client.delete("/api/sets/s1")).status_code == 200

    async with _As(client, bob):
        response = await client.get(f"/api/sets/{claimed['set_id']}/audio")
    assert response.status_code == 200


async def test_deleting_the_copy_keeps_the_source_playable(client):
    await _seed_set(client)
    token = await _share(client)
    bob = await _session(client, BOB, BOB_EMAIL)
    async with _As(client, bob):
        claimed = (await client.post(f"/api/shares/{token}/claim")).json()
        assert (await client.delete(
            f"/api/sets/{claimed['set_id']}")).status_code == 200

    assert (await client.get("/api/sets/s1/audio")).status_code == 200


async def test_a_copy_is_still_made_when_the_audio_cannot_follow(client):
    """The tracklist is the set; missing audio is said, not fatal."""
    await _seed_set(client, with_audio=False)
    token = await _share(client)
    bob = await _session(client, BOB, BOB_EMAIL)

    async with _As(client, bob):
        claimed = (await client.post(f"/api/shares/{token}/claim")).json()

    assert claimed["audio"] is False
    copy = await client.web.library.get_set(claimed["set_id"], user_id=BOB)
    assert copy["title"] == "A Night Out"
    assert not copy["audio_path"]
