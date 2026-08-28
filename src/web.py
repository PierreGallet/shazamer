#!/usr/bin/env python3
"""Shazamer HTTP API.

Structural changes from the previous version:

- Analysis runs through `core.pipeline`, which streams. Peak memory no longer
  scales with set length, so there is no duration cap any more.
- Progress is **pushed** over Server-Sent Events instead of polled every
  second, and it reports real work: the decode percentage comes from samples
  actually decoded, not from a guess.
- Results land in a SQLite library rather than loose files in `outputs/`, so
  tracks are queryable across sets.
- The audio is **kept** and served with Range support, because the waveform
  player needs to seek into it.
- Path handling is guarded, HTML is never assembled server-side, and CORS is
  no longer `*` with credentials (a combination browsers reject outright).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.acquire import resolve as acquire_resolve
from src.acquire.slskd import SlskdClient, SlskdError, search_query
from src.core.pipeline import AnalyzeConfig, Pipeline
from src.export import formats as export_formats
from src.identify.shazam import ShazamIdentifier
from src.jobs import queue as jobs
from src.sentry_setup import init_sentry
from src.sources import download as dl
from src.store.library import Library
from src.tasks import Task, TaskManager

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)


def _version() -> str:
    """Read the released version rather than restating it.

    A hardcoded string drifts the moment release-please cuts a version, and
    then /api/health confidently reports something false — which is worse than
    reporting nothing, because it is exactly what you check when asking what
    is deployed.
    """
    manifest = Path(__file__).resolve().parent.parent / ".release-please-manifest.json"
    try:
        return json.loads(manifest.read_text())["."]
    except (OSError, json.JSONDecodeError, KeyError):
        return "unknown"

init_sentry()

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
# Tracks fetched from Soulseek, served to the browser and swept with the rest.
DOWNLOAD_DIR = BASE_DIR / "downloads"
MEDIA_DIR = BASE_DIR / "media"
DATA_DIR = BASE_DIR / "data"
TMP_DIR = BASE_DIR / "tmp"
FRONTEND_DIST = BASE_DIR / "web" / "dist"
# The Docusaurus build, served alongside the app rather than on GitHub
# Pages: one domain, one deploy, and the docs are always the ones that
# describe the version actually running.
DOCS_DIST = BASE_DIR / "docs-site" / "build"

for directory in (UPLOAD_DIR, MEDIA_DIR, DATA_DIR, TMP_DIR, DOWNLOAD_DIR):
    directory.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024))
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aac",
                      ".wma", ".aiff", ".aif", ".webm"}
# Four, not eight. Eight was chosen when throughput plateaued around three
# probes a second and extra slots were merely useless. They are not useless
# now: Shazam refuses aggressively, and every parallel slot is another request
# feeding the limit that then pauses *all* of them. On a 75-minute set the
# refusals cost more wall-clock than the parallelism saved.
CONCURRENCY = int(os.environ.get("SHAZAM_CONCURRENCY", "4"))
KEEP_AUDIO_DAYS = int(os.environ.get("KEEP_AUDIO_DAYS", "14"))
# Downloads are kept far longer than set audio, and for a different reason.
# Set audio is a byproduct — it exists so the waveform can be scrubbed. A
# downloaded track is a record you went looking for, and on this server it is
# also what gets shared back to Soulseek in return for what you take. Sweeping
# it on the same short schedule would empty the share every fortnight.
KEEP_DOWNLOADS_DAYS = int(os.environ.get("KEEP_DOWNLOADS_DAYS", "180"))
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:8000").split(",")
    if o.strip()]

library = Library(DATA_DIR / "library.db")
tasks = TaskManager(TMP_DIR / "tasks")


def sweep_media(max_age_days: int, downloads_days: Optional[int] = None) -> int:
    """Drop files past their retention window.

    Two windows, because two kinds of file. Set audio and uploads are
    byproducts: they exist so the waveform can be scrubbed, and deleting them
    leaves the tracklist intact — only playback goes away. A downloaded track
    is the thing you were after, and here it is also the Soulseek share, so it
    lives far longer.
    """
    now = datetime.now().timestamp()
    cutoffs = {
        MEDIA_DIR: now - max_age_days * 86400,
        UPLOAD_DIR: now - max_age_days * 86400,
        DOWNLOAD_DIR: now - (downloads_days if downloads_days is not None
                             else KEEP_DOWNLOADS_DAYS) * 86400,
    }
    removed = 0
    for folder, cutoff in cutoffs.items():
        removed += _sweep_folder(folder, cutoff)
    return removed


def _sweep_folder(folder: Path, cutoff: float) -> int:
    removed = 0
    for path in folder.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            logger.warning("Could not sweep %s: %s", path, exc)
    return removed


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The API does not run analyses when a queue is configured, so an
    # interrupted task belongs to the worker and will be handed back to it.
    interrupted = tasks.mark_interrupted(requeued=jobs.enabled())
    if interrupted:
        logger.info("Marked %d interrupted task(s) after restart", interrupted)
    tasks.sweep()
    swept = sweep_media(KEEP_AUDIO_DAYS)
    if swept:
        logger.info("Swept %d media file(s) past the %d-day window",
                    swept, KEEP_AUDIO_DAYS)
    yield


# /docs belongs to the written documentation, which people read; the
# generated OpenAPI page moves under /api where the rest of the API lives.
app = FastAPI(title="Shazamer", version=_version(), lifespan=lifespan,
              docs_url="/api/docs", redoc_url=None,
              openapi_url="/api/openapi.json")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)


# ── Helpers ──────────────────────────────────────────────────────────────

def make_pipeline() -> Pipeline:
    return Pipeline(
        identifier=ShazamIdentifier(concurrency=CONCURRENCY),
        config=AnalyzeConfig(concurrency=CONCURRENCY),
    )


class URLRequest(BaseModel):
    url: str
    # Set to re-analyse in place. An imported set carries no audio, waveform,
    # BPM or key — none of that was recorded before 1.0 — so the only way to
    # get them is to run the source again. Re-analysing under the same id
    # replaces the stub instead of leaving a duplicate beside it.
    replaces: Optional[str] = None


class StarRequest(BaseModel):
    key: str
    title: str = ""
    artist: str = ""


class WatchRequest(BaseModel):
    url: str
    title: str = ""


class SoulseekSearchRequest(BaseModel):
    artist: str = ""
    title: str = ""
    query: str = ""


class SoulseekDownloadRequest(BaseModel):
    username: str
    filename: str
    size: int = 0


class AcquireRequest(BaseModel):
    """Fetch a track. With no `chosen`, the best-ranked candidate is taken."""
    key: str
    artist: str
    title: str
    label: str = ""
    year: str = ""
    album: str = ""
    genre: str = ""
    # A candidate the user picked from the ranked list, as returned by
    # /api/acquire/candidates.
    chosen: Optional[dict] = None


# ── Analysis ─────────────────────────────────────────────────────────────

async def run_analysis(task_id: str, path: Path, title: str, *,
                       source_url: str = "", source_kind: str = "upload",
                       uploader: str = "", quality: str = "",
                       set_id: Optional[str] = None) -> None:
    """Analyse a local file and store the result. Runs as a background task."""
    task = tasks.get(task_id)
    if task is None:
        return

    loop = asyncio.get_running_loop()

    def on_progress(stage: str, pct: int, message: str) -> None:
        # Called from the pipeline; hop back onto the loop thread safely.
        tasks.update(task, stage=stage, progress=pct, message=message,
                     status="processing")

    try:
        tasks.update(task, status="processing", stage="starting", progress=1,
                     message="Starting analysis...", filename=title)
        result = await make_pipeline().run(str(path), on_progress=on_progress)

        set_id = set_id or task_id
        await library.save_set(
            set_id, title, result.to_dict(),
            source_url=source_url, source_kind=source_kind, uploader=uploader,
            audio_path=str(path), quality=quality,
        )
        tasks.finish(task, status="completed",
                     message=f"{result.stats.get('identified', 0)} tracks identified",
                     set_id=set_id)
        logger.info("Analysis %s complete: %s", task_id, result.stats)

        # Enrichment follows as its own job so the set is usable immediately;
        # labels appear a minute or two later. Best-effort: a set without them
        # is still a tracklist.
        if not await jobs.enqueue("enrich_set_job", set_id,
                                  job_id=f"enrich:{set_id}"):
            logger.debug("No queue for enrichment of %s", set_id)

    except asyncio.CancelledError:
        # No set was written, so the audio has no owner.
        discard_media(path)
        tasks.finish(task, status="cancelled", message="Analysis cancelled")
        raise
    except Exception as exc:
        logger.exception("Analysis %s failed", task_id)
        _report(exc, task_id=task_id, stage="analysis")
        discard_media(path)
        tasks.finish(task, status="error", message="Analysis failed", error=str(exc))


async def run_url_analysis(task_id: str, url: str,
                           set_id: Optional[str] = None) -> None:
    """Download then analyse. Progress spans both phases."""
    task = tasks.get(task_id)
    if task is None:
        return

    downloaded: Optional[Path] = None
    try:
        tasks.update(task, status="downloading", stage="downloading", progress=1,
                     message="Resolving URL...")

        def on_download(pct: float, phase: str) -> None:
            # Downloading occupies the first 12% of the bar; analysis, which
            # takes far longer, gets the rest.
            tasks.update(task, stage="downloading",
                         progress=1 + int(pct * 0.11),
                         message=(f"Downloading audio... {pct:.0f}%"
                                  if phase == "downloading"
                                  else "Preparing audio..."))

        media = await dl.download_audio(url, MEDIA_DIR, task_id,
                                        on_progress=on_download)
        downloaded = media.path
        tasks.update(task, stage="downloaded", progress=13,
                     message=f"Downloaded ({media.quality_label}). Analysing...",
                     filename=media.title, quality=media.quality_label)

        await run_analysis(task_id, media.path, media.title,
                           source_url=media.webpage_url,
                           source_kind=media.extractor.lower(),
                           uploader=media.uploader,
                           quality=media.quality_label,
                           set_id=set_id)

    except asyncio.CancelledError:
        discard_media(downloaded)
        tasks.finish(task, status="cancelled", message="Cancelled")
        raise
    except dl.DownloadError as exc:
        # A failed download can still have written a partial file.
        discard_media(downloaded or MEDIA_DIR / task_id)
        _discard_partials(task_id)
        tasks.finish(task, status="error", message="Download failed", error=str(exc))
    except Exception as exc:
        logger.exception("URL analysis %s failed", task_id)
        _report(exc, task_id=task_id, stage="download")
        discard_media(downloaded)
        tasks.finish(task, status="error", message="Download failed", error=str(exc))


def _discard_partials(task_id: str) -> None:
    """Remove anything yt-dlp left behind for a task, whatever its extension.

    The container format is only known once the download succeeds, so a failure
    has to be cleaned up by prefix.
    """
    for leftover in MEDIA_DIR.glob(f"{task_id}.*"):
        discard_media(leftover)


async def dispatch(task: "Task", function: str, *args) -> None:
    """Send an analysis to the queue, or run it here when there is none.

    The queue is what makes an analysis survive a deploy, so it is preferred
    whenever it is reachable. Falling back to in-process keeps local
    development and the test suite working without Redis, and keeps the app
    usable — degraded, not broken — if Redis is down.

    The fallback is explicitly the old behaviour, including its weakness: a
    restart loses the work. It says so in the log rather than pretending
    otherwise.
    """
    # Recorded before enqueueing so a crash between the two still leaves
    # enough for the worker to reclaim the job.
    task.job = {"function": function, "args": list(args)}
    tasks.persist(task)

    if await jobs.enqueue(function, *args, job_id=task.id):
        tasks.update(task, message="Queued for analysis...")
        return

    if jobs.enabled():
        logger.warning("Queue unreachable; running %s in-process. It will not "
                       "survive a restart.", task.id)
    target = {"analyze_upload_job": run_analysis,
              "analyze_url_job": run_url_analysis}[function]
    handle = asyncio.create_task(_run_inline(target, *args))
    tasks.attach(task, handle)


async def _run_inline(target, *args) -> None:
    """Adapt a job signature (which carries an arq context) to a direct call."""
    if target is run_analysis:
        task_id, path, title = args
        await target(task_id, Path(path), title)
    else:
        task_id, url, set_id = args
        await target(task_id, url, set_id=set_id)


def discard_media(path: Optional[Path]) -> None:
    """Delete audio belonging to an analysis that produced nothing.

    Audio is deliberately kept for a set that succeeded — the waveform player
    streams it. When the analysis fails or is cancelled no set is written, so
    nothing will ever reference the file and the boot sweep would sit on it for
    the whole retention window. Seven failed attempts at one 69-minute set left
    503 MB behind before this existed.

    Guarded by location: only files under the directories this app writes to
    are removable, so a bad path can never delete anything else.
    """
    if path is None:
        return
    try:
        resolved = Path(path).resolve()
    except OSError:
        return
    roots = (MEDIA_DIR.resolve(), UPLOAD_DIR.resolve(), DOWNLOAD_DIR.resolve())
    if not any(resolved.is_relative_to(root) for root in roots):
        logger.warning("Refusing to discard %s: outside the media directories",
                       resolved)
        return
    try:
        resolved.unlink(missing_ok=True)
        logger.info("Discarded unused audio %s", resolved.name)
    except OSError as exc:
        logger.warning("Could not discard %s: %s", resolved, exc)


def _report(exc: Exception, **tags) -> None:
    if isinstance(exc, (ValueError, dl.DownloadError)):
        return  # user-facing conditions, not bugs
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for key, value in tags.items():
                scope.set_tag(key, value)
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass


@app.post("/api/analyze/upload")
async def analyze_upload(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file selected")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format {suffix or '(none)'}. Supported: "
                   + ", ".join(sorted(e.lstrip('.') for e in ALLOWED_EXTENSIONS)),
        )

    task_id = str(uuid.uuid4())
    dest = UPLOAD_DIR / f"{task_id}{suffix}"

    # Stream to disk rather than reading the whole upload into memory — a 2 GB
    # file must not become 2 GB of resident process.
    size = 0
    try:
        with open(dest, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds {MAX_UPLOAD_BYTES // (1024**3)} GB",
                    )
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Could not save upload: {exc}")

    title = Path(file.filename).stem
    task = tasks.create(task_id, filename=title)
    await dispatch(task, "analyze_upload_job", task_id, str(dest), title)
    return {"task_id": task_id, "filename": title}


@app.post("/api/analyze/url")
async def analyze_url(request: URLRequest):
    url = (request.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http(s)://")

    replaces = (request.replaces or "").strip() or None
    if replaces is not None and await library.get_set(replaces) is None:
        raise HTTPException(status_code=404,
                            detail="The set to replace no longer exists")

    task_id = str(uuid.uuid4())
    task = tasks.create(task_id, filename="Resolving...", source_url=url)
    await dispatch(task, "analyze_url_job", task_id, url, replaces)
    return {"task_id": task_id, "url": url, "replaces": replaces}


@app.get("/api/tasks")
async def list_active_tasks():
    """Analyses currently running, so the UI can offer a way back to them.

    Read from disk rather than memory: the work happens in the worker
    container, so the API has no in-process record of it.
    """
    return tasks.active_on_disk()


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.snapshot()


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    # The job may be running in the worker, where there is no local handle to
    # cancel — ask the queue first, then fall back to the in-process case.
    if await jobs.abort(task_id) or await tasks.cancel(task_id):
        return {"cancelled": True}
    raise HTTPException(status_code=400, detail="Task is not running")


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request):
    """Server-Sent Events stream of a task's progress.

    Replaces one-second polling. The client gets every state change the moment
    it happens, and a heartbeat keeps proxies from closing an idle connection
    during the long identification phase.
    """
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    async def stream():
        yield _sse(task.snapshot())
        if task.terminal:
            yield "event: end\ndata: {}\n\n"
            return

        # An analysis normally runs in the worker container, so there is no
        # in-process queue to subscribe to — the shared task file is the
        # channel between the two. When the job is running here instead (no
        # queue configured, or the queue was unreachable) the in-memory path is
        # still used, because it is immediate and avoids a pointless poll.
        if task._handle is not None:
            async for frame in _stream_local(task, request):
                yield frame
            return

        last_beat = asyncio.get_running_loop().time()
        async for snapshot in tasks.watch(task_id):
            if await request.is_disconnected():
                return
            yield _sse(snapshot)
            last_beat = asyncio.get_running_loop().time()
            if snapshot.get("status") in ("completed", "error", "cancelled"):
                yield "event: end\ndata: {}\n\n"
                return
            # Long identification stretches produce few state changes; a
            # comment keeps proxies from closing an idle connection.
            if asyncio.get_running_loop().time() - last_beat > 15:
                yield ": heartbeat\n\n"
        yield "event: end\ndata: {}\n\n"

    async def _stream_local(task, request):
        queue = tasks.subscribe(task)
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if payload is None:
                    yield _sse(task.snapshot())
                    yield "event: end\ndata: {}\n\n"
                    return
                yield _sse(payload)
        finally:
            tasks.unsubscribe(task, queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # nginx must not buffer an SSE stream
    })


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# ── Sets ─────────────────────────────────────────────────────────────────

@app.get("/api/sets")
async def list_sets(limit: int = Query(50, ge=1, le=200)):
    return await library.list_sets(limit)


@app.get("/api/sets/{set_id}")
async def get_set(set_id: str):
    data = await library.get_set(set_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Set not found")
    data.pop("audio_path", None)
    return data


@app.delete("/api/sets/{set_id}")
async def delete_set(set_id: str):
    data = await library.get_set(set_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Set not found")
    audio = data.get("audio_path")
    if audio:
        try:
            path = Path(audio).resolve()
            if path.is_relative_to(MEDIA_DIR.resolve()) or \
               path.is_relative_to(UPLOAD_DIR.resolve()):
                path.unlink(missing_ok=True)
        except OSError:
            pass
    await library.delete_set(set_id)
    return {"deleted": True}


@app.api_route("/api/sets/{set_id}/audio", methods=["GET", "HEAD"])
async def stream_set_audio(set_id: str, request: Request):
    """Serve a set's audio with Range support, so the player can seek.

    Without ranges a browser downloads the whole mix before it can jump to
    01:12:00 — unusable for a three-hour set.
    """
    data = await library.get_set(set_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Set not found")

    raw = data.get("audio_path") or ""
    if not raw:
        raise HTTPException(status_code=404, detail="No audio kept for this set")
    path = Path(raw).resolve()
    if not (path.is_relative_to(MEDIA_DIR.resolve())
            or path.is_relative_to(UPLOAD_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Invalid audio path")
    if not path.exists():
        raise HTTPException(
            status_code=410,
            detail=f"Audio was removed after {KEEP_AUDIO_DAYS} days. "
                   "The tracklist is still here — re-run the set to play it again.",
        )

    file_size = path.stat().st_size
    media_type = _media_type(path.suffix)
    range_header = request.headers.get("range")

    if range_header is None:
        return FileResponse(path, media_type=media_type, headers={
            "Accept-Ranges": "bytes", "Content-Length": str(file_size)})

    start, end = _parse_range(range_header, file_size)
    if start is None:
        return Response(status_code=416,
                        headers={"Content-Range": f"bytes */{file_size}"})

    length = end - start + 1

    async def body():
        chunk = 256 * 1024
        remaining = length
        with open(path, "rb") as fh:
            fh.seek(start)
            while remaining > 0:
                data = fh.read(min(chunk, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(body(), status_code=206, media_type=media_type, headers={
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    })


def _parse_range(header: str, file_size: int):
    if not header.startswith("bytes="):
        return None, None
    spec = header[6:].split(",")[0].strip()
    try:
        raw_start, _, raw_end = spec.partition("-")
        if raw_start:
            start = int(raw_start)
            end = int(raw_end) if raw_end else file_size - 1
        else:
            # Suffix form: "bytes=-500" means the final 500 bytes.
            start = max(0, file_size - int(raw_end))
            end = file_size - 1
    except ValueError:
        return None, None
    if start >= file_size or start > end:
        return None, None
    return start, min(end, file_size - 1)


def _media_type(suffix: str) -> str:
    return {
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
        ".ogg": "audio/ogg", ".opus": "audio/ogg", ".webm": "audio/webm",
        ".wav": "audio/wav", ".flac": "audio/flac", ".aiff": "audio/aiff",
        ".aif": "audio/aiff",
    }.get(suffix.lower(), "application/octet-stream")


@app.get("/api/sets/{set_id}/export/{fmt}")
async def export_set(set_id: str, fmt: str):
    data = await library.get_set(set_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Set not found")
    if fmt not in export_formats.EXPORTERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown format. Available: {', '.join(export_formats.EXPORTERS)}")

    title = data.get("title") or set_id
    payload = {"duration": data.get("duration", 0), "tracks": data.get("tracks", []),
               "stats": data.get("stats", {}), "waveform": []}

    fn, media_type, ext = export_formats.EXPORTERS[fmt]
    if fmt == "rekordbox":
        body = fn(payload, title, audio_path=data.get("audio_path", ""))
    elif fmt == "m3u":
        body = fn(payload, title)
    else:
        body = fn(payload)

    safe_name = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:80]
    filename = f"{safe_name or 'tracklist'}.{ext}"
    return Response(content=body, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"'})


# ── Library ──────────────────────────────────────────────────────────────

@app.get("/api/library/recurring")
async def recurring(min_sets: int = Query(2, ge=2, le=20),
                    limit: int = Query(100, ge=1, le=500)):
    return await library.recurring_tracks(min_sets=min_sets, limit=limit)


@app.get("/api/library/search")
async def search_library(q: str = "", bpm_min: Optional[float] = None,
                         bpm_max: Optional[float] = None,
                         camelot: Optional[str] = None,
                         starred: bool = False,
                         limit: int = Query(200, ge=1, le=1000)):
    return await library.search_tracks(q, bpm_min=bpm_min, bpm_max=bpm_max,
                                       camelot=camelot, starred_only=starred,
                                       limit=limit)


@app.get("/api/library/crate")
async def get_crate():
    return await library.crate()


@app.post("/api/library/star")
async def star_track(request: StarRequest):
    if not request.key:
        raise HTTPException(status_code=400, detail="Track key is required")
    starred = await library.toggle_star(request.key, request.title, request.artist)
    return {"key": request.key, "starred": starred}


# ── Acquisition ──────────────────────────────────────────────────────────

@app.get("/api/acquire/sources")
async def acquisition_sources(artist: str = "", title: str = "", isrc: str = ""):
    sources = acquire_resolve.resolve(
        artist, title, isrc=isrc,
        soulseek_configured=acquire_resolve.soulseek_configured())
    return {"sources": [s.to_dict() for s in sources],
            "soulseek_configured": acquire_resolve.soulseek_configured()}


@app.get("/api/acquire/soulseek/status")
async def soulseek_status():
    client = SlskdClient()
    if not client.configured:
        return {"configured": False, "reachable": False,
                "hint": "Set SLSKD_URL and SLSKD_API_KEY to enable Soulseek."}
    return {"configured": True, "reachable": await client.health()}


@app.post("/api/acquire/soulseek/search")
async def soulseek_search(request: SoulseekSearchRequest):
    query = request.query or search_query(request.artist, request.title)
    if not query.strip():
        raise HTTPException(status_code=400, detail="Nothing to search for")
    try:
        candidates = await SlskdClient().search(query)
    except SlskdError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"query": query, "candidates": [c.to_dict() for c in candidates[:40]]}


@app.post("/api/acquire/soulseek/download")
async def soulseek_download(request: SoulseekDownloadRequest):
    from src.acquire.slskd import Candidate
    candidate = Candidate(
        username=request.username, filename=request.filename, size=request.size,
        extension=request.filename.rsplit(".", 1)[-1].lower(), bitrate=None,
        sample_rate=None, bit_depth=None, length=None, queue_length=0,
        free_slot=False, upload_speed=0, score=0.0)
    try:
        return await SlskdClient().enqueue(candidate)
    except SlskdError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/acquire/candidates")
async def acquire_candidates(artist: str, title: str,
                             limit: int = Query(5, ge=1, le=20)):
    """The best few Soulseek matches, ranked, without downloading anything.

    Shown before fetching because the difference that matters most — extended
    mix against radio edit — is invisible until someone looks, and a filename
    on Soulseek is whatever the uploader typed.
    """
    from src.acquire.runner import rank_candidates

    if not (artist and title):
        raise HTTPException(status_code=400, detail="Artist and title required")
    try:
        return await rank_candidates(artist, title, limit=limit)
    except SlskdError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/acquire/track")
async def acquire_track_endpoint(request: AcquireRequest):
    """Find the best Soulseek match for a track and fetch it.

    Returns immediately with a download id: a Soulseek transfer can take an
    hour behind a peer's queue, so the work happens in the worker and the UI
    follows the row.
    """
    if not (request.key and request.artist and request.title):
        raise HTTPException(status_code=400,
                            detail="A track key, artist and title are required")
    if not acquire_resolve.soulseek_configured():
        raise HTTPException(
            status_code=503,
            detail="Soulseek is not configured on this server. Set SLSKD_URL "
                   "and SLSKD_API_KEY to enable it.")

    download_id = await library.start_download(
        request.key, request.artist, request.title)

    meta = {"label": request.label, "year": request.year,
            "album": request.album, "genre": request.genre}
    queued = await jobs.enqueue("acquire_track_job", download_id, request.key,
                                request.artist, request.title, meta,
                                request.chosen,
                                job_id=f"acquire:{download_id}")
    if not queued:
        # No queue: do it here rather than refuse. It will not survive a
        # restart, which the row's message says.
        from src.acquire.runner import acquire_track
        from src.identify.shazam import ShazamIdentifier

        asyncio.create_task(acquire_track(
            library, DOWNLOAD_DIR, request.key, request.artist, request.title,
            download_id=download_id, meta=meta, chosen=request.chosen,
            identifier=ShazamIdentifier(concurrency=1)))

    return {"download_id": download_id, "queued": queued}


@app.get("/api/acquire/downloads")
async def list_downloads(key: Optional[str] = None,
                         limit: int = Query(50, ge=1, le=200)):
    """Download attempts, for one track or the most recent overall."""
    if key:
        return await library.downloads_for(key)
    return await library.recent_downloads(limit)


@app.get("/api/acquire/downloads/{download_id}")
async def get_download(download_id: int):
    row = await library.get_download(download_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Download not found")
    return row


@app.get("/api/acquire/downloads/{download_id}/file")
async def serve_download(download_id: int):
    """Hand the file to the browser.

    The server is not storage: this is how a track reaches you, and the file is
    swept on the same schedule as set audio.
    """
    stored = await library.download_path(download_id)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail="That download has no file — it may still be running, or it "
                   "may have failed.")

    path = Path(stored).resolve()
    if not path.is_relative_to(DOWNLOAD_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists():
        raise HTTPException(
            status_code=410,
            detail=f"The file was removed after {KEEP_DOWNLOADS_DAYS} days. "
                   "Fetch it again if you still want it.")

    return FileResponse(path, filename=path.name,
                        media_type="application/octet-stream")


@app.get("/api/acquire/soulseek/downloads")
async def soulseek_downloads():
    try:
        return await SlskdClient().downloads()
    except SlskdError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── Watches ──────────────────────────────────────────────────────────────

@app.get("/api/watches")
async def list_watches():
    return await library.list_watches()


@app.post("/api/watches")
async def add_watch(request: WatchRequest):
    url = (request.url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http(s)://")
    watch_id = str(uuid.uuid4())
    title = request.title
    if not title:
        try:
            info = await dl.probe_url(url)
            title = info.get("title") or info.get("channel") or url
        except dl.DownloadError:
            title = url
    await library.add_watch(watch_id, url, title)
    return {"id": watch_id, "url": url, "title": title}


@app.delete("/api/watches/{watch_id}")
async def delete_watch(watch_id: str):
    if not await library.delete_watch(watch_id):
        raise HTTPException(status_code=404, detail="Watch not found")
    return {"deleted": True}


@app.post("/api/watches/{watch_id}/check")
async def check_watch(watch_id: str, limit: int = Query(20, ge=1, le=100)):
    """List what is new on a followed channel since the last check."""
    watches = {w["id"]: w for w in await library.list_watches()}
    watch = watches.get(watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="Watch not found")

    try:
        entries = await dl.list_channel(watch["url"], limit=limit)
    except dl.DownloadError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    seen = set(await library.watch_seen_ids(watch_id))
    fresh = [e for e in entries if e["id"] and e["id"] not in seen]
    await library.mark_watch_checked(
        watch_id, list(seen | {e["id"] for e in entries if e["id"]}))
    return {"watch_id": watch_id, "checked": len(entries), "new": fresh}


# ── Health & frontend ────────────────────────────────────────────────────

@app.api_route("/api/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "version": app.version,
            "soulseek": acquire_resolve.soulseek_configured()}


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")),
              name="assets")

# Mounted before the SPA catch-all below, which would otherwise answer every
# /docs/* request with the app's index.html. html=True serves directory
# indexes, which is how Docusaurus's pretty URLs resolve.
if DOCS_DIST.exists():
    app.mount("/docs", StaticFiles(directory=str(DOCS_DIST), html=True),
              name="docs")

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
async def serve_frontend(full_path: str = ""):
    """Serve the built frontend.

    Unknown paths return index.html so client-side routing works; API routes
    are matched before this catch-all by FastAPI's ordering. Requests are
    resolved and checked against the dist root, so a crafted path cannot walk
    out of it.
    """
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")

    if not FRONTEND_DIST.exists():
        return JSONResponse(
            {"detail": "Frontend not built. Run `make build` (or `npm run build` "
                       "in web/) to produce web/dist."},
            status_code=503,
        )

    if full_path:
        candidate = (FRONTEND_DIST / full_path).resolve()
        if (candidate.is_relative_to(FRONTEND_DIST.resolve())
                and candidate.is_file()):
            return FileResponse(candidate)
    return FileResponse(FRONTEND_DIST / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
