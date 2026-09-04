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
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from fastapi import (Depends, FastAPI, File, HTTPException, Query, Request,
                     Response, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

from src.config import PUBLIC_URL
from pydantic import BaseModel

from src.acquire import resolve as acquire_resolve
from src.acquire.slskd import SlskdClient, SlskdError, search_query
from src.core import dedupe
from src.core.pipeline import AnalyzeConfig, Pipeline
from src.enrich import preview as preview_lookup
from src.export import formats as export_formats
from src.identify.shazam import ShazamIdentifier
from src.jobs import queue as jobs
from src.sentry_setup import init_sentry
from src.sources import download as dl
from src.store.library import Library
from src import auth, mail
from src.auth import (clear_session_cookie, client_key, email_limiter,
                      ip_limiter, make_dependencies, set_session_cookie,
                      verify_limiter)
from src.store.accounts import (Accounts, CODE_TTL, looks_like_email,
                                normalise_email)
from src.tasks import Task, TaskManager, confirm_weight

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
# Overridable so a test can point the databases somewhere disposable, and
# so a deployment can put them on a different volume from the code.
# Without it the suite wrote real accounts into the working copy.
DATA_DIR = Path(os.environ.get("SHAZAMER_DATA_DIR") or (BASE_DIR / "data"))
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
# Zero means keep it. A set's audio is what makes the waveform scrubbable and
# lets you hear the moment a track was claimed for — which is most of why the
# tracklist is worth having. Deleting it after a fortnight turned a library
# into a fortnight's library.
#
# The fortnight was a guess made before there was anything to measure.
# Measured: six sets take 325 MB, about 54 MB each, against 69 GB free — room
# for something like thirteen hundred of them. It was solving a problem that
# does not exist, and the cost of being wrong the other way is somebody's
# archive.
#
# Disk is still bounded, by MIN_FREE_DISK_GB below, which deletes only when
# space actually runs short and takes the oldest first.
KEEP_AUDIO_DAYS = int(os.environ.get("KEEP_AUDIO_DAYS", "0"))
# When free space falls below this, the oldest set audio is dropped until it
# is back above. A real limit, applied when it bites, rather than a timer that
# throws away files nobody was short of room for.
MIN_FREE_DISK_GB = float(os.environ.get("MIN_FREE_DISK_GB", "5"))
# Avatars are stored inline as data URIs rather than as files: there is one
# per account, they are small, and a file store means a path, a sweeper and a
# way to serve it. This caps what "small" means.
MAX_AVATAR_BYTES = int(os.environ.get("MAX_AVATAR_BYTES", str(200 * 1024)))
# How many Soulseek results reach the browser. A popular record brings
# hundreds, and the point of the list is to choose from it rather than to read
# all of it — but the cap is reported, not applied quietly.
SEARCH_RESULT_CAP = int(os.environ.get("SEARCH_RESULT_CAP", "60"))
# Downloads are kept far longer than set audio, and for a different reason.
# Set audio is a byproduct — it exists so the waveform can be scrubbed. A
# downloaded track is a record you went looking for, and on this server it is
# also what gets shared back to Soulseek in return for what you take. Sweeping
# it on the same short schedule would empty the share every fortnight.
KEEP_DOWNLOADS_DAYS = int(os.environ.get("KEEP_DOWNLOADS_DAYS", "180"))
# Where this instance lives, for links that leave the app. Defined in
# src/config.py with a per-environment default, so it no longer has to be set
# in .env — see that module for why guessing it from the request Host header
# would be a vulnerability rather than a convenience.

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:8000").split(",")
    if o.strip()]

library = Library(DATA_DIR / "library.db")
# Its own file, not a table in the library. Sessions and login codes have a
# different lifetime, a different backup story and a different blast radius
# from a tracklist — losing accounts.db costs everyone a sign-in; losing
# library.db costs them their work.
accounts = Accounts(DATA_DIR / "accounts.db")
current_user_optional, current_user = make_dependencies(accounts)
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
    removed = 0

    # An age limit only if one was asked for. Zero — the default — means the
    # audio stays until the disk says otherwise.
    if max_age_days > 0:
        cutoff = now - max_age_days * 86400
        removed += _sweep_folder(MEDIA_DIR, cutoff)
        removed += _sweep_folder(UPLOAD_DIR, cutoff)

    keep_downloads = (downloads_days if downloads_days is not None
                      else KEEP_DOWNLOADS_DAYS)
    if keep_downloads > 0:
        removed += _sweep_folder(DOWNLOAD_DIR,
                                 now - keep_downloads * 86400)

    removed += _sweep_for_space()
    return removed


def _sweep_for_space(min_free_gb: Optional[float] = None) -> int:
    """Drop the oldest set audio until there is room again.

    Only when space actually runs short, and oldest first — the set you
    analysed this morning is the one you are most likely to be listening to,
    and the one from a year ago is the one you can re-fetch from its source.

    Uploads and set audio only. A downloaded track is the thing you went
    looking for and cannot always be found again; deleting that to make room
    would be deleting the point of the exercise.
    """
    import shutil

    limit = MIN_FREE_DISK_GB if min_free_gb is None else min_free_gb
    if limit <= 0:
        return 0
    try:
        free_gb = shutil.disk_usage(MEDIA_DIR).free / (1024 ** 3)
    except OSError:
        return 0
    if free_gb >= limit:
        return 0

    candidates = []
    for folder in (MEDIA_DIR, UPLOAD_DIR):
        for path in folder.iterdir():
            try:
                if path.is_file():
                    candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    candidates.sort()

    removed = 0
    for _, path in candidates:
        if free_gb >= limit:
            break
        try:
            size = path.stat().st_size
            path.unlink()
            free_gb += size / (1024 ** 3)
            removed += 1
            logger.warning("Low disk: dropped %s to reclaim space", path.name)
        except OSError:
            continue
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
    await accounts.sweep()
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

# Prometheus metrics on /metrics.
#
# Request rate per route, error rate per status class, and latency percentiles
# — the three things alerts are actually built on. Logs cannot answer "are we
# above 2% 5xx over five minutes"; this can.
#
# No multiprocess registry needed here, unlike noctambule: the Dockerfile runs
# a single uvicorn process (no --workers), so one in-process registry holds the
# whole picture. Add PROMETHEUS_MULTIPROC_DIR if workers are ever introduced,
# or successive scrapes will hit different processes and the counters will
# appear to move backwards.
#
# Registered HERE and not further down on purpose: the SPA catch-all at the end
# of this module answers every unmatched path with index.html, and FastAPI
# resolves routes in declaration order. Declared after it, /metrics would serve
# HTML and Prometheus would reject the target on Content-Type.
Instrumentator(
    # Exact status codes rather than the default 2xx/4xx/5xx buckets: telling
    # 401 from 403 from 404 is most of the value when a route starts failing,
    # and the extra cardinality is one series per code actually returned.
    #
    # Route cardinality is already safe without any option here: the `handler`
    # label carries the route TEMPLATE (/api/x/{id}), not the resolved URL, and
    # requests matching no route are grouped under a single "none" handler.
    should_group_status_codes=False,
    excluded_handlers=["/metrics", "/api/health"],
).instrument(app).expose(app, include_in_schema=False)


# ── Helpers ──────────────────────────────────────────────────────────────

def make_pipeline(on_work=None) -> Pipeline:
    return Pipeline(
        identifier=ShazamIdentifier(concurrency=CONCURRENCY),
        config=AnalyzeConfig(concurrency=CONCURRENCY),
        on_work=on_work,
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

    def on_work(identify_probes: int, confirm_probes: int) -> None:
        # What the rest of the bar is going to cost. Confirmation packs its
        # probes into a twentieth of the width, so this is the difference
        # between a countdown that is roughly right and one that is out by a
        # factor of three.
        task.confirm_cost = confirm_weight(identify_probes, confirm_probes)

    try:
        tasks.update(task, status="processing", stage="starting", progress=1,
                     message="Starting analysis...", filename=title)
        result = await make_pipeline(on_work=on_work).run(
            str(path), on_progress=on_progress)

        set_id = set_id or task_id

        # Before filing it: if these bytes are already on disk under another
        # set, share one copy. Re-analysing a mix — to pick up a fix, or
        # because the first run went badly — otherwise keeps both, and that
        # was 44% of this install's set audio.
        #
        # A hard link, so each set keeps its own path and the kernel counts
        # the references. Deleting one set unlinks one name; the bytes go
        # when the last set holding them does.
        try:
            if path.parent == MEDIA_DIR:
                dedupe.link_if_duplicate(MEDIA_DIR, path)
        except Exception:                      # noqa: BLE001 - never fatal
            # Saving disk is not worth failing an analysis that worked.
            logger.debug("Deduplication skipped", exc_info=True)

        await library.save_set(
            set_id, title, result.to_dict(),
            # Off the task, not off a session: this finishes in the worker,
            # which has neither a request nor a cookie to ask.
            user_id=task.user_id,
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
async def analyze_upload(file: UploadFile = File(...),
                         user: Dict[str, Any] = Depends(current_user)):
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
    task = tasks.create(task_id, filename=title, user_id=user["id"])
    await dispatch(task, "analyze_upload_job", task_id, str(dest), title)
    return {"task_id": task_id, "filename": title}


@app.post("/api/analyze/url")
async def analyze_url(request: URLRequest, user: Dict[str, Any] = Depends(current_user)):
    url = (request.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http(s)://")

    replaces = (request.replaces or "").strip() or None
    if replaces is not None and await library.get_set(
            replaces, user_id=user["id"]) is None:
        raise HTTPException(status_code=404,
                            detail="The set to replace no longer exists")

    task_id = str(uuid.uuid4())
    task = tasks.create(task_id, filename="Resolving...", source_url=url,
                        user_id=user["id"])
    await dispatch(task, "analyze_url_job", task_id, url, replaces)
    return {"task_id": task_id, "url": url, "replaces": replaces}


def _may_see(task, user) -> bool:
    """Whether `user` may look at `task`.

    An empty owner means the task predates accounts. Those stay visible rather
    than becoming unreachable the moment this feature is switched on.
    """
    owner = getattr(task, "user_id", "") or ""
    return owner in ("", user["id"])


@app.get("/api/tasks")
async def list_active_tasks(user: Dict[str, Any] = Depends(current_user)):
    """Analyses currently running, so the UI can offer a way back to them.

    Read from disk rather than memory: the work happens in the worker
    container, so the API has no in-process record of it.

    Filtered here rather than in the store, because the task files are a
    directory shared between two containers and not a queryable thing. Tasks
    written before accounts existed carry no owner; they are shown, because
    hiding somebody's running analysis behind an upgrade is worse than showing
    it to the one person who was already using this.
    """
    mine = user["id"]
    return [t for t in tasks.active_on_disk()
            if t.get("user_id", "") in ("", mine)]


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, user: Dict[str, Any] = Depends(current_user)):
    task = tasks.get(task_id)
    if task is None or not _may_see(task, user):
        raise HTTPException(status_code=404, detail="Task not found")
    return task.snapshot()


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, user: Dict[str, Any] = Depends(current_user)):
    existing = tasks.get(task_id)
    if existing is not None and not _may_see(existing, user):
        raise HTTPException(status_code=404, detail="Task not found")
    # The job may be running in the worker, where there is no local handle to
    # cancel — ask the queue first, then fall back to the in-process case.
    if await jobs.abort(task_id) or await tasks.cancel(task_id):
        return {"cancelled": True}
    raise HTTPException(status_code=400, detail="Task is not running")


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request,
                      user: Dict[str, Any] = Depends(current_user)):
    """Server-Sent Events stream of a task's progress.

    Replaces one-second polling. The client gets every state change the moment
    it happens, and a heartbeat keeps proxies from closing an idle connection
    during the long identification phase.
    """
    task = tasks.get(task_id)
    if task is None or not _may_see(task, user):
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


# ── Accounts ─────────────────────────────────────────────────────────────

class EmailRequest(BaseModel):
    email: str


class VerifyRequest(BaseModel):
    email: str
    code: str


@app.get("/api/auth/me")
async def whoami(user: Optional[Dict[str, Any]] = Depends(current_user_optional)):
    """Who is signed in, and whether signing in is required at all.

    Answers 200 either way. The frontend needs to know it is *not* signed in
    without treating that as an error, and a 401 here would be logged and
    reported as one on every first page load.
    """
    return {
        "authenticated": user is not None,
        # Always. Kept in the payload because the frontend reads it to
        # decide whether to show the sign-in screen, and a client built
        # against an older server should not have to guess.
        "auth_required": True,
        "email": (user or {}).get("email", ""),
        "can_send_mail": mail.configured(),
    }


@app.post("/api/auth/request-code")
async def request_code(request: EmailRequest, http: Request):
    """Send a one-time code.

    Always answers the same way. Whether an address has an account, whether a
    code was already in flight, whether the mail bounced — none of it is
    reported, because all of it would answer "does this person use Shazamer?"
    for anybody who asks.
    """
    email = normalise_email(request.email)
    if not looks_like_email(email):
        raise HTTPException(status_code=400, detail="That is not an email address")

    if not mail.configured():
        # The one case worth reporting: the server cannot send at all, so
        # waiting for a code would be waiting for ever. This says nothing
        # about the address.
        raise HTTPException(
            status_code=503,
            detail="This server cannot send email yet. Set SMTP_HOST, "
                   "SMTP_USER, SMTP_PASSWORD and MAIL_FROM.")

    quiet = {"sent": True, "expires_in": int(CODE_TTL.total_seconds())}
    if not email_limiter.allow(email) or not ip_limiter.allow(client_key(http)):
        logger.warning("Rate-limited a code request for %s", email)
        return quiet

    code = await accounts.start_login(email)
    if code is None:
        return quiet                # one already in flight; do not send twice

    try:
        await mail.send_login_code(email, code,
                                   minutes=int(CODE_TTL.total_seconds() // 60))
    except mail.MailError:
        # Logged inside `mail`, without the code. Still answers `quiet`: a
        # bounce tells the caller the address is real.
        pass
    return quiet


@app.post("/api/auth/verify")
async def verify_code(request: VerifyRequest, http: Request,
                      response: Response):
    email = normalise_email(request.email)
    if not verify_limiter.allow(client_key(http)):
        raise HTTPException(status_code=429,
                            detail="Too many attempts. Try again later.")

    user = await accounts.verify_login(email, request.code)
    if user is None:
        # One message for every failure — wrong, expired, used, never issued.
        raise HTTPException(status_code=400,
                            detail="That code is wrong or has expired.")

    # The first account to exist adopts everything made before accounts did,
    # so an existing library does not vanish behind a login screen.
    if await accounts.count_users() == 1:
        adopted = await library.adopt_orphans(user["id"])
        if adopted:
            logger.info("First account adopted %d pre-existing row(s)", adopted)

    token = await accounts.create_session(
        user["id"], http.headers.get("user-agent", ""))
    set_session_cookie(response, token)
    return {"authenticated": True, "email": user["email"]}


@app.post("/api/auth/logout")
async def logout(http: Request, response: Response):
    await accounts.end_session(http.cookies.get(auth.COOKIE_NAME, ""))
    clear_session_cookie(response)
    return {"authenticated": False}


@app.post("/api/auth/logout-everywhere")
async def logout_everywhere(http: Request, response: Response,
                            user: Dict[str, Any] = Depends(current_user)):
    """Sign out of every device. The answer to a lost or stolen one."""
    ended = await accounts.end_all_sessions(user["id"])
    clear_session_cookie(response)
    return {"authenticated": False, "sessions_ended": ended}


# ── Profile ──────────────────────────────────────────────────────────────

class ProfileRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    avatar: Optional[str] = None


class EmailChangeRequest(BaseModel):
    email: str


class EmailConfirmRequest(BaseModel):
    code: str


@app.get("/api/profile")
async def get_profile(user: Dict[str, Any] = Depends(current_user)):
    profile = await accounts.profile(user["id"])
    if profile is None:
        raise HTTPException(status_code=404, detail="No such account")
    return profile


@app.patch("/api/profile")
async def patch_profile(request: ProfileRequest,
                        user: Dict[str, Any] = Depends(current_user)):
    """Name and avatar. Not the address — that has to be proved."""
    if request.avatar and len(request.avatar) > MAX_AVATAR_BYTES:
        raise HTTPException(
            status_code=413,
            detail="That image is too large. Around 200 KB is the limit.")
    await accounts.update_profile(
        user["id"],
        first_name=(request.first_name or "").strip()[:80]
        if request.first_name is not None else None,
        last_name=(request.last_name or "").strip()[:80]
        if request.last_name is not None else None,
        avatar=request.avatar)
    return await accounts.profile(user["id"])


@app.post("/api/profile/email")
async def request_email_change(request: EmailChangeRequest, http: Request,
                               user: Dict[str, Any] = Depends(current_user)):
    """Send a code to the address being moved to.

    To the new one, not the old: the claim being made is that this mailbox is
    reachable and yours. Verifying the old address would prove nothing about
    the new one, and a typo would then lock the account behind an inbox
    nobody reads.
    """
    email = normalise_email(request.email)
    if not looks_like_email(email):
        raise HTTPException(status_code=400, detail="That is not an email address")
    if not mail.configured():
        raise HTTPException(status_code=503,
                            detail="This server cannot send email yet.")
    if not verify_limiter.allow(client_key(http)):
        raise HTTPException(status_code=429, detail="Too many attempts.")

    code = await accounts.start_email_change(user["id"], email)
    if code is not None:
        try:
            await mail.send_login_code(
                email, code, minutes=int(CODE_TTL.total_seconds() // 60))
        except mail.MailError:
            pass
    # Identical either way: whether the address is already taken is not this
    # endpoint's to report.
    return {"sent": True}


@app.post("/api/profile/email/confirm")
async def confirm_email_change(request: EmailConfirmRequest, http: Request,
                               user: Dict[str, Any] = Depends(current_user)):
    if not verify_limiter.allow(client_key(http)):
        raise HTTPException(status_code=429, detail="Too many attempts.")
    changed = await accounts.confirm_email_change(user["id"], request.code)
    if changed is None:
        raise HTTPException(status_code=400,
                            detail="That code is wrong or has expired.")
    return {"email": changed}


# ── Sharing ──────────────────────────────────────────────────────────────

class ShareRequest(BaseModel):
    email: Optional[str] = None


@app.post("/api/sets/{set_id}/share")
async def share_set(set_id: str, request: ShareRequest,
                    user: Dict[str, Any] = Depends(current_user)):
    """Create an invitation to copy one of your sets, and optionally mail it."""
    profile = await accounts.profile(user["id"]) or {}
    from_name = profile.get("display_name") or "Someone"
    to_email = normalise_email(request.email or "")
    if to_email and not looks_like_email(to_email):
        raise HTTPException(status_code=400, detail="That is not an email address")

    token = await library.create_share(set_id, user_id=user["id"],
                                       from_name=from_name, to_email=to_email)
    if token is None:
        raise HTTPException(status_code=404, detail="Set not found")

    base = PUBLIC_URL or (ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else "")
    link = f"{base}/shared/{token}"
    sent = False
    if to_email and mail.configured():
        details = await library.peek_share(token) or {}
        try:
            await mail.send_share(to_email, from_name,
                                  details.get("title", "a tracklist"), link,
                                  details.get("track_count", 0))
            sent = True
        except mail.MailError:
            # The link still works; the invitation just has to be passed on
            # by hand. Reporting success here would be a lie.
            pass
    return {"token": token, "link": link, "emailed": sent}


@app.get("/api/shares/{token}")
async def peek_share(token: str):
    """What is behind an invitation. Deliberately open.

    Someone who has not signed in yet has to be able to see what they are
    being offered before being asked to make an account for it. It reveals a
    title, a track count and a first name — which is what the person sharing
    it chose to send.
    """
    details = await library.peek_share(token)
    if details is None:
        raise HTTPException(status_code=404,
                            detail="That invitation has expired or never existed")
    return details


@app.post("/api/shares/{token}/claim")
async def claim_share(token: str, user: Dict[str, Any] = Depends(current_user)):
    result = await library.claim_share(token, user_id=user["id"])
    if result is None:
        raise HTTPException(status_code=404,
                            detail="That invitation no longer points anywhere")
    return result


# ── Sets ─────────────────────────────────────────────────────────────────

@app.get("/api/sets")
async def list_sets(limit: int = Query(50, ge=1, le=200),
                    user: Dict[str, Any] = Depends(current_user)):
    return await library.list_sets(limit=limit, user_id=user["id"])


@app.get("/api/sets/{set_id}")
async def get_set(set_id: str, user: Dict[str, Any] = Depends(current_user)):
    data = await library.get_set(set_id, user_id=user["id"])
    if data is None:
        raise HTTPException(status_code=404, detail="Set not found")
    data.pop("audio_path", None)

    # A track's `bpm` here is the tempo the DJ played it at. Where the record
    # itself has been downloaded and measured, its own tempo comes along
    # beside it under `record` — both are true and the page has to be able to
    # tell them apart, which one unlabelled field cannot.
    described = await library.described_by_key(user_id=user["id"])
    if described:
        for track in data.get("tracks", []):
            found = described.get(track.get("key") or track.get("track_key"))
            if found:
                track["record"] = found
    return data


@app.post("/api/acquire/describe")
async def describe_downloads(user: Dict[str, Any] = Depends(current_user)):
    """Measure every downloaded file nobody has looked at yet.

    Deliberately manual. A full sweep of a large crate is hours of CPU on a
    host that runs five other projects, and that should be a decision somebody
    made rather than something a deploy started on its own. New downloads are
    described without asking; this is for the backlog.
    """
    from .acquire.describe import sweep

    report = await sweep(library, user_id=user["id"])
    return report.as_dict()


@app.delete("/api/sets/{set_id}")
async def delete_set(set_id: str, user: Dict[str, Any] = Depends(current_user)):
    data = await library.get_set(set_id, user_id=user["id"])
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
    await library.delete_set(set_id, user_id=user["id"])
    return {"deleted": True}


@app.api_route("/api/sets/{set_id}/audio", methods=["GET", "HEAD"])
async def stream_set_audio(set_id: str, request: Request,
                           user: Dict[str, Any] = Depends(current_user)):
    """Serve a set's audio with Range support, so the player can seek.

    Without ranges a browser downloads the whole mix before it can jump to
    01:12:00 — unusable for a three-hour set.
    """
    data = await library.get_set(set_id, user_id=user["id"])
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
async def export_set(set_id: str, fmt: str, user: Dict[str, Any] = Depends(current_user)):
    data = await library.get_set(set_id, user_id=user["id"])
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
                    limit: int = Query(100, ge=1, le=500),
                    user: Dict[str, Any] = Depends(current_user)):
    return await library.recurring_tracks(min_sets=min_sets, limit=limit,
                                          user_id=user["id"])


@app.get("/api/library/search")
async def search_library(q: str = "", bpm_min: Optional[float] = None,
                         bpm_max: Optional[float] = None,
                         camelot: Optional[str] = None,
                         starred: bool = False,
                         limit: int = Query(200, ge=1, le=1000),
                         user: Dict[str, Any] = Depends(current_user)):
    return await library.search_tracks(q, bpm_min=bpm_min, bpm_max=bpm_max,
                                       camelot=camelot, starred_only=starred,
                                       limit=limit, user_id=user["id"])


@app.get("/api/tracks/{track_key}/preview")
async def track_preview(track_key: str,
                        user: Dict[str, Any] = Depends(current_user)):
    """A ~30 second excerpt of the identified record, for checking by ear.

    A tracklist is a set of claims, and the only way to check one is to hear
    the record next to the moment it was claimed for. A wrong match at low
    confidence looks exactly like a right one until you do.

    Answers 200 with a null url rather than 404 when there is no excerpt to be
    had: a dub or a white label simply is not on Apple Music, which for a set
    worth digging through is most of it. That is an ordinary outcome, not an
    error, and the interface says "no preview" rather than showing a failure.
    """
    cached = await library.preview_for(track_key)
    if cached:
        return {"key": track_key, "url": _preview_proxy(track_key),
                "source": "cached"}

    names = await library.track_names(track_key)
    if names is None:
        raise HTTPException(status_code=404, detail="Track not found")

    url = await preview_lookup.find_preview(
        names["artist"], names["title"], names["isrc"])
    if url:
        await library.remember_preview(track_key, url)
    return {"key": track_key, "url": _preview_proxy(track_key) if url else None,
            "source": "lookup" if url else "none"}


def _preview_proxy(track_key: str) -> str:
    return f"/api/tracks/{urllib.parse.quote(track_key, safe='')}/preview.m4a"


@app.get("/api/tracks/{track_key}/preview.m4a")
async def stream_track_preview(track_key: str,
                               user: Dict[str, Any] = Depends(current_user)):
    """Serve the excerpt through here rather than linking Apple directly.

    Apple labels these `audio/x-m4p`, which browsers decline to play — the
    bytes are ordinary unprotected AAC, verified with ffprobe, but the content
    type alone is enough for the element to refuse. Relabelling it is the
    reason this exists.

    Two things fall out of it for free: Apple never sees who is listening, and
    no future content-security policy has to allow an external audio host.
    """
    url = await library.preview_for(track_key)
    if not url:
        # Looked up here rather than by a separate call the page makes first.
        # That ordering matters in the browser: an await before play() ends
        # the user gesture, and Chrome then refuses to start the audio. With
        # the address deterministic and the lookup behind it, the page can
        # call play() synchronously on the click.
        names = await library.track_names(track_key)
        if names is None:
            raise HTTPException(status_code=404, detail="Track not found")
        url = await preview_lookup.find_preview(
            names["artist"], names["title"], names["isrc"])
        if not url:
            raise HTTPException(status_code=404,
                                detail="No excerpt for that track")
        await library.remember_preview(track_key, url)

    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url) as upstream:
                if upstream.status != 200:
                    raise HTTPException(
                        status_code=502,
                        detail="The excerpt could not be fetched")
                body = await upstream.read()
    except aiohttp.ClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return Response(
        content=body,
        media_type="audio/mp4",
        headers={
            # Thirty seconds of audio, immutable once published: worth letting
            # the browser keep so re-checking a match costs nothing.
            "Cache-Control": "private, max-age=86400",
            "Content-Length": str(len(body)),
        },
    )


class FeedbackRequest(BaseModel):
    verdict: str                       # "right" or "wrong"
    note: Optional[str] = None


@app.post("/api/sets/{set_id}/tracks/{position}/feedback")
async def track_feedback(set_id: str, position: int,
                         request: FeedbackRequest,
                         user: Dict[str, Any] = Depends(current_user)):
    """Say whether an identification is right or wrong.

    Both verdicts, because only one of them is not data. A rule that kills
    every wrong answer also kills some right ones, and without the right ones
    recorded there is no way to see that it did.

    This does not change the tracklist. Nothing here can correct Shazam — the
    identification is theirs. What a verdict does is make our own heuristics
    measurable: the one piece of feedback this project has had proved that
    every invented match sat under half a probe window, and that became a
    rule. Labels are how the next one gets found rather than guessed.
    """
    if request.verdict not in ("right", "wrong"):
        raise HTTPException(status_code=400,
                            detail="verdict must be 'right' or 'wrong'")
    ok = await library.record_feedback(
        set_id, position, request.verdict, user_id=user["id"],
        note=(request.note or "")[:500])
    if not ok:
        raise HTTPException(status_code=404, detail="No such track in that set")
    return {"recorded": True, "verdict": request.verdict}


@app.get("/api/sets/{set_id}/feedback")
async def set_feedback(set_id: str,
                       user: Dict[str, Any] = Depends(current_user)):
    """Verdicts already given on this set, so the interface can show them."""
    return await library.feedback_for(set_id, user_id=user["id"])


@app.get("/api/library/appearances")
async def track_appearances(key: str,
                            user: Dict[str, Any] = Depends(current_user)):
    """Which of your sets a record turns up in, and at what moment.

    A track appearing across several sets is the strongest signal this tool
    produces — and until now it was a number on a card that led nowhere.
    """
    if not key:
        raise HTTPException(status_code=400, detail="A track key is required")
    return await library.appearances(key, user_id=user["id"])


@app.get("/api/library/crate")
async def get_crate(user: Dict[str, Any] = Depends(current_user)):
    return await library.crate(user_id=user["id"])


@app.post("/api/library/star")
async def star_track(request: StarRequest, user: Dict[str, Any] = Depends(current_user)):
    if not request.key:
        raise HTTPException(status_code=400, detail="Track key is required")
    starred = await library.toggle_star(request.key, request.title,
                                        request.artist, user_id=user["id"])
    return {"key": request.key, "starred": starred}


# ── Acquisition ──────────────────────────────────────────────────────────

@app.get("/api/acquire/sources")
async def acquisition_sources(artist: str = "", title: str = "", isrc: str = "",
                              user: Dict[str, Any] = Depends(current_user)):
    sources = acquire_resolve.resolve(
        artist, title, isrc=isrc,
        soulseek_configured=acquire_resolve.soulseek_configured())
    return {"sources": [s.to_dict() for s in sources],
            "soulseek_configured": acquire_resolve.soulseek_configured()}


@app.get("/api/acquire/soulseek/status")
async def soulseek_status(user: Dict[str, Any] = Depends(current_user)):
    client = SlskdClient()
    if not client.configured:
        return {"configured": False, "reachable": False,
                "hint": "Set SLSKD_URL and SLSKD_API_KEY to enable Soulseek."}
    return {"configured": True, "reachable": await client.health()}


@app.post("/api/acquire/soulseek/search")
async def soulseek_search(request: SoulseekSearchRequest, user: Dict[str, Any] = Depends(current_user)):
    query = request.query or search_query(request.artist, request.title)
    if not query.strip():
        raise HTTPException(status_code=400, detail="Nothing to search for")
    try:
        candidates = await SlskdClient().search(query)
    except SlskdError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    # Capped so a popular record does not ship a thousand rows to the
    # browser. Reported rather than applied silently: a list that stops at
    # forty without saying so reads as "that is all there is".
    shown = candidates[:SEARCH_RESULT_CAP]
    return {"query": query, "total": len(candidates),
            "truncated": len(candidates) > len(shown),
            "candidates": [c.to_dict() for c in shown]}


@app.post("/api/acquire/soulseek/download")
async def soulseek_download(request: SoulseekDownloadRequest, user: Dict[str, Any] = Depends(current_user)):
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


@app.get("/api/acquire/soulseek/transfer")
async def soulseek_transfer(username: str, filename: str,
                            user: Dict[str, Any] = Depends(current_user)):
    """How a queued Soulseek download is getting on.

    Without this the interface said "Queued" and stopped, so a transfer that
    had finished looked exactly like one that was stuck behind forty people —
    and a peer that never answered looked the same again.

    Answers 200 with `known: false` rather than 404 when slskd has no record
    of it: a transfer it has forgotten is not an error, it is one that ended
    long enough ago to be swept.
    """
    try:
        state = await SlskdClient().transfer_state(username, filename)
    except SlskdError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if state is None:
        return {"known": False}
    return {"known": True, **state}


@app.get("/api/acquire/query")
async def acquire_query(artist: str, title: str,
                        user: Dict[str, Any] = Depends(current_user)):
    """What would be asked of Soulseek for this track.

    Its own endpoint because it has to be on screen *before* the search
    finishes — a Soulseek search takes twenty seconds, and the query is the
    first thing worth knowing when the answer comes back thin. Costs nothing:
    it is string handling, and no peer is contacted.

    Built here rather than in the browser so there is one implementation. Two
    would drift, and the one on screen would stop being the one that was sent.
    """
    from src.acquire.slskd import search_query

    return {"query": search_query(artist, title)}


@app.get("/api/acquire/candidates")
async def acquire_candidates(artist: str, title: str,
                             limit: int = Query(5, ge=1, le=20),
                             user: Dict[str, Any] = Depends(current_user)):
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
async def acquire_track_endpoint(request: AcquireRequest,
                                 user: Dict[str, Any] = Depends(current_user)):
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
        request.key, request.artist, request.title, user_id=user["id"])

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
                         limit: int = Query(50, ge=1, le=200),
                         user: Dict[str, Any] = Depends(current_user)):
    """Download attempts, for one track or the most recent overall."""
    if key:
        return await library.downloads_for(key, user_id=user["id"])
    return await library.recent_downloads(limit, user_id=user["id"])


@app.get("/api/acquire/downloads/{download_id}")
async def get_download(download_id: int, user: Dict[str, Any] = Depends(current_user)):
    row = await library.get_download(download_id, user_id=user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="Download not found")
    return row


@app.get("/api/acquire/downloads/{download_id}/file")
async def serve_download(download_id: int, user: Dict[str, Any] = Depends(current_user)):
    """Hand the file to the browser.

    The server is not storage: this is how a track reaches you, and the file is
    swept on the same schedule as set audio.
    """
    stored = await library.download_path(download_id, user_id=user["id"])
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
async def list_watches(user: Dict[str, Any] = Depends(current_user)):
    return await library.list_watches(user_id=user["id"])


@app.post("/api/watches")
async def add_watch(request: WatchRequest, user: Dict[str, Any] = Depends(current_user)):
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
    await library.add_watch(watch_id, url, title, user_id=user["id"])
    return {"id": watch_id, "url": url, "title": title}


@app.delete("/api/watches/{watch_id}")
async def delete_watch(watch_id: str, user: Dict[str, Any] = Depends(current_user)):
    if not await library.delete_watch(watch_id, user_id=user["id"]):
        raise HTTPException(status_code=404, detail="Watch not found")
    return {"deleted": True}


@app.post("/api/watches/{watch_id}/check")
async def check_watch(watch_id: str, limit: int = Query(20, ge=1, le=100),
                      user: Dict[str, Any] = Depends(current_user)):
    """List what is new on a followed channel since the last check."""
    watches = {w["id"]: w for w in await library.list_watches(
        user_id=user["id"])}
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
