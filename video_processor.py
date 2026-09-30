"""
services/video_processor.py

Handles the first step of the pipeline: turning a video (an uploaded
file, or an audio stream downloaded from a link) into a small
transcription-friendly audio file, plus the small helpers around it
(supported extensions, file hashing, duration, temp file cleanup).

PERFORMANCE NOTE (important for the project report):
The original version used MoviePy to extract audio. MoviePy decodes the
whole audio track into a NumPy array inside Python, which is slow and
needs a lot of memory for long videos. Here we call **ffmpeg directly**
and let ffmpeg stream the audio straight to a small mono 16 kHz WAV.
That keeps the CPU work inside ffmpeg (written in C) instead of Python,
and it produces a file that is roughly 10x smaller than CD-quality
stereo audio, so Whisper has far less to read.
"""

import hashlib
import os
import re
import shutil
import subprocess

# Video formats the app accepts for upload
ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm"}

# Audio settings used for transcription:
#   -ac 1           -> mono (one channel instead of two = half the data)
#   -ar 16000       -> 16 kHz, which is what Whisper works with internally
#   -c:a pcm_s16le  -> uncompressed 16-bit PCM, easy for Whisper to read
AUDIO_SAMPLE_RATE = "16000"
AUDIO_CHANNELS = "1"
AUDIO_CODEC = "pcm_s16le"


def allowed_file(filename):
    """Check the uploaded file has one of our supported video extensions."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def find_ffmpeg():
    """Return the path of a usable ffmpeg executable, or None.

    First we look on the system PATH (that is where a normal ffmpeg
    install puts it). If it is not there, we fall back to the copy that
    ships inside the 'imageio-ffmpeg' Python package, which is already
    installed as a dependency of MoviePy. That means the project works
    on a machine where ffmpeg was never installed by hand.
    """
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def extract_audio(source_path, audio_output_path):
    """Extract a small mono 16 kHz WAV from a video (or audio) file.

    source_path:       any media file ffmpeg can read (mp4, mkv, m4a, webm ...)
    audio_output_path: where to save the WAV file

    Returns audio_output_path on success.
    Raises an Exception with a readable message when the file has no
    audio track or cannot be decoded.
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise Exception(
            "ffmpeg was not found. Install ffmpeg or run: pip install imageio-ffmpeg"
        )

    command = [
        ffmpeg,
        "-y",                       # overwrite the output if it already exists
        "-i", source_path,          # input file
        "-vn",                      # no video: we never analyse video frames
        "-ac", AUDIO_CHANNELS,
        "-ar", AUDIO_SAMPLE_RATE,
        "-c:a", AUDIO_CODEC,
        "-loglevel", "error",       # keep the console clean for the demo
        audio_output_path,
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError:
        raise Exception("ffmpeg could not be started. Please check your ffmpeg installation.")

    if result.returncode != 0:
        error_text = (result.stderr or "").strip().splitlines()
        short_error = error_text[-1] if error_text else "unknown ffmpeg error"
        raise Exception(f"Audio extraction failed: {short_error}")

    if not os.path.exists(audio_output_path) or os.path.getsize(audio_output_path) == 0:
        raise Exception("This video does not contain a usable audio track.")

    return audio_output_path


def get_media_duration(media_path):
    """Return the media length in seconds (used for display and limits).

    MoviePy only reads the file header here, so this is a quick call.
    Returns None if the duration cannot be read.
    """
    try:
        from moviepy.editor import VideoFileClip
        clip = VideoFileClip(media_path)
        duration = clip.duration
        clip.close()
        return duration
    except Exception:
        pass

    try:
        from moviepy.editor import AudioFileClip
        clip = AudioFileClip(media_path)
        duration = clip.duration
        clip.close()
        return duration
    except Exception:
        pass

    # MoviePy is an OPTIONAL dependency (it is not installed by default).
    # Without it we ask ffmpeg to read the file header instead: ffmpeg
    # prints "Duration: ..." without decoding any media, so this is still
    # a fast call and works for every upload and every video link.
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        try:
            result = subprocess.run([ffmpeg, "-i", media_path],
                                    capture_output=True, text=True,
                                    errors="replace")
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
                              (result.stderr or ""))
            if match:
                hours, minutes, seconds = match.groups()
                return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except Exception:
            pass
    return None


def get_file_hash(file_path):
    """Return a SHA-256 hash of a file, read in 1 MB chunks.

    Used as the video's unique identifier. If the same file is uploaded
    twice the hash is identical, so we can load the saved study material
    from SQLite instead of processing the video again.
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def format_duration(seconds):
    """Turn 754.2 seconds into a friendly '12:34' string for the interface."""
    if not seconds:
        return "--:--"
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def cleanup_temp_files(*paths):
    """Delete temporary video/audio files. Missing files are ignored.

    The app keeps only the transcript, summary, key points, quiz and
    metadata - never the big uploaded video - so the project folder and
    the student's disk do not fill up.
    """
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            # A file we could not delete is not worth crashing the app over
            pass