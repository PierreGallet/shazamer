"""The rate-limit gate, and the merge that has to run after confirmation.

Both come from the same production run: 85 of 128 probes lost to HTTP 429,
and a track reported three times in a row because confirmation renamed the
segments between two of its plays.
"""
import asyncio

import pytest

from src.core.segment import Segment, coalesce
from src.identify.shazam import _RateGate

pytestmark = pytest.mark.anyio


async def test_one_refusal_pauses_every_probe():
    """A rate limit is a property of the service, not of one probe.

    The old per-probe backoff let three probes keep asking while the fourth
    waited, so the limit never got the quiet it was asking for.
    """
    gate = _RateGate(base=0.05, ceiling=1.0)
    await gate.penalise()

    waited = []

    async def probe(n):
        start = asyncio.get_running_loop().time()
        await gate.wait()
        waited.append((n, asyncio.get_running_loop().time() - start))

    await asyncio.gather(*(probe(i) for i in range(4)))
    assert all(d >= 0.04 for _, d in waited), waited


async def test_a_wave_of_refusals_only_doubles_once():
    """Four probes refused together are one refusal, not four.

    Counting each would take the penalty from 5s to 80s on the first wave,
    and the analysis would spend its time asleep rather than probing.
    """
    gate = _RateGate(base=1.0, ceiling=64.0)
    held = await asyncio.gather(*(gate.penalise() for _ in range(4)))

    assert gate.pauses == 1, "one wave, one pause"
    assert gate._penalty == 2.0, "doubled once"
    assert all(h > 0 for h in held)


async def test_success_walks_the_penalty_back_down():
    """Otherwise the first bad patch slows the rest of the analysis forever."""
    gate = _RateGate(base=1.0, ceiling=64.0)
    await gate.penalise()
    await asyncio.sleep(0)
    assert gate._penalty == 2.0

    for _ in range(4):
        gate.relax()
    assert gate._penalty == 1.0, "never below base"


def test_confirmation_leftovers_are_merged():
    """Adjacent segments naming the same record are one play, not three.

    This is the shape confirmation produces when its extra probes overturn a
    segment into agreeing with its neighbour: no gap to bridge, identical
    keys, three rows in the tracklist.
    """
    same = [
        Segment(start=415.5, end=484.6, key="ben kim::reload", payload={"title": "Reload"},
                votes=2, probes=3, matched=2),
        Segment(start=484.6, end=513.6, key="ben kim::reload", payload={"title": "Reload"},
                votes=1, probes=3, matched=1),
        Segment(start=513.6, end=557.3, key="ben kim::reload", payload={"title": "Reload"},
                votes=1, probes=3, matched=1),
    ]
    out = coalesce(same, min_segment=20.0)

    assert len(out) == 1, [(s.start, s.end, s.key) for s in out]
    assert out[0].start == 415.5 and out[0].end == 557.3
    assert out[0].votes == 4, "votes from all three count towards the one play"


def test_coalesce_does_not_merge_two_different_records():
    """The inverse must hold, or the merge invents a play that never happened."""
    out = coalesce([
        Segment(start=0, end=200, key="a::one", payload={"title": "One"},
                votes=3, probes=3, matched=3),
        Segment(start=200, end=400, key="b::two", payload={"title": "Two"},
                votes=3, probes=3, matched=3),
    ], min_segment=20.0)
    assert len(out) == 2


def test_coalesce_is_idempotent():
    """It runs twice on every analysis now — once in merge, once after
    confirmation — so a second pass must not keep eating segments."""
    segments = [
        Segment(start=0, end=180, key="a::one", payload={"title": "One"},
                votes=3, probes=3, matched=3),
        Segment(start=180, end=240, key=None, votes=0, probes=2, matched=0),
        Segment(start=240, end=500, key="a::one", payload={"title": "One"},
                votes=2, probes=2, matched=2),
    ]
    once = coalesce(segments, min_segment=20.0)
    twice = coalesce(list(once), min_segment=20.0)
    assert [(s.start, s.end, s.key) for s in once] == \
           [(s.start, s.end, s.key) for s in twice]


async def test_a_refusal_does_not_spend_the_answer_budget():
    """Being turned away is not a failed attempt at an answer.

    `max_attempts` is 4. A probe refused six times and then answered must
    still return that answer: the old loop counted refusals as attempts, so a
    set analysed during a busy hour abandoned probes it would have got a
    minute later.
    """
    from src.identify._http import RateLimited
    from src.identify.shazam import ShazamIdentifier

    ident = ShazamIdentifier(concurrency=1, max_attempts=4,
                             rate_limit_attempts=8)
    ident._gate = _RateGate(base=0.01, ceiling=0.05)

    calls = {"n": 0}

    async def recognize(_bytes):
        calls["n"] += 1
        if calls["n"] <= 6:
            raise RateLimited("429")
        return {"track": {"title": "Reload", "subtitle": "Ben Kim"}}

    ident._shazam.recognize = recognize
    match = await ident.identify(b"probe")

    assert match is not None, "gave up on a probe that would have answered"
    assert match.title == "Reload"
    assert ident.lost_to_rate_limit == 0


async def test_a_probe_is_eventually_given_up_on():
    """The budget is generous, not infinite — an analysis has to finish."""
    from src.identify._http import RateLimited
    from src.identify.shazam import ShazamIdentifier

    ident = ShazamIdentifier(concurrency=1, max_attempts=4,
                             rate_limit_attempts=3)
    ident._gate = _RateGate(base=0.01, ceiling=0.05)

    async def always_refuse(_bytes):
        raise RateLimited("429")

    ident._shazam.recognize = always_refuse

    assert await ident.identify(b"probe") is None
    assert ident.lost_to_rate_limit == 1, "a lost probe must be countable"
