"""Segmentation, merging and the parallelism that makes analysis fast."""
import pytest

from src.core.pipeline import AnalyzeConfig, Pipeline, format_timestamp
from src.core.segment import ProbeResult, grid_probes, merge_probes

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
