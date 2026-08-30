"""Whether a file's declared bitrate is true of its audio or only of its header.

`Candidate.quality_label` builds "MP3 320 kbps" out of fields slskd reads from
the file the peer is offering (`src/acquire/slskd.py:133`). Those describe the
container. On a network where files have been re-encoded and re-shared for
twenty years, the container and the audio come apart routinely.

Every lossy encoder throws away the top of the spectrum, and where it stops
scales with the bitrate. That cutoff is a property of the audio and a
re-encode cannot put back what the first encode discarded. Measured on one
minute of a real set, encoded by libmp3lame at each rate and then re-encoded:

    file                  declared   cutoff
    enc_96.mp3              96 kbps   15.2 kHz
    enc_128.mp3            128 kbps   16.1 kHz
    enc_192.mp3            192 kbps   18.5 kHz
    enc_256.mp3            256 kbps   19.1 kHz
    enc_320.mp3            320 kbps   20.0 kHz
    fake_320.mp3           320 kbps   16.1 kHz   <- a 128 in a 320 wrapper
    fake_320_from96.mp3    320 kbps   15.2 kHz   <- a 96 in a 320 wrapper

Nothing cheaper finds those two. All three 320s are 2 402 263 bytes, to the
byte, and all three imply 320.3 kbps from size over duration — because the
fraud really is 320 kbps of data, reconstructed from 128 kbps of information.

WHAT IT FINDS, AND WHAT IT CANNOT PROMISE
-----------------------------------------
A cutoff is evidence that content is *missing*, and missing content cannot be
faked back. So a flag is a real finding. The absence of one is not a
clearance.

LAME's lowpass turns out to be content-dependent rather than purely a function
of the bitrate. Encoding dense broadband material at 128 kbps, it keeps the
whole band and starves the bits elsewhere instead — measured, on a pink-noise
source with a beat:

    kHz            14      15      16    16.5      17      18      20      21
    music 128   -49.0   -51.4   -59.2   -83.4  -121.9  -117.5  -116.6  -121.3
    music 320   -49.3   -50.9   -58.7   -59.7   -62.6   -66.9   -63.2  -121.3
    noise 128   -41.0   -41.3   -41.4   -41.4   -41.6   -41.6   -46.5  -132.1
    noise 320   -41.2   -41.6   -41.6   -41.5   -41.6   -41.7   -46.4  -132.2

The music rows separate by 40 dB at 16.5 kHz. The noise rows are identical.

So a dense, loud, noise-like record transcoded from 128 can pass unflagged.
That is the right way round for a label: a false negative is the status quo,
and a false positive would accuse a file of something it did not do.

WHAT THIS IS NOT FOR
--------------------
It never stops a download. The file is saved, always, and this is a sentence
printed beside it. A wrong label is visible and can be ignored; a refused
download is a dead end, and the person using this was explicit about which he
wants.

It also cannot run before a download. The audio is on somebody else's machine
until the transfer finishes, so the picker cannot rank by this — see the RFC
for the two other routes that were measured and rejected.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Enough audio to average over, not so much that a long track costs anything.
SECONDS = 45.0
FRAME = 8192
SAMPLE_RATE = 44100

# How far below the loudest bin a frequency may sit and still count as content.
# Wide, deliberately: the question is "did the encoder keep anything here at
# all", not "how much".
FLOOR_DB = -70.0

# The lowest cutoff consistent with each declared rate, in Hz, read off the
# table above with room for encoders that are more generous than libmp3lame.
# A file below its row is carrying less than it claims.
#
# Stated as a floor rather than a range because the failure is one-directional:
# an encoder can keep more than it needs to, and no encoder invents content it
# was never given.
CONSISTENT_WITH = (
    (320, 19_000),
    (256, 18_000),
    (192, 17_000),
    (128, 15_500),
    (96, 14_000),
)

# A lossless file has no cutoff — it runs to Nyquist. Below this at 44.1 kHz,
# something upstream was lossy, whatever the extension says.
LOSSLESS_FLOOR = 21_000


def cutoff_hz(path: Path, seconds: float = SECONDS) -> Optional[float]:
    """The highest frequency still carrying energy, or None if unreadable.

    Averaged over the whole passage rather than measured on one frame: a
    single frame can be silence or a cymbal crash, and what a codec discarded
    permanently only shows in the mean.
    """
    try:
        decoded = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", str(path), "-t", str(seconds),
             "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
            capture_output=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Could not decode %s: %s", path, exc)
        return None

    samples = np.frombuffer(decoded, dtype=np.float32)
    # Ten seconds is the floor. Less than that is one phrase of music, and a
    # phrase with no cymbals in it looks exactly like a transcode.
    if samples.size < SAMPLE_RATE * 10:
        return None

    frames = samples[: (samples.size // FRAME) * FRAME].reshape(-1, FRAME)
    magnitudes = np.abs(
        np.fft.rfft(frames * np.hanning(FRAME), axis=1)).mean(axis=0)
    if not np.any(magnitudes):
        return None

    decibels = 20 * np.log10(np.maximum(magnitudes, 1e-12))
    decibels -= decibels.max()
    above = np.where(decibels > FLOOR_DB)[0]
    if above.size == 0:
        return None
    return float(np.fft.rfftfreq(FRAME, 1 / SAMPLE_RATE)[above[-1]])


def assess(path: Path, declared_bitrate: int = 0,
           lossless: bool = False) -> Tuple[Optional[float], str]:
    """(cutoff in Hz, a sentence about it — empty when nothing is wrong).

    The sentence names a ceiling rather than a bitrate: a genuinely quiet
    recording with no high content is indistinguishable from a transcode, and
    "this is a 128" would be a confident lie where "consistent with at most
    128" is true of both.
    """
    found = cutoff_hz(path)
    if found is None:
        return None, ""

    if lossless:
        if found < LOSSLESS_FLOOR:
            return found, (
                f"the audio stops at {found / 1000:.1f} kHz, so this lossless "
                f"file was made from a lossy source — it costs the disk of a "
                f"FLAC and carries the information of an MP3")
        return found, ""

    if not declared_bitrate:
        return found, ""

    # The best rate this audio could honestly be. Anything above it is a claim
    # the audio does not support.
    honest = 0
    for rate, floor in reversed(CONSISTENT_WITH):
        if found >= floor:
            honest = rate
    if honest and declared_bitrate > honest:
        return found, (
            f"declares {declared_bitrate} kbps, but the audio stops at "
            f"{found / 1000:.1f} kHz — consistent with {honest} kbps at best")
    return found, ""
