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


# ── F1: the inbox ──────────────────────────────────────────────────────────

async def _incoming(client):
    response = await client.get("/api/shares/incoming")
    assert response.status_code == 200, response.text
    return response.json()


async def test_an_addressed_share_waits_in_the_recipient_s_inbox(client):
    """The production case: the mail went out, the person never saw it."""
    await _seed_set(client)
    token = await _share(client, email=BOB_EMAIL)
    bob = await _session(client, BOB, BOB_EMAIL)

    async with _As(client, bob):
        inbox = await _incoming(client)

    assert [s["token"] for s in inbox] == [token]
    assert inbox[0]["title"] == "A Night Out"
    assert inbox[0]["track_count"] == 1


async def test_a_share_sent_before_the_account_existed_shows_up(client):
    """Matched on the email at read time, so signing up later still finds it."""
    await _seed_set(client)
    token = await _share(client, email=BOB_EMAIL)

    bob = await _session(client, BOB, BOB_EMAIL)     # the account comes after
    async with _As(client, bob):
        assert [s["token"] for s in await _incoming(client)] == [token]


async def test_the_address_is_matched_whatever_its_case(client):
    await _seed_set(client)
    await _share(client, email="Bob@Example.com")
    bob = await _session(client, BOB, BOB_EMAIL)
    async with _As(client, bob):
        assert len(await _incoming(client)) == 1


async def test_claimed_and_dismissed_shares_leave_the_inbox(client):
    await _seed_set(client)
    await _seed_set(client, set_id="s2")
    taken = await _share(client, email=BOB_EMAIL)
    declined = await _share(client, "s2", email=BOB_EMAIL)
    bob = await _session(client, BOB, BOB_EMAIL)

    async with _As(client, bob):
        assert (await client.post(f"/api/shares/{taken}/claim")).status_code == 200
        response = await client.post(f"/api/shares/{declined}/dismiss")
        assert response.status_code == 200
        assert await _incoming(client) == []


async def test_dismissing_is_not_revoking(client):
    """Declining hides it from the inbox; the link itself still works."""
    await _seed_set(client)
    token = await _share(client, email=BOB_EMAIL)
    bob = await _session(client, BOB, BOB_EMAIL)

    async with _As(client, bob):
        await client.post(f"/api/shares/{token}/dismiss")
        claimed = await client.post(f"/api/shares/{token}/claim")
    assert claimed.status_code == 200


async def test_nobody_else_sees_or_dismisses_someone_s_inbox(client):
    await _seed_set(client)
    token = await _share(client, email=BOB_EMAIL)
    eve = await _session(client, "eve-account", "eve@example.com")

    async with _As(client, eve):
        assert await _incoming(client) == []
        response = await client.post(f"/api/shares/{token}/dismiss")
    assert response.status_code == 404


async def test_a_link_share_never_lands_in_an_inbox(client):
    await _seed_set(client)
    await _share(client)
    bob = await _session(client, BOB, BOB_EMAIL)
    async with _As(client, bob):
        assert await _incoming(client) == []


async def test_the_inbox_upgrades_an_existing_database(tmp_path):
    """The new columns must reach a library created before them."""
    import sqlite3

    from src.store.library import Library

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE shares (token TEXT PRIMARY KEY, set_id TEXT"
                     " NOT NULL, from_user TEXT NOT NULL, from_name TEXT"
                     " DEFAULT '', to_email TEXT DEFAULT '', created_at TEXT"
                     " NOT NULL, claimed_at TEXT, claimed_by TEXT)")
    Library(db)
    Library(db)                                       # and twice is harmless
    with sqlite3.connect(db) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(shares)")}
    assert {"dismissed_at", "revoked_at"} <= columns
