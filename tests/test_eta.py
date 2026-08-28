"""The countdown, and why it used to lie.

Progress is one 0-100 bar across stages whose cost per point differs by an
order of magnitude. Extrapolating the current stage's rate across everything
left promised three minutes at 79% of a run with nine and a half to go.
"""
import pytest

from src.tasks import Task, confirm_weight, _weighted

pytestmark = pytest.mark.anyio


def test_confirm_weight_matches_what_was_measured():
    """152 confirmation probes against 128 identification probes.

    Measured on a real 75-minute set: identification took 374 s for 128 probes
    and confirmation 444 s, which at the same 2.9 s per probe is about 152 of
    them. The ratio comes out at 10.4x purely from how densely each stage packs
    probes into its share of the bar — 5 points against 44.
    """
    assert confirm_weight(128, 152) == pytest.approx(10.4, abs=0.2)


def test_confirmation_costs_nothing_when_there_is_nothing_to_confirm():
    """The stage is skipped, and the bar should not budget for it.

    This is the case that killed the first attempt at this fix: a fixed weight
    of 10.5 in a table replayed against a run with little to confirm and missed
    by 215%.
    """
    assert confirm_weight(128, 0) < 0.1


def test_confirm_weight_scales_with_the_work_actually_planned():
    light, heavy = confirm_weight(128, 10), confirm_weight(128, 200)
    assert light < 1.0 < heavy
    assert heavy / light == pytest.approx(20.0, rel=0.01)


def test_weighted_progress_is_monotonic_and_ordered():
    """It stands in for elapsed work, so it must never go backwards."""
    for cost in (None, 0.05, 10.4):
        values = [_weighted(p, cost) for p in range(0, 101)]
        assert values == sorted(values), cost
        assert values[0] == 0.0


def test_the_bar_is_uneven_and_the_model_says_so():
    """81% of the bar is not 81% of the work when confirmation is heavy."""
    cost = confirm_weight(128, 152)
    done_at_81 = _weighted(81, cost) / _weighted(100.0, cost)
    assert done_at_81 < 0.6, (
        f"at 81% the model thinks {done_at_81:.0%} of the work is done; "
        "the whole point is that confirmation still lies ahead")


async def test_the_estimate_accounts_for_expensive_stages_ahead():
    """Two runs, same rate, differing only in confirmation work ahead.

    The one with 152 confirmation probes queued must promise more time than
    the one with none, at identical progress and identical observed speed.
    """
    def run(confirm_probes):
        task = Task("t")
        task.confirm_cost = confirm_weight(128, confirm_probes)
        # Same observed history: 10 points of progress per sample.
        base = 1000.0
        import src.tasks as tasks_mod
        real = tasks_mod.time.monotonic
        try:
            for i, pct in enumerate((50, 60, 70, 79)):
                tasks_mod.time.monotonic = lambda t=base + i * 60: t
                task.observe(pct)
        finally:
            tasks_mod.time.monotonic = real
        return task.eta_seconds

    heavy, light = run(152), run(0)
    assert heavy is not None and light is not None
    assert heavy > light * 2, (
        f"confirmation work ahead barely moved the estimate: "
        f"{light}s with none queued, {heavy}s with 152 probes queued")
