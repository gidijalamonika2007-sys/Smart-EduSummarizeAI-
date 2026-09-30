"""
app.py

Main Flask application for EduSummarize AI - an AI powered educational
video summarizer and quiz generator.

This file only contains ROUTES (the "traffic controller" of the app).
The real work lives in two folders:

    services/   audio extraction, transcription, AI calls, background jobs
    database/   all the SQLite SQL

THE BIG CHANGE FROM THE FIRST VERSION
-------------------------------------
The old /process route did the whole pipeline INSIDE the request, so the
browser sat on "Processing your video..." until everything (transcript +
summary + quiz) was finished.

Now /process finishes in a moment:

    video accepted  ->  database row  ->  job handed to a background
    thread  ->  redirect to /processing/<id>  ->  JavaScript polls
    /api/status/<id> every 1.5 s and updates the page.

The student therefore sees the transcript as soon as it exists, then the
summary, then the quiz - they never wait for the whole pipeline.
"""

import os
import sys
import time
import uuid
from functools import wraps

# ---------------------------------------------------------------------
# SAFETY NET for launches WITHOUT a console (pythonw.exe, a Windows
# service, some IDE runners): Python then sets sys.stdout / sys.stderr
# to None, and libraries that write to the streams directly - most
# notably tqdm, which Whisper uses for its model-download and
# transcription progress bars - crash with:
#
#     'NoneType' object has no attribute 'write'
#
# Point both streams at the null device BEFORE anything else imports,
# so video processing always has a writable stream to talk to.
# ---------------------------------------------------------------------
for _stream_name in ("stdout", "stderr"):
    if getattr(sys, _stream_name, None) is None:
        setattr(sys, _stream_name, open(os.devnull, "w", encoding="utf-8"))

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, abort, session)
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

from database import db
from services import (ai_service, job_manager, pipeline, quiz_generator,
                      transcription, url_processor, video_processor)

# Load OPENAI_API_KEY (and anything else) from .env
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
AUDIO_FOLDER = os.path.join(BASE_DIR, "audio")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024   # 500 MB upload limit

# Create/upgrade the database tables when the app starts
db.init_db()


# ---------------------------------------------------------------------
# AUTHENTICATION HELPERS  (session-based, werkzeug password hashes)
# ---------------------------------------------------------------------

def _current_user():
    """The logged-in account for this session, or None."""
    return db.get_user_by_id(session.get("user_id"))


@app.context_processor
def inject_current_user():
    """Make current_user available in EVERY template (used by the navbar)."""
    return {"current_user": _current_user()}


def login_required(view):
    """Send anonymous visitors to the login page, remembering where they were going."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "info")
            return redirect(url_for("login_page", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Only logged-in administrators may open the admin pages."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = _current_user()
        if not user:
            flash("Please log in as an administrator.", "warning")
            return redirect(url_for("admin_login_page"))
        if user.get("role") != "admin":
            flash("You do not have permission to open the admin area.", "danger")
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def _safe_next_url(next_url):
    """Only allow redirecting to pages on THIS site after login."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return url_for("dashboard")


# ---------------------------------------------------------------------
# HOME PAGE + ABOUT
# ---------------------------------------------------------------------

@app.route("/")
def index():
    analytics = db.get_analytics()
    return render_template("index.html", analytics=analytics,
                           demo_mode=ai_service.is_demo_mode())


@app.route("/about")
def about():
    analytics = db.get_analytics()
    return render_template("about.html", analytics=analytics,
                           demo_mode=ai_service.is_demo_mode())


# ---------------------------------------------------------------------
# UPLOAD PAGE  (Bootstrap tabs: Upload File  OR  Video URL)
# ---------------------------------------------------------------------

@app.route("/upload", methods=["GET"])
def upload_page():
    return render_template("upload.html", demo_mode=ai_service.is_demo_mode())


@app.route("/process", methods=["POST"])
def process_video():
    """Accept a video (file, link or demo) and start background processing.

    This route deliberately does NOT wait for the pipeline. It creates a
    database row, starts the job in a thread and redirects to the
    processing page, which polls for progress.
    """
    # -------------------------------------------------------------
    # DEMO MODE - loads instantly, never touches Whisper or the API
    # -------------------------------------------------------------
    if request.form.get("use_demo") == "1":
        video_id = db.save_video(
            "demo-introduction-to-ai.mp4",
            is_demo=1, source_type="demo",
            title="Introduction to Artificial Intelligence (Demo)",
            duration=240, quick_mode=1,
            user_id=session.get("user_id"),
        )
        db.clear_video_results(video_id)
        db.update_processing_status(video_id, "TRANSCRIBING", 30, "Loading demo lecture")

        # The demo lecture is processed exactly like a real video: its study
        # material is generated FROM ITS OWN transcript, not from a fixed
        # sample answer.
        demo_transcript = ai_service.DEMO_TRANSCRIPT.strip()
        material = ai_service.generate_study_material(demo_transcript, quick_mode=True)
        db.save_study_material(
            video_id,
            transcript=demo_transcript,
            summary=material["summary"],
            key_points=material["key_points"],
            important_terms=material["important_terms"],
            short_answers=material["short_answers"],
            notes=material.get("notes", ""),
            transcript_words=len(demo_transcript.split()),
        )
        db.set_video_language(video_id, "en")
        db.delete_quiz_questions(video_id)
        db.save_quiz_questions(video_id,
                               ai_service.generate_quiz(demo_transcript, 5, "Medium"))
        db.save_mind_map(video_id, ai_service.generate_mind_map(
            demo_transcript, title="Introduction to Artificial Intelligence"))
        db.save_timings(video_id, 0, 0, 0, 0)
        db.update_processing_status(video_id, "COMPLETED", 100, "Demo material loaded")
        return redirect(url_for("summary_page", video_id=video_id))

    # Which tab was used?
    input_type = request.form.get("input_type", "upload")
    quick_mode = 0 if request.form.get("detailed_mode") == "1" else 1

    if input_type == "url":
        return _start_url_job(quick_mode)
    return _start_upload_job(quick_mode)


# ---------------------------------------------------------------------
# STARTING A JOB  (returns immediately - no waiting inside the request)
# ---------------------------------------------------------------------

def _start_upload_job(quick_mode):
    """Accept an uploaded video file and start background processing.

    Steps:
      1. validate the file extension
      2. calculate the SHA-256 hash (the video's unique id)
      3. check SQLite for an already processed copy -> CACHE HIT
      4. otherwise save the row, start the job and go to the processing page
    """
    file = request.files.get("video")

    if not file or not file.filename:
        flash("Please choose a video file, or paste a video link.", "danger")
        return redirect(url_for("upload_page"))

    if not video_processor.allowed_file(file.filename):
        flash("Unsupported video format. Please upload MP4, MOV, AVI, MKV or WEBM.", "danger")
        return redirect(url_for("upload_page"))

    # Save the upload with a random prefix so two students cannot clash
    safe_name = os.path.basename(file.filename)
    stored_name = f"{uuid.uuid4().hex[:12]}_{safe_name}"
    upload_path = os.path.join(UPLOAD_FOLDER, stored_name)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    file.save(upload_path)

    # The file hash identifies the video itself. The same video uploaded
    # again (even with a different name) produces the same hash.
    content_hash = video_processor.get_file_hash(upload_path)

    # ---- CACHE CHECK: has this exact video been processed before? ----
    existing = db.find_existing_video(content_hash)
    if existing:
        video_processor.cleanup_temp_files(upload_path)
        flash("Previously processed video found - loading saved study materials.", "info")
        return redirect(url_for("summary_page", video_id=existing["id"]))

    # ---- Length check: warn BEFORE any expensive processing ----
    # (only when a limit is configured - MAX_VIDEO_MINUTES=0 means the
    # whole video is processed whatever its duration)
    duration = video_processor.get_media_duration(upload_path)
    if (duration and pipeline.MAX_VIDEO_MINUTES > 0
            and duration > pipeline.MAX_VIDEO_MINUTES * 60):
        video_processor.cleanup_temp_files(upload_path)
        flash(
            f"This video is about {int(round(duration / 60))} minutes long, which is over the "
            f"{int(pipeline.MAX_VIDEO_MINUTES)} minute limit. Please try a shorter video.",
            "warning",
        )
        return redirect(url_for("upload_page"))

    video_id = db.save_video(
        safe_name, source_type="upload", content_hash=content_hash,
        title=os.path.splitext(safe_name)[0], duration=duration,
        quick_mode=quick_mode, user_id=session.get("user_id"),
    )

    job_manager.start_job(video_id, pipeline.run_upload_job, upload_path)
    return redirect(url_for("processing_page", video_id=video_id))


def _start_url_job(quick_mode):
    """Accept a video link and start background processing."""
    url = (request.form.get("video_url") or "").strip()

    # ---- 1. Validate BEFORE any expensive work (this also blocks SSRF) ----
    is_valid, message = url_processor.validate_video_url(url)
    if not is_valid:
        flash(message, "danger")
        return redirect(url_for("upload_page"))

    # ---- 2. CACHE CHECK: has this link been processed before? ----
    url_hash = url_processor.get_url_hash(url)
    existing = db.find_existing_video(url_hash)
    if existing:
        flash("Previously processed video found - loading saved study materials.", "info")
        return redirect(url_for("summary_page", video_id=existing["id"]))

    # ---- 3. Create the row and start the background job ----
    video_id = db.save_video(
        "video-link", source_type="url", source_url=url, content_hash=url_hash,
        title="Video link", quick_mode=quick_mode,
        user_id=session.get("user_id"),
    )
    job_manager.start_job(video_id, pipeline.run_url_job, url)
    return redirect(url_for("processing_page", video_id=video_id))


# ---------------------------------------------------------------------
# PROCESSING PAGE + STATUS/RESULT JSON APIs
# ---------------------------------------------------------------------

@app.route("/processing/<int:video_id>")
def processing_page(video_id):
    """The live progress page. Loads instantly and then updates itself.

    The page itself does no processing: JavaScript polls /api/status/<id>
    every 1.5 seconds and shows each result as soon as it is ready.
    """
    video = db.get_video(video_id)
    if not video:
        abort(404)
    return render_template("processing.html", video=video)


@app.route("/api/status/<int:video_id>")
def status_api(video_id):
    """Small JSON endpoint used by the polling JavaScript.

    Only a few booleans and short strings, so the response stays tiny and
    polling it every 1.5 seconds costs almost nothing.
    """
    video = db.get_video(video_id)
    if not video:
        return jsonify({"error": "Video not found"}), 404

    quiz_ready = len(db.get_quiz_questions(video_id)) > 0
    completed = video["status"] == db.STATUS_COMPLETED or quiz_ready

    return jsonify({
        "video_id": video_id,
        "status": video["status"],
        "progress": video["progress"],
        "message": video["status_message"],
        "error": video["error_message"],
        "title": video["title"],
        "duration": video["duration"],
        "source_type": video["source_type"],
        "transcript_ready": video["transcript_ready"],
        "summary_ready": video["summary_ready"],
        "key_points_ready": video["key_points_ready"],
        "quiz_ready": quiz_ready,
        "completed": completed,
        "cancelled": bool(video["cancelled"]),
        "summary_url": url_for("summary_page", video_id=video_id),
        "quiz_url": url_for("quiz_page", video_id=video_id) if quiz_ready else None,
    })


@app.route("/api/result/<int:video_id>")
def result_api(video_id):
    """Return only the parts of the result that already exist.

    The processing page calls this when a step turns 'ready', so the
    student can start reading the transcript while the quiz is still
    being generated.
    """
    video = db.get_video(video_id)
    if not video:
        return jsonify({"error": "Video not found"}), 404

    payload = {
        "video_id": video_id,
        "title": video["title"],
        "status": video["status"],
        "progress": video["progress"],
    }

    # Each block is included only when it is actually available
    if video["transcript_ready"]:
        payload["transcript"] = video["transcript"]
        payload["transcript_words"] = video["transcript_words"]
    if video["summary_ready"]:
        payload["summary"] = video["summary"]
    if video["key_points_ready"]:
        payload["key_points"] = video["key_points"]
    if video["important_terms"]:
        payload["important_terms"] = video["important_terms"]
    if video["short_answers"]:
        payload["short_answers"] = video["short_answers"]

    questions = db.get_quiz_questions(video_id)
    payload["quiz_ready"] = len(questions) > 0
    payload["question_count"] = len(questions)

    return jsonify(payload)


@app.route("/cancel/<int:video_id>", methods=["POST"])
def cancel_processing(video_id):
    """Cancel a running job.

    The pipeline checks the cancel flag between steps, so work stops at
    the next safe point and the temporary files are deleted in the
    'finally' block of the job. The database record stays valid, so the
    student can safely leave the page at any time.
    """
    video = db.get_video(video_id)
    if not video:
        abort(404)

    job_manager.request_cancel(video_id)
    db.mark_video_cancelled(video_id)
    flash("Processing cancelled. Temporary files were cleaned up.", "info")
    return redirect(url_for("history_page"))


# ---------------------------------------------------------------------
# SUMMARY / STUDY MATERIAL PAGE
# ---------------------------------------------------------------------

@app.route("/summary/<int:video_id>")
def summary_page(video_id):
    """Show the transcript, summary, key points and important terms.

    This page also displays the measured performance numbers (video
    duration vs processing time, transcript words, etc.) which are
    useful for the project evaluation.
    """
    video = db.get_video(video_id)
    if not video:
        abort(404)

    questions = db.get_quiz_questions(video_id)
    attempts = db.get_attempt_history(video_id)

    return render_template("summary.html",
        video=video,
        quiz_ready=len(questions) > 0,
        question_count=len(questions),
        attempt_count=len(attempts),
        word_count=video["transcript_words"],
        language_label=transcription.language_name(video.get("language"))
        if video.get("language") else None,
    )


# ---------------------------------------------------------------------
# QUIZ PAGES
# ---------------------------------------------------------------------

@app.route("/quiz/<int:video_id>")
def quiz_page(video_id):
    """Show the multiple-choice questions for a video."""
    video = db.get_video(video_id)
    if not video:
        abort(404)

    questions = db.get_quiz_questions(video_id)
    if not questions:
        # The quiz is still being generated - the processing page knows
        # how to wait for it and switches to the quiz automatically.
        flash("The quiz is still being prepared. One moment please.", "info")
        return redirect(url_for("processing_page", video_id=video_id))

    return render_template("quiz.html", video=video, questions=questions)


@app.route("/quiz/<int:video_id>/submit", methods=["POST"])
def submit_quiz(video_id):
    """Grade the quiz IN PYTHON (no AI call) and store the attempt.

    Because the answers are compared directly with the correct_answer
    letter, this route finishes in milliseconds and the results
    dashboard appears immediately.
    """
    video = db.get_video(video_id)
    questions = db.get_quiz_questions(video_id)
    if not video or not questions:
        abort(404)

    # The form posts one radio group per question: question_<id> = A/B/C/D
    student_answers = {
        str(q["id"]): (request.form.get(f"question_{q['id']}") or "")
        for q in questions
    }

    score_data = quiz_generator.calculate_score(questions, student_answers)

    # One INSERT per attempt - old attempts are never overwritten, so the
    # "Quiz Progress Over Time" line chart can show improvement.
    result_id = db.save_result(
        video_id=video_id,
        score=score_data["score"],
        total_questions=score_data["total_questions"],
        percentage=score_data["percentage"],
        correct_answers=score_data["correct_answers"],
        incorrect_answers=score_data["incorrect_answers"],
        answers=student_answers,
        question_details=score_data["review"],
    )

    return redirect(url_for("results_page", result_id=result_id))


@app.route("/quiz/<int:video_id>/regenerate", methods=["POST"])
def generate_new_quiz(video_id):
    """Regenerate the questions with different settings (from the Summary page).

    The student can choose how many questions and which difficulty. The
    old questions are replaced, but the stored ATTEMPTS (results) are
    kept, so the progress chart still shows every previous attempt.
    """
    video = db.get_video(video_id)
    if not video or not video["transcript"]:
        abort(404)

    num_questions = request.form.get("num_questions", "5")
    difficulty = request.form.get("difficulty", "Medium")
    num_questions = int(num_questions) if num_questions.isdigit() else 5
    if num_questions not in (5, 10):
        num_questions = 5
    if difficulty not in ("Easy", "Medium", "Hard", "Mixed"):
        difficulty = "Medium"

    questions = ai_service.generate_quiz(
        video["transcript"], num_questions=num_questions, difficulty=difficulty
    )

    if questions:
        db.delete_quiz_questions(video_id)
        db.save_quiz_questions(video_id, questions)
        flash(f"{len(questions)} new questions generated.", "success")
    else:
        flash("The AI could not generate new questions. Please try again.", "danger")

    return redirect(url_for("quiz_page", video_id=video_id))


# ---------------------------------------------------------------------
# RESULTS PAGE - QUIZ PERFORMANCE DASHBOARD
# ---------------------------------------------------------------------

@app.route("/results/<int:result_id>")
def results_page(result_id):
    """Quiz Performance Dashboard.

    ALL scoring/analytics numbers are calculated here in Python (from the
    stored attempt - no AI call, no video processing), and the template
    only hands them to Chart.js for drawing. This keeps the dashboard
    instant and means JavaScript contains no scoring logic.
    """
    result = db.get_result(result_id)
    if not result:
        abort(404)

    video = db.get_video(result["video_id"])
    if not video:
        abort(404)

    questions = db.get_quiz_questions(video["id"])
    # Re-run the same comparison so the review list always matches the
    # stored attempt (the stored data is what the charts are built from).
    score_data = quiz_generator.calculate_score(questions, result["answers_json"])
    review = score_data["review"]

    # ---- chart data, all computed in Python above ----
    topic_scores = quiz_generator.build_topic_scores(review)
    difficulty_scores = quiz_generator.build_difficulty_scores(review)
    topics_to_review = quiz_generator.get_topics_to_review(review)
    question_chart = quiz_generator.build_question_chart(review)

    attempts = db.get_attempt_history(video["id"])
    attempt_labels = [f"Attempt {i + 1}" for i in range(len(attempts))]
    attempt_percentages = [round(a["percentage"], 1) for a in attempts]

    # Hide the topic chart when there is not enough topic data to be useful
    show_topic_chart = len(topic_scores["labels"]) >= 2
    show_difficulty_chart = len(difficulty_scores["labels"]) >= 2
    show_attempt_chart = len(attempts) >= 2

    return render_template(
        "results.html",
        video=video,
        result=result,
        review=review,
        message=quiz_generator.get_performance_message(result["percentage"]),

        # values passed safely into JavaScript for Chart.js
        question_chart=question_chart,
        topic_scores=topic_scores,
        difficulty_scores=difficulty_scores,
        attempt_labels=attempt_labels,
        attempt_percentages=attempt_percentages,
        show_topic_chart=show_topic_chart,
        show_difficulty_chart=show_difficulty_chart,
        show_attempt_chart=show_attempt_chart,
        topics_to_review=topics_to_review,
    )


# ---------------------------------------------------------------------
# TRANSCRIPT PAGE
# ---------------------------------------------------------------------

@app.route("/transcript/<int:video_id>")
def transcript_page(video_id):
    """Full transcript view - available as soon as transcription finishes."""
    video = db.get_video(video_id)
    if not video:
        abort(404)

    if not video["transcript"]:
        flash("The transcript is not ready yet.", "info")
        return redirect(url_for("processing_page", video_id=video_id))

    return render_template("transcript.html", video=video)


# ---------------------------------------------------------------------
# HISTORY PAGE
# ---------------------------------------------------------------------

@app.route("/history")
def history_page():
    """Every processed video with source, date, status and quiz score.

    Videos with a cached result are opened directly - they are NEVER
    reprocessed (see /process, which checks the content hash first).
    """
    history = db.get_history()
    return render_template("history.html", history=history,
                           demo_mode=ai_service.is_demo_mode())


# ---------------------------------------------------------------------
# USER ACCOUNTS: REGISTER / LOGIN / LOGOUT
# ---------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register_page():
    """Create a new student account, then log them in straight away."""
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip() or None
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""

        # Simple, readable validation - each rule is checked on its own
        if len(username) < 3 or len(username) > 30:
            flash("Username must be between 3 and 30 characters.", "danger")
        elif not username.replace("_", "").replace("-", "").isalnum():
            flash("Username may only contain letters, numbers, - and _.", "danger")
        elif username.lower() == "admin":
            flash("That username is reserved.", "danger")
        elif len(password) < 6:
            flash("Password must be at least 6 characters long.", "danger")
        elif password != confirm:
            flash("The two passwords do not match.", "danger")
        elif db.get_user_by_username(username):
            flash("That username is already taken.", "danger")
        else:
            user_id = db.create_user(username, generate_password_hash(password),
                                      email=email)
            if user_id:
                session.clear()
                session["user_id"] = user_id
                flash(f"Welcome, {username}! Your account is ready.", "success")
                return redirect(url_for("dashboard"))
            flash("Could not create the account. Please try again.", "danger")

        return redirect(url_for("register_page"))

    return render_template("register.html", demo_mode=ai_service.is_demo_mode())


@app.route("/login", methods=["GET", "POST"])
def login_page():
    """Log an existing student account in."""
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        next_url = request.form.get("next")
        user = db.get_user_by_username(username)

        if user and check_password_hash(user["password_hash"], password):
            if not user["is_active"]:
                flash("This account has been disabled. Please contact the administrator.", "danger")
                return redirect(url_for("login_page"))
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['username']}!", "success")
            # Admins logging in through the normal form still go to their dashboard
            if user["role"] == "admin" and (not next_url or next_url == "/dashboard"):
                return redirect(url_for("admin_dashboard"))
            return redirect(_safe_next_url(next_url))

        flash("Incorrect username or password.", "danger")
        return redirect(url_for("login_page", next=next_url))

    return render_template("login.html", demo_mode=ai_service.is_demo_mode())


@app.route("/logout")
def logout_page():
    """End the session and return to the home page."""
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------
# USER DASHBOARD  (personal stats + saved videos + saved results)
# ---------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    """The logged-in student's home: their videos, results and stats."""
    user = _current_user()
    analytics = db.get_analytics(user_id=user["id"])
    history = db.get_history(user_id=user["id"])
    saved_results = [item for item in history if item.get("result_id")]

    return render_template(
        "dashboard.html",
        user=user,
        analytics=analytics,
        history=history,
        saved_results=saved_results,
        demo_mode=ai_service.is_demo_mode(),
    )


# ---------------------------------------------------------------------
# MIND MAP PAGE
# ---------------------------------------------------------------------

@app.route("/mindmap/<int:video_id>", methods=["GET"])
def mindmap_page(video_id):
    """Show the mind map tree for a video, generating it on first visit."""
    video = db.get_video(video_id)
    if not video:
        abort(404)

    if not video.get("mind_map"):
        if video.get("transcript"):
            # First visit: build the map now so the link always works,
            # even for videos processed before this feature existed.
            try:
                mind_map = ai_service.generate_mind_map(
                    video["transcript"], title=video["title"])
                db.save_mind_map(video_id, mind_map)
                video = db.get_video(video_id)
            except Exception:
                flash("The mind map could not be generated right now. Please try again.", "warning")
        else:
            flash("The transcript is not ready yet, so no mind map exists for this video.", "info")
            return redirect(url_for("processing_page", video_id=video_id))

    return render_template("mindmap.html", video=video)


@app.route("/mindmap/<int:video_id>/regenerate", methods=["POST"])
def regenerate_mindmap(video_id):
    """Rebuild the mind map from the same transcript."""
    video = db.get_video(video_id)
    if not video or not video.get("transcript"):
        abort(404)

    try:
        mind_map = ai_service.generate_mind_map(
            video["transcript"], title=video["title"])
        db.save_mind_map(video_id, mind_map)
        flash("Mind map regenerated.", "success")
    except Exception:
        flash("The mind map could not be generated. Please try again.", "danger")

    return redirect(url_for("mindmap_page", video_id=video_id))


# ---------------------------------------------------------------------
# ADMIN: LOGIN / DASHBOARD / USER MANAGEMENT
# ---------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login_page():
    """Administrator sign-in (separate page, same accounts table)."""
    current = _current_user()
    if current and current.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = db.get_user_by_username(username)

        if user and user["role"] == "admin" and check_password_hash(user["password_hash"], password):
            if not user["is_active"]:
                flash("This administrator account has been disabled.", "danger")
                return redirect(url_for("admin_login_page"))
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome, admin {user['username']}!", "success")
            return redirect(url_for("admin_dashboard"))

        flash("Incorrect administrator username or password.", "danger")
        return redirect(url_for("admin_login_page"))

    return render_template("admin_login.html")


@app.route("/admin")
@admin_required
def admin_dashboard():
    """Overview of the whole system: users, videos, quizzes, recent activity."""
    analytics = db.get_analytics()
    users = db.get_all_users()
    history = db.get_history()[:10]      # 10 newest videos for the activity table

    stats = {
        "total_users": db.count_users(),
        "active_users": sum(1 for u in users if u["is_active"]),
        "total_videos": analytics["videos_processed"],
        "total_quizzes": analytics["quizzes_completed"],
        "average_score": analytics["average_score"],
    }

    return render_template("admin_dashboard.html",
                           stats=stats, users=users, history=history)


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(user_id):
    """Enable or disable one account (data is never deleted by this action)."""
    target = db.get_user_by_id(user_id)
    if not target:
        abort(404)
    if target["id"] == session.get("user_id"):
        flash("You cannot disable your own account.", "warning")
    else:
        db.set_user_active(user_id, 0 if target["is_active"] else 1)
        state = "disabled" if not target["is_active"] else "enabled"
        flash(f"Account '{target['username']}' {state}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    """Delete one account. Their videos stay in the database."""
    target = db.get_user_by_id(user_id)
    if not target:
        abort(404)
    if target["id"] == session.get("user_id"):
        flash("You cannot delete your own account.", "warning")
    else:
        db.delete_user(user_id)
        flash(f"Account '{target['username']}' deleted. Their videos were kept.", "success")
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------------
# ERROR PAGES
# ---------------------------------------------------------------------

@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html",
                           error_code=404,
                           error_message="That page could not be found.",
                           message="That page could not be found."), 404


@app.errorhandler(500)
def server_error(_error):
    return render_template("error.html",
                           error_code=500,
                           error_message="Something went wrong on the server.",
                           message="Something went wrong on the server."), 500


# ---------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------

if __name__ == "__main__":
    # threaded=True lets the status polling run while background jobs work
    app.run(debug=True, threaded=True)