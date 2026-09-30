"""
services/job_manager.py

A very small background-job helper, built only on the Python standard
library (concurrent.futures.ThreadPoolExecutor). No Celery, no Redis,
no message broker - which keeps the project easy to explain.

WHY THIS EXISTS (the main performance fix):
The old version processed everything inside the HTTP request, so the
browser had to wait for extract audio -> transcribe -> AI -> quiz before
it could even receive a page. If the student refreshed, the work was
lost and the request had to finish anyway.

Now the flow is:
    Flask receives the video
        -> creates a database record (status UPLOADED)
        -> hands the job to the thread pool
        -> returns the processing page IMMEDIATELY
    JavaScript then polls /api/status/<id> every 1.5 seconds and the
    page updates on its own.

The thread pool is limited to JOBS_PER_USER workers so a student cannot
accidentally start fifty Whisper jobs and freeze the laptop.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

# How many videos may be processed at the same time. Whisper is CPU
# heavy, so two is already a lot on a normal student laptop.
MAX_WORKERS = int(os.environ.get("MAX_BACKGROUND_WORKERS", "2"))

# The shared thread pool. Created once when this module is imported.
_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="evai-job")

# video_id -> True  (set when the student presses "Cancel Processing")
_cancel_flags = {}

# video_id -> "running" / "finished"
_job_state = {}

# Protects the two dictionaries above, because several threads may use
# them at the same time.
_lock = threading.Lock()


def start_job(video_id, target, *args, **kwargs):
    """Run 'target' in a background thread and return immediately.

    target is a normal function (the pipeline). It receives video_id
    first, followed by whatever extra arguments are given.
    """
    with _lock:
        _cancel_flags[video_id] = False
        _job_state[video_id] = "running"

    future = _executor.submit(_run_safely, video_id, target, args, kwargs)

    # When the job finishes, remember that the slot is free again
    future.add_done_callback(lambda _f: _mark_finished(video_id))
    return future


def _run_safely(video_id, target, args, kwargs):
    """Run the job and make sure an unexpected error can never kill the server."""
    try:
        target(video_id, *args, **kwargs)
    except Exception as error:                     # noqa: BLE001 - last line of defence
        print(f"[job_manager] Video {video_id} failed: {error}")
        try:
            from database import db
            db.update_processing_status(
                video_id, "FAILED", 100,
                "Processing failed", error_message=str(error)
            )
        except Exception:
            pass


def _mark_finished(video_id):
    with _lock:
        _job_state[video_id] = "finished"


def request_cancel(video_id):
    """Ask a running job to stop. The pipeline checks this between steps."""
    with _lock:
        _cancel_flags[video_id] = True


def is_cancelled(video_id):
    """True when the student pressed Cancel for this video."""
    with _lock:
        return _cancel_flags.get(video_id, False)


def is_running(video_id):
    """True while the job for this video is still in the thread pool."""
    with _lock:
        return _job_state.get(video_id) == "running"


def forget_job(video_id):
    """Clean up the small in-memory entries once they are no longer needed."""
    with _lock:
        _cancel_flags.pop(video_id, None)
        _job_state.pop(video_id, None)