#!/usr/bin/env python3
import os
import sys
import json
import asyncio
import uuid
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
import numpy as np

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import yt_dlp
import numpy as np

from src.shazamer import DJSetAnalyzer
from src.task_store import TaskStore

import logging

logger = logging.getLogger(__name__)

app = FastAPI(title="Shazamer Web")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
BASE_DIR = Path(__file__).resolve().parent.parent

# Mount static files
app.mount(
    "/static", StaticFiles(directory=str(BASE_DIR / "src" / "static")), name="static"
)
UPLOAD_FOLDER = BASE_DIR / "uploads"
OUTPUT_FOLDER = BASE_DIR / "outputs"
TMP_FOLDER = BASE_DIR / "tmp"
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
ALLOWED_EXTENSIONS = {"mp3", "wav", "flac", "m4a", "ogg", "wma", "aac"}
# Cap audio duration to keep us under the container memory limit. STFT memory
# grows linearly with audio length; 7200s (2h) at 22050Hz fits in ~2GB peak.
MAX_AUDIO_DURATION_SECONDS = int(os.environ.get("MAX_AUDIO_DURATION_SECONDS", "7200"))

# Create necessary directories
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)
TMP_FOLDER.mkdir(exist_ok=True)
TASK_STORE_DIR = BASE_DIR / "tmp" / "tasks"

# Store analysis tasks (in-memory hot cache; disk-backed via task_store)
analysis_tasks: Dict[str, dict] = {}
task_store = TaskStore(TASK_STORE_DIR)
_interrupted = task_store.mark_interrupted()
if _interrupted:
    logger.info("Marked %d in-flight task(s) as interrupted after restart", _interrupted)


def persist(task_id: str) -> None:
    """Write the current in-memory state of a task to disk.

    Call at phase transitions (status change, completion, error). Progress
    updates between phases do not need to be persisted — their absence only
    means a slightly stale bar after restart, not data loss.
    """
    task = analysis_tasks.get(task_id)
    if task is not None:
        task_store.save(task_id, task)


def probe_duration(filepath: str) -> float:
    """Return audio duration in seconds without loading the file into RAM."""
    try:
        from pydub.utils import mediainfo
        info = mediainfo(filepath)
        return float(info.get("duration", 0) or 0)
    except Exception as exc:
        logger.warning("Could not probe duration for %s: %s", filepath, exc)
        return 0.0


class TaskStatus(BaseModel):
    task_id: str
    status: str
    progress: int
    message: str
    filename: Optional[str] = None
    results: Optional[List[dict]] = None
    json_output: Optional[str] = None
    txt_output: Optional[str] = None
    error: Optional[str] = None
    current_segment: Optional[int] = None
    total_segments: Optional[int] = None
    unique_tracks: Optional[int] = None
    total_tracks_found: Optional[int] = None


class AnalysisResult(BaseModel):
    filename: str
    track_count: int
    created: str
    json_path: str
    txt_path: str


class URLDownloadRequest(BaseModel):
    url: str


@app.get("/")
async def index():
    return FileResponse("src/static/index.html")


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    # Validate file
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file selected")

    if not allowed_file(file.filename):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type. Allowed types: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    # Check file size
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413, detail="File too large. Maximum size is 500MB"
        )

    # Save uploaded file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_filename = f"{timestamp}_{file.filename}"
    filepath = UPLOAD_FOLDER / unique_filename

    with open(filepath, "wb") as f:
        f.write(content)

    # Generate task ID
    task_id = str(uuid.uuid4())

    # Initialize task status
    analysis_tasks[task_id] = {
        "status": "pending",
        "progress": 0,
        "message": "Starting analysis...",
        "filename": file.filename,
        "filepath": str(filepath),
        "start_time": datetime.now().isoformat(),
    }
    persist(task_id)

    # Start analysis in background
    asyncio.create_task(analyze_file(task_id, str(filepath), file.filename))

    return {"task_id": task_id, "filename": file.filename}


@app.post("/api/download-url")
async def download_url(request: URLDownloadRequest):
    # Validate URL
    if not request.url:
        raise HTTPException(status_code=400, detail="URL is required")

    # Generate task ID
    task_id = str(uuid.uuid4())

    # Initialize task status
    analysis_tasks[task_id] = {
        "status": "downloading",
        "progress": 0,
        "message": "Starting download...",
        "filename": "Downloading from URL...",
        "url": request.url,
        "start_time": datetime.now().isoformat(),
    }
    persist(task_id)

    # Start download and analysis in background
    asyncio.create_task(download_and_analyze(task_id, request.url))

    return {"task_id": task_id, "url": request.url}


async def download_and_analyze(task_id: str, url: str):
    filepath = None
    try:
        # Update status
        analysis_tasks[task_id]["status"] = "downloading"
        analysis_tasks[task_id]["message"] = "Downloading audio from URL..."
        analysis_tasks[task_id]["progress"] = 5

        # Configure yt-dlp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"{timestamp}_%(title)s.%(ext)s"
        output_path = UPLOAD_FOLDER / output_filename

        # Use subprocess to call yt-dlp with remote components enabled
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--remote-components", "ejs:github",
            "-f", "bestaudio/best",
            "-x",  # Extract audio
            "--audio-format", "mp3",
            "--audio-quality", "192",
            "-o", str(output_path),
            "--no-playlist",
            "--force-ipv4",
            "--newline",  # Force progress on new lines for parsing
            url
        ]

        # Run yt-dlp and parse progress from stderr in real-time
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Read stderr line by line for progress updates
        stderr_lines = []
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            line_str = line.decode(errors="replace").strip()
            stderr_lines.append(line_str)

            # Parse yt-dlp progress: [download]  45.2% of 5.23MiB ...
            if "[download]" in line_str and "%" in line_str:
                try:
                    pct_str = line_str.split("%")[0].split()[-1]
                    dl_pct = float(pct_str)
                    # Map download 0-100% to progress 2-7%
                    progress = 2 + int(dl_pct * 0.05)
                    analysis_tasks[task_id]["progress"] = progress
                    analysis_tasks[task_id]["message"] = f"Downloading audio... {dl_pct:.0f}%"
                except (ValueError, IndexError):
                    pass
            elif "[ExtractAudio]" in line_str or "Post-process" in line_str:
                analysis_tasks[task_id]["progress"] = 8
                analysis_tasks[task_id]["message"] = "Converting to MP3..."
            elif "[download] Destination:" in line_str:
                analysis_tasks[task_id]["progress"] = 3
                analysis_tasks[task_id]["message"] = "Downloading audio..."

        await process.wait()

        if process.returncode != 0:
            error_msg = "\n".join(stderr_lines[-5:]) if stderr_lines else "Unknown error"
            raise Exception(f"yt-dlp failed: {error_msg}")
        
        # Find the downloaded file
        # yt-dlp adds .mp3 extension after conversion
        possible_files = list(UPLOAD_FOLDER.glob(f"{timestamp}_*.mp3"))
        if not possible_files:
            raise Exception("Downloaded file not found")

        filepath = str(possible_files[0])
        filename = possible_files[0].name

        # Update task with filename
        analysis_tasks[task_id]["filename"] = filename
        analysis_tasks[task_id]["filepath"] = filepath

        # Now analyze the file
        analysis_tasks[task_id]["status"] = "processing"
        analysis_tasks[task_id]["message"] = "Download complete. Starting analysis..."
        analysis_tasks[task_id]["progress"] = 10
        persist(task_id)

        await analyze_file(task_id, filepath, filename)

    except Exception as e:
        analysis_tasks[task_id] = {
            "status": "error",
            "progress": 0,
            "message": "Download failed",
            "error": str(e),
            "filename": "Download failed",
        }
        persist(task_id)
        # Clean up if download failed
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except:
                pass


async def analyze_file(task_id: str, filepath: str, original_filename: str):
    try:
        # Guard: reject files that would blow the container's memory budget.
        # STFT memory scales with audio length; past this cap we'd crash uvicorn.
        duration = probe_duration(filepath)
        if duration and duration > MAX_AUDIO_DURATION_SECONDS:
            max_min = MAX_AUDIO_DURATION_SECONDS // 60
            actual_min = int(duration // 60)
            raise ValueError(
                f"Audio too long for analysis: {actual_min} min (max "
                f"{max_min} min). Please trim the file and retry."
            )

        # Update status
        analysis_tasks[task_id]["status"] = "processing"
        analysis_tasks[task_id]["message"] = "Loading audio file..."
        analysis_tasks[task_id]["progress"] = 10
        analysis_tasks[task_id]["current_segment"] = 0
        analysis_tasks[task_id]["total_segments"] = 0
        persist(task_id)

        # Create custom analyzer with progress callback
        class ProgressAnalyzer(DJSetAnalyzer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.task_id = task_id
                self.total_segments = 0
                self.current_segment = 0

            def load_audio(self):
                analysis_tasks[self.task_id]["message"] = "Decoding audio file..."
                analysis_tasks[self.task_id]["progress"] = 10
                result = super().load_audio()
                duration = len(result[0]) / result[1]
                mins = int(duration // 60)
                analysis_tasks[self.task_id]["message"] = f"Audio loaded ({mins} min). Preparing spectral analysis..."
                analysis_tasks[self.task_id]["progress"] = 12
                return result

            async def recognize_segment(
                self, audio_path: str, start_time: float
            ) -> Optional[Dict]:
                # Update progress for each segment
                self.current_segment += 1
                progress = 25 + int(
                    (self.current_segment / self.total_segments) * 65
                )  # 25-90% for recognition

                analysis_tasks[self.task_id]["progress"] = progress
                analysis_tasks[self.task_id][
                    "message"
                ] = f"Identifying track {self.current_segment}/{self.total_segments}..."
                analysis_tasks[self.task_id]["current_segment"] = self.current_segment
                analysis_tasks[self.task_id]["total_segments"] = self.total_segments

                return await super().recognize_segment(audio_path, start_time)

            def detect_song_boundaries(
                self, audio_data: np.ndarray, sample_rate: int
            ) -> List[int]:
                import librosa as _librosa
                from scipy.ndimage import gaussian_filter1d
                from scipy.signal import find_peaks as _find_peaks

                # Step 1: Spectral centroid
                analysis_tasks[self.task_id]["message"] = "Computing spectral centroid..."
                analysis_tasks[self.task_id]["progress"] = 14
                spectral_centroid = _librosa.feature.spectral_centroid(y=audio_data, sr=sample_rate)[0]

                # Step 2: RMS energy
                analysis_tasks[self.task_id]["message"] = "Computing RMS energy..."
                analysis_tasks[self.task_id]["progress"] = 16
                rms_energy = _librosa.feature.rms(y=audio_data)[0]

                # Step 3: Normalization & gradient
                analysis_tasks[self.task_id]["message"] = "Analyzing frequency transitions..."
                analysis_tasks[self.task_id]["progress"] = 18
                spectral_centroid_norm = (spectral_centroid - np.mean(spectral_centroid)) / np.std(spectral_centroid)
                rms_energy_norm = (rms_energy - np.mean(rms_energy)) / np.std(rms_energy)
                combined_feature = np.abs(np.gradient(spectral_centroid_norm)) + np.abs(np.gradient(rms_energy_norm))

                # Step 4: Smoothing & peak detection
                analysis_tasks[self.task_id]["message"] = "Detecting song boundaries..."
                analysis_tasks[self.task_id]["progress"] = 20
                combined_feature_smooth = gaussian_filter1d(combined_feature, sigma=10)
                percentile_threshold = (1 - self.peak_threshold) * 100
                peaks, _ = _find_peaks(
                    combined_feature_smooth,
                    height=np.percentile(combined_feature_smooth, percentile_threshold),
                    distance=int(self.min_song_duration * sample_rate / 512),
                )

                # Build boundaries
                hop_length = 512
                boundaries = [0]
                for peak in peaks:
                    boundaries.append(peak * hop_length)
                boundaries.append(len(audio_data))

                filtered_boundaries = [boundaries[0]]
                for i in range(1, len(boundaries)):
                    if (boundaries[i] - filtered_boundaries[-1]) / sample_rate >= self.min_song_duration:
                        filtered_boundaries.append(boundaries[i])
                if filtered_boundaries[-1] != boundaries[-1]:
                    filtered_boundaries[-1] = boundaries[-1]

                self.total_segments = len(filtered_boundaries) - 1
                analysis_tasks[self.task_id]["total_segments"] = self.total_segments
                analysis_tasks[self.task_id]["message"] = f"Found {self.total_segments} segments. Starting identification..."
                analysis_tasks[self.task_id]["progress"] = 23

                return filtered_boundaries

        # Create analyzer
        analyzer = ProgressAnalyzer(filepath, debug=False)
        analyzer.task_id = task_id

        # Run analysis
        results = await analyzer.analyze()

        # Update progress for deduplication
        analysis_tasks[task_id]["progress"] = 95
        analysis_tasks[task_id][
            "message"
        ] = "Processing results and removing duplicates..."

        # Deduplicate results
        seen_tracks = set()
        deduplicated_results = []
        for track in results:
            track_key = f"{track['artist'].lower()}_{track['title'].lower()}"
            if track_key not in seen_tracks:
                seen_tracks.add(track_key)
                deduplicated_results.append(track)

        # Save results
        base_name = Path(original_filename).stem
        json_output = OUTPUT_FOLDER / f"{base_name}_tracklist.json"
        txt_output = OUTPUT_FOLDER / f"{base_name}_tracklist.txt"

        # Check if file exists and add suffix if needed
        counter = 1
        while json_output.exists():
            json_output = OUTPUT_FOLDER / f"{base_name}_tracklist({counter}).json"
            txt_output = OUTPUT_FOLDER / f"{base_name}_tracklist({counter}).txt"
            counter += 1

        # Save JSON
        with open(json_output, "w") as f:
            json.dump(deduplicated_results, f, indent=2)

        # Save TXT
        with open(txt_output, "w") as f:
            for track in deduplicated_results:
                confidence = (
                    f" [{track['match_count']} matches]"
                    if "match_count" in track
                    else ""
                )
                f.write(
                    f"{track['start_time']} - {track['title']} - {track['artist']}{confidence}\n"
                )

        # Update task status
        analysis_tasks[task_id] = {
            "status": "completed",
            "progress": 100,
            "message": f"Found {len(deduplicated_results)} unique tracks",
            "results": deduplicated_results,
            "json_output": str(json_output),
            "txt_output": str(txt_output),
            "filename": original_filename,
            "end_time": datetime.now().isoformat(),
            "total_segments": analyzer.total_segments,
            "unique_tracks": len(deduplicated_results),
            "total_tracks_found": len(results),
        }
        persist(task_id)

    except Exception as e:
        analysis_tasks[task_id] = {
            "status": "error",
            "progress": 0,
            "message": "Analysis failed",
            "error": str(e),
            "filename": original_filename,
        }
        persist(task_id)
    finally:
        # Clean up uploaded file
        try:
            os.remove(filepath)
        except:
            pass


@app.get("/api/status/{task_id}", response_model=TaskStatus)
async def get_task_status(task_id: str):
    task = analysis_tasks.get(task_id)
    if task is None:
        # Fallback to disk: the process may have restarted while analysis was in
        # flight (OOM, redeploy). The disk copy was marked 'interrupted' at
        # startup, so the frontend sees a clean error instead of a 404.
        task = task_store.load(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        analysis_tasks[task_id] = task

    return TaskStatus(
        task_id=task_id,
        status=task.get("status", "unknown"),
        progress=task.get("progress", 0),
        message=task.get("message", ""),
        filename=task.get("filename"),
        results=task.get("results"),
        json_output=task.get("json_output"),
        txt_output=task.get("txt_output"),
        error=task.get("error"),
        current_segment=task.get("current_segment"),
        total_segments=task.get("total_segments"),
        unique_tracks=task.get("unique_tracks"),
        total_tracks_found=task.get("total_tracks_found"),
    )


@app.get("/api/download/{task_id}/{format}")
async def download_result(task_id: str, format: str):
    task = analysis_tasks.get(task_id) or task_store.load(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task["status"] != "completed":
        raise HTTPException(status_code=400, detail="Analysis not completed")

    if format == "json":
        filepath = task["json_output"]
    elif format == "txt":
        filepath = task["txt_output"]
    else:
        raise HTTPException(
            status_code=400, detail="Invalid format. Use 'json' or 'txt'"
        )

    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(filepath, filename=os.path.basename(filepath))


@app.get("/api/recent", response_model=List[AnalysisResult])
async def get_recent_analyses():
    output_files = []

    for json_file in OUTPUT_FOLDER.glob("*_tracklist.json"):
        try:
            with open(json_file) as f:
                data = json.load(f)

            txt_file = json_file.with_suffix(".txt")
            output_files.append(
                AnalysisResult(
                    filename=json_file.stem.replace("_tracklist", ""),
                    track_count=len(data),
                    created=datetime.fromtimestamp(json_file.stat().st_mtime).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    json_path=str(json_file),
                    txt_path=str(txt_file) if txt_file.exists() else "",
                )
            )
        except:
            pass

    # Sort by creation time (newest first)
    output_files.sort(key=lambda x: x.created, reverse=True)

    return output_files[:10]  # Return last 10


@app.get("/outputs/{filename}")
async def serve_output_file(filename: str):
    """Serve files from the outputs directory"""
    filepath = OUTPUT_FOLDER / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(filepath, filename=filename)


@app.get("/api/view/{filename}")
async def view_file_content(filename: str):
    """Return the content of a text file with enhanced HTML formatting"""
    filepath = OUTPUT_FOLDER / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if not filename.endswith(".txt"):
        raise HTTPException(status_code=400, detail="Only .txt files can be viewed")

    try:
        # Try to load corresponding JSON file to get URLs
        json_filename = filename.replace(".txt", ".json")
        json_filepath = OUTPUT_FOLDER / json_filename
        tracks_data = {}

        if json_filepath.exists():
            with open(json_filepath, "r", encoding="utf-8") as f:
                tracks = json.load(f)
                # Create a lookup by artist and title
                for track in tracks:
                    key = f"{track.get('title', '')} - {track.get('artist', '')}"
                    tracks_data[key] = track.get("shazam_url", "")

        # Read the text file and enhance it with links
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()

        enhanced_content = []
        for line in lines:
            line = line.strip()
            if line and " - " in line:
                # Extract time, title, and artist
                parts = line.split(" - ", 2)
                if len(parts) >= 3:
                    time_part = parts[0]
                    title = parts[1]
                    artist_and_confidence = parts[2]

                    # Remove confidence info from artist
                    artist = (
                        artist_and_confidence.split(" [")[0]
                        if " [" in artist_and_confidence
                        else artist_and_confidence
                    )
                    confidence = (
                        " [" + artist_and_confidence.split(" [")[1]
                        if " [" in artist_and_confidence
                        else ""
                    )

                    # Look for URL
                    key = f"{title} - {artist}"
                    url = tracks_data.get(key, "")

                    if url:
                        enhanced_line = f"{time_part} - <a href='{url}' target='_blank' style='color: #667eea; text-decoration: none; border-bottom: 1px dotted #667eea;'>{title}</a> - {artist}{confidence}"
                    else:
                        enhanced_line = line

                    enhanced_content.append(enhanced_line)
                else:
                    enhanced_content.append(line)
            else:
                enhanced_content.append(line)

        return {
            "content": "\n".join(enhanced_content),
            "filename": filename,
            "is_html": True,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
