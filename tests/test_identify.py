"""Response parsing and the fuzzy identity used for voting and merging."""
import pytest

from src.identify.base import normalize_artist, normalize_key, normalize_title
from src.identify.shazam import parse_shazam_track

SHAZAM_RESPONSE = {
    "matches": [{"id": "1"}, {"id": "1"}, {"id": "2"}],
    "track": {
        "title": "Loose Lips",
        "subtitle": "Chris Stussy",
        "url": "https://www.shazam.com/track/123",
        "isrc": "GBXYZ1234567",
        "images": {"coverart": "https://img/low.jpg",
                   "coverarthq": "https://img/hq.jpg"},
        "genres": {"primary": "Dance"},
        "sections": [{
            "type": "SONG",
            "metadata": [
                {"title": "Album", "text": "Up The Stuss"},
                {"title": "Label", "text": "Stuss Records"},
                {"title": "Released", "text": "2023"},
            ],
        }],
    },
}


def test_parses_a_full_response():
    match = parse_shazam_track(SHAZAM_RESPONSE)
    assert match is not None
    assert (match.title, match.artist) == ("Loose Lips", "Chris Stussy")
    assert match.album == "Up The Stuss"
    assert match.label == "Stuss Records"
    assert match.year == "2023"
    assert match.genre == "Dance"
    assert match.isrc == "GBXYZ1234567"
    assert match.cover_url == "https://img/hq.jpg", "should prefer the HQ artwork"
    assert match.raw_matches == 2, "duplicate match ids must be counted once"


@pytest.mark.parametrize("payload", [
    {}, {"matches": []}, {"track": None}, {"track": {}},
    {"track": {"title": ""}}, {"track": {"subtitle": "Artist"}},
])
def test_returns_none_when_there_is_no_usable_track(payload):
    assert parse_shazam_track(payload) is None


def test_missing_artist_falls_back_rather_than_crashing():
    match = parse_shazam_track({"track": {"title": "Untitled"}})
    assert match is not None
    assert match.artist == "Unknown"


@pytest.mark.parametrize("raw,expected", [
    ("Track (Original Mix)", "track"),
    ("Track - Extended Mix", "track"),
    ("Track [Remastered 2019]", "track"),
    ("Track (Official Music Video)", "track"),
    ("Track feat. Someone", "track"),
    ("Track (feat. Someone Else)", "track"),
    ("Tráck Ëxtra", "track extra"),
    ("Track (Original Mix) [Remastered]", "track"),
])
def test_title_normalisation_collapses_mix_decorations(raw, expected):
    assert normalize_title(raw) == expected


def test_a_remix_is_not_the_same_track():
    """Stripping decorations must not go so far it merges distinct records."""
    assert normalize_title("Track (Skee Mask Remix)") != normalize_title("Track")


@pytest.mark.parametrize("raw,expected", [
    ("Artist A & Artist B", "artist a"),
    ("Artist A, Artist B", "artist a"),
    ("Artist A x Artist B", "artist a"),
    ("Artist A vs. Artist B", "artist a"),
    ("Artist A feat. Artist B", "artist a"),
])
def test_artist_normalisation_keys_on_the_primary_credit(raw, expected):
    assert normalize_artist(raw) == expected


def test_the_same_record_credited_differently_shares_one_key():
    a = normalize_key("Chris Stussy & Rossi.", "Loose Lips (Original Mix)")
    b = normalize_key("Chris Stussy", "Loose Lips")
    assert a == b


def test_different_records_do_not_collide():
    assert normalize_key("A", "Track One") != normalize_key("A", "Track Two")


class FlakyShazam:
    """Fails a given number of times, then answers."""

    def __init__(self, failures, error=None):
        self.failures = failures
        self.error = error or Exception("Failed to decode json")
        self.calls = 0

    async def recognize(self, wav_bytes):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return SHAZAM_RESPONSE


@pytest.fixture
def identifier(monkeypatch):
    """A ShazamIdentifier with its network client replaced and no real waits."""
    import asyncio

    from src.identify.shazam import ShazamIdentifier

    monkeypatch.setattr("shazamio.Shazam", lambda **kwargs: None)
    instance = ShazamIdentifier(concurrency=2, max_attempts=4, backoff=0.001)

    async def instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)
    return instance


@pytest.mark.anyio
async def test_a_refusal_is_retried_not_recorded_as_no_match(identifier):
    """Under load Shazam stops returning JSON and serves something else.

    Treated as "nothing found", that manufactures gaps: in one production run
    113 of 206 probes came back this way and every one became an unidentified
    segment — more than half the set silently blanked, with nothing to say the
    question had never been asked.
    """
    fake = FlakyShazam(failures=2)
    identifier._shazam = fake

    match = await identifier.identify(b"audio")

    assert match is not None, "a recoverable refusal was filed as no-match"
    assert match.title == "Loose Lips"
    assert fake.calls == 3


@pytest.mark.anyio
async def test_a_genuine_no_match_is_not_retried(identifier):
    """An unmatched window is a real answer — and a common, useful one."""

    class Empty:
        calls = 0

        async def recognize(self, wav_bytes):
            Empty.calls += 1
            return {"matches": []}

    identifier._shazam = Empty()
    assert await identifier.identify(b"audio") is None
    assert Empty.calls == 1, "a legitimate no-match was retried"


@pytest.mark.anyio
async def test_retries_give_up_rather_than_hanging_a_set(identifier):
    fake = FlakyShazam(failures=99)
    identifier._shazam = fake

    assert await identifier.identify(b"audio") is None
    assert fake.calls == identifier.max_attempts


@pytest.mark.anyio
async def test_a_permanent_error_is_not_retried(identifier):
    """Retrying something that cannot succeed just spends the analysis budget."""
    fake = FlakyShazam(failures=99, error=ValueError("malformed audio payload"))
    identifier._shazam = fake

    assert await identifier.identify(b"audio") is None
    assert fake.calls == 1


def test_the_default_concurrency_reflects_what_the_service_gives():
    """Measured live, throughput plateaus around three probes per second.

    Two in parallel and eight in parallel move the same number; the extra slots
    buy nothing and are what tips the service into refusing.
    """
    from src.identify.shazam import ShazamIdentifier

    import inspect
    default = inspect.signature(ShazamIdentifier).parameters["concurrency"].default
    assert default <= 4, (
        f"concurrency defaults to {default}; measurement showed no gain past 2"
    )


@pytest.mark.anyio
async def test_a_stalled_request_cannot_hang_the_analysis(identifier):
    """A probe that never answers must not hold the whole run.

    An analysis stalled on exactly this: the job ran for half an hour with the
    CPU idle, waiting inside a library that was itself waiting. It did not fail
    either, which is worse — nothing said so.
    """
    import asyncio

    class NeverAnswers:
        calls = 0

        async def recognize(self, wav_bytes):
            NeverAnswers.calls += 1
            # An event nobody sets, not a sleep: the fixture replaces
            # asyncio.sleep to keep the backoff instant, and a sleep here would
            # be short-circuited by that — the stall would never happen and the
            # test would pass for the wrong reason.
            await asyncio.Event().wait()

    identifier._shazam = NeverAnswers()
    identifier.probe_timeout = 0.05

    assert await identifier.identify(b"audio") is None
    assert NeverAnswers.calls == identifier.max_attempts, (
        "a timeout should be retried, then given up on"
    )


def test_the_library_retry_budget_is_not_doubled():
    """shazamio retries internally; ours must not multiply with it.

    Its default is twenty attempts with a sixty-second ceiling. Four attempts
    layered on top gave a worst case of eighty minutes for one probe out of a
    hundred and eighteen.
    """
    from src.identify.shazam import ShazamIdentifier

    identifier = ShazamIdentifier(concurrency=1)
    worst_case = identifier.max_attempts * identifier.probe_timeout
    assert worst_case <= 300, (
        f"a single probe can take {worst_case / 60:.0f} minutes"
    )
