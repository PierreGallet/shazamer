#!/usr/bin/env python3
"""Command-line entry point.

Thin wrapper over `core.pipeline` — the CLI and the web API now run exactly the
same code. The previous version had the web path re-implement boundary
detection to emit progress, and the copy silently dropped the parameters that
halved STFT memory, so the deployed path used twice the RAM of the one the
comments described. A progress callback removes the need for the duplicate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from src.core.pipeline import AnalyzeConfig, Pipeline
from src.export import formats as export_formats
from src.identify.shazam import ShazamIdentifier

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Identify the tracks in a DJ set or long mix.")
    parser.add_argument("input_file", help="Audio file to analyse")
    parser.add_argument("-o", "--output",
                        help="Output path (default: outputs/<name>_tracklist.json)")
    parser.add_argument("--strategy", choices=["grid", "spectral"], default="grid",
                        help="grid: probe on a fixed cadence and merge (default, "
                             "better on beatmatched sets). spectral: detect "
                             "boundaries first (better on hard-cut compilations).")
    parser.add_argument("--interval", type=float,
                        help="Seconds between probes (default: from set length)")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Parallel identification requests (default: 8)")
    parser.add_argument("--probe-duration", type=float, default=12.0,
                        help="Seconds per probe; Shazam uses a centred 10s of it")
    parser.add_argument("--no-musical-features", action="store_true",
                        help="Skip BPM and key detection (faster)")
    parser.add_argument("--formats", default="json,txt",
                        help="Comma-separated: "
                             + ",".join(export_formats.EXPORTERS))
    parser.add_argument("--min-song-duration", type=float,
                        help="[spectral strategy] minimum song length in seconds")
    parser.add_argument("--threshold", type=float,
                        help="[spectral strategy] peak sensitivity, 0-1")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress")
    return parser


async def main() -> int:
    args = build_parser().parse_args()

    source = Path(args.input_file)
    if not source.exists():
        logger.error("File not found: %s", source)
        return 1

    config = AnalyzeConfig(
        strategy=args.strategy,
        probe_interval=args.interval,
        probe_duration=args.probe_duration,
        concurrency=args.concurrency,
        compute_musical_features=not args.no_musical_features,
        min_song_duration=args.min_song_duration,
        peak_threshold=args.threshold,
    )
    pipeline = Pipeline(ShazamIdentifier(concurrency=args.concurrency), config)

    last = [-1]

    def on_progress(stage: str, pct: int, message: str) -> None:
        if args.quiet or pct == last[0]:
            return
        last[0] = pct
        sys.stderr.write(f"\r\033[K[{pct:3d}%] {message}")
        sys.stderr.flush()

    result = await pipeline.run(str(source), on_progress=on_progress)
    if not args.quiet:
        sys.stderr.write("\n")

    payload = result.to_dict()
    base = Path(args.output).with_suffix("") if args.output else \
        _unique_base(Path("outputs") / f"{source.stem}_tracklist")

    written = []
    for fmt in [f.strip() for f in args.formats.split(",") if f.strip()]:
        if fmt not in export_formats.EXPORTERS:
            logger.warning("Unknown format %r, skipping", fmt)
            continue
        fn, _, ext = export_formats.EXPORTERS[fmt]
        if fmt == "rekordbox":
            body = fn(payload, source.stem, audio_path=str(source.resolve()))
        elif fmt == "m3u":
            body = fn(payload, source.stem)
        else:
            body = fn(payload)
        path = base.with_suffix(f".{ext}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        written.append(path)

    _print_summary(result, written)
    return 0


def _unique_base(base: Path) -> Path:
    if not any(base.with_suffix(f".{e}").exists()
               for _, _, e in export_formats.EXPORTERS.values()):
        return base
    counter = 1
    while True:
        candidate = base.with_name(f"{base.name}({counter})")
        if not any(candidate.with_suffix(f".{e}").exists()
                   for _, _, e in export_formats.EXPORTERS.values()):
            return candidate
        counter += 1


def _print_summary(result, written) -> None:
    stats = result.stats
    print(f"\n{stats['identified']} tracks identified "
          f"({stats['unidentified']} unidentified) across "
          f"{stats['segments']} segments — "
          f"{stats['coverage'] * 100:.0f}% of the set covered, "
          f"in {stats['elapsed_seconds']:.0f}s")
    print("-" * 78)
    for track in result.tracks:
        if not track.identified:
            print(f"[{track.start_label}] ID ?")
            continue
        extras = []
        if track.bpm:
            extras.append(f"{track.bpm:.0f}")
        if track.camelot:
            extras.append(track.camelot)
        tail = f"  ({' · '.join(extras)})" if extras else ""
        print(f"[{track.start_label}] {track.artist} — {track.title}{tail}")
    print("-" * 78)
    for path in written:
        print(f"  {path}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
