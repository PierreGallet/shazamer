"""Fetching audio from a URL, at the best quality the platform actually has.

The quality ladder implemented here follows one rule: **never transcode
upward**. Re-encoding a 160 kbps Opus stream to 320 kbps MP3 produces a file
twice the size carrying strictly less information — the encoder already threw
those bits away and nothing brings them back.

Concretely per platform:

- **SoundCloud** hides a real win. When the uploader enabled downloads, yt-dlp
  exposes a format literally named `download`, which is the *original uploaded
  file* — frequently sourced from a WAV or a master. It is requested first.
- **YouTube** tops out at Opus ~160 kbps (itag 251) or AAC 128 kbps. We keep
  the native stream; a `-x --audio-format mp3 --audio-quality 192` pass, as the
  old code did, spent minutes of CPU to lose quality on a file about to be
  deleted anyway.
- **Analysis never needs a transcode at all** — ffmpeg reads Opus, M4A and
  WebM directly, and downsamples to 16 kHz mono for fingerprinting regardless.

Transcoding is offered separately, on demand, for the DJ-library export path
where a piece of software genuinely cannot read the container.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

# Ordered preference. yt-dlp walks these left to right and takes the first hit.
FORMAT_LADDER = {
    "soundcloud": "download/http_mp3_320/hls_mp3_320/http_mp3_128/hls_mp3_128/bestaudio/best",
    "youtube": "bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio/best",
    "default": "bestaudio/best",
}

_PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")


class DownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    title: str
    duration: float
    extractor: str
    webpage_url: str
    uploader: str = ""
    format_note: str = ""
    abr: Optional[float] = None
    acodec: str = ""
    # yt-dlp's own id for the media: a video, a track, one carousel slide.
    # With the extractor it names the source whatever way the link was
    # written, which is what reusing an analysis keys on (media_key).
    media_id: str = ""

    @property
    def source_key(self) -> str:
        return f"{self.extractor}:{self.media_id}" if self.media_id else ""

    @property
    def quality_label(self) -> str:
        """A short, honest description of what we actually got."""
        if self.acodec and self.abr:
            return f"{self.acodec} {self.abr:.0f} kbps"
        if self.acodec:
            return self.acodec
        return self.format_note or "unknown"


def platform_for(url: str) -> str:
    lowered = url.lower()
    if "soundcloud.com" in lowered:
        return "soundcloud"
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "youtube"
    return "default"


def _instagram_slide(url: str) -> Optional[int]:
    """The carousel slide a link points at, from its `img_index`.

    Instagram writes it into the address bar as you swipe, so a copied link
    names the slide that was on screen. yt-dlp never reads it.
    """
    values = parse_qs(urlparse(url).query).get("img_index", [])
    try:
        slide = int(values[0])
    except (IndexError, ValueError):
        return None
    return slide if slide > 0 else None


def _selection_args(url: str) -> List[str]:
    """Which entries of an Instagram carousel to fetch.

    yt-dlp treats a carousel as a playlist whatever `--no-playlist` says, and
    a photo slide has no formats, so the plain command fails on the first
    photo — and every video it did fetch lands on the same file name.
    """
    if "instagram.com" not in url.lower():
        return []
    slide = _instagram_slide(url)
    if slide:
        return ["--playlist-items", str(slide)]
    # No slide named: skip photos and keep the first video. yt-dlp exits 101
    # once --max-downloads is reached, which is the success case here.
    return ["--ignore-no-formats-error", "--match-filter", "formats",
            "--max-downloads", "1"]


def _ytdlp_cmd() -> List[str]:
    """Prefer the standalone binary, fall back to the module in this venv."""
    binary = shutil.which("yt-dlp")
    return [binary] if binary else [sys.executable, "-m", "yt_dlp"]


async def probe_url(url: str, timeout: float = 60.0) -> dict:
    """Fetch metadata without downloading. Used to validate and preview."""
    cmd = _ytdlp_cmd() + [
        "--dump-single-json", "--no-playlist", "--no-warnings",
        "--flat-playlist", url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DownloadError(f"Timed out reading metadata for {url}")

    if proc.returncode != 0:
        raise DownloadError(_clean_ytdlp_error(err.decode(errors="replace")))
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"Could not parse metadata for {url}: {exc}") from exc


async def download_audio(url: str, dest_dir: Path, stem: str,
                         on_progress: Optional[ProgressFn] = None,
                         timeout: float = 3600.0) -> DownloadedMedia:
    """Download the best native audio stream for `url` into `dest_dir`.

    `stem` should be unique per task — the old implementation globbed on a
    second-resolution timestamp, so two downloads starting in the same second
    could pick up each other's file.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    platform = platform_for(url)
    info_path = dest_dir / f"{stem}.info.json"
    selection = _selection_args(url)

    cmd = _ytdlp_cmd() + [
        "-f", FORMAT_LADDER[platform],
        "--no-playlist",
        "--newline",
        "--no-warnings",
        "--force-ipv4",
        "--write-info-json",
        # A carousel's own info.json shares the output name and would replace
        # the video's.
        "--no-write-playlist-metafiles",
        "--no-part",
        *selection,
        "-o", str(dest_dir / f"{stem}.%(ext)s"),
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    assert proc.stdout is not None and proc.stderr is not None

    stderr_tail: List[str] = []

    async def read_stdout() -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            match = _PROGRESS_RE.search(text)
            if match and on_progress:
                on_progress(float(match.group(1)), "downloading")
            elif "[ExtractAudio]" in text and on_progress:
                on_progress(100.0, "extracting")

    async def read_stderr() -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            stderr_tail.append(line.decode(errors="replace").rstrip())
            del stderr_tail[:-40]

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stdout(), read_stderr(), proc.wait()),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DownloadError(f"Download timed out after {timeout:.0f}s")

    slide = _instagram_slide(url) if selection else None
    ok_codes = (0, 101) if "--max-downloads" in selection else (0,)
    if proc.returncode not in ok_codes:
        stderr = "\n".join(stderr_tail)
        if slide and "no video formats" in stderr.lower():
            raise DownloadError(
                f"Slide {slide} of this Instagram post is a photo, so there is "
                f"no audio to analyse. Open a video slide and copy its link.")
        raise DownloadError(_clean_ytdlp_error(stderr))

    media_files = [
        p for p in dest_dir.glob(f"{stem}.*")
        if p.suffix not in (".json", ".part", ".ytdl")
    ]
    if not media_files and selection and not slide:
        info_path.unlink(missing_ok=True)
        raise DownloadError(
            "This Instagram post holds only photos, so there is no audio to "
            "analyse.")
    if not media_files:
        raise DownloadError(
            "yt-dlp reported success but produced no audio file. The track may "
            "be private, geo-blocked or removed."
        )
    media = max(media_files, key=lambda p: p.stat().st_size)

    info = {}
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
        info_path.unlink(missing_ok=True)

    return DownloadedMedia(
        path=media,
        title=info.get("title") or media.stem,
        duration=float(info.get("duration") or 0),
        extractor=info.get("extractor_key") or platform,
        webpage_url=info.get("webpage_url") or url,
        uploader=info.get("uploader") or info.get("channel") or "",
        format_note=info.get("format_note") or "",
        abr=info.get("abr"),
        acodec=info.get("acodec") or "",
        media_id=str(info.get("id") or ""),
    )


async def media_key(url: str, timeout: float = 60.0) -> Optional[str]:
    """`extractor:id` for what downloading `url` would fetch, without fetching.

    The same key `DownloadedMedia.source_key` stores after a download, so an
    analysis can be found before paying for another one. None whenever it
    cannot be known for sure: a failed lookup, a playlist, or an Instagram
    carousel with no slide named — the download would take its first video,
    which the metadata alone does not say. None only ever means "analyse as
    usual".
    """
    slide = _instagram_slide(url) if "instagram.com" in url.lower() else None
    try:
        if slide:
            cmd = _ytdlp_cmd() + [
                "--dump-json", "--skip-download", "--no-warnings",
                "--ignore-no-formats-error", "--playlist-items", str(slide),
                url,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            lines = [l for l in out.decode(errors="replace").splitlines()
                     if l.strip()]
            info = json.loads(lines[0]) if lines else {}
        else:
            info = await probe_url(url, timeout=timeout)
    except (DownloadError, asyncio.TimeoutError, json.JSONDecodeError,
            OSError) as exc:
        logger.info("No media key for %s: %s", url, exc)
        return None
    if info.get("_type") == "playlist" or not info.get("id"):
        return None
    extractor = info.get("extractor_key") or ""
    return f"{extractor}:{info['id']}" if extractor else None


async def list_channel(url: str, limit: int = 50) -> List[dict]:
    """Enumerate the entries of a channel, artist page or playlist.

    This is what makes "follow this artist" possible: yt-dlp resolves a channel
    URL to a flat list of entries without downloading anything.
    """
    cmd = _ytdlp_cmd() + [
        "--dump-json", "--flat-playlist", "--no-warnings",
        "--playlist-end", str(limit), url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise DownloadError(_clean_ytdlp_error(err.decode(errors="replace")))

    entries = []
    for line in out.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        entries.append({
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "url": item.get("url") or item.get("webpage_url", ""),
            "duration": item.get("duration"),
            "uploader": item.get("uploader") or item.get("channel", ""),
            "thumbnail": item.get("thumbnail", ""),
        })
    return entries


async def transcode(src: Path, dest: Path, bitrate: str = "320k") -> Path:
    """Produce an MP3 copy for DJ software that cannot read the native codec.

    Offered explicitly, never applied silently: this step always loses quality.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-nostdin", "-y",
        "-i", str(src), "-c:a", "libmp3lame", "-b:a", bitrate,
        str(dest),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise DownloadError(
            f"Transcode failed: {err.decode(errors='replace').strip()[-300:]}")
    return dest


def _clean_ytdlp_error(raw: str) -> str:
    """Surface the line that actually explains the failure.

    yt-dlp's stderr is mostly noise; the user needs the one sentence saying
    the video is private, age-gated or region-locked.
    """
    lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
    for line in reversed(lines):
        if "ERROR:" in line:
            message = line.split("ERROR:", 1)[1].strip()
            message = re.sub(r"^\[[^\]]+\]\s*", "", message)
            message = re.sub(r"^[\w-]+:\s*", "", message, count=1)
            return _with_staleness_hint(message or line)
    return _with_staleness_hint(
        lines[-1] if lines else "yt-dlp failed with no output")


# Failures that usually mean the extractor has fallen behind the site rather
# than anything being wrong with the link. Sites change their pages and
# yt-dlp catches up within days — but only if this copy is updated.
_STALE_SIGNS = (
    "login required", "unable to extract", "unable to download webpage",
    "failed to parse", "no video formats", "requested content is not available",
    "sign in to confirm", "unsupported url",
)

# Beyond about this, an extractor bug has almost certainly been fixed upstream
# and never picked up. Instagram failed for exactly this reason: the installed
# build was four months old and the fix had shipped six weeks earlier.
STALE_AFTER_DAYS = 45


def _installed_age_days() -> Optional[int]:
    """How old the installed yt-dlp is, from its own version string."""
    try:
        from yt_dlp.version import __version__ as raw
        from datetime import date, datetime
        stamp = datetime.strptime(raw.split(".dev")[0][:10], "%Y.%m.%d").date()
        return (date.today() - stamp).days
    except Exception:                       # noqa: BLE001 - a hint, not a fact
        return None


def _with_staleness_hint(message: str) -> str:
    """Add the likely cause when the failure looks like extractor drift.

    Worth saying out loud because the message the site gives is actively
    misleading: Instagram answers "login required" for a public post that a
    logged-out browser opens fine, and the obvious next step — hunting for
    cookies — is the wrong one.
    """
    if not any(sign in message.lower() for sign in _STALE_SIGNS):
        return message
    age = _installed_age_days()
    if age is None or age < STALE_AFTER_DAYS:
        return message
    return (f"{message} (yt-dlp here is {age} days old, which is the usual "
            f"cause of this — sites change and the fix normally ships within "
            f"days. Rebuilding the image picks it up.)")
