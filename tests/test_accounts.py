"""Accounts, sessions, and the compartmentalisation they exist for.

The tests that matter here are the ones about *not* seeing things: a bug in
scoping does not crash, it quietly shows one person another person's library.
"""
import pytest

from src.store.accounts import (Accounts, MAX_CODE_ATTEMPTS, looks_like_email,
                                normalise_email)
from src.store.library import Library

pytestmark = pytest.mark.anyio


@pytest.fixture
def accounts(tmp_path):
    return Accounts(tmp_path / "accounts.db")


@pytest.fixture
def library(tmp_path):
    return Library(tmp_path / "library.db")


def _set(title="Set"):
    return {"duration": 60.0, "waveform": [], "stats": {},
            "tracks": [{"index": 1, "start": 0, "end": 60, "identified": True,
                        "key": "a::x", "title": "X", "artist": "A"}]}


# ── Codes ────────────────────────────────────────────────────────────────

async def test_a_correct_code_creates_the_account(accounts):
    code = await accounts.start_login("Pierre@Example.COM ")
    user = await accounts.verify_login("pierre@example.com", code)
    assert user is not None
    assert user["email"] == "pierre@example.com", "address should be normalised"


async def test_a_code_works_once(accounts):
    code = await accounts.start_login("a@b.com")
    assert await accounts.verify_login("a@b.com", code) is not None
    assert await accounts.verify_login("a@b.com", code) is None, (
        "a code that still works after use is a code someone can replay")


async def test_a_wrong_code_is_refused_and_counted(accounts):
    await accounts.start_login("a@b.com")
    for _ in range(MAX_CODE_ATTEMPTS):
        assert await accounts.verify_login("a@b.com", "000000") is None

    # The real code must now be dead too: otherwise the attempt limit only
    # slows guessing down rather than stopping it.
    assert await accounts.verify_login("a@b.com", "111111") is None


async def test_asking_twice_in_a_row_does_not_send_a_second_code(accounts):
    """Otherwise a mistyped address is a way to flood someone's inbox."""
    first = await accounts.start_login("a@b.com")
    assert first is not None
    assert await accounts.start_login("a@b.com") is None


async def test_verifying_without_asking_fails(accounts):
    assert await accounts.verify_login("nobody@b.com", "123456") is None


def test_email_normalisation_does_not_merge_distinct_people():
    """No Gmail-specific folding: elsewhere those are different mailboxes."""
    assert normalise_email("  A.B+tag@Example.com ") == "a.b+tag@example.com"
    assert normalise_email("a.b@example.com") != normalise_email("ab@example.com")


def test_obviously_bad_addresses_are_rejected():
    for bad in ("", "nope", "a@b", "a b@c.com", "@b.com", "a@.com"):
        assert not looks_like_email(bad), bad
    assert looks_like_email("a@b.com")


# ── Sessions ─────────────────────────────────────────────────────────────

async def test_a_session_identifies_its_user(accounts):
    code = await accounts.start_login("a@b.com")
    user = await accounts.verify_login("a@b.com", code)
    token = await accounts.create_session(user["id"])

    seen = await accounts.user_for_session(token)
    assert seen is not None and seen["id"] == user["id"]


async def test_a_forged_or_ended_session_identifies_nobody(accounts):
    code = await accounts.start_login("a@b.com")
    user = await accounts.verify_login("a@b.com", code)
    token = await accounts.create_session(user["id"])

    assert await accounts.user_for_session("not-a-real-token") is None
    assert await accounts.user_for_session("") is None

    await accounts.end_session(token)
    assert await accounts.user_for_session(token) is None


async def test_the_token_is_not_stored(accounts, tmp_path):
    """Read access to the database must not be enough to mint a session."""
    import sqlite3

    code = await accounts.start_login("a@b.com")
    user = await accounts.verify_login("a@b.com", code)
    token = await accounts.create_session(user["id"])

    rows = sqlite3.connect(tmp_path / "accounts.db").execute(
        "SELECT token_hash FROM sessions").fetchall()
    assert rows and all(token not in str(r) for r in rows)


async def test_signing_out_everywhere_ends_every_session(accounts):
    code = await accounts.start_login("a@b.com")
    user = await accounts.verify_login("a@b.com", code)
    tokens = [await accounts.create_session(user["id"]) for _ in range(3)]

    assert await accounts.end_all_sessions(user["id"]) == 3
    for token in tokens:
        assert await accounts.user_for_session(token) is None


# ── Compartmentalisation ─────────────────────────────────────────────────

async def test_one_account_does_not_see_another_s_sets(library):
    await library.save_set("s1", "Mine", _set(), user_id="alice")
    await library.save_set("s2", "Theirs", _set(), user_id="bob")

    mine = await library.list_sets(user_id="alice")
    assert [s["id"] for s in mine] == ["s1"]
    assert await library.get_set("s2", user_id="alice") is None, (
        "one account could read another's set by knowing its id")


async def test_a_set_cannot_be_deleted_by_someone_else(library):
    await library.save_set("s1", "Theirs", _set(), user_id="bob")
    assert await library.delete_set("s1", user_id="alice") is False
    assert await library.get_set("s1", user_id="bob") is not None


async def test_two_accounts_can_star_the_same_track(library):
    """The crate keyed on track alone, so the second star was a conflict."""
    assert await library.toggle_star("a::x", "X", "A", user_id="alice") is True
    assert await library.toggle_star("a::x", "X", "A", user_id="bob") is True

    assert [t["key"] for t in await library.crate(user_id="alice")] == ["a::x"]
    assert [t["key"] for t in await library.crate(user_id="bob")] == ["a::x"]

    # And unstarring is not shared either.
    await library.toggle_star("a::x", user_id="alice")
    assert await library.crate(user_id="alice") == []
    assert len(await library.crate(user_id="bob")) == 1


async def test_two_accounts_can_follow_the_same_channel(library):
    """watches.url was UNIQUE, so the second follow was silently dropped."""
    await library.add_watch("w1", "https://x/chan", "Chan", user_id="alice")
    await library.add_watch("w2", "https://x/chan", "Chan", user_id="bob")

    assert len(await library.list_watches(user_id="alice")) == 1
    assert len(await library.list_watches(user_id="bob")) == 1
    assert len(await library.list_watches(user_id=None)) == 2, (
        "the scheduled check must still see everybody's")


async def test_a_download_cannot_be_read_by_id_from_another_account(library):
    """Download ids are small integers, so unscoped means walkable."""
    download_id = await library.start_download("a::x", "A", "X",
                                               user_id="bob")
    assert await library.get_download(download_id, user_id="alice") is None
    assert await library.download_path(download_id, user_id="alice") is None
    assert await library.get_download(download_id, user_id="bob") is not None


async def test_digging_only_counts_your_own_sets(library):
    """A track heard across sets is a signal about *your* listening."""
    for i in range(3):
        await library.save_set(f"b{i}", "Theirs", _set(), user_id="bob")
    await library.save_set("a1", "Mine", _set(), user_id="alice")

    assert await library.recurring_tracks(min_sets=2, user_id="alice") == []
    assert len(await library.recurring_tracks(min_sets=2, user_id="bob")) == 1

    found = await library.search_tracks("X", user_id="alice")
    assert {t["set_title"] for t in found} == {"Mine"}


async def test_the_first_account_adopts_what_came_before_it(library):
    """An existing library must not vanish behind a new login screen."""
    await library.save_set("old", "From before", _set(), user_id="")
    await library.toggle_star("a::x", "X", "A", user_id="")

    assert await library.list_sets(user_id="alice") == []
    adopted = await library.adopt_orphans("alice")

    assert adopted >= 2
    assert [s["id"] for s in await library.list_sets(user_id="alice")] == ["old"]
    assert len(await library.crate(user_id="alice")) == 1


# ── Through the API ──────────────────────────────────────────────────────

@pytest.fixture
async def auth_client(tmp_path, monkeypatch):
    """An app instance with mail captured. Accounts are always on."""
    import importlib

    # Before web is imported: it builds the accounts store at import time and
    # binds the request dependencies to that instance, so reassigning the
    # attribute afterwards would leave the endpoints on the old one — pointed
    # at the working copy's real database.
    monkeypatch.setenv("SHAZAMER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SLSKD_URL", "")
    # Cookies would otherwise be Secure-only, and the test transport is http.
    monkeypatch.setenv("COOKIE_SECURE", "0")

    import src.auth as auth_mod
    importlib.reload(auth_mod)
    import src.mail as mail_mod
    importlib.reload(mail_mod)
    import src.web as web
    importlib.reload(web)

    # The limiters hold state at module level and every test here uses the
    # same handful of addresses, so without this the fifth test in a run is
    # rate-limited by the first four and fails for a reason that has nothing
    # to do with what it is testing.
    for limiter in (auth_mod.email_limiter, auth_mod.ip_limiter,
                    auth_mod.verify_limiter):
        limiter._hits.clear()

    sent = []

    async def capture(to, code, minutes=10):
        sent.append({"to": to, "code": code})

    monkeypatch.setattr(web.mail, "send_login_code", capture)
    monkeypatch.setattr(web.mail, "configured", lambda: True)

    for name in ("uploads", "media"):
        (tmp_path / name).mkdir(exist_ok=True)
    web.UPLOAD_DIR = tmp_path / "uploads"
    web.MEDIA_DIR = tmp_path / "media"
    web.library = web.Library(tmp_path / "library.db")
    web.tasks = web.TaskManager(tmp_path / "tasks")

    from httpx import ASGITransport, AsyncClient
    transport = ASGITransport(app=web.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.web = web            # type: ignore[attr-defined]
        c.sent = sent          # type: ignore[attr-defined]
        yield c

    monkeypatch.undo()
    importlib.reload(auth_mod)
    importlib.reload(web)


async def test_a_signed_out_caller_is_refused(auth_client):
    assert (await auth_client.get("/api/sets")).status_code == 401
    me = await auth_client.get("/api/auth/me")
    assert me.status_code == 200, "checking who you are must not itself 401"
    assert me.json()["authenticated"] is False


async def test_signing_in_end_to_end(auth_client):
    r = await auth_client.post("/api/auth/request-code",
                               json={"email": "a@b.com"})
    assert r.status_code == 200
    code = auth_client.sent[-1]["code"]

    r = await auth_client.post("/api/auth/verify",
                               json={"email": "a@b.com", "code": code})
    assert r.status_code == 200

    assert (await auth_client.get("/api/sets")).status_code == 200
    assert (await auth_client.get("/api/auth/me")).json()["email"] == "a@b.com"


async def test_the_session_cookie_is_not_readable_by_scripts(auth_client):
    await auth_client.post("/api/auth/request-code", json={"email": "a@b.com"})
    r = await auth_client.post(
        "/api/auth/verify",
        json={"email": "a@b.com", "code": auth_client.sent[-1]["code"]})

    header = r.headers.get("set-cookie", "")
    assert "httponly" in header.lower(), "a readable session cookie is an XSS away"
    # A year, so ordinary use never asks again.
    assert "max-age=31536000" in header.lower().replace(" ", "")


async def test_signing_out_ends_the_session(auth_client):
    await auth_client.post("/api/auth/request-code", json={"email": "a@b.com"})
    await auth_client.post(
        "/api/auth/verify",
        json={"email": "a@b.com", "code": auth_client.sent[-1]["code"]})
    assert (await auth_client.get("/api/sets")).status_code == 200

    await auth_client.post("/api/auth/logout")
    assert (await auth_client.get("/api/sets")).status_code == 401


async def test_requesting_a_code_says_nothing_about_the_address(auth_client):
    """Same answer for a known and an unknown address, or this is a directory."""
    first = await auth_client.post("/api/auth/request-code",
                                   json={"email": "known@b.com"})
    await auth_client.post(
        "/api/auth/verify",
        json={"email": "known@b.com", "code": auth_client.sent[-1]["code"]})

    second = await auth_client.post("/api/auth/request-code",
                                    json={"email": "nobody@b.com"})
    assert first.status_code == second.status_code
    assert first.json() == second.json()


async def test_a_wrong_code_is_refused_through_the_api(auth_client):
    await auth_client.post("/api/auth/request-code", json={"email": "a@b.com"})
    r = await auth_client.post("/api/auth/verify",
                               json={"email": "a@b.com", "code": "000000"})
    assert r.status_code == 400
    assert (await auth_client.get("/api/sets")).status_code == 401


async def test_the_worker_can_act_on_a_download_it_was_handed(library):
    """The worker has a row id and no session, and must still read the row.

    Scoping every method broke this without a test noticing: the acquire
    runner called `get_download(id)` and got a TypeError, which would have
    failed every Soulseek transfer in production. Passing None is deliberate
    and explicit — the argument stays required, so forgetting it still raises
    rather than quietly returning somebody else's download.
    """
    download_id = await library.start_download("a::x", "A", "X", user_id="bob")

    assert await library.get_download(download_id, user_id=None) is not None
    assert await library.get_download(download_id, user_id="alice") is None

    import inspect
    signature = inspect.signature(library.get_download)
    assert signature.parameters["user_id"].default is inspect.Parameter.empty, (
        "user_id must stay required; a default is how a request path forgets")


# ── Profile ──────────────────────────────────────────────────────────────

async def test_a_display_name_never_leaks_the_whole_address():
    """"Shared by pierre" is friendly; the full address is not ours to pass on.

    A share reaches someone who may forward it again, so whatever the name
    resolves to travels further than the person who set it expects.
    """
    from src.store.accounts import display_name

    assert display_name({"first_name": "Pierre", "last_name": "Gallet"}) == \
        "Pierre Gallet"
    assert display_name({"email": "pierre.gallet@hotmail.fr"}) == "pierre.gallet"
    assert "@" not in display_name({"email": "pierre.gallet@hotmail.fr"})


async def test_changing_an_address_is_proved_on_the_new_one(accounts):
    """A typo must not lock someone out of their own account."""
    code = await accounts.start_login("old@b.com")
    user = await accounts.verify_login("old@b.com", code)

    change = await accounts.start_email_change(user["id"], "new@b.com")
    assert change is not None

    # Until it is confirmed, nothing has moved.
    assert (await accounts.profile(user["id"]))["email"] == "old@b.com"
    assert (await accounts.profile(user["id"]))["pending_email"] == "new@b.com"

    assert await accounts.confirm_email_change(user["id"], "000000") is None
    assert (await accounts.profile(user["id"]))["email"] == "old@b.com"

    assert await accounts.confirm_email_change(user["id"], change) == "new@b.com"
    assert (await accounts.profile(user["id"]))["email"] == "new@b.com"


async def test_an_address_someone_else_holds_cannot_be_taken(accounts):
    first = await accounts.verify_login(
        "a@b.com", await accounts.start_login("a@b.com"))
    await accounts.verify_login("b@b.com", await accounts.start_login("b@b.com"))

    assert await accounts.start_email_change(first["id"], "b@b.com") is None
    assert (await accounts.profile(first["id"]))["email"] == "a@b.com"


# ── Sharing ──────────────────────────────────────────────────────────────

def _shared_set():
    return {"duration": 60.0, "waveform": [1, 2], "stats": {},
            "tracks": [{"index": 1, "start": 0, "end": 60, "identified": True,
                        "key": "a::x", "title": "X", "artist": "A"}]}


async def test_a_shared_set_becomes_the_recipient_s_own(library):
    """A copy, not a window onto someone else's library.

    Which means the sender cannot take it back, cannot see what is done with
    it, and deleting theirs leaves the other intact. Every one of those is a
    property somebody would otherwise be surprised by.
    """
    await library.save_set("s1", "A Night Out", _shared_set(), user_id="alice",
                           audio_path="/tmp/alice.mp3")
    token = await library.create_share("s1", user_id="alice",
                                       from_name="Alice")
    claimed = await library.claim_share(token, user_id="bob")

    assert claimed["set_id"] != "s1", "bob got a pointer, not a copy"
    theirs = await library.get_set(claimed["set_id"], user_id="bob")
    assert theirs["title"] == "A Night Out"
    assert len(theirs["tracks"]) == 1
    assert theirs["shared_by"] == "Alice"
    assert not theirs["audio_path"], (
        "the audio was copied; it is a byproduct swept on a timer and "
        "duplicating it per share fills the disk")

    await library.delete_set("s1", user_id="alice")
    assert await library.get_set(claimed["set_id"], user_id="bob") is not None


async def test_following_the_same_invitation_twice_makes_one_copy(library):
    await library.save_set("s1", "Set", _shared_set(), user_id="alice")
    token = await library.create_share("s1", user_id="alice", from_name="Alice")

    first = await library.claim_share(token, user_id="bob")
    second = await library.claim_share(token, user_id="bob")

    assert first["set_id"] == second["set_id"]
    assert len(await library.list_sets(user_id="bob")) == 1


async def test_a_set_you_do_not_own_cannot_be_shared(library):
    await library.save_set("s1", "Set", _shared_set(), user_id="alice")
    assert await library.create_share("s1", user_id="bob",
                                      from_name="Bob") is None


async def test_the_sender_claiming_their_own_share_changes_nothing(library):
    await library.save_set("s1", "Set", _shared_set(), user_id="alice")
    token = await library.create_share("s1", user_id="alice", from_name="Alice")

    result = await library.claim_share(token, user_id="alice")
    assert result["set_id"] == "s1"
    assert len(await library.list_sets(user_id="alice")) == 1, (
        "sharing with yourself duplicated the set")


async def test_an_invitation_can_be_read_without_signing_in(library):
    """The landing page has to say what is on offer before asking for an account."""
    await library.save_set("s1", "A Night Out", _shared_set(), user_id="alice")
    token = await library.create_share("s1", user_id="alice", from_name="Alice")

    peek = await library.peek_share(token)
    assert peek["title"] == "A Night Out"
    assert peek["from_name"] == "Alice"
    assert peek["track_count"] == 1
    assert await library.peek_share("not-a-token") is None


async def test_a_recurring_track_can_be_traced_to_its_sets(library):
    """The strongest signal this tool produces has to lead somewhere.

    "Showing up across your sets" was a count on a card that answered
    nothing: which sets, and at what moment, had no way to be asked.
    """
    payload = {"duration": 600.0, "waveform": [], "stats": {}, "tracks": [
        {"index": 1, "start": 120, "end": 400, "identified": True,
         "key": "a::x", "title": "X", "artist": "A"}]}
    await library.save_set("s1", "First Night", payload, user_id="alice")
    await library.save_set("s2", "Second Night", payload, user_id="alice")
    await library.save_set("s3", "Theirs", payload, user_id="bob")

    found = await library.appearances("a::x", user_id="alice")
    assert [a["set_title"] for a in found] == ["Second Night", "First Night"]
    assert found[0]["start_label"] == "00:02:00"
    assert all(a["set_id"] for a in found), "no way to open the set"

    # And it stays each person's own signal.
    assert len(await library.appearances("a::x", user_id="bob")) == 1


async def test_a_verdict_freezes_the_numbers_it_was_given_about(library):
    """A label attached to figures that have since moved is worse than none.

    Re-analysing a set changes every segment's span, strength and confidence.
    If the label pointed at the row rather than at the numbers, the dataset
    would quietly rewrite itself and any rule measured against it would be
    measured against the present rather than against what was judged.
    """
    payload = {"duration": 58.4, "waveform": [], "stats": {}, "tracks": [
        {"index": 1, "start": 0, "end": 8.3, "identified": True,
         "key": "a::x", "title": "X", "artist": "A", "strength": "weak",
         "confidence": 1.0},
        {"index": 2, "start": 8.3, "end": 14.3, "identified": True,
         "key": "b::y", "title": "Y", "artist": "B", "strength": "weak",
         "confidence": 1.0}]}
    await library.save_set("s1", "A reel", payload, user_id="alice")

    assert await library.record_feedback("s1", 1, "right", user_id="alice")
    assert await library.record_feedback("s1", 2, "wrong", user_id="alice")

    labels = await library.all_feedback()
    assert {r["verdict"] for r in labels} == {"right", "wrong"}
    good = next(r for r in labels if r["verdict"] == "right")
    assert good["span"] == pytest.approx(8.3), "the span was not captured"
    assert good["set_duration"] == pytest.approx(58.4)

    # Re-analysed with different spans: the stored labels do not move.
    payload["tracks"][0]["end"] = 30.0
    await library.save_set("s1", "A reel", payload, user_id="alice")
    still = next(r for r in await library.all_feedback()
                 if r["verdict"] == "right")
    assert still["span"] == pytest.approx(8.3)


async def test_changing_your_mind_replaces_the_verdict(library):
    """Two contradictory labels on one segment is not data."""
    payload = {"duration": 60.0, "waveform": [], "stats": {}, "tracks": [
        {"index": 1, "start": 0, "end": 60, "identified": True,
         "key": "a::x", "title": "X", "artist": "A"}]}
    await library.save_set("s1", "Set", payload, user_id="alice")

    await library.record_feedback("s1", 1, "wrong", user_id="alice")
    await library.record_feedback("s1", 1, "right", user_id="alice")

    labels = await library.all_feedback()
    assert len(labels) == 1
    assert labels[0]["verdict"] == "right"


async def test_a_verdict_on_someone_else_s_set_is_refused(library):
    payload = {"duration": 60.0, "waveform": [], "stats": {}, "tracks": [
        {"index": 1, "start": 0, "end": 60, "identified": True,
         "key": "a::x", "title": "X", "artist": "A"}]}
    await library.save_set("s1", "Theirs", payload, user_id="bob")

    assert await library.record_feedback("s1", 1, "wrong",
                                         user_id="alice") is False
    assert await library.all_feedback() == []


async def test_an_invented_verdict_is_refused(library):
    payload = {"duration": 60.0, "waveform": [], "stats": {}, "tracks": [
        {"index": 1, "start": 0, "end": 60, "identified": True,
         "key": "a::x", "title": "X", "artist": "A"}]}
    await library.save_set("s1", "Set", payload, user_id="alice")

    assert await library.record_feedback("s1", 1, "maybe",
                                         user_id="alice") is False


async def test_the_evidence_behind_a_match_survives_a_save(library):
    """`confidence` is votes over probes, and the denominator was being dropped.

    A segment covered by one probe scores 1.0 by agreeing with itself, and on
    the live library that maximum covered fifteen well-evidenced tracks and
    eighteen thin ones. The number could not be read without its denominator,
    and the denominator did not survive being written to disk — so the
    tracklist tooltip interpolated `undefined` on every reloaded set.
    """
    payload = {"duration": 600.0, "waveform": [], "stats": {}, "tracks": [
        {"index": 1, "start": 0, "end": 300, "identified": True,
         "key": "a::x", "title": "X", "artist": "A", "confidence": 1.0,
         "strength": "strong", "votes": 4, "probes": 4},
        {"index": 2, "start": 300, "end": 600, "identified": True,
         "key": "b::y", "title": "Y", "artist": "B", "confidence": 1.0,
         "strength": "weak", "votes": 1, "probes": 1}]}
    await library.save_set("s1", "Set", payload, user_id="alice")

    read = await library.get_set("s1", user_id="alice")
    solid, thin = read["tracks"]

    assert solid["confidence"] == thin["confidence"] == 1.0, (
        "the fixture no longer shows why confidence cannot be read alone")
    assert (solid["votes"], solid["probes"]) == (4, 4)
    assert (thin["votes"], thin["probes"]) == (1, 1)


async def test_a_verdict_freezes_the_evidence_as_well_as_the_score(library):
    """The dimension six labels from one reel could not supply.

    Every one of them scored 1.00 on confidence, so the study tool measured it
    at 0.50 separation — no discriminating power. That was a property of the
    sample: each segment rested on a single probe. Without the probe count
    stored beside the verdict there is no way for a later study to tell that
    apart from the metric being useless.
    """
    payload = {"duration": 600.0, "waveform": [], "stats": {}, "tracks": [
        {"index": 1, "start": 0, "end": 300, "identified": True,
         "key": "a::x", "title": "X", "artist": "A", "confidence": 1.0,
         "strength": "strong", "votes": 4, "probes": 4}]}
    await library.save_set("s1", "Set", payload, user_id="alice")
    assert await library.record_feedback("s1", 1, "right", user_id="alice")

    label = (await library.all_feedback())[0]
    assert label["confidence"] == 1.0
    assert label["probes"] == 4, "the denominator was not frozen with the score"
    assert label["votes"] == 4


async def test_a_set_from_before_the_evidence_was_stored_still_reads(library):
    """Every set in the library predates this."""
    payload = {"duration": 600.0, "waveform": [], "stats": {}, "tracks": [
        {"index": 1, "start": 0, "end": 600, "identified": True,
         "key": "a::x", "title": "X", "artist": "A", "confidence": 1.0}]}
    await library.save_set("s1", "Set", payload, user_id="alice")

    track = (await library.get_set("s1", user_id="alice"))["tracks"][0]
    # Zero, not absent and not null: the reader distinguishes "no evidence
    # recorded" from "no probes", and says so in words rather than a number.
    assert track["votes"] == 0 and track["probes"] == 0


async def test_there_is_no_way_to_turn_accounts_off(monkeypatch):
    """The switch is gone, and setting it must not bring it back.

    It existed as a development convenience and defaulted to on, which made it
    safe in principle and one environment file away from not being. A setting
    that opens the library is a setting that can be left open — by a copied
    deploy, by a stack file, by anyone who wanted past a login once.
    """
    import importlib

    import src.auth as auth_mod

    monkeypatch.setenv("AUTH_ENABLED", "0")
    importlib.reload(auth_mod)
    try:
        assert not hasattr(auth_mod, "AUTH_ENABLED")
        assert not hasattr(auth_mod, "SOLO_USER")
    finally:
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        importlib.reload(auth_mod)


async def test_an_anonymous_request_is_refused(client):
    """The client fixture is signed in; this asks the same app without a cookie.

    The point of the fixture change: three hundred tests now go through the
    real cookie check, and this one proves the check would have stopped them.
    """
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=client.web.app)
    async with AsyncClient(transport=transport, base_url="http://test") as guest:
        for path in ("/api/sets", "/api/library/appearances",
                     "/api/acquire/downloads", "/api/profile"):
            answer = await guest.get(path)
            assert answer.status_code == 401, (
                f"{path} answered {answer.status_code} with no session")


async def test_a_forged_cookie_is_refused(client):
    """Tokens are stored as hashes, so a guessed one matches nothing."""
    from httpx import ASGITransport, AsyncClient
    import src.auth as auth_mod

    transport = ASGITransport(app=client.web.app)
    async with AsyncClient(transport=transport, base_url="http://test") as guest:
        guest.cookies.set(auth_mod.COOKIE_NAME, "not-a-real-token")
        assert (await guest.get("/api/sets")).status_code == 401


async def test_whoami_says_auth_is_required_and_answers_without_a_session(client):
    """The frontend reads this to decide whether to show the sign-in screen.

    It has to answer 200 to an anonymous caller — a 401 on the first page load
    would be reported as an error on every visit by somebody not yet signed in.
    """
    from httpx import ASGITransport, AsyncClient

    signed_in = (await client.get("/api/auth/me")).json()
    assert signed_in["authenticated"] is True
    assert signed_in["auth_required"] is True

    transport = ASGITransport(app=client.web.app)
    async with AsyncClient(transport=transport, base_url="http://test") as guest:
        answer = await guest.get("/api/auth/me")
        assert answer.status_code == 200, "a first page load would log an error"
        assert answer.json() == {**answer.json(), "authenticated": False,
                                 "auth_required": True}
