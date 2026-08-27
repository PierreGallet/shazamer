"""Tracklist exports, Rekordbox in particular."""
from xml.etree import ElementTree as ET

import pytest

from src.export import formats

RESULT = {
    "duration": 3600.0,
    "stats": {"identified": 2},
    "tracks": [
        {"index": 1, "start": 0, "end": 300, "start_label": "00:00:00",
         "duration": 300, "identified": True, "title": "Loose Lips",
         "artist": "Chris Stussy", "album": "Up The Stuss",
         "label": "Stuss Records", "year": "2023", "genre": "Dance",
         "bpm": 126.4, "camelot": "8A", "musical_key": "A min",
         "confidence": 1.0, "url": "https://shazam/1", "isrc": "GB1"},
        {"index": 2, "start": 300, "end": 480, "start_label": "00:05:00",
         "duration": 180, "identified": False, "title": "ID ?", "artist": "",
         "album": "", "label": "", "year": "", "genre": "", "bpm": None,
         "camelot": None, "musical_key": None, "confidence": 0.0, "url": ""},
        {"index": 3, "start": 480, "end": 900, "start_label": "00:08:00",
         "duration": 420, "identified": True, "title": "So U Kno",
         "artist": "Overmono", "album": "", "label": "XL", "year": "2021",
         "genre": "Electronic", "bpm": 132.0, "camelot": "3B",
         "musical_key": "C# maj", "confidence": 0.75, "url": ""},
    ],
}


def test_text_export_lists_gaps_too():
    text = formats.to_text(RESULT)
    lines = text.strip().splitlines()
    assert len(lines) == 3
    assert "Chris Stussy - Loose Lips" in lines[0]
    assert "126 BPM" in lines[0] and "8A" in lines[0]
    assert lines[1] == "00:05:00 - ID ?"


def test_text_export_can_hide_gaps():
    text = formats.to_text(RESULT, include_unidentified=False)
    assert "ID ?" not in text
    assert len(text.strip().splitlines()) == 2


def test_csv_has_a_header_and_one_row_per_segment():
    rows = formats.to_csv(RESULT).strip().splitlines()
    assert rows[0].startswith("#,start,artist,title")
    assert len(rows) == 4


def test_m3u_skips_unidentified_entries():
    m3u = formats.to_m3u(RESULT, "My Set")
    assert m3u.startswith("#EXTM3U")
    assert "#PLAYLIST:My Set" in m3u
    assert m3u.count("#EXTINF") == 2


def test_rekordbox_xml_is_wellformed_with_bpm_and_key():
    xml = formats.to_rekordbox_xml(RESULT, "Paradise City")
    root = ET.fromstring(xml)

    collection = root.find("COLLECTION")
    assert collection is not None
    assert collection.get("Entries") == "2", "only identified tracks belong"

    first = collection.find("TRACK")
    assert first is not None
    assert first.get("Name") == "Loose Lips"
    assert first.get("Artist") == "Chris Stussy"
    assert first.get("AverageBpm") == "126.40"
    assert first.get("Tonality") == "8A", "Rekordbox reads Camelot here"
    assert first.get("Year") == "2023"
    assert first.get("TotalTime") == "300"

    playlist = root.find("./PLAYLISTS/NODE/NODE")
    assert playlist is not None
    assert playlist.get("Name") == "Paradise City"
    assert playlist.get("Entries") == "2"
    assert len(playlist.findall("TRACK")) == 2


def test_rekordbox_location_is_a_file_uri(tmp_path):
    audio = tmp_path / "set.mp3"
    audio.write_bytes(b"\x00")
    xml = formats.to_rekordbox_xml(RESULT, "Set", audio_path=str(audio))
    location = ET.fromstring(xml).find("./COLLECTION/TRACK").get("Location")
    assert location.startswith("file://localhost/")
    assert location.endswith("set.mp3")


def test_rekordbox_survives_a_messy_year():
    payload = {**RESULT, "tracks": [{**RESULT["tracks"][0], "year": "circa 1998!"}]}
    track = ET.fromstring(
        formats.to_rekordbox_xml(payload, "Set")).find("./COLLECTION/TRACK")
    assert track.get("Year") == "1998"


def test_exports_handle_an_empty_result():
    empty = {"duration": 0, "tracks": [], "stats": {}}
    assert formats.to_text(empty) == "\n"
    assert formats.to_csv(empty).strip().splitlines()[0].startswith("#,start")
    root = ET.fromstring(formats.to_rekordbox_xml(empty, "Nothing"))
    assert root.find("COLLECTION").get("Entries") == "0"


@pytest.mark.parametrize("fmt", sorted(formats.EXPORTERS))
def test_every_registered_format_is_callable(fmt):
    fn, media_type, extension = formats.EXPORTERS[fmt]
    assert callable(fn) and media_type and extension
