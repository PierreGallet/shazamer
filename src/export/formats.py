"""Tracklist exports.

The Rekordbox XML is the one that matters. A tracklist you read is a
curiosity; a tracklist that lands in your DJ software as a playlist with BPM
and key already filled in is a tool. Rekordbox reads this format natively
(Preferences → Advanced → Database → rekordbox.xml), and Traktor and Serato
both import it.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List
from xml.etree import ElementTree as ET


def to_json(result: Dict[str, Any]) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False)


def to_text(result: Dict[str, Any], *, include_unidentified: bool = True) -> str:
    """The classic tracklist, close to what people paste under a mix."""
    lines: List[str] = []
    for track in result.get("tracks", []):
        if not track.get("identified"):
            if not include_unidentified:
                continue
            lines.append(f"{track['start_label']} - ID ?")
            continue
        extras = []
        if track.get("bpm"):
            extras.append(f"{track['bpm']:.0f} BPM")
        if track.get("camelot"):
            extras.append(track["camelot"])
        if track.get("label"):
            extras.append(track["label"])
        suffix = f"  [{' · '.join(extras)}]" if extras else ""
        lines.append(
            f"{track['start_label']} - {track['artist']} - {track['title']}{suffix}")
    return "\n".join(lines) + "\n"


def to_csv(result: Dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["#", "start", "artist", "title", "album", "label", "year",
                     "genre", "bpm", "key", "camelot", "confidence", "url"])
    for track in result.get("tracks", []):
        writer.writerow([
            track.get("index", ""), track.get("start_label", ""),
            track.get("artist", ""), track.get("title", ""),
            track.get("album", ""), track.get("label", ""), track.get("year", ""),
            track.get("genre", ""), track.get("bpm") or "",
            track.get("musical_key") or "", track.get("camelot") or "",
            track.get("confidence", ""), track.get("url", ""),
        ])
    return buffer.getvalue()


def to_m3u(result: Dict[str, Any], title: str = "Tracklist") -> str:
    """Extended M3U. Durations are per-segment, which is what you heard."""
    lines = ["#EXTM3U", f"#PLAYLIST:{title}"]
    for track in result.get("tracks", []):
        if not track.get("identified"):
            continue
        seconds = int(track.get("duration") or 0)
        lines.append(f"#EXTINF:{seconds},{track['artist']} - {track['title']}")
        lines.append(track.get("url") or f"# {track['artist']} - {track['title']}")
    return "\n".join(lines) + "\n"


def to_rekordbox_xml(result: Dict[str, Any], playlist_name: str,
                     *, audio_path: str = "") -> str:
    """Rekordbox collection XML with BPM and Tonality pre-filled.

    `Location` must be a file:// URL. When we hold the set's audio we point
    every entry at it — Rekordbox then shows one long file with the tracks as
    a playlist, which is the honest representation: you own the mix, not the
    individual releases.
    """
    root = ET.Element("DJ_PLAYLISTS", {"Version": "1.0.0"})
    ET.SubElement(root, "PRODUCT", {
        "Name": "Shazamer", "Version": "1.0.0", "Company": "shazamer"})

    tracks = [t for t in result.get("tracks", []) if t.get("identified")]
    collection = ET.SubElement(root, "COLLECTION", {"Entries": str(len(tracks))})

    location = ""
    if audio_path:
        location = Path(audio_path).resolve().as_uri().replace(
            "file://", "file://localhost", 1)

    for i, track in enumerate(tracks, start=1):
        attrs = {
            "TrackID": str(i),
            "Name": track.get("title", ""),
            "Artist": track.get("artist", ""),
            "Album": track.get("album", ""),
            "Genre": track.get("genre", ""),
            "Kind": "MP3 File",
            "TotalTime": str(int(track.get("duration") or 0)),
            "Location": location,
        }
        if track.get("bpm"):
            attrs["AverageBpm"] = f"{float(track['bpm']):.2f}"
        if track.get("camelot"):
            attrs["Tonality"] = track["camelot"]
        if track.get("label"):
            attrs["Label"] = track["label"]
        if track.get("year"):
            year = "".join(ch for ch in str(track["year"]) if ch.isdigit())[:4]
            if year:
                attrs["Year"] = year
        # Where in the mix this track starts — preserved as a memory cue.
        ET.SubElement(collection, "TRACK", attrs)

    playlists = ET.SubElement(root, "PLAYLISTS")
    root_node = ET.SubElement(playlists, "NODE", {
        "Type": "0", "Name": "ROOT", "Count": "1"})
    node = ET.SubElement(root_node, "NODE", {
        "Name": playlist_name, "Type": "1", "KeyType": "0",
        "Entries": str(len(tracks))})
    for i in range(1, len(tracks) + 1):
        ET.SubElement(node, "TRACK", {"Key": str(i)})

    ET.indent(root, space="  ")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(root, encoding="unicode"))


EXPORTERS = {
    "json": (to_json, "application/json", "json"),
    "txt": (to_text, "text/plain; charset=utf-8", "txt"),
    "csv": (to_csv, "text/csv; charset=utf-8", "csv"),
    "m3u": (to_m3u, "audio/x-mpegurl", "m3u8"),
    "rekordbox": (to_rekordbox_xml, "application/xml", "xml"),
}
