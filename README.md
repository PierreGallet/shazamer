# Shazamer 🎵

> Automatically identify tracks in DJ sets and long audio mixes using audio fingerprinting

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)

Shazamer analyzes long audio files (DJ sets, playlists, radio shows) to automatically detect song boundaries and identify tracks using the Shazam API. Perfect for DJs, radio hosts, and music enthusiasts who want to generate tracklists from their mixes.

## Features

- 🎵 **Automatic Track Detection**: Uses spectral analysis to detect song boundaries in continuous mixes
- 🔍 **Shazam Integration**: Identifies tracks using audio fingerprinting
- 📊 **Confidence Scoring**: Shows match count for each track (1-20 matches)
- 🕒 **Timestamp Tracking**: Precise timestamps for each detected track
- 📁 **Multiple Output Formats**: JSON (detailed) and TXT (simple) outputs
- ⚡ **Async Processing**: Fast, parallel recognition of multiple segments
- 🎛️ **Customizable Parameters**: Adjust detection sensitivity and minimum song duration

## Prerequisites

- **Python 3.12**: Required (shazamio has compatibility issues with Python 3.13+)
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

### Manual Installation
```bash
# Create virtual environment with Python 3.12
python3.12 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Usage

### Using Make (Easiest)
```bash
# Analyze any audio file directly
make ~/Music/your_dj_set.mp3

# Or with spaces in the filename
make analyze FILE="/path/to/my dj set.mp3"
```

### Manual Usage
```bash
# Activate virtual environment
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Basic usage
python shazamer.py your_dj_set.mp3
# Creates: outputs/your_dj_set_tracklist.json and outputs/your_dj_set_tracklist.txt

# With custom output
python shazamer.py mix.mp3 -o outputs/summer_mix_2024.json

# With options
python shazamer.py your_dj_set.mp3 --min-song-duration 45 --threshold 0.4
```

Output files will be saved in the `outputs/` directory (created automatically).

### Options

- `-o, --output`: Custom output file path (default: outputs/<input_filename>_tracklist.json)
- `--min-song-duration`: Minimum song duration in seconds (default: 30)
- `--threshold`: Peak detection threshold 0-1 (default: 0.3)
  - Lower values (0.1-0.2) = More sensitive, detects more boundaries
  - Higher values (0.4-0.5) = Less sensitive, detects fewer boundaries

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