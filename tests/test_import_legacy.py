"""Importing pre-1.0 tracklists.

The converter is pure, so it is tested directly rather than through the file
system. What matters is that nothing is invented: derived values must be
derived, and what was never recorded must stay absent rather than being
filled in with a plausible-looking default.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from tests.conftest import TEST_USER

_spec = importlib.util.spec_from_file_location(
    "import_legacy",
    Path(__file__).resolve().parent.parent / "scripts" / "import_legacy.py",
)
import_legacy = importlib.util.module_from_spec(_spec)
sys.modules["import_legacy"] = import_legacy
_spec.loader.exec_module(import_legacy)


LEGACY = [
    {"title": "Flim Flam", "artist": "Yellow Sox", "start_time": "00:00:00",
     "start_time_seconds": 0.0, "shazam_url": "https://shazam/1", "match_count": 1},
    {"title": "Lovelee Dae (Bicep Remix)", "artist": "Blaze & Bicep",
     "start_time": "00:02:00", "start_time_seconds": 120.0,
     "shazam_url": "https://shazam/2", "match_count": 3},
    {"title": "Klong", "artist": "Paul C", "start_time": "00:05:00",
     "start_time_seconds": 300.0, "shazam_url": "", "match_count": 9},
]


@pytest.fixture
def legacy_file(tmp_path):
    import json
    path = tmp_path / "20251229_024213_My Set_tracklist.json"
    path.write_text(json.dumps(LEGACY), encoding="utf-8")
    return path


def test_converts_every_entry(legacy_file):
    result = import_legacy.convert(legacy_file)
    assert result is not None
    assert len(result["tracks"]) == 3
    assert all(t["identified"] for t in result["tracks"])


def test_segment_ends_come_from_the_next_start(legacy_file):
    tracks = import_legacy.convert(legacy_file)["tracks"]
    assert tracks[0]["start"] == 0.0 and tracks[0]["end"] == 120.0
    assert tracks[1]["start"] == 120.0 and tracks[1]["end"] == 300.0
    # The last one has no successor, so it gets the median gap: the gaps are
    # 120 s and 180 s, median 150, so it runs 300 → 450.
    assert tracks[2]["end"] == pytest.approx(450.0)


def test_keys_are_normalised_so_recurrence_works(legacy_file):
    """The main reason to import: linking to sets analysed after 1.0."""
    from src.identify.base import normalize_key

    tracks = import_legacy.convert(legacy_file)["tracks"]
    assert tracks[1]["key"] == normalize_key("Blaze & Bicep",
                                             "Lovelee Dae (Bicep Remix)")
    assert all(t["key"] for t in tracks)


def test_nothing_is_invented(legacy_file):
    """Values that were never computed must stay empty, not be guessed."""
    result = import_legacy.convert(legacy_file)
    assert result["waveform"] == []
    for track in result["tracks"]:
        assert track["bpm"] is None
        assert track["camelot"] is None
        # match_count was never a confidence, so it is not reused as one.
        assert track["confidence"] == 0.0
    assert result["stats"]["strategy"] == "legacy"
    assert result["stats"]["imported"] is True


def test_entries_out_of_order_are_sorted(tmp_path):
    import json
    path = tmp_path / "x_tracklist.json"
    path.write_text(json.dumps(list(reversed(LEGACY))), encoding="utf-8")
    starts = [t["start"] for t in import_legacy.convert(path)["tracks"]]
    assert starts == sorted(starts)


@pytest.mark.parametrize("content", ["[]", "{}", "not json"])
def test_unusable_files_are_skipped_not_fatal(tmp_path, content):
    path = tmp_path / "bad_tracklist.json"
    path.write_text(content, encoding="utf-8")
    assert import_legacy.convert(path) is None


def test_title_drops_the_old_timestamp_prefix(legacy_file):
    assert import_legacy.title_for(legacy_file) == "My Set"


def test_set_id_is_stable_so_reruns_replace(legacy_file, tmp_path):
    other = tmp_path / "20251229_024213_My Set_tracklist.json"
    assert import_legacy.set_id_for(legacy_file) == import_legacy.set_id_for(other)
    assert import_legacy.set_id_for(legacy_file).startswith("legacy-")


async def test_imported_set_reads_back_and_exports(legacy_file, tmp_path):
    """An imported set must behave like any other in the library."""
    from src.export import formats
    from src.store.library import Library

    library = Library(tmp_path / "lib.db")
    payload = import_legacy.convert(legacy_file)
    await library.save_set(import_legacy.set_id_for(legacy_file), "My Set",
                           payload, source_kind="legacy",
                           created_at="2025-12-29T02:42:13+00:00", user_id=TEST_USER)

    stored = await library.get_set(import_legacy.set_id_for(legacy_file), user_id=TEST_USER)
    assert stored["created_at"].startswith("2025-12-29"), "original date lost"
    assert len(stored["tracks"]) == 3
    assert "Flim Flam" in formats.to_text(stored)
    assert "Flim Flam" in formats.to_rekordbox_xml(stored, "My Set")
