"""Acquisition: picking a candidate, and what happens to the bytes.

No slskd here. What matters is the judgement around it — which of forty
candidates to take, whether the delivered file is the track that was asked
for, and that a failure is recorded rather than swallowed.
"""
import subprocess
from pathlib import Path

import pytest

from src.acquire.library import (VerificationFailed, collect, safe_filename,
                                 tag, verify)
from src.acquire.slskd import Candidate, score_candidate, search_query
from src.identify.base import TrackMatch, normalize_key
from src.store.library import Library

from tests.conftest import TEST_USER

pytestmark = pytest.mark.anyio


async def _instant(_seconds):
    """Skip the wait without skipping the loop the wait is inside."""
    return None


def _file(name, ext="mp3", bitrate=320, size=8_000_000, length=360, **user):
    raw = {"filename": f"@@abc\\Music\\{name}.{ext}", "extension": ext,
           "size": size, "bitRate": bitrate, "length": length}
    who = {"username": "peer", "hasFreeUploadSlot": True, "queueLength": 0,
           "uploadSpeed": 500_000, **user}
    return raw, who


def _score(name, **kwargs):
    raw, who = _file(name, **kwargs)
    candidate = score_candidate(raw, who, "Skee Mask Rev8617")
    return candidate.score if candidate else None


def test_the_default_profile_prefers_what_plays_everywhere():
    """FLAC does not import into Apple Music and is three times the size.

    The default is `portable` for that reason: a 320 kbps MP3 opens in every
    DJ application and on every phone. `lossless` is there for anyone pointing
    this at Rekordbox or Serato.
    """
    assert _score("Skee Mask - Rev8617", ext="mp3") > \
           _score("Skee Mask - Rev8617", ext="flac", bitrate=None)


def test_the_lossless_profile_reverses_that(monkeypatch):
    from src.acquire import slskd

    monkeypatch.setattr(slskd, "_FORMAT_SCORE",
                        slskd.FORMAT_PROFILES["lossless"])
    assert _score("Skee Mask - Rev8617", ext="flac", bitrate=None) > \
           _score("Skee Mask - Rev8617", ext="mp3")


def test_the_apple_profile_puts_flac_last(monkeypatch):
    from src.acquire import slskd

    monkeypatch.setattr(slskd, "_FORMAT_SCORE", slskd.FORMAT_PROFILES["apple"])
    assert _score("Skee Mask - Rev8617", ext="alac", bitrate=None) > \
           _score("Skee Mask - Rev8617", ext="flac", bitrate=None)
    assert _score("Skee Mask - Rev8617", ext="mp3") > \
           _score("Skee Mask - Rev8617", ext="flac", bitrate=None)


def test_an_extended_mix_beats_a_radio_edit_even_in_a_better_format():
    """The version matters more than the codec.

    A radio edit has no intro to beatmatch into and no outro to mix out of, so
    it is close to useless at the decks — worse than nothing arriving, because
    it looks like the job is done.
    """
    extended = _score("Skee Mask - Rev8617 (Extended Mix)", ext="mp3", length=480)
    radio = _score("Skee Mask - Rev8617 (Radio Edit)", ext="flac",
                   bitrate=None, length=195)
    assert extended > radio * 1.5, "a radio edit came close to an extended mix"


def test_longer_wins_outright_rather_than_by_a_hair():
    """Shazam often identifies the radio edit, so length cannot be matched
    against the identified track — it has to be preferred absolutely."""
    long_version = _score("Skee Mask - Rev8617", length=540)
    short_version = _score("Skee Mask - Rev8617", length=330)
    assert long_version - short_version > 15


def test_a_whole_mix_is_not_a_track():
    assert _score("Skee Mask - Rev8617", length=2100) < \
           _score("Skee Mask - Rev8617", length=420)


def test_a_missing_length_is_not_held_against_a_peer():
    """Plenty of peers report nothing; refusing them discards good files."""
    unknown = _score("Skee Mask - Rev8617", length=None)
    known_good = _score("Skee Mask - Rev8617", length=420)
    known_bad = _score("Skee Mask - Rev8617", length=150)
    assert known_bad < unknown < known_good


def test_a_matching_filename_beats_a_stranger():
    right, who = _file("Skee Mask - Rev8617")
    wrong, who2 = _file("Some Other Song Entirely")
    assert (score_candidate(right, who, "Skee Mask Rev8617").score >
            score_candidate(wrong, who2, "Skee Mask Rev8617").score)


def test_a_long_queue_costs_a_candidate():
    """A perfect file behind forty people is worse than a good one now."""
    free, who = _file("Skee Mask - Rev8617")
    queued, who2 = _file("Skee Mask - Rev8617",
                         hasFreeUploadSlot=False, queueLength=40)
    assert (score_candidate(free, who, "Skee Mask Rev8617").score >
            score_candidate(queued, who2, "Skee Mask Rev8617").score)


def test_non_audio_files_are_not_candidates():
    cover, who = _file("folder", "jpg")
    assert score_candidate(cover, who, "anything") is None


@pytest.mark.parametrize("artist,title,expected", [
    ("Fred again..", "Delilah", "Fred again Delilah"),
    ("Chaos_In_The_CBD", "Midnight-In-Peckham", "Chaos In The CBD Midnight In Peckham"),
    ("  Spaced  ", "  Out  ", "Spaced Out"),
])
def test_the_search_query_is_clean(artist, title, expected):
    """Peers match on a plain substring, so a stray double space costs hits."""
    query = search_query(artist, title)
    assert query == expected
    assert "  " not in query


@pytest.mark.parametrize("artist,title,ext,expected", [
    ("Skee Mask", "Rev8617", ".flac", "Skee Mask - Rev8617.flac"),
    ("AC/DC", "Back: In Black?", "mp3", "AC_DC - Back_ In Black_.mp3"),
    ("", "", "", "track.bin"),
])
def test_filenames_survive_a_filesystem(artist, title, ext, expected):
    assert safe_filename(artist, title, ext) == expected


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "whatever the uploader typed.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=60", "-c:a", "libmp3lame",
         "-b:a", "128k", str(path)], check=True)
    return path


class StubIdentifier:
    """Answers with a fixed track, whatever it is given."""

    def __init__(self, artist, title):
        self.artist, self.title = artist, title
        self.calls = 0

    async def identify(self, wav_bytes):
        self.calls += 1
        if self.artist is None:
            return None
        return TrackMatch(title=self.title, artist=self.artist, provider="stub")


async def test_a_verified_file_is_renamed_and_tagged(audio_file, tmp_path):
    from mutagen.easyid3 import EasyID3

    wanted = normalize_key("Skee Mask", "Rev8617")
    got = await collect(
        audio_file, tmp_path / "out", "Skee Mask", "Rev8617",
        identifier=StubIdentifier("Skee Mask", "Rev8617"), expected_key=wanted,
        meta={"label": "Ilian Tape", "year": "2018", "album": "Compro"},
    )

    assert got.verified is True
    assert got.path.name == "Skee Mask - Rev8617.mp3"
    tags = EasyID3(str(got.path))
    assert tags["artist"] == ["Skee Mask"]
    assert tags["album"] == ["Compro"]
    assert tags["date"] == ["2018"]


async def test_a_mislabelled_file_is_refused(audio_file, tmp_path):
    """Soulseek filenames are whatever the uploader typed.

    A wrong record filed under the right name is worse than no record: it is
    found out at the decks.
    """
    with pytest.raises(VerificationFailed) as raised:
        await collect(
            audio_file, tmp_path / "out", "Skee Mask", "Rev8617",
            identifier=StubIdentifier("Rick Astley", "Never Gonna Give You Up"),
            expected_key=normalize_key("Skee Mask", "Rev8617"),
        )

    assert "Rick Astley" in str(raised.value)
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


async def test_verification_can_be_waived(audio_file, tmp_path):
    got = await collect(
        audio_file, tmp_path / "out", "Skee Mask", "Rev8617",
        identifier=StubIdentifier("Somebody Else", "Something Else"),
        expected_key=normalize_key("Skee Mask", "Rev8617"),
        require_verification=False,
    )
    assert got.path.exists()
    assert got.verified is False
    assert "not fingerprint-verified" in got.note


async def test_an_unrecognisable_file_is_not_called_wrong(audio_file, tmp_path):
    """Failing to read a file is a different thing from reading a wrong one."""
    matched, actually = await verify(
        audio_file, StubIdentifier(None, None), "anything")
    assert matched is False
    assert actually == "", "an unidentifiable file must not be given a name"


async def test_two_downloads_of_the_same_track_do_not_collide(audio_file,
                                                              tmp_path):
    out = tmp_path / "out"
    identifier = StubIdentifier("Skee Mask", "Rev8617")
    key = normalize_key("Skee Mask", "Rev8617")

    first = await collect(audio_file, out, "Skee Mask", "Rev8617",
                          identifier, key)
    second = await collect(audio_file, out, "Skee Mask", "Rev8617",
                           identifier, key)

    assert first.path != second.path, "the second download overwrote the first"
    assert first.path.exists() and second.path.exists()


def test_tagging_a_format_that_cannot_hold_tags_is_not_fatal(tmp_path):
    """A correctly named file with no tags is still a usable record."""
    path = tmp_path / "not-audio.bin"
    path.write_bytes(b"\x00" * 512)
    assert tag(path, {"artist": "A", "title": "B"}) is False


async def test_a_download_records_its_outcome(tmp_path):
    """"Nothing happened" and "the peer vanished" must look different."""
    library = Library(tmp_path / "lib.db")
    download_id = await library.start_download("a::b", "Artist", "Track", user_id=TEST_USER)

    queued = await library.get_download(download_id, user_id=TEST_USER)
    assert queued["status"] == "queued"
    assert queued["available"] is False

    await library.update_download(download_id, status="failed",
                                  message="The peer went offline at 60%")
    failed = await library.get_download(download_id, user_id=TEST_USER)
    assert failed["status"] == "failed"
    assert "60%" in failed["message"]


async def test_a_served_path_is_kept_off_the_wire(tmp_path):
    """The row goes to the browser; a server filesystem path does not."""
    library = Library(tmp_path / "lib.db")
    download_id = await library.start_download("a::b", "Artist", "Track", user_id=TEST_USER)
    await library.update_download(download_id, status="ready",
                                  local_path="/srv/downloads/x.flac")

    row = await library.get_download(download_id, user_id=TEST_USER)
    assert "local_path" not in row
    assert row["filename"] == "x.flac"
    assert await library.download_path(download_id, user_id=TEST_USER) == "/srv/downloads/x.flac"


async def test_a_search_waits_for_the_peers_to_be_handed_over(monkeypatch):
    """slskd fills the response array only when the search completes.

    Peers arrive within seconds and `responseCount` reflects them immediately,
    but `responses` stays empty until the search reaches a completed state —
    about twenty seconds. The client waited twelve, so every search returned
    nothing and every acquisition reported "no peer is sharing this one".
    Measured against a query with twenty-six peers behind it.
    """
    from src.acquire.slskd import SlskdClient

    calls = {"n": 0}

    async def fake_request(self, method, path, **kwargs):
        if method == "POST":
            return {"id": "s1"}
        if method == "DELETE":
            return {}
        calls["n"] += 1
        if calls["n"] < 4:
            # In progress: peers counted, none handed over.
            return {"state": "InProgress", "responseCount": 26, "responses": []}
        return {
            "state": "Completed, TimedOut", "responseCount": 1,
            "responses": [{
                "username": "peer", "hasFreeUploadSlot": True,
                "uploadSpeed": 900_000, "queueLength": 0,
                "files": [{"filename": "Alan Dixon - Acid Drop.mp3",
                           "size": 12_000_000, "bitRate": 320, "length": 380}],
            }],
        }

    monkeypatch.setattr(SlskdClient, "_request", fake_request)
    monkeypatch.setattr("asyncio.sleep", _instant)

    found = await SlskdClient(base_url="http://x", api_key="k").search("Acid Drop")
    assert found, "gave up before slskd handed the peers over"
    assert found[0].username == "peer"


async def test_a_search_that_runs_long_is_stopped_and_read(monkeypatch):
    """A slow search should cost the stragglers, not the whole result.

    The response array only fills when the search completes, so waiting for a
    deadline and giving up returns nothing at all. Stopping it forces
    completion and hands over everyone who answered in time.

    The stop needs a JSON body — without one slskd replies 415, the search
    keeps running, and the outcome reads as "nobody is sharing this" when in
    fact twenty-three people were.
    """
    from src.acquire.slskd import SlskdClient

    state = {"stopped": False, "puts": []}

    async def fake_request(self, method, path, **kwargs):
        if method == "POST":
            return {"id": "s1"}
        if method == "DELETE":
            return {}
        if method == "PUT":
            state["puts"].append(kwargs.get("json"))
            state["stopped"] = True
            return {}
        if state["stopped"]:
            return {
                "state": "Completed, Cancelled", "responseCount": 1,
                "responses": [{
                    "username": "peer", "hasFreeUploadSlot": True,
                    "uploadSpeed": 900_000, "queueLength": 0,
                    "files": [{"filename": "Alan Dixon - Acid Drop.mp3",
                               "size": 12_000_000, "bitRate": 320,
                               "length": 380}],
                }],
            }
        # Never completes on its own.
        return {"state": "InProgress", "responseCount": 23, "responses": []}

    monkeypatch.setattr(SlskdClient, "_request", fake_request)
    monkeypatch.setattr("asyncio.sleep", _instant)

    found = await SlskdClient(base_url="http://x", api_key="k").search(
        "Acid Drop", wait=4.0)

    assert state["stopped"], "a search that never completes was never stopped"
    assert state["puts"] == [{}], "the stop must carry a body or slskd 415s"
    assert found and found[0].username == "peer"


def test_the_query_keeps_words_whole():
    """Stripping an accent must leave a word, not a hole.

    NFKD decomposes é into e plus a combining mark, so removing marks turns
    "cohérent" into "coherent". It does not decompose ø, æ, ß, ł or đ at all —
    those are letters in their own right — so the same pass would turn "Møme"
    into "M me" and match nothing. They are transliterated first.
    """
    from src.acquire.slskd import search_query

    assert search_query("cohérent", "") == "coherent"
    assert search_query("Møme", "") == "Mome"
    assert search_query("Straße", "") == "Strasse"
    assert search_query("Łukasz", "") == "Lukasz"
    assert search_query("Sigur Rós", "") == "Sigur Ros"
    # And a hyphen separates rather than joins.
    assert search_query("hip-hop", "world") == "hip hop world"


def test_the_query_drops_what_a_filename_will_not_match():
    """Peers match a plain substring, so a mix suffix costs the whole result.

    Measured on the live network: "Todd Terry & Sound Design Bounce to the
    Beat (Chris Stussy Remix)" found 9 files; without the suffix, 166.
    Ranking still prefers extended mixes — but only among candidates the
    search returned.
    """
    from src.acquire.slskd import search_query

    assert search_query("Todd Terry & Sound Design",
                        "Bounce to the Beat (Chris Stussy Remix)") == \
        "Todd Terry Sound Design Bounce to the Beat"
    assert search_query("Ivan Gough & Feenixpawl",
                        "In My Mind (feat. Georgi Kay) [Axwell Mix]") == \
        "Ivan Gough Feenixpawl In My Mind"


def test_a_fully_bracketed_title_still_searches_for_something():
    """Otherwise the query is empty, which matches everything."""
    from src.acquire.slskd import search_query

    assert search_query("X", "(Untitled)").strip()
