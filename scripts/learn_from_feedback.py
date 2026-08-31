#!/usr/bin/env python3
"""What the verdicts say about which identifications to trust.

This reproduces, from stored labels, the thing that actually improved the
algorithm once: a person said which findings on one set were real and which
were invented, the segments' properties were laid side by side, and the
difference between the two groups turned out to be obvious. That difference
became a rule.

It found this, on a fifty-eight second reel:

    span    share of a 12s probe window   verdict
     8.3s              70%                real
     6.0s              50%                invented
    20.8s             173%                real
     5.0s              42%                invented
     3.4s              28%                invented
    14.9s             124%                real

Every invented answer sat under half a probe window. A segment shorter than
the window is named on evidence that is mostly about its neighbours, so the
name is not evidence about the segment — which is a reason, not a fitted
threshold, and that is why it was worth acting on.

What this does NOT do is retrain anything. The identification is Shazam's and
cannot be corrected from here. What labels buy is the ability to *measure* our
own heuristics instead of guessing at them.

    python scripts/learn_from_feedback.py
    python scripts/learn_from_feedback.py --probe-window 12
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.store.library import Library  # noqa: E402


def _field(row: Dict[str, Any], name: str) -> float:
    """A column that may predate the verdict being read.

    Labels recorded before the evidence count was stored have no `probes`, and
    a missing value must read as "not recorded" rather than as zero — a zero
    would say a track had no probes behind it, which is a different and false
    claim.
    """
    try:
        value = row[name]
    except (KeyError, IndexError):
        return 0.0
    return float(value or 0)


def _summarise(values: Sequence[float]) -> str:
    if not values:
        return "—"
    ordered = sorted(values)
    middle = ordered[len(ordered) // 2]
    return f"{min(ordered):.2f} … {middle:.2f} … {max(ordered):.2f}"


def _separation(right: Sequence[float], wrong: Sequence[float]) -> float:
    """How cleanly a feature splits the two groups, 0 to 1.

    The share of (right, wrong) pairs that are the right way round — which is
    the area under the ROC curve, computed the plain way because these are
    tens of points, not millions. 1.0 means every real finding scores above
    every invented one; 0.5 means the feature says nothing.
    """
    if not right or not wrong:
        return 0.5
    wins = sum(1 for r in right for w in wrong if r > w)
    ties = sum(1 for r in right for w in wrong if r == w)
    return (wins + 0.5 * ties) / (len(right) * len(wrong))


def _best_threshold(right: Sequence[float],
                    wrong: Sequence[float]) -> Tuple[float, int, int]:
    """A cut that keeps the real findings and drops the invented ones.

    Returns (threshold, real kept, invented dropped). Chosen to lose no real
    finding first, and only then to drop as many invented ones as it can:
    deleting somebody's correct track is a worse failure than leaving a wrong
    one on screen, because the wrong one is visible and the deletion is not.
    """
    if not right or not wrong:
        return 0.0, len(right), 0
    best = (min(right), len(right), 0)
    for cut in sorted(set(list(right) + list(wrong))):
        kept = sum(1 for r in right if r >= cut)
        dropped = sum(1 for w in wrong if w < cut)
        if kept == len(right) and dropped > best[2]:
            best = (cut, kept, dropped)
    return best


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--probe-window", type=float, default=12.0,
                        help="Seconds the fingerprinter is handed")
    args = parser.parse_args()

    root = Path(args.data_dir) if args.data_dir else Path("data")
    library = Library(root / "library.db")
    labels = await library.all_feedback()

    if not labels:
        print("No verdicts recorded yet.")
        print("Mark a few tracks right or wrong on a set you know, then run "
              "this again.")
        return 0

    right = [r for r in labels if r["verdict"] == "right"]
    wrong = [r for r in labels if r["verdict"] == "wrong"]
    print(f"{len(labels)} verdict(s): {len(right)} real, {len(wrong)} invented,"
          f" across {len({r['set_id'] for r in labels})} set(s)")

    if not right or not wrong:
        # Said plainly rather than producing a confident-looking table from
        # one side of the question.
        missing = "invented" if right else "real"
        print(f"\nOnly one kind of verdict so far. Nothing can be measured "
              f"until some findings are marked {missing} too — a rule that "
              f"drops every wrong answer also drops some right ones, and with "
              f"no right ones recorded there is no way to see that it did.")
        return 0

    # Ordered by how much mechanism stands behind each one, because that is
    # how ties are broken below. `span / probe window` leads: it is the only
    # feature that names a cause — a segment shorter than what the
    # fingerprinter was handed is named on evidence mostly about its
    # neighbours. `span / set length` is last of the three because a rule
    # phrased as a fraction of the recording cannot hold across a reel and a
    # three-hour set at once.
    features: Dict[str, Callable[[Dict[str, Any]], float]] = {
        "span / probe window": lambda r: float(r["span"] or 0) / args.probe_window,
        "span (s)": lambda r: float(r["span"] or 0),
        "span / set length": lambda r: (float(r["span"] or 0)
                                        / max(float(r["set_duration"] or 1), 1)),
        # Reported, but read the note this script prints about it. `confidence`
        # is votes over probes, so a segment covered by one probe scores 1.0 by
        # construction and the metric reaches its own maximum on both the best
        # and the worst evidence in the set.
        "confidence": lambda r: float(r["confidence"] or 0),
        # The denominator, which is the part that was missing. Whether 1.0 from
        # four probes behaves like 1.0 from one is the question six labels from
        # a single reel could not answer, and it is now recorded.
        "probes": lambda r: float(_field(r, "probes")),
        "votes": lambda r: float(_field(r, "votes")),
        "strength rank": lambda r: {"weak": 1.0, "medium": 2.0,
                                    "strong": 3.0}.get(r["strength"] or "", 0.0),
        "start (s)": lambda r: float(r["start"] or 0),
    }

    print(f"\n{'feature':22} {'real':>22} {'invented':>22}   sep   rule")
    ranked: List[Tuple[float, int, str, str]] = []
    for order, (name, extract) in enumerate(features.items()):
        r_values = [extract(r) for r in right]
        w_values = [extract(r) for r in wrong]
        sep = _separation(r_values, w_values)
        cut, kept, dropped = _best_threshold(r_values, w_values)
        rule = (f"≥ {cut:.2f} keeps {kept}/{len(right)}, drops "
                f"{dropped}/{len(wrong)}") if dropped else "no clean cut"
        print(f"  {name:20} {_summarise(r_values):>22} "
              f"{_summarise(w_values):>22}  {sep:.2f}  {rule}")
        ranked.append((sep, order, name, rule))

    # Ties broken by declaration order, which runs from the features with a
    # mechanism behind them to the ones that are merely available. With six
    # labels three features separate perfectly, and picking between them
    # alphabetically once nominated `span / set length` — the one that cannot
    # generalise, since it says a track is real if it fills a seventh of the
    # recording, which is true of a reel and false of every three-hour set.
    ranked.sort(key=lambda row: (-row[0], row[1]))
    top_sep, _order, top_name, top_rule = ranked[0]
    tied = [row[2] for row in ranked if row[0] == top_sep]
    print()
    if top_sep >= 0.9 and "no clean cut" not in top_rule:
        print(f"Strongest signal: {top_name} ({top_sep:.2f}). {top_rule}.")
        if len(tied) > 1:
            others = ", ".join(n for n in tied if n != top_name)
            print(f"Separates no better than {others} — too few labels to "
                  f"tell them apart.")
        print("Worth acting on only if there is a *reason* it separates —")
        print("a threshold fitted to a handful of points is a coincidence "
              "until it has one.")
    else:
        print(f"Nothing separates cleanly yet (best: {top_name} at "
              f"{top_sep:.2f}).")
        print("More verdicts, or the difference is not in these features.")

    single = sum(1 for r in labels if _field(r, "probes") == 1)
    if single:
        print(f"\n{single} of {len(labels)} labelled segments rest on a single "
              f"probe, and those score 1.00 on confidence by construction — a "
              f"lone probe agrees with itself. Read the confidence row above "
              f"against the probes row, never on its own.")

    if len(labels) < 20:
        print(f"\n{len(labels)} labels is few. Treat all of the above as a "
              f"hypothesis to check, not a finding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
