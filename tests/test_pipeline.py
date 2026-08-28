"""Segmentation, merging and the parallelism that makes analysis fast."""
import asyncio
from typing import List

import pytest

from src.core.pipeline import AnalyzeConfig, Pipeline, format_timestamp
from src.core.segment import (ProbeResult, Segment, confirmation_times,
                              grid_probes, merge_probes)

pytestmark = pytest.mark.anyio


async def test_pipeline_recovers_the_planned_tracklist(synthetic_set, stub_identifier):
    result = await Pipeline(
        stub_identifier,
        AnalyzeConfig(probe_interval=10.0, concurrency=6,
                      compute_musical_features=False),
    ).run(synthetic_set["path"])

    identified = [t for t in result.tracks if t.identified]
    titles = [t.title for t in identified]
    assert titles == ["Track A", "Track B", "Track D"]

    # The unidentifiable stretch must survive as a visible gap, not vanish.
    gaps = [t for t in result.tracks if not t.identified]
    assert len(gaps) == 1
    assert gaps[0].duration == pytest.approx(synthetic_set["segment_seconds"], abs=20)


async def test_boundaries_land_near_the_real_transitions(synthetic_set, stub_identifier):
    result = await Pipeline(
        stub_identifier,
        AnalyzeConfig(probe_interval=10.0, compute_musical_features=False),
    ).run(synthetic_set["path"])

    seconds = synthetic_set["segment_seconds"]
    for i, track in enumerate(result.tracks):
        assert track.start == pytest.approx(i * seconds, abs=15), (
            f"segment {i} starts at {track.start:.1f}s, expected ~{i * seconds:.0f}s"
        )


async def test_probes_run_in_parallel(synthetic_set, stub_identifier):
    """The old pipeline awaited one probe at a time behind a 0.5/s throttle."""
    await Pipeline(
        stub_identifier,
        AnalyzeConfig(probe_interval=8.0, concurrency=6,
                      compute_musical_features=False),
    ).run(synthetic_set["path"])

    assert stub_identifier.calls > 6
    assert stub_identifier.max_concurrent > 1, (
        "probes ran strictly sequentially — the concurrency win is gone"
    )


async def test_progress_is_monotonic_and_reaches_a_hundred(synthetic_set,
                                                           stub_identifier):
    seen = []
    await Pipeline(
        stub_identifier,
        AnalyzeConfig(probe_interval=10.0, compute_musical_features=False),
    ).run(synthetic_set["path"], on_progress=lambda s, p, m: seen.append((s, p, m)))

    percentages = [p for _, p, _ in seen]
    assert percentages == sorted(percentages), "progress went backwards"
    assert percentages[-1] == 100
    assert {"decoding", "identifying", "merging"} <= {s for s, _, _ in seen}


async def test_progress_callback_errors_do_not_break_the_run(synthetic_set,
                                                             stub_identifier):
    def explode(stage, pct, message):
        raise RuntimeError("consumer blew up")

    result = await Pipeline(
        stub_identifier,
        AnalyzeConfig(probe_interval=15.0, compute_musical_features=False),
    ).run(synthetic_set["path"], on_progress=explode)
    assert result.stats["identified"] >= 1


async def test_musical_features_are_attached(synthetic_set, stub_identifier):
    result = await Pipeline(
        stub_identifier,
        AnalyzeConfig(probe_interval=10.0, compute_musical_features=True),
    ).run(synthetic_set["path"])

    identified = [t for t in result.tracks if t.identified]
    assert any(t.bpm for t in identified), "no BPM detected on any track"
    assert any(t.camelot for t in identified), "no key detected on any track"
    for track in identified:
        if track.camelot:
            assert track.camelot[-1] in ("A", "B")
            assert 1 <= int(track.camelot[:-1]) <= 12


def test_grid_probes_stay_inside_the_set():
    probes = grid_probes(600, interval=30, edge_margin=10)
    assert probes[0] >= 10
    assert probes[-1] <= 590
    assert all(b > a for a, b in zip(probes, probes[1:]))


def test_grid_probes_handle_a_clip_shorter_than_the_margin():
    assert grid_probes(6, interval=30, edge_margin=10) == [3.0]
    assert grid_probes(0) == []


def test_merge_collapses_consecutive_matches():
    probes = [
        ProbeResult(0, "a::x", {"title": "X", "artist": "A"}),
        ProbeResult(20, "a::x", {"title": "X", "artist": "A"}),
        ProbeResult(40, "a::x", {"title": "X", "artist": "A"}),
        ProbeResult(60, "b::y", {"title": "Y", "artist": "B"}),
    ]
    segments = merge_probes(probes, duration=80)
    assert [s.key for s in segments] == ["a::x", "b::y"]
    assert segments[0].probes == 3
    assert segments[0].confidence == 1.0


def test_merge_keeps_unmatched_runs_visible():
    probes = [
        ProbeResult(0, "a::x", {"title": "X", "artist": "A"}),
        ProbeResult(30, None),
        ProbeResult(60, None),
        ProbeResult(90, "b::y", {"title": "Y", "artist": "B"}),
    ]
    segments = merge_probes(probes, duration=120)
    assert [s.identified for s in segments] == [True, False, True]


def test_merge_absorbs_a_single_stray_probe():
    """One odd probe inside a long track must not split it into three."""
    probes = [ProbeResult(t, "a::x", {"title": "X", "artist": "A"})
              for t in (0, 20, 40)]
    probes.insert(2, ProbeResult(30, "z::stray", {"title": "Z", "artist": "Z"}))
    probes += [ProbeResult(60, "a::x", {"title": "X", "artist": "A"})]

    segments = merge_probes(sorted(probes, key=lambda p: p.time),
                            duration=80, min_segment=25)
    assert len(segments) == 1
    assert segments[0].key == "a::x"


def test_merge_of_nothing_is_nothing():
    assert merge_probes([], duration=100) == []


@pytest.mark.parametrize("seconds,expected", [
    (0, "00:00:00"), (61, "00:01:01"), (3661, "01:01:01"), (-5, "00:00:00"),
])
def test_timestamp_formatting(seconds, expected):
    assert format_timestamp(seconds) == expected


def test_merge_keeps_a_genuine_short_interlude():
    """A brief segment between two *different* tracks is real, not a stray.

    This is the counterpart to the stray test: the absorption rule must key on
    the neighbours agreeing, not merely on the segment being short, or every
    short interlude in a set would be silently deleted.
    """
    probes = [
        ProbeResult(0, "a::x", {"title": "X", "artist": "A"}),
        ProbeResult(20, "a::x", {"title": "X", "artist": "A"}),
        ProbeResult(40, "i::interlude", {"title": "Interlude", "artist": "I"}),
        ProbeResult(60, "b::y", {"title": "Y", "artist": "B"}),
        ProbeResult(80, "b::y", {"title": "Y", "artist": "B"}),
    ]
    segments = merge_probes(probes, duration=100, min_segment=25)
    assert [s.key for s in segments] == ["a::x", "i::interlude", "b::y"]


async def test_probe_extraction_is_bounded_not_just_identification(
    synthetic_set, stub_identifier, monkeypatch,
):
    """Regression: ffmpeg processes must not scale with set length.

    The identifier's semaphore only guards its HTTP call, so gathering over
    every probe used to spawn one ffmpeg per probe immediately — all alive at
    once while they queued for a slot. A 30 minute set opened ~95 processes
    and survived; a three hour set opened ~430 and the container was
    OOM-killed in production.

    What matters is the count of *concurrent extractions*, so that is what is
    measured here rather than the identifier's own concurrency.
    """
    import src.core.audio as audio_io

    concurrent = 0
    peak = 0

    async def counting_probe(path, start, duration=12.0):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        try:
            await asyncio.sleep(0.02)
            return f"T={start:<28.3f}".encode()[:30]
        finally:
            concurrent -= 1

    monkeypatch.setattr(audio_io, "extract_probe", counting_probe)

    limit = 4
    await Pipeline(
        stub_identifier,
        AnalyzeConfig(probe_interval=4.0, concurrency=limit,
                      compute_musical_features=False),
    ).run(synthetic_set["path"])

    assert stub_identifier.calls > limit, "not enough probes to prove anything"
    assert peak <= limit, (
        f"{peak} extractions ran at once with concurrency={limit} — "
        "ffmpeg spawning is unbounded again"
    )


async def test_musical_feature_extraction_is_bounded_too(synthetic_set,
                                                         stub_identifier,
                                                         monkeypatch):
    """The BPM/key pass opens its own ffmpeg per track; bound that as well."""
    import src.core.audio as audio_io

    concurrent = 0
    peak = 0
    real = audio_io.extract_pcm

    async def counting_pcm(path, start, duration, sample_rate=22050):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        try:
            return await real(path, start, duration, sample_rate)
        finally:
            concurrent -= 1

    monkeypatch.setattr(audio_io, "extract_pcm", counting_pcm)

    limit = 4
    await Pipeline(
        stub_identifier,
        AnalyzeConfig(probe_interval=10.0, concurrency=limit,
                      compute_musical_features=True),
    ).run(synthetic_set["path"])

    assert peak <= max(2, limit // 2), (
        f"{peak} PCM extractions ran at once — the BPM/key pass is unbounded"
    )


async def test_the_event_loop_stays_responsive_during_analysis(synthetic_set,
                                                               stub_identifier):
    """The server must keep answering while a set is being analysed.

    Feature extraction is librosa doing an STFT per block — hundreds of
    milliseconds of solid CPU — and blocks arrive as fast as ffmpeg can decode
    them. Run inline, it pins the event loop for nearly the whole analysis: in
    production the healthcheck timed out and the container was killed as
    unhealthy, taking the analysis with it.

    Measured the way it actually matters: a heartbeat ticks alongside the
    pipeline and the longest gap between ticks is checked. That catches the
    blocking regardless of which stage introduces it.
    """
    gaps: List[float] = []
    stop = False

    async def heartbeat() -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while not stop:
            await asyncio.sleep(0.02)
            now = loop.time()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    try:
        await Pipeline(
            stub_identifier,
            AnalyzeConfig(probe_interval=10.0, concurrency=4,
                          compute_musical_features=True),
        ).run(synthetic_set["path"])
    finally:
        stop = True
        await beat

    worst = max(gaps)
    assert len(gaps) > 20, "heartbeat barely ran — the loop was starved"
    # Generous: the production healthcheck allows 10 s. Anything approaching a
    # second here means CPU work is running on the loop thread again.
    assert worst < 1.0, (
        f"event loop blocked for {worst:.2f}s during analysis — CPU-bound work "
        "is running on the loop thread"
    )


def test_strength_separates_evidence_from_agreement():
    """One probe agreeing with itself is not a strong finding.

    Agreement alone reports 1.0 for a single probe — the weakest possible
    evidence wearing the strongest possible number. `strength` weighs how many
    probes stand behind the answer, which is what the badge in the UI shows.
    """
    def seg(votes: int, probes: int) -> Segment:
        return Segment(start=0, end=300, key="a::b", payload={},
                       votes=votes, probes=probes)

    assert seg(1, 1).confidence == 1.0 and seg(1, 1).strength == "weak"
    assert seg(2, 2).strength == "medium"
    assert seg(3, 3).strength == "strong"
    assert seg(2, 3).strength == "medium"       # one dissenter, still plural
    assert seg(1, 3).strength == "weak"         # outvoted
    assert Segment(start=0, end=10, key=None, payload=None).strength == "none"


def test_confirmation_positions_spread_across_the_segment():
    """Clustered probes re-read the same audio and confirm nothing."""
    segment = Segment(start=0, end=300, key="a::b", payload={}, votes=1, probes=1)
    times = confirmation_times(segment, already=[10.0], wanted=3)

    assert len(times) == 2
    assert all(6 <= t <= 294 for t in times), "probes must avoid the edges"
    assert min(b - a for a, b in zip(times, times[1:])) > 8, "probes clustered"


def test_confirmation_skips_segments_too_short_to_hold_probes():
    segment = Segment(start=0, end=8, key="a::b", payload={}, votes=1, probes=1)
    assert confirmation_times(segment, already=[], wanted=3) == []


def test_confirmation_skips_segments_that_already_have_enough():
    segment = Segment(start=0, end=300, key="a::b", payload={}, votes=4, probes=4)
    assert confirmation_times(segment, already=[], wanted=3) == []


async def test_confirmation_strengthens_a_thinly_probed_track(synthetic_set,
                                                              stub_identifier):
    """A segment found by one probe should end up backed by several."""
    result = await Pipeline(
        stub_identifier,
        AnalyzeConfig(probe_interval=35.0, concurrency=4, votes_per_segment=3,
                      compute_musical_features=False),
    ).run(synthetic_set["path"])

    identified = [t for t in result.tracks if t.identified]
    assert identified, "nothing identified — the fixture changed"
    assert all(t.probes >= 2 for t in identified), (
        "segments left resting on a single probe: "
        f"{[(t.title, t.probes) for t in identified]}"
    )
    assert any(t.strength == "strong" for t in identified)


async def test_confirmation_can_overturn_a_wrong_match(synthetic_set):
    """Extra probes are evidence, not decoration: a majority can flip a segment.

    The stub answers with a wrong track only for the very first probe it sees,
    the way one unlucky window inside a long track can fingerprint as something
    else. Confirmation should catch it.
    """
    from src.identify.base import TrackMatch

    class FlipIdentifier:
        name = "flip"

        def __init__(self):
            self.calls = 0

        async def identify(self, wav_bytes: bytes):
            self.calls += 1
            await asyncio.sleep(0)
            if self.calls == 1:
                return TrackMatch(title="Wrong", artist="Nobody", provider="stub")
            return TrackMatch(title="Right", artist="Somebody", provider="stub")

    result = await Pipeline(
        FlipIdentifier(),
        AnalyzeConfig(probe_interval=60.0, concurrency=1, votes_per_segment=3,
                      compute_musical_features=False, min_segment=5.0),
    ).run(synthetic_set["path"])

    titles = {t.title for t in result.tracks if t.identified}
    assert "Right" in titles
    assert "Wrong" not in titles, "a single bad probe survived confirmation"


async def test_votes_per_segment_of_one_skips_the_pass(synthetic_set,
                                                       stub_identifier):
    """The pass must be genuinely optional, not merely cheap."""
    before = stub_identifier.calls
    result = await Pipeline(
        stub_identifier,
        AnalyzeConfig(probe_interval=20.0, concurrency=4, votes_per_segment=1,
                      compute_musical_features=False),
    ).run(synthetic_set["path"])

    grid = len(grid_probes(result.duration, interval=20.0))
    assert stub_identifier.calls - before == grid, "extra probes were fired"


async def test_the_analysis_reports_where_its_time_went(synthetic_set,
                                                        stub_identifier):
    """Stage timings, because the imbalance between stages is the diagnosis.

    A run that spent two hours identifying and ninety seconds decoding looks
    identical from outside to one that split the time evenly. Working that out
    once meant reading container logs.
    """
    result = await Pipeline(
        stub_identifier,
        AnalyzeConfig(probe_interval=15.0, concurrency=4),
    ).run(synthetic_set["path"])

    timings = result.stats["stage_seconds"]
    assert {"decoding", "identifying"} <= set(timings)
    assert all(v >= 0 for v in timings.values())

    # The parts should account for the whole, give or take rounding.
    total = sum(timings.values())
    assert abs(total - result.stats["elapsed_seconds"]) < 1.0, (
        f"stages sum to {total:.1f}s but the run took "
        f"{result.stats['elapsed_seconds']}s — time is unaccounted for"
    )


def test_a_stage_re_entered_is_summed_not_overwritten():
    """Reporting a stage twice must not hide half its cost."""
    import time

    from src.core.pipeline import StageTimer

    timer = StageTimer()
    timer.enter("identifying")
    time.sleep(0.05)
    timer.enter("merging")
    time.sleep(0.05)
    timer.enter("identifying")      # back to it, as confirmation does
    time.sleep(0.05)

    timings = timer.finish()
    assert timings["identifying"] > timings["merging"], (
        "the second visit to a stage was dropped"
    )


def test_repeated_reports_of_the_same_stage_are_one_entry():
    """Identification reports per probe; that must not fragment the timing."""
    from src.core.pipeline import StageTimer

    timer = StageTimer()
    for _ in range(50):
        timer.enter("identifying")
    assert list(timer.finish()) == ["identifying"]


def test_a_gap_between_two_plays_of_the_same_track_is_bridged():
    """Fingerprinting a mix fails constantly and unevenly.

    A breakdown, a filter sweep, two records overlapping, a passage the
    database does not have — on a set where a quarter of probes match, that
    shreds a track into pieces. One Axwell record came back as three
    thirty-second segments separated by gaps, reading as three separate plays.
    """
    key = "axwell::feel the vibe"
    payload = {"title": "Feel the Vibe", "artist": "Axwell"}
    probes = [
        ProbeResult(700, key, payload),
        ProbeResult(760, None),
        ProbeResult(820, None),
        ProbeResult(880, key, payload),
    ]
    segments = merge_probes(probes, duration=1000)

    identified = [s for s in segments if s.identified]
    assert len(identified) == 1, (
        f"one track came out as {len(identified)} plays: "
        f"{[(s.start, s.end) for s in identified]}"
    )
    assert identified[0].end - identified[0].start > 200


def test_a_different_track_in_between_is_not_bridged():
    """The record really did change; merging would invent a play."""
    a, b = "a::one", "b::two"
    probes = [
        ProbeResult(0, a, {"title": "One", "artist": "A"}),
        ProbeResult(120, b, {"title": "Two", "artist": "B"}),
        ProbeResult(240, a, {"title": "One", "artist": "A"}),
    ]
    keys = [s.key for s in merge_probes(probes, duration=400)]
    assert keys == [a, b, a]


def test_a_long_silence_is_not_bridged():
    """Past a few minutes, the same title twice is two plays, not one.

    The cap is on the gap rather than the total span: a segment starts where
    the previous track ended, which for the first track is the start of the
    file — so a span-based cap rejects ordinary bridges for reasons that have
    nothing to do with the music.
    """
    key = "a::one"
    payload = {"title": "One", "artist": "A"}
    probes = [ProbeResult(600, key, payload)]
    probes += [ProbeResult(t, None) for t in range(660, 1500, 60)]
    probes.append(ProbeResult(1560, key, payload))

    identified = [s for s in merge_probes(probes, duration=1700) if s.identified]
    assert len(identified) == 2, "a fifteen-minute silence was bridged"


def test_a_bridge_is_not_refused_just_for_starting_at_the_beginning():
    """Regression: the first track of a set was never bridged.

    Its segment starts at zero, so any cap measured from the segment start
    counted the whole file rather than the silence being crossed.
    """
    key = "a::one"
    payload = {"title": "One", "artist": "A"}
    probes = [ProbeResult(20, key, payload), ProbeResult(80, None),
              ProbeResult(140, key, payload)]

    identified = [s for s in merge_probes(probes, duration=900) if s.identified]
    assert len(identified) == 1


def test_silence_is_not_dissent():
    """A probe that came back empty says nothing about whether a track is right.

    Counting it as disagreement makes a track established across seven minutes
    look contested, when the silence is just fingerprinting failing on a
    breakdown.
    """
    established = Segment(start=0, end=600, key="a::b", payload={},
                          votes=3, probes=10, matched=3)
    assert established.agreement == 1.0
    assert established.strength == "strong"

    contested = Segment(start=0, end=600, key="a::b", payload={},
                        votes=1, probes=3, matched=3)
    assert contested.agreement < 0.5
    assert contested.strength == "weak"


def test_confidence_still_reports_how_much_was_recognised():
    """The two numbers answer different questions and both are worth keeping."""
    segment = Segment(start=0, end=600, key="a::b", payload={},
                      votes=3, probes=10, matched=3)
    assert segment.confidence == 0.3, "confidence should be share of all probes"
    assert segment.agreement == 1.0, "agreement should be share of those that spoke"


async def test_confirmation_does_not_leave_a_track_playing_twice():
    """A record must not be reported as two consecutive plays of itself.

    Confirmation votes can rename a segment, and when the new name is the one
    both its neighbours already carry, all three are one play. Merging ran
    only *before* confirmation, so nothing put them back together — a
    production set listed the same track at 06:55, 08:04 and 08:33, touching,
    with identical artist and title, and no gap between them to bridge.
    """
    from src.identify.base import TrackMatch

    right = {"title": "Reload", "artist": "Ben Kim"}
    segments = [
        Segment(start=0.0, end=415.5, key="ben kim::reload", payload=right,
                votes=4, probes=4, matched=4),
        # The one that will change hands: a single probe named something else.
        Segment(start=415.5, end=484.6, key="andrea::good vibes",
                payload={"title": "Good Vibes", "artist": "Andrea"},
                votes=1, probes=1, matched=1),
        Segment(start=484.6, end=900.0, key="ben kim::reload", payload=right,
                votes=4, probes=4, matched=4),
    ]

    class AlwaysRight:
        name = "stub"

        async def identify(self, wav_bytes):
            return TrackMatch(title="Reload", artist="Ben Kim", provider="stub")

    async def tagged_probe(path, start, duration=12.0):
        return b"x"

    import src.core.audio as audio_io
    monkey = pytest.MonkeyPatch()
    monkey.setattr(audio_io, "extract_probe", tagged_probe)
    try:
        pipeline = Pipeline(AlwaysRight(),
                            AnalyzeConfig(votes_per_segment=3,
                                          compute_musical_features=False))
        out = await pipeline._confirm_segments(
            "unused.wav", segments,
            [ProbeResult(time=t) for t in (0.0, 100.0, 200.0, 450.0, 600.0)],
            lambda *a, **k: None)
    finally:
        monkey.undo()

    keys = [s.key for s in out]
    assert keys == ["ben kim::reload"], (
        f"the same record left listed {len(keys)} times in a row: {keys}")
    assert out[0].start == 0.0 and out[0].end == 900.0
