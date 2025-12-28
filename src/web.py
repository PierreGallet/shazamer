#!/usr/bin/env python3
import os
import json
import asyncio
import uuid
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from src.shazamer import DJSetAnalyzer

app = FastAPI(title="Shazamer Web")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory="src/static"), name="static")

# Configuration
UPLOAD_FOLDER = Path("uploads")
OUTPUT_FOLDER = Path("outputs")
TMP_FOLDER = Path("tmp")
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'flac', 'm4a', 'ogg', 'wma', 'aac'}

# Create necessary directories
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)
TMP_FOLDER.mkdir(exist_ok=True)

# Store analysis tasks
analysis_tasks: Dict[str, dict] = {}

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

@app.get("/")
async def index():
    return FileResponse("src/static/index.html")

def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    # Validate file
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file selected")
    
    if not allowed_file(file.filename):
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid file type. Allowed types: {', '.join(ALLOWED_EXTENSIONS)}"
        )
    
    # Check file size
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 500MB")
    
    # Save uploaded file
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    unique_filename = f"{timestamp}_{file.filename}"
    filepath = UPLOAD_FOLDER / unique_filename
    
    with open(filepath, "wb") as f:
        f.write(content)
    
    # Generate task ID
    task_id = str(uuid.uuid4())
    
    # Initialize task status
    analysis_tasks[task_id] = {
        'status': 'pending',
        'progress': 0,
        'message': 'Starting analysis...',
        'filename': file.filename,
        'filepath': str(filepath),
        'start_time': datetime.now().isoformat()
    }
    
    # Start analysis in background
    asyncio.create_task(analyze_file(task_id, str(filepath), file.filename))
    
    return {"task_id": task_id, "filename": file.filename}

async def analyze_file(task_id: str, filepath: str, original_filename: str):
    try:
        # Update status
        analysis_tasks[task_id]['status'] = 'processing'
        analysis_tasks[task_id]['message'] = 'Loading audio file...'
        analysis_tasks[task_id]['progress'] = 10
        analysis_tasks[task_id]['current_segment'] = 0
        analysis_tasks[task_id]['total_segments'] = 0
        
        # Create custom analyzer with progress callback
        class ProgressAnalyzer(DJSetAnalyzer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.task_id = task_id
                self.total_segments = 0
                self.current_segment = 0
            
            async def recognize_segment(self, audio_path: str, start_time: float) -> Optional[Dict]:
                # Update progress for each segment
                self.current_segment += 1
                progress = 20 + int((self.current_segment / self.total_segments) * 70)  # 20-90% for recognition
                
                analysis_tasks[self.task_id]['progress'] = progress
                analysis_tasks[self.task_id]['message'] = f'Analyzing track {self.current_segment}/{self.total_segments}...'
                analysis_tasks[self.task_id]['current_segment'] = self.current_segment
                analysis_tasks[self.task_id]['total_segments'] = self.total_segments
                
                return await super().recognize_segment(audio_path, start_time)
            
            def detect_song_boundaries(self, audio_data: np.ndarray, sample_rate: int) -> List[int]:
                analysis_tasks[self.task_id]['message'] = 'Detecting song boundaries...'
                analysis_tasks[self.task_id]['progress'] = 15
                
                boundaries = super().detect_song_boundaries(audio_data, sample_rate)
                self.total_segments = len(boundaries) - 1
                analysis_tasks[self.task_id]['total_segments'] = self.total_segments
                return boundaries
        
        # Create analyzer
        analyzer = ProgressAnalyzer(filepath, debug=False)
        analyzer.task_id = task_id
        
        # Run analysis
        results = await analyzer.analyze()
        
        # Update progress for deduplication
        analysis_tasks[task_id]['progress'] = 95
        analysis_tasks[task_id]['message'] = 'Processing results and removing duplicates...'
        
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
        with open(json_output, 'w') as f:
            json.dump(deduplicated_results, f, indent=2)
        
        # Save TXT
        with open(txt_output, 'w') as f:
            for track in deduplicated_results:
                confidence = f" [{track['match_count']} matches]" if 'match_count' in track else ""
                f.write(f"{track['start_time']} - {track['title']} - {track['artist']}{confidence}\n")
        
        # Update task status
        analysis_tasks[task_id] = {
            'status': 'completed',
            'progress': 100,
            'message': f'Found {len(deduplicated_results)} unique tracks',
            'results': deduplicated_results,
            'json_output': str(json_output),
            'txt_output': str(txt_output),
            'filename': original_filename,
            'end_time': datetime.now().isoformat(),
            'total_segments': analyzer.total_segments,
            'unique_tracks': len(deduplicated_results),
            'total_tracks_found': len(results)
        }
        
    except Exception as e:
        analysis_tasks[task_id] = {
            'status': 'error',
            'progress': 0,
            'message': 'Analysis failed',
            'error': str(e),
            'filename': original_filename
        }
    finally:
        # Clean up uploaded file
        try:
            os.remove(filepath)
        except:
            pass

@app.get("/api/status/{task_id}", response_model=TaskStatus)
async def get_task_status(task_id: str):
    if task_id not in analysis_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task = analysis_tasks[task_id]
    return TaskStatus(
        task_id=task_id,
        status=task.get('status', 'unknown'),
        progress=task.get('progress', 0),
        message=task.get('message', ''),
        filename=task.get('filename'),
        results=task.get('results'),
        json_output=task.get('json_output'),
        txt_output=task.get('txt_output'),
        error=task.get('error'),
        current_segment=task.get('current_segment'),
        total_segments=task.get('total_segments'),
        unique_tracks=task.get('unique_tracks'),
        total_tracks_found=task.get('total_tracks_found')
    )

@app.get("/api/download/{task_id}/{format}")
async def download_result(task_id: str, format: str):
    if task_id not in analysis_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task = analysis_tasks[task_id]
    if task['status'] != 'completed':
        raise HTTPException(status_code=400, detail="Analysis not completed")
    
    if format == 'json':
        filepath = task['json_output']
    elif format == 'txt':
        filepath = task['txt_output']
    else:
        raise HTTPException(status_code=400, detail="Invalid format. Use 'json' or 'txt'")
    
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileResponse(filepath, filename=os.path.basename(filepath))

@app.get("/api/recent", response_model=List[AnalysisResult])
async def get_recent_analyses():
    output_files = []
    
    for json_file in OUTPUT_FOLDER.glob('*_tracklist.json'):
        try:
            with open(json_file) as f:
                data = json.load(f)
                
            txt_file = json_file.with_suffix('.txt')
            output_files.append(AnalysisResult(
                filename=json_file.stem.replace('_tracklist', ''),
                track_count=len(data),
                created=datetime.fromtimestamp(json_file.stat().st_mtime).strftime('%Y-%m-%d %H:%M'),
                json_path=str(json_file),
                txt_path=str(txt_file) if txt_file.exists() else ""
            ))
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
    
    if not filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="Only .txt files can be viewed")
    
    try:
        # Try to load corresponding JSON file to get URLs
        json_filename = filename.replace('.txt', '.json')
        json_filepath = OUTPUT_FOLDER / json_filename
        tracks_data = {}
        
        if json_filepath.exists():
            with open(json_filepath, 'r', encoding='utf-8') as f:
                tracks = json.load(f)
                # Create a lookup by artist and title
                for track in tracks:
                    key = f"{track.get('title', '')} - {track.get('artist', '')}"
                    tracks_data[key] = track.get('shazam_url', '')
        
        # Read the text file and enhance it with links
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        enhanced_content = []
        for line in lines:
            line = line.strip()
            if line and ' - ' in line:
                # Extract time, title, and artist
                parts = line.split(' - ', 2)
                if len(parts) >= 3:
                    time_part = parts[0]
                    title = parts[1]
                    artist_and_confidence = parts[2]
                    
                    # Remove confidence info from artist
                    artist = artist_and_confidence.split(' [')[0] if ' [' in artist_and_confidence else artist_and_confidence
                    confidence = ' [' + artist_and_confidence.split(' [')[1] if ' [' in artist_and_confidence else ''
                    
                    # Look for URL
                    key = f"{title} - {artist}"
                    url = tracks_data.get(key, '')
                    
                    if url:
                        enhanced_line = f"{time_part} - <a href='{url}' target='_blank' style='color: #667eea; text-decoration: none; border-bottom: 1px dotted #667eea;'>{title}</a> - {artist}{confidence}"
                    else:
                        enhanced_line = line
                    
                    enhanced_content.append(enhanced_line)
                else:
                    enhanced_content.append(line)
            else:
                enhanced_content.append(line)
        
        return {"content": '\n'.join(enhanced_content), "filename": filename, "is_html": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)