#!/usr/bin/env python3
import asyncio
import argparse
import json
import sys
from pathlib import Path
from typing import List, Dict, Tuple, Optional
import numpy as np
import librosa
import soundfile as sf
from scipy.signal import find_peaks
from pydub import AudioSegment
from shazamio import Shazam
from asyncio_throttle import Throttler
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class DJSetAnalyzer:
    def __init__(self, input_file: str, min_song_duration: int = 30, 
                 peak_threshold: float = 0.3, throttle_rate: float = 0.5, 
                 debug: bool = False):
        self.input_file = Path(input_file)
        self.min_song_duration = min_song_duration
        self.peak_threshold = peak_threshold
        self.throttler = Throttler(rate_limit=throttle_rate)
        self.shazam = Shazam()
        self.debug = debug
        
    def load_audio(self) -> Tuple[np.ndarray, int]:
        logger.info(f"Loading audio file: {self.input_file}")
        audio_data, sample_rate = librosa.load(str(self.input_file), sr=None, mono=True)
        logger.info(f"Audio loaded. Duration: {len(audio_data)/sample_rate:.1f} seconds, Sample rate: {sample_rate}Hz")
        return audio_data, sample_rate
    
    def detect_song_boundaries(self, audio_data: np.ndarray, sample_rate: int) -> List[int]:
        logger.info("Detecting song boundaries using spectral analysis...")
        
        # Calculate spectral centroid (good for detecting transitions)
        spectral_centroid = librosa.feature.spectral_centroid(y=audio_data, sr=sample_rate)[0]
        
        # Calculate RMS energy
        rms_energy = librosa.feature.rms(y=audio_data)[0]
        
        # Combine features with normalization
        spectral_centroid_norm = (spectral_centroid - np.mean(spectral_centroid)) / np.std(spectral_centroid)
        rms_energy_norm = (rms_energy - np.mean(rms_energy)) / np.std(rms_energy)
        
        # Calculate derivative to find rapid changes
        combined_feature = np.abs(np.gradient(spectral_centroid_norm)) + np.abs(np.gradient(rms_energy_norm))
        
        # Smooth the signal
        from scipy.ndimage import gaussian_filter1d
        combined_feature_smooth = gaussian_filter1d(combined_feature, sigma=10)
        
        # Find peaks (potential song boundaries)
        # Convert threshold (0-1) to percentile (0-100)
        percentile_threshold = (1 - self.peak_threshold) * 100
        peaks, properties = find_peaks(combined_feature_smooth, 
                                     height=np.percentile(combined_feature_smooth, percentile_threshold),
                                     distance=int(self.min_song_duration * sample_rate / 512))  # 512 is hop_length default
        
        # Convert frame indices to sample indices
        hop_length = 512
        boundaries = [0]  # Start of audio
        for peak in peaks:
            sample_idx = peak * hop_length
            boundaries.append(sample_idx)
        boundaries.append(len(audio_data))  # End of audio
        
        # Filter out segments that are too short
        filtered_boundaries = [boundaries[0]]
        for i in range(1, len(boundaries)):
            if (boundaries[i] - filtered_boundaries[-1]) / sample_rate >= self.min_song_duration:
                filtered_boundaries.append(boundaries[i])
        
        # Ensure last boundary is included
        if filtered_boundaries[-1] != boundaries[-1]:
            filtered_boundaries[-1] = boundaries[-1]
        
        logger.info(f"Detected {len(filtered_boundaries) - 1} potential songs")
        return filtered_boundaries
    
    def save_audio_segment(self, audio_data: np.ndarray, sample_rate: int, 
                          start_sample: int, end_sample: int, index: int) -> str:
        # Create tmp directory if it doesn't exist
        Path("tmp").mkdir(exist_ok=True)
        
        segment = audio_data[start_sample:end_sample]
        output_path = f"tmp/temp_segment_{index}.wav"
        sf.write(output_path, segment, sample_rate)
        return output_path
    
    async def recognize_segment(self, audio_path: str, start_time: float) -> Optional[Dict]:
        try:
            async with self.throttler:
                logger.info(f"Recognizing segment at {start_time:.1f}s...")
                result = await self.shazam.recognize(audio_path)
                
                # Debug mode: log full response
                if self.debug and result:
                    logger.debug(f"Full Shazam response: {json.dumps(result, indent=2)}")
                    if 'matches' in result:
                        match_ids = [m.get('id') for m in result.get('matches', []) if m.get('id')]
                        unique_ids = set(match_ids)
                        logger.debug(f"Match analysis: {len(match_ids)} total matches, {len(unique_ids)} unique IDs")
                        if len(match_ids) > len(unique_ids):
                            logger.debug(f"Found {len(match_ids) - len(unique_ids)} duplicate match IDs")
                
                if result and 'track' in result:
                    # Convert start_time to hh:mm:ss format
                    hours = int(start_time // 3600)
                    minutes = int((start_time % 3600) // 60)
                    seconds = int(start_time % 60)
                    time_formatted = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                    
                    # Check matches array for confidence info
                    # Count unique match IDs to avoid counting duplicates
                    matches = result.get('matches', [])
                    unique_match_ids = set(match.get('id') for match in matches if match.get('id'))
                    match_count = len(unique_match_ids)
                    
                    track_info = {
                        'title': result['track'].get('title', 'Unknown'),
                        'artist': result['track'].get('subtitle', 'Unknown'),
                        'start_time': time_formatted,
                        'start_time_seconds': start_time,
                        'shazam_url': result['track'].get('url', ''),
                        'match_count': match_count
                    }
                    
                    # Log with confidence indicator based on match count
                    if match_count <= 5:
                        confidence = "high confidence"
                    elif match_count <= 15:
                        confidence = "medium confidence"
                    else:
                        confidence = "low confidence"
                    
                    logger.info(f"Found: {track_info['artist']} - {track_info['title']} ({match_count} matches, {confidence})")
                    
                    return track_info
                else:
                    logger.warning(f"No match found for segment at {start_time:.1f}s")
                    return None
        except Exception as e:
            logger.error(f"Error recognizing segment at {start_time:.1f}s: {e}")
            return None
    
    async def analyze(self) -> List[Dict]:
        # Load audio
        audio_data, sample_rate = self.load_audio()
        
        # Detect song boundaries
        boundaries = self.detect_song_boundaries(audio_data, sample_rate)
        
        # Process each segment
        results = []
        temp_files = []
        
        try:
            logger.info(f"Processing {len(boundaries) - 1} segments...")
            
            for i in range(len(boundaries) - 1):
                start_sample = boundaries[i]
                end_sample = boundaries[i + 1]
                start_time = start_sample / sample_rate
                
                # Save segment to temporary file
                temp_file = self.save_audio_segment(audio_data, sample_rate, 
                                                  start_sample, end_sample, i)
                temp_files.append(temp_file)
                
                # Recognize the segment
                track_info = await self.recognize_segment(temp_file, start_time)
                if track_info:
                    results.append(track_info)
                
                # Progress update
                if (i + 1) % 10 == 0:
                    logger.info(f"Progress: {i + 1}/{len(boundaries) - 1} segments processed")
        
        finally:
            # Clean up temporary files even if an error occurs
            logger.info("Cleaning up temporary files...")
            for temp_file in temp_files:
                Path(temp_file).unlink(missing_ok=True)
        
        return results

async def main():
    parser = argparse.ArgumentParser(description='Analyze DJ sets and identify tracks using Shazam')
    parser.add_argument('input_file', help='Path to the audio file (DJ set or playlist)')
    parser.add_argument('-o', '--output', help='Output file for the tracklist (default: outputs/<input_filename>_tracklist.json)')
    parser.add_argument('--min-song-duration', type=int, default=30, 
                       help='Minimum song duration in seconds (default: 30)')
    parser.add_argument('--threshold', type=float, default=0.3,
                       help='Peak detection threshold (0-1, default: 0.3)')
    parser.add_argument('--debug', action='store_true',
                       help='Enable debug mode to see full Shazam responses')
    
    args = parser.parse_args()
    
    if not Path(args.input_file).exists():
        logger.error(f"Input file not found: {args.input_file}")
        sys.exit(1)
    
    analyzer = DJSetAnalyzer(
        args.input_file,
        min_song_duration=args.min_song_duration,
        peak_threshold=args.threshold,
        debug=args.debug
    )
    
    try:
        logger.info("Starting analysis...")
        results = await analyzer.analyze()
        
        # Deduplicate tracks based on title and artist
        seen_tracks = set()
        deduplicated_results = []
        for track in results:
            track_key = f"{track['artist'].lower()}_{track['title'].lower()}"
            if track_key not in seen_tracks:
                seen_tracks.add(track_key)
                deduplicated_results.append(track)
        
        logger.info(f"Deduplication: {len(results)} tracks reduced to {len(deduplicated_results)} unique tracks")
        
        # Determine output path
        if args.output:
            output_path = Path(args.output)
        else:
            # Use input filename as base for output
            input_basename = Path(args.input_file).stem
            output_path = Path(f"outputs/{input_basename}_tracklist.json")
        
        # Create output directory if it doesn't exist
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Save results in JSON format
        with open(output_path, 'w') as f:
            json.dump(deduplicated_results, f, indent=2)
        
        # Save results in TXT format
        txt_output = str(output_path).replace('.json', '.txt')
        with open(txt_output, 'w') as f:
            for track in deduplicated_results:
                confidence = f" [{track['match_count']} matches]" if 'match_count' in track else ""
                f.write(f"{track['start_time']} - {track['title']} - {track['artist']}{confidence}\n")
        
        # Print summary
        print(f"\nAnalysis complete! Found {len(deduplicated_results)} unique tracks:")
        print("-" * 80)
        for track in deduplicated_results:
            print(f"[{track['start_time']}] {track['artist']} - {track['title']}")
        print("-" * 80)
        print(f"\nFull tracklist saved to:")
        print(f"  JSON: {args.output}")
        print(f"  TXT: {txt_output}")
        
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())