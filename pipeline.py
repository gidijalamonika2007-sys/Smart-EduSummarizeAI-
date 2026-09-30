"""
services/pipeline.py

This is the "worker" that runs in the background after a video is
accepted. It is deliberately written as a list of clearly named steps so
it is easy to follow and explain:

    run_upload_job()  -> used when the student uploads a video file
    run_url_job()     -> used when the student pastes a video link

Both end up calling _transcribe_and_generate(), which does the work that
is the same in both cases and writes each finished piece of the result
straight into SQLite so the page can show it immediately.

Progress values (the percentages are UI estimates, exactly as the
instructions require - they are not a promise about remaining time):

    video received       5%
    audio preparation   15%
    audio ready         25%
    transcribing     30-60%
    transcript ready    60%
    summary/key points 60-80%
    quiz               80-95%
    complete            100%
"""

import os
import time

from database import db
from services import ai_service, job_manager, transcription, url_processor, video_processor

# Folder for the temporary audio / downloaded files. Both are deleted
# again as soon as the transcript exists, so they never pile up.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_FOLDER = os.path.join(PROJECT_ROOT, "uploads")
AUDIO_FOLDER = os.path.join(PROJECT_ROOT, "audio")

# Progress percentages used across the pipeline
PROGRESS_RECEIVED = 5
PROGRESS_AUDIO_START = 15
PROGRESS_AUDIO_END = 25
PROGRESS_TRANSCRIBE_START = 30
PROGRESS_TRANSCRIBE_END = 60
PROGRESS_SUMMARY_START = 60
PROGRESS_SUMMARY_END = 80
PROGRESS_QUIZ_START = 85
PROGRESS_DONE = 100

# Maximum video length in minutes. Set MAX_VIDEO_MINUTES in the .env file
# to change it. 0 (the default) means NO limit: every video is processed
# from beginning to end, whatever its duration.
MAX_VIDEO_MINUTES = float(os.environ.get("MAX_VIDEO_MINUTES", "0"))


def _audio_path(video_id):
    """Where the temporary WAV for this video lives."""
    os.makedirs(AUDIO_FOLDER, exist_ok=True)
    return os.path.join(AUDIO_FOLDER, f"video_{video_id}.wav")


def _stop_if_cancelled(video_id):
    """Raise a CancelledError when the student pressed "Cancel Processing"."""
    if job_manager.is_cancelled(video_id):
        raise CancelledError("Processing cancelled by the user.")


class CancelledError(Exception):
    """Raised internally to unwind the pipeline when a job is cancelled."""


# ---------------------------------------------------------------------
# JOB 1: UPLOADED VIDEO FILE
# ---------------------------------------------------------------------

def run_upload_job(video_id, upload_path, audio_path=None):
    """Process an uploaded video file.

    Steps: extract audio -> transcribe -> study notes -> quiz -> cleanup.
    The uploaded video file is deleted at the end to save disk space.
    """
    started = time.time()
    timings = {"audio": 0.0, "transcription": 0.0, "ai": 0.0}
    audio_path = audio_path or _audio_path(video_id)

    try:
        # ---- 0. Start from a clean slate -------------------------------------
        # Old transcript/summary/terms/quiz of THIS video row are removed, so
        # a re-processed video can never show results from an earlier run.
        db.clear_video_results(video_id)

        # ---- 1. Audio extraction (ffmpeg does the heavy lifting) ----
        db.update_processing_status(video_id, "PREPARING_AUDIO", PROGRESS_AUDIO_START,
                                    "Preparing audio")

        step_started = time.time()
        video_processor.extract_audio(upload_path, audio_path)
        timings["audio"] = round(time.time() - step_started, 2)

        # The extracted audio has exactly the length of the video, so we
        # can store a REAL duration for the page (ffmpeg reads only the
        # header - no decoding - so this is fast).
        duration = video_processor.get_media_duration(audio_path)
        if duration:
            _save_duration(video_id, duration)

        db.update_processing_status(video_id, "PREPARING_AUDIO", PROGRESS_AUDIO_END,
                                    "Audio ready")
        _stop_if_cancelled(video_id)

        # ---- 2. Transcript + study notes + quiz (shared code) ----
        _transcribe_and_generate(video_id, audio_path, started, timings)

    except CancelledError:
        # Nothing to do here: the status was already set when Cancel was pressed.
        pass
    except Exception as error:                 # noqa: BLE001 - reported to the student
        _save_failure(video_id, error, started, timings)
    finally:
        # The big temporary files are no longer needed - delete both. Only
        # the transcript / summary / notes / quiz / metadata stay.
        video_processor.cleanup_temp_files(upload_path, audio_path)
        job_manager.forget_job(video_id)


# ---------------------------------------------------------------------
# JOB 2: VIDEO LINK (URL)
# ---------------------------------------------------------------------

def run_url_job(video_id, url):
    """Process a video link.

    Steps: read metadata (no download) -> download ONLY the audio ->
    transcribe -> study notes -> quiz -> cleanup.
    """
    started = time.time()
    timings = {"audio": 0.0, "transcription": 0.0, "ai": 0.0}
    downloaded_audio = None
    converted_audio = _audio_path(video_id)

    try:
        # ---- 0. Start from a clean slate (see run_upload_job) ----
        db.clear_video_results(video_id)

        # ---- 1. Read the title/length first, so the page can show them early ----
        db.update_processing_status(video_id, "PREPARING_AUDIO", PROGRESS_AUDIO_START,
                                    "Reading video information")
        try:
            info = url_processor.get_url_metadata(url)
            _save_url_metadata(video_id, info)

            # Refuse very long videos BEFORE spending time downloading them
            # (only when a limit is configured - 0 means unlimited)
            duration = info.get("duration")
            if duration and MAX_VIDEO_MINUTES > 0 and duration > MAX_VIDEO_MINUTES * 60:
                raise Exception(
                    f"This video is {round(duration / 60)} minutes long, which is over the "
                    f"{int(MAX_VIDEO_MINUTES)} minute limit. Please try a shorter video."
                )
        except Exception as error:
            # A metadata failure should not stop the job while the cached
            # result may still exist - but for a fresh link it means we
            # cannot process it, so the job fails with the friendly message.
            if "minute limit" in str(error):
                raise
            # Continue and let the audio download report the real problem
            info = {}
            _save_url_metadata(video_id, {"title": None, "duration": None})

        _stop_if_cancelled(video_id)

        # ---- 2. Download the AUDIO ONLY (much smaller than the video) ----
        db.update_processing_status(video_id, "PREPARING_AUDIO", PROGRESS_AUDIO_START,
                                    "Retrieving audio from the video link")

        def download_progress(percent):
            # Downloading is part of the "audio preparation" stage (15-25%)
            overall = PROGRESS_AUDIO_START + int(percent * 10 / 100)
            db.update_processing_status(video_id, "PREPARING_AUDIO", overall,
                                        f"Retrieving audio ({percent}%)")

        step_started = time.time()
        downloaded_audio, info = url_processor.download_audio_from_url(
            url, AUDIO_FOLDER, progress_callback=download_progress
        )

        # Normalise whatever we downloaded (m4a, opus, mp3 ...) into the
        # same small mono 16 kHz WAV that uploads use, so transcription is
        # identical for both input types.
        db.update_processing_status(video_id, "PREPARING_AUDIO", PROGRESS_AUDIO_END,
                                    "Preparing audio")
        video_processor.extract_audio(downloaded_audio, converted_audio)
        timings["audio"] = round(time.time() - step_started, 2)

        _save_url_metadata(video_id, info)
        _stop_if_cancelled(video_id)

        # ---- 3. Transcript + study notes + quiz (shared code) ----
        _transcribe_and_generate(video_id, converted_audio, started, timings)

    except CancelledError:
        pass
    except Exception as error:                 # noqa: BLE001 - reported to the student
        # If yt-dlp already produced our friendly message, keep it.
        message = str(error)
        if not message or message == "None":
            message = url_processor.URL_PROCESSING_ERROR
        _save_failure(video_id, message, started, timings)
    finally:
        video_processor.cleanup_temp_files(downloaded_audio, converted_audio)
        job_manager.forget_job(video_id)


# ---------------------------------------------------------------------
# SHARED STEP: TRANSCRIBE, THEN STUDY NOTES, THEN QUIZ
# ---------------------------------------------------------------------

def _transcribe_and_generate(video_id, audio_path, started, timings):
    """The part that is identical for uploads and video links.

    The transcript is written to the database the moment it exists, so
    the student can start reading while the summary and the quiz are
    still being prepared.
    """
    _stop_if_cancelled(video_id)
    db.update_processing_status(video_id, "TRANSCRIBING", PROGRESS_TRANSCRIBE_START,
                                "Detecting speech language")

    # ---- 1. LANGUAGE is detected FIRST, from the first seconds --------
    detected_language, _probability = transcription.detect_language(audio_path)
    if detected_language:
        language_label = transcription.language_name(detected_language)
        db.set_video_language(video_id, detected_language)
        db.update_processing_status(
            video_id, "TRANSCRIBING", PROGRESS_TRANSCRIBE_START + 2,
            f"Language detected: {language_label}")
    _stop_if_cancelled(video_id)

    def transcription_progress(percent):
        # 0-100 inside Whisper -> 30-60% on the page
        overall = PROGRESS_TRANSCRIBE_START + int(
            percent * (PROGRESS_TRANSCRIBE_END - PROGRESS_TRANSCRIBE_START) / 100
        )
        db.update_processing_status(video_id, "TRANSCRIBING", overall,
                                    f"Creating transcript ({percent}%)")

    step_started = time.time()
    transcript, language = transcription.transcribe_audio(
        audio_path, progress_callback=transcription_progress,
        language=detected_language,
    )
    timings["transcription"] = round(time.time() - step_started, 2)

    # ---- 2. VALIDATE the transcript BEFORE generating anything ---------
    # A missing/near-empty transcript means transcription failed - show
    # the error instead of ever producing results from nothing.
    if not transcript or len(transcript.split()) < 5:
        raise Exception(
            "Transcription did not produce a usable transcript, so no "
            "results were generated. Please check the video's audio and "
            "try again."
        )

    if not detected_language and language and language != "unknown":
        detected_language = language
        db.set_video_language(video_id, detected_language)

    # >>> FIRST USEFUL RESULT: the transcript is saved and readable now <<<
    _save_transcript(video_id, transcript)
    word_total = transcription.count_words(transcript)
    db.update_processing_status(
        video_id, "TRANSCRIPT_READY", PROGRESS_TRANSCRIBE_END,
        f"Transcript ready ({word_total} words, "
        f"{transcription.language_name(detected_language)})")

    _stop_if_cancelled(video_id)

    # ---- Study notes: ONE AI request for summary + key points + terms ----
    db.update_processing_status(video_id, "GENERATING_SUMMARY", PROGRESS_SUMMARY_START,
                                "Creating study notes")

    video = db.get_video(video_id)
    quick_mode = bool(video.get("quick_mode", 1)) if video else True

    def ai_progress(percent):
        overall = PROGRESS_SUMMARY_START + int(
            percent * (PROGRESS_SUMMARY_END - PROGRESS_SUMMARY_START) / 100
        )
        db.update_processing_status(video_id, "GENERATING_SUMMARY", overall,
                                    "Creating study notes")

    ai_started = time.time()
    material = ai_service.generate_study_material(
        transcript, quick_mode=quick_mode, progress_callback=ai_progress
    )
    timings["ai"] += round(time.time() - ai_started, 2)

    _save_material(video_id, material)
    db.update_processing_status(video_id, "SUMMARY_READY", PROGRESS_SUMMARY_END,
                                "Study notes ready")

    _stop_if_cancelled(video_id)

    # ---- Mind map: one more structured request while the student reads ----
    db.update_processing_status(video_id, "GENERATING_SUMMARY", 82,
                                "Creating mind map")
    try:
        mind_map = ai_service.generate_mind_map(transcript)
        db.save_mind_map(video_id, mind_map)
    except Exception as map_error:
        # A missing mind map must never fail the whole video
        print(f"[pipeline] Mind map skipped for video {video_id}: {map_error}")

    _stop_if_cancelled(video_id)

    # ---- The quiz comes last: the student can already read while this runs ----
    db.update_processing_status(video_id, "GENERATING_QUIZ", PROGRESS_QUIZ_START,
                                "Creating quiz")

    # Every real video gets a FULL 10-question quiz built from different
    # parts of its complete transcript (the demo lecture keeps its
    # classic 5 so the smoke test stays stable).
    num_questions = 10
    difficulty = "Medium" if quick_mode else "Mixed"

    ai_started = time.time()
    questions = ai_service.generate_quiz(transcript, num_questions=num_questions,
                                         difficulty=difficulty)
    db.delete_quiz_questions(video_id)
    db.save_quiz_questions(video_id, questions)
    timings["ai"] += round(time.time() - ai_started, 2)

    _finish(video_id, started, timings)


# ---------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------

def _save_transcript(video_id, transcript):
    """Store just the transcript, so it is readable before the AI finishes."""
    db.save_study_material(
        video_id,
        transcript=transcript,
        transcript_words=transcription.count_words(transcript),
    )


def _save_material(video_id, material):
    """Store the summary, key points, terms, notes and (detailed mode) short answers."""
    db.save_study_material(
        video_id,
        summary=material.get("summary", ""),
        key_points=material.get("key_points", []),
        important_terms=material.get("important_terms", []),
        short_answers=material.get("short_answers", []),
        notes=material.get("notes", ""),
    )


def _save_duration(video_id, duration):
    conn = db.get_connection()
    conn.execute("UPDATE videos SET duration = ? WHERE id = ?", (duration, video_id))
    conn.commit()
    conn.close()


def _save_url_metadata(video_id, info):
    """Keep the real title and length of a video link (read without downloading)."""
    conn = db.get_connection()
    conn.execute(
        "UPDATE videos SET duration = COALESCE(?, duration), title = COALESCE(?, title) WHERE id = ?",
        (info.get("duration"), info.get("title"), video_id)
    )
    conn.commit()
    conn.close()


def _finish(video_id, started, timings):
    """Mark the video complete and save the MEASURED processing times."""
    total = round(time.time() - started, 2)
    db.save_timings(video_id, timings["audio"], timings["transcription"],
                    timings["ai"], total)
    db.update_processing_status(video_id, "COMPLETED", PROGRESS_DONE, "Completed")


def _save_failure(video_id, error, started, timings):
    """Store a readable error message so the page can explain what happened."""
    message = str(error) or "Processing failed for an unknown reason."

    # Keep the timings we already measured - useful for the report even
    # when a job fails.
    try:
        db.save_timings(video_id, timings["audio"], timings["transcription"],
                        timings["ai"], round(time.time() - started, 2))
    except Exception:
        pass

    db.update_processing_status(video_id, "FAILED", 100, "Processing failed",
                                error_message=message)
    print(f"[pipeline] Video {video_id} failed: {message}")
