"""Timestamp formatting, shared by everything that displays a position.

Its own module because three layers need it — the pipeline when it builds a
tracklist, the store when it reads one back, and the exporters — and none of
them should have to import either of the others to get it. Leaving it in the
pipeline is what let a set loaded from the library reach the exporters without
a `start_label` at all.
"""
from __future__ import annotations


def format_timestamp(seconds: float) -> str:
    """Seconds to `hh:mm:ss`. Negative input clamps to zero."""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
