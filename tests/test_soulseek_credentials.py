"""Soulseek credentials a user saves in Profile (RFC #137, S1).

The password is the one secret here that people reuse elsewhere, so it is
never stored in clear and never handed back: a leaked accounts.db, or a
backup of it, must not give it away.
"""
import os
import sqlite3
import stat

import pytest

from src.store.accounts import Accounts
from src.store.soulseek_credentials import SoulseekCredentials
from tests.conftest import _seed_account

pytestmark = pytest.mark.anyio


@pytest.fixture
async def store(tmp_path):
    # The same file as the accounts, as in production: rows belong to users.
    accounts = Accounts(tmp_path / "accounts.db")
    await _seed_account(accounts, "alice", "alice@example.com")
    return SoulseekCredentials(tmp_path / "accounts.db",
                               key_path=tmp_path / "soulseek.key")


async def test_rows_go_with_their_user(store, tmp_path):
    await store.save("alice", "dj_alice", "pw")
    conn = sqlite3.connect(tmp_path / "accounts.db")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM users WHERE id = 'alice'")
    conn.commit()
    assert await store.get("alice") is None


async def test_the_password_is_encrypted_at_rest(store, tmp_path):
    await store.save("alice", "dj_alice", "hunter2-secret")

    raw = sqlite3.connect(tmp_path / "accounts.db").execute(
        "SELECT username, password FROM soulseek_credentials").fetchone()
    assert raw[0] == "dj_alice"
    assert b"hunter2-secret" not in bytes(raw[1])

    creds = await store.get("alice")
    assert (creds.username, creds.password) == ("dj_alice", "hunter2-secret")


async def test_the_key_file_is_readable_by_its_owner_only(store, tmp_path):
    await store.save("alice", "dj_alice", "pw")
    mode = stat.S_IMODE(os.stat(tmp_path / "soulseek.key").st_mode)
    assert mode == 0o600


async def test_the_public_view_never_carries_the_password(store):
    await store.save("alice", "dj_alice", "hunter2-secret")
    view = await store.describe("alice")
    # Saved only after a login succeeded, so saving is the first "ok".
    assert view["last_ok_at"]
    assert {k: view[k] for k in ("configured", "username", "last_error")} == {
        "configured": True, "username": "dj_alice", "last_error": None}
    assert "hunter2-secret" not in repr(view)
    assert "hunter2-secret" not in repr(await store.get("alice"))
    assert await store.describe("bob") == {
        "configured": False, "username": None,
        "last_ok_at": None, "last_error": None, "share_downloads": True}


async def test_forgetting_removes_them(store):
    await store.save("alice", "dj_alice", "pw")
    await store.forget("alice")
    assert await store.get("alice") is None


async def test_outcomes_are_recorded(store):
    await store.save("alice", "dj_alice", "pw")
    await store.note_error("alice", "Soulseek refused the login: INVALIDPASS")
    assert (await store.describe("alice"))["last_error"].endswith("INVALIDPASS")
    await store.note_ok("alice")
    view = await store.describe("alice")
    assert view["last_error"] is None and view["last_ok_at"]


async def test_a_lost_key_reads_as_not_configured_and_says_why(store, tmp_path):
    """Without the key nothing can be decrypted: ask for the password again
    rather than fail somewhere deep in a download."""
    await store.save("alice", "dj_alice", "pw")
    (tmp_path / "soulseek.key").unlink()
    fresh = SoulseekCredentials(tmp_path / "accounts.db",
                                key_path=tmp_path / "soulseek.key")
    assert await fresh.get("alice") is None
    view = await fresh.describe("alice")
    assert view["configured"] is False
    assert "enter your password again" in view["last_error"]


async def test_sharing_downloads_is_on_by_default_and_can_be_turned_off(store):
    await store.save("alice", "dj_alice", "pw")
    assert (await store.describe("alice"))["share_downloads"] is True
    assert await store.shares_downloads("alice") is True
    await store.set_sharing("alice", False)
    assert (await store.describe("alice"))["share_downloads"] is False
    assert await store.shares_downloads("alice") is False
    await store.save("alice", "dj_alice", "new-pw")     # a new password
    assert await store.shares_downloads("alice") is False  # keeps the choice


async def test_a_table_from_before_sharing_gains_the_column(tmp_path):
    accounts = Accounts(tmp_path / "accounts.db")
    await _seed_account(accounts, "alice", "alice@example.com")
    conn = sqlite3.connect(tmp_path / "accounts.db")
    conn.execute("CREATE TABLE soulseek_credentials (user_id TEXT PRIMARY KEY,"
                 " username TEXT NOT NULL, password BLOB NOT NULL,"
                 " last_ok_at TEXT, last_error TEXT, updated_at TEXT NOT NULL)")
    conn.commit()
    store = SoulseekCredentials(tmp_path / "accounts.db",
                                key_path=tmp_path / "soulseek.key")
    await store.save("alice", "dj_alice", "pw")
    assert await store.shares_downloads("alice") is True
