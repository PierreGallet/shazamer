# Shazamer 🎵

> Automatically identify tracks in DJ sets and long audio mixes using audio fingerprinting

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Release](https://img.shields.io/github/release/pierregallet/shazamer.svg)](https://github.com/pierregallet/shazamer/releases/latest)
[![Release Please](https://github.com/pierregallet/shazamer/actions/workflows/release-please.yml/badge.svg)](https://github.com/pierregallet/shazamer/actions/workflows/release-please.yml)

Shazamer analyzes long audio files (DJ sets, playlists, radio shows) to automatically detect song boundaries and identify tracks using the Shazam API. Perfect for DJs, radio hosts, and music enthusiasts who want to generate tracklists from their mixes.

## Features

- 🎵 **Automatic Track Detection**: Uses spectral analysis to detect song boundaries in continuous mixes
- 🔍 **Shazam Integration**: Identifies tracks using audio fingerprinting
- 📊 **Confidence Scoring**: Shows match count for each track (1-20 matches)
- 🕒 **Timestamp Tracking**: Precise timestamps for each detected track
- 📁 **Multiple Output Formats**: JSON (detailed) and TXT (simple) outputs
- ⚡ **Async Processing**: Fast, parallel recognition of multiple segments
- 🎛️ **Customizable Parameters**: Adjust detection sensitivity and minimum song duration
- 🌐 **Web Interface**: Easy-to-use web UI for drag-and-drop analysis

## Prerequisites

- **Python 3.12**: Required (shazamio has compatibility issues with Python 3.13+)
  - The Makefile will automatically install Python 3.12 if not present
- **FFmpeg**: Required for audio processing. Install with:
  ```bash
  # macOS
  brew install ffmpeg
  
  # Ubuntu/Debian
  sudo apt-get install ffmpeg
  
  # Windows
  # Download from https://ffmpeg.org/download.html
  ```

## Installation

### Using Make (Recommended)
```bash
make install
```

This will automatically:
- Install Homebrew (on macOS) if not present
- Install Python 3.12 if not present
- Install uv package manager for fast dependency management
- Create a virtual environment
- Install all dependencies

### Manual Installation
```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment with Python 3.12
uv venv venv --python python3.12

# Install dependencies
uv pip install -r requirements.txt
```

## Usage

### Web Interface (Easiest)
```bash
# Start the web interface
make web

# Then open http://localhost:5000 in your browser
```

The web interface provides:
- Drag-and-drop file upload
- Real-time progress tracking
- Visual results display
- Download results as JSON or TXT
- Recent analyses history

### Command Line
```bash
# Analyze any audio file directly
make ~/Music/your_dj_set.mp3

# Or with spaces in the filename
make analyze FILE="/path/to/my dj set.mp3"
```

### Manual Usage
```bash
# Basic usage with uv
uv run python src/shazamer.py your_dj_set.mp3
# Creates: outputs/your_dj_set_tracklist.json and outputs/your_dj_set_tracklist.txt

# With custom output
uv run python src/shazamer.py mix.mp3 -o outputs/summer_mix_2024.json

# With options
uv run python src/shazamer.py your_dj_set.mp3 --min-song-duration 45 --threshold 0.4
```

Output files will be saved in the `outputs/` directory (created automatically).

### Options

- `-o, --output`: Custom output file path (default: outputs/<input_filename>_tracklist.json)
- `--min-song-duration`: Minimum song duration in seconds (default: auto-adjusted based on audio length)
- `--threshold`: Peak detection threshold 0-1 (default: auto-adjusted based on audio length)
  - Lower values (0.1-0.2) = More sensitive, detects more boundaries
  - Higher values (0.4-0.5) = Less sensitive, detects fewer boundaries
- `--debug`: Enable debug mode to see full Shazam responses

### Parameter Recommendations

The tool automatically adjusts parameters based on the audio duration if you use defaults:

| Audio Duration | Threshold | Min Song Duration | Typical Use Case |
|----------------|-----------|-------------------|------------------|
| < 1 hour       | 0.30      | 30 seconds        | Short DJ sets, radio shows |
| 1-2 hours      | 0.25      | 45 seconds        | Standard DJ sets |
| 2-3 hours      | 0.20      | 60 seconds        | Extended sets |
| > 3 hours      | 0.15      | 90 seconds        | Long festival sets, marathons |

**Manual Override Examples:**
```bash
# For a very smooth, minimal mix with long transitions
uv run python src/shazamer.py smooth_mix.mp3 --threshold 0.1 --min-song-duration 120

# For a fast-paced mix with quick transitions
uv run python src/shazamer.py hardcore_mix.mp3 --threshold 0.4 --min-song-duration 20

# For a radio show with talk segments
uv run python src/shazamer.py radio_show.mp3 --threshold 0.35 --min-song-duration 90
```

## How it works

1. **Audio Loading**: Loads the entire audio file using librosa
2. **Boundary Detection**: Uses spectral analysis to detect transitions between songs:
   - Analyzes spectral centroid (frequency balance)
   - Monitors RMS energy changes
   - Finds peaks in combined features to identify transitions
3. **Song Recognition**: Each detected segment is sent to Shazam for identification
4. **Output**: Generates a JSON file with track information and timestamps

## Output Format

The tool outputs two files:

### JSON Format
Contains detailed information for each recognized track:
```json
[
  {
    "title": "Song Title",
    "artist": "Artist Name",
    "start_time": "00:00:00",
    "start_time_seconds": 0.0,
    "shazam_url": "https://www.shazam.com/track/...",
    "match_count": 15
  },
  ...
]
```

### TXT Format
Simple format with one track per line:
```
00:00:00 - Song Title - Artist Name [15 matches]
00:04:13 - Another Song - Another Artist [12 matches]
```

The `match_count` indicates the number of potential song matches found by Shazam:
- **1-5 matches**: High confidence (clear match)
- **6-15 matches**: Medium confidence (some ambiguity)
- **16+ matches**: Low confidence (many potential songs, unclear match)

## Requirements

- Python 3.10+
- Audio file in a format supported by librosa (MP3, WAV, FLAC, etc.)
- Internet connection for Shazam API

## Example Output

### Console Output
```
Analysis complete! Found 24 unique tracks:
--------------------------------------------------------------------------------
[00:00:00] J.1.0 - Crystaline (Original Mix)
[00:04:13] Faithless - Drifting Away (Paradiso Remix)
[00:06:26] Andy Compton - That Acid Track
[00:07:16] BLR & Rave & Crave - Taj
...
--------------------------------------------------------------------------------

Full tracklist saved to:
  JSON: outputs/tracklist.json
  TXT: outputs/tracklist.txt
```

### TXT Output (tracklist.txt)
```
00:00:00 - Crystaline (Original Mix) - J.1.0 [1 matches]
00:04:13 - Drifting Away (Paradiso Remix) - Faithless [3 matches]
00:06:26 - That Acid Track - Andy Compton [20 matches]
...
```

## How It Works

1. **Audio Analysis**: The tool loads your audio file and analyzes spectral features (spectral centroid and RMS energy) to detect transitions between songs
2. **Boundary Detection**: Peaks in the combined feature analysis indicate potential song boundaries
3. **Track Recognition**: Each detected segment is sent to Shazam for identification
4. **Deduplication**: Duplicate tracks are automatically removed from the final tracklist

## Notes & Limitations

- Song boundary detection works best with clear transitions between tracks
- Very smooth DJ mixes might not have all transitions detected  
- Recognition depends on tracks being in Shazam's database
- Underground/unreleased tracks may not be recognized
- The tool includes rate limiting to respect Shazam API limits
- Processing time depends on the length of the audio file (approximately 1-2 minutes per hour of audio)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. For major changes, please open an issue first to discuss what you would like to change.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [shazamio](https://github.com/shazamio/shazamio) for the Python Shazam API implementation
- [librosa](https://librosa.org/) for audio analysis capabilities
- [pydub](https://github.com/jiaaro/pydub) for audio file handling