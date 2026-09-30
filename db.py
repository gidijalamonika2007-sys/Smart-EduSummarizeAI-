"""
database/db.py

This file handles everything related to SQLite:
- creating the database and tables the first time the app runs
- upgrading (migrating) an older database so it keeps working
- saving new videos, quizzes and quiz attempts
- reading data back out for the summary, quiz, results and history pages

Keeping all SQL in one file makes the rest of the app easier to read,
and makes it easy to explain during a project viva.

NOTE ABOUT NAMES: the "results" table stores one row per QUIZ ATTEMPT.
Every time the student retakes a quiz we INSERT a new row, so old
attempts are never overwritten (that is what the "Quiz Progress Over
Time" chart uses).
"""

import sqlite3
import os
import json
from datetime import datetime

# The database file lives in the project root, next to app.py
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database.db")

# A video is only "finished" when it has this status. Older versions of the
# app used the word "ready", so the migration below converts those rows.
STATUS_COMPLETED = "COMPLETED"


def get_connection():
    """Open a connection to the SQLite database.

    row_factory = sqlite3.Row lets us access columns by name
    (e.g. row["filename"]) instead of only by index, which is
    much easier to read in the templates.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------
# Extra columns that are added to an OLDER database file
# ---------------------------------------------------------------------
# SQLite can add new columns with ALTER TABLE, so an existing
# database.db keeps working without deleting the student's data.

VIDEO_EXTRA_COLUMNS = [
    ("source_type", "TEXT DEFAULT 'upload'"),   # 'upload' | 'url' | 'demo'
    ("source_url", "TEXT"),                     # the link, when source_type = 'url'
    ("content_hash", "TEXT"),                   # SHA-256 used to find cached videos
    ("title", "TEXT"),                          # video title (URL metadata or filename)
    ("duration", "REAL"),                       # video length in seconds
    ("progress", "INTEGER DEFAULT 0"),          # 0-100, shown on the processing page
    ("status_message", "TEXT"),                 # short readable description of the step
    ("error_message", "TEXT"),                  # filled in when status = FAILED
    ("quick_mode", "INTEGER DEFAULT 1"),        # 1 = Quick, 0 = Detailed Study Mode
    ("audio_processing_seconds", "REAL"),
    ("transcription_seconds", "REAL"),
    ("ai_generation_seconds", "REAL"),
    ("total_processing_seconds", "REAL"),
    ("transcript_words", "INTEGER DEFAULT 0"),
    ("cancelled", "INTEGER DEFAULT 0"),
    ("user_id", "INTEGER"),                      # owner of the video (NULL = uploaded before accounts existed / anonymous)
    ("mind_map", "TEXT"),                        # AI mind map, stored as JSON text
    ("notes", "TEXT"),                           # detailed study notes (headings + bullets)
    ("language", "TEXT"),                        # detected speech language code (en, te, ...)
]

QUIZ_EXTRA_COLUMNS = [
    ("topic", "TEXT"),        # subject of the question (used by result charts)
    ("difficulty", "TEXT"),   # Easy / Medium / Hard (used by result charts)
]

RESULT_EXTRA_COLUMNS = [
    ("correct_answers", "INTEGER DEFAULT 0"),
    ("incorrect_answers", "INTEGER DEFAULT 0"),
]


def _add_column_if_missing(conn, table, column, definition):
    """Add a column to a table only if it is not there yet (a mini migration)."""
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    existing = [row["name"] for row in cur.fetchall()]
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


def _run_migrations(conn):
    """Make sure an older database.db has all the columns the new code needs."""
    for column, definition in VIDEO_EXTRA_COLUMNS:
        _add_column_if_missing(conn, "videos", column, definition)
    for column, definition in QUIZ_EXTRA_COLUMNS:
        _add_column_if_missing(conn, "quizzes", column, definition)
    for column, definition in RESULT_EXTRA_COLUMNS:
        _add_column_if_missing(conn, "results", column, definition)

    # Old versions of the app used status = 'ready'. Bring those rows up to date
    # so the rest of the code only has to understand one word.
    cur = conn.cursor()
    cur.execute("UPDATE videos SET status = 'COMPLETED' WHERE status = 'ready'")
    conn.commit()


def init_db():
    """Create the tables if they do not already exist.

    Called once when the Flask app starts up. Safe to run many times.
    """
    conn = get_connection()
    cur = conn.cursor()

    # One row per submitted video (uploaded file, video link or demo).
    # Only small text data is stored here - never the video itself.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            title TEXT,
            source_type TEXT DEFAULT 'upload',
            source_url TEXT,
            content_hash TEXT,
            duration REAL,
            transcript TEXT,
            summary TEXT,
            key_points TEXT,      -- stored as JSON text
            important_terms TEXT, -- stored as JSON text
            short_answers TEXT,   -- stored as JSON text
            notes TEXT,           -- detailed study notes (headings + bullets)
            language TEXT,        -- detected speech language (en, te, ...)
            transcript_words INTEGER DEFAULT 0,
            is_demo INTEGER DEFAULT 0,
            status TEXT DEFAULT 'UPLOADED',
            progress INTEGER DEFAULT 0,
            status_message TEXT,
            error_message TEXT,
            quick_mode INTEGER DEFAULT 1,
            cancelled INTEGER DEFAULT 0,
            audio_processing_seconds REAL,
            transcription_seconds REAL,
            ai_generation_seconds REAL,
            total_processing_seconds REAL,
            created_at TEXT NOT NULL
        )
    """)

    # Stores every generated MCQ, linked back to its video
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            option_a TEXT NOT NULL,
            option_b TEXT NOT NULL,
            option_c TEXT NOT NULL,
            option_d TEXT NOT NULL,
            correct_answer TEXT NOT NULL,
            explanation TEXT,
            topic TEXT,
            difficulty TEXT,
            FOREIGN KEY (video_id) REFERENCES videos (id)
        )
    """)

    # Stores each completed quiz attempt (one row per attempt).
    # "results" is the table name this app has always used; it IS the
    # quiz attempt table - retaking a quiz inserts a new row.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            score INTEGER NOT NULL,
            total_questions INTEGER NOT NULL,
            percentage REAL NOT NULL,
            correct_answers INTEGER DEFAULT 0,
            incorrect_answers INTEGER DEFAULT 0,
            answers_json TEXT,   -- student's chosen answers, for the review page
            created_at TEXT NOT NULL,
            FOREIGN KEY (video_id) REFERENCES videos (id)
        )
    """)

    # One row per answered question. These rows feed the results dashboard.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quiz_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id INTEGER NOT NULL,
            video_id INTEGER NOT NULL,
            question_id INTEGER,
            student_answer TEXT,
            correct_answer TEXT,
            is_correct INTEGER DEFAULT 0,
            topic TEXT,
            difficulty TEXT,
            FOREIGN KEY (result_id) REFERENCES results (id)
        )
    """)

    # One row per registered account. role = 'user' (student) or 'admin'.
    # is_active lets an administrator disable an account without deleting it.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    _run_migrations(conn)
    _seed_default_admin(conn)
    conn.close()


# ---------------------------------------------------------------------
# VIDEO helpers
# ---------------------------------------------------------------------

def save_video(filename, is_demo=0, source_type="upload", source_url=None,
               content_hash=None, title=None, duration=None, quick_mode=1,
               user_id=None):
    """Insert a new video row and return its new id.

    The row is created as soon as the video is accepted, before any
    expensive work happens, so the processing page has something to
    show immediately.

    user_id links the video to the account that submitted it (None for
    anonymous visitors, which keeps the old behaviour working).
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO videos
            (filename, title, source_type, source_url, content_hash, duration,
             is_demo, status, progress, status_message, quick_mode, user_id,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        filename, title or filename, source_type, source_url, content_hash, duration,
        is_demo, "UPLOADED", 5, "Video received", quick_mode, user_id,
        datetime.now().isoformat()
    ))
    conn.commit()
    video_id = cur.lastrowid
    conn.close()
    return video_id


def save_study_material(video_id, transcript=None, summary=None, key_points=None,
                        important_terms=None, short_answers=None,
                        transcript_words=None, notes=None):
    """Store the transcript and/or the AI study material for a video.

    Every argument is optional: the pipeline saves the transcript FIRST
    (so the student can read it while the AI is still working) and saves
    the summary/key points/terms/notes later. NULL/None values leave the
    existing column untouched.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE videos
        SET transcript = COALESCE(?, transcript),
            summary = COALESCE(?, summary),
            key_points = COALESCE(?, key_points),
            important_terms = COALESCE(?, important_terms),
            short_answers = COALESCE(?, short_answers),
            notes = COALESCE(?, notes),
            transcript_words = COALESCE(?, transcript_words)
        WHERE id = ?
    """, (
        transcript, summary,
        json.dumps(key_points) if key_points is not None else None,
        json.dumps(important_terms) if important_terms is not None else None,
        json.dumps(short_answers) if short_answers is not None else None,
        notes, transcript_words, video_id
    ))
    conn.commit()
    conn.close()


def update_processing_status(video_id, status, progress, message, error_message=None):
    """Update the step/progress shown on the processing page (polled by JavaScript)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE videos
        SET status = ?, progress = ?, status_message = ?,
            error_message = COALESCE(?, error_message)
        WHERE id = ?
    """, (status, progress, message, error_message, video_id))
    conn.commit()
    conn.close()


def save_timings(video_id, audio_processing_seconds, transcription_seconds,
                 ai_generation_seconds, total_processing_seconds):
    """Save the measured timings used by the performance dashboard."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE videos
        SET audio_processing_seconds = ?, transcription_seconds = ?,
            ai_generation_seconds = ?, total_processing_seconds = ?
        WHERE id = ?
    """, (audio_processing_seconds, transcription_seconds,
          ai_generation_seconds, total_processing_seconds, video_id))
    conn.commit()
    conn.close()


def mark_video_cancelled(video_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE videos SET cancelled = 1, status = 'CANCELLED', progress = 100,
               status_message = 'Processing cancelled'
        WHERE id = ?
    """, (video_id,))
    conn.commit()
    conn.close()


def set_video_status(video_id, status, message=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE videos SET status = ?, status_message = COALESCE(?, status_message) WHERE id = ?",
                (status, message, video_id))
    conn.commit()
    conn.close()


def find_existing_video(content_hash):
    """Look for an ALREADY FINISHED video with the same content hash.

    This is the caching feature: if the student submits the same file
    or the same link twice we reuse the saved study material instead of
    extracting audio, transcribing and calling the AI again.

    Returns the video dict, or None when nothing is cached.
    """
    if not content_hash:
        return None

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM videos
        WHERE content_hash = ? AND status = ?
        ORDER BY id DESC
        LIMIT 1
    """, (content_hash, STATUS_COMPLETED))
    row = cur.fetchone()
    conn.close()

    if row is None:
        return None
    return get_video(row["id"])


def get_video(video_id):
    """Return one video row as a dict, or None if it doesn't exist."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM videos WHERE id = ?", (video_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None

    video = dict(row)
    # Convert the JSON text columns back into Python lists/dicts
    video["key_points"] = json.loads(video["key_points"]) if video["key_points"] else []
    video["important_terms"] = json.loads(video["important_terms"]) if video["important_terms"] else []
    video["short_answers"] = json.loads(video["short_answers"]) if video["short_answers"] else []
    # The mind map is stored as JSON text and only parsed for the mind map page
    video["mind_map"] = json.loads(video["mind_map"]) if video.get("mind_map") else None

    # Older rows may have NULLs in the new columns - give them safe defaults
    video["progress"] = video.get("progress") or 0
    video["status_message"] = video.get("status_message") or "Submitted"
    video["source_type"] = video.get("source_type") or ("demo" if video["is_demo"] else "upload")
    video["title"] = video.get("title") or video["filename"]
    video["transcript_ready"] = bool(video["transcript"])
    video["summary_ready"] = bool(video["summary"])
    video["key_points_ready"] = bool(video["key_points"])
    video["transcript_words"] = video.get("transcript_words") or (
        len(video["transcript"].split()) if video["transcript"] else 0
    )
    video["summary_words"] = len(video["summary"].split()) if video["summary"] else 0
    return video


# ---------------------------------------------------------------------
# QUIZ helpers
# ---------------------------------------------------------------------

def save_quiz_questions(video_id, questions):
    """Save a list of MCQ dicts for a given video.

    Each dict must have: question, option_a, option_b, option_c,
    option_d, correct_answer, explanation and may have topic + difficulty.
    """
    conn = get_connection()
    cur = conn.cursor()
    for q in questions:
        cur.execute("""
            INSERT INTO quizzes
            (video_id, question, option_a, option_b, option_c, option_d,
             correct_answer, explanation, topic, difficulty)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            video_id, q["question"], q["option_a"], q["option_b"],
            q["option_c"], q["option_d"], q["correct_answer"], q.get("explanation", ""),
            q.get("topic", "General"), q.get("difficulty", "Medium")
        ))
    conn.commit()
    conn.close()


def get_quiz_questions(video_id):
    """Return every MCQ for a video, as a list of dicts."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM quizzes WHERE video_id = ? ORDER BY id ASC", (video_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def delete_quiz_questions(video_id):
    """Remove the old questions before generating a fresh set for a video."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM quizzes WHERE video_id = ?", (video_id,))
    conn.commit()
    conn.close()


def clear_video_results(video_id):
    """Delete every stored RESULT for one video.

    Called at the start of processing, so a video can never show results
    from an earlier run. This matters because save_study_material() writes
    its columns with COALESCE, which keeps the old text when a new attempt
    fails - and that is exactly how results would "mix" between runs.

    Removes the transcript, summary, key points, terms, short answers,
    mind map, quiz questions and quiz attempts of that ONE video. The
    video row itself (and its upload/user info) is kept.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM quiz_answers WHERE video_id = ?", (video_id,))
    cur.execute("DELETE FROM results WHERE video_id = ?", (video_id,))
    cur.execute("DELETE FROM quizzes WHERE video_id = ?", (video_id,))
    cur.execute("""
        UPDATE videos
        SET transcript = NULL,
            transcript_words = 0,
            summary = NULL,
            key_points = NULL,
            important_terms = NULL,
            short_answers = NULL,
            notes = NULL,
            language = NULL,
            mind_map = NULL
        WHERE id = ?
    """, (video_id,))
    conn.commit()
    conn.close()


def set_video_language(video_id, language):
    """Store the language detected BEFORE transcription (language badge)."""
    conn = get_connection()
    conn.execute("UPDATE videos SET language = ? WHERE id = ?",
                 (language, video_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# QUIZ ATTEMPT helpers  (stored in the "results" table)
# ---------------------------------------------------------------------

def save_result(video_id, score, total_questions, percentage, answers,
                correct_answers=None, incorrect_answers=None, question_details=None):
    """Insert one quiz attempt and return its new id.

    Every attempt is a NEW row, so previous attempts stay in the database
    and the "Quiz Progress Over Time" chart can show the improvement.
    """
    if correct_answers is None:
        correct_answers = score
    if incorrect_answers is None:
        incorrect_answers = max(total_questions - score, 0)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO results
            (video_id, score, total_questions, percentage, correct_answers,
             incorrect_answers, answers_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (video_id, score, total_questions, percentage, correct_answers,
          incorrect_answers, json.dumps(answers), datetime.now().isoformat()))
    result_id = cur.lastrowid

    # One row per question - these feed the charts on the results page
    for item in (question_details or []):
        cur.execute("""
            INSERT INTO quiz_answers
                (result_id, video_id, question_id, student_answer, correct_answer,
                 is_correct, topic, difficulty)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result_id, video_id, item.get("question_id"),
            item.get("student_answer"), item.get("correct_answer"),
            1 if item.get("is_correct") else 0,
            item.get("topic"), item.get("difficulty")
        ))

    conn.commit()
    conn.close()
    return result_id


def get_result(result_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM results WHERE id = ?", (result_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    result = dict(row)
    result["answers_json"] = json.loads(result["answers_json"]) if result["answers_json"] else {}
    # Older rows do not have these two columns filled in
    result["correct_answers"] = result.get("correct_answers") or result["score"]
    result["incorrect_answers"] = result.get("incorrect_answers") or (
        result["total_questions"] - result["score"]
    )
    return result


def get_attempt_history(video_id):
    """Return every attempt for one video, oldest first (for the line chart)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, score, total_questions, percentage, created_at
        FROM results
        WHERE video_id = ?
        ORDER BY id ASC
    """, (video_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_latest_result(video_id):
    """Return the newest attempt for a video (or None)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM results WHERE video_id = ? ORDER BY id DESC LIMIT 1", (video_id,))
    row = cur.fetchone()
    conn.close()
    return get_result(row["id"]) if row else None


# ---------------------------------------------------------------------
# HISTORY + ANALYTICS
# ---------------------------------------------------------------------

def save_mind_map(video_id, mind_map):
    """Store the AI-generated mind map (a nested dict) as JSON text."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE videos SET mind_map = ? WHERE id = ?",
                (json.dumps(mind_map), video_id))
    conn.commit()
    conn.close()


def get_history(user_id=None):
    """Return every processed video with its newest quiz score, newest first.

    A LEFT JOIN is used so videos that were processed but not yet quizzed
    still appear (the History page shows their status instead of a score).

    When user_id is given, only that account's videos are returned - this
    powers the personal Dashboard / saved results.
    """
    conn = get_connection()
    cur = conn.cursor()
    where = "WHERE videos.user_id = ?" if user_id else ""
    params = (user_id,) if user_id else ()
    cur.execute(f"""
        SELECT videos.id AS video_id,
               videos.title,
               videos.filename,
               videos.source_type,
               videos.source_url,
               videos.status,
               videos.created_at,
               videos.duration,
               videos.transcript_words,
               videos.total_processing_seconds,
               videos.mind_map,
               results.id AS result_id,
               results.score,
               results.total_questions,
               results.percentage,
               results.created_at AS result_date,
               (SELECT COUNT(*) FROM results r2 WHERE r2.video_id = videos.id) AS attempt_count
        FROM videos
        LEFT JOIN results ON results.id = (
            SELECT id FROM results WHERE video_id = videos.id ORDER BY id DESC LIMIT 1
        )
        {where}
        ORDER BY videos.id DESC
    """, params)
    rows = cur.fetchall()
    conn.close()
    history = []
    for row in rows:
        item = dict(row)
        # mind_map is JSON text; the dashboard only needs to know if one exists
        item["mind_map_ready"] = bool(item.get("mind_map"))
        item.pop("mind_map", None)
        history.append(item)
    return history


def get_analytics(user_id=None):
    """Compute the simple stats shown on the home/analytics section.

    Pass user_id to get the numbers for ONE account (used by the Dashboard);
    leave it out for the global stats shown on the home page.
    """
    conn = get_connection()
    cur = conn.cursor()

    if user_id:
        cur.execute("SELECT COUNT(*) AS c FROM videos WHERE user_id = ?", (user_id,))
    else:
        cur.execute("SELECT COUNT(*) AS c FROM videos")
    videos_processed = cur.fetchone()["c"]

    result_filter = (" FROM results JOIN videos ON videos.id = results.video_id"
                     " WHERE videos.user_id = ?" if user_id else " FROM results")
    result_params = (user_id,) if user_id else ()

    cur.execute("SELECT COUNT(*) AS c" + result_filter, result_params)
    quizzes_completed = cur.fetchone()["c"]

    cur.execute("SELECT AVG(percentage) AS avg_pct" + result_filter, result_params)
    avg_row = cur.fetchone()
    average_score = round(avg_row["avg_pct"], 1) if avg_row["avg_pct"] is not None else 0

    cur.execute("SELECT MAX(percentage) AS best" + result_filter, result_params)
    best_row = cur.fetchone()
    best_score = round(best_row["best"], 1) if best_row["best"] is not None else 0

    if user_id:
        cur.execute("""SELECT results.percentage FROM results
                       JOIN videos ON videos.id = results.video_id
                       WHERE videos.user_id = ?
                       ORDER BY results.id ASC""", (user_id,))
    else:
        cur.execute("SELECT percentage FROM results ORDER BY id ASC")
    history_scores = [row["percentage"] for row in cur.fetchall()]

    # Average measured processing time (real numbers, not an estimate)
    timing_where = "WHERE total_processing_seconds IS NOT NULL"
    if user_id:
        timing_where += " AND user_id = ?"
    cur.execute("SELECT AVG(total_processing_seconds) AS t FROM videos " + timing_where,
                (user_id,) if user_id else ())
    timing_row = cur.fetchone()
    average_processing = round(timing_row["t"], 1) if timing_row["t"] is not None else 0

    conn.close()
    return {
        "videos_processed": videos_processed,
        "quizzes_completed": quizzes_completed,
        "average_score": average_score,
        "best_score": best_score,
        "history_scores": history_scores,
        "average_processing": average_processing,
    }


# ---------------------------------------------------------------------
# USER ACCOUNT helpers  (stored in the "users" table)
# ---------------------------------------------------------------------
# Passwords are NEVER stored in plain text: the application hashes them
# with werkzeug.security before calling create_user(), and login only
# compares a stored hash with a candidate password.

def _seed_default_admin(conn):
    """Create the first administrator account if no admin exists yet.

    Default credentials: admin / admin123  (documented in the README).
    The password is hashed exactly like any other user password.
    """
    from werkzeug.security import generate_password_hash

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin'")
    if cur.fetchone()["c"] == 0:
        cur.execute("""
            INSERT INTO users (username, email, password_hash, role, is_active, created_at)
            VALUES (?, ?, ?, 'admin', 1, ?)
        """, ("admin", "admin@localhost",
              generate_password_hash("admin123"),
              datetime.now().isoformat()))
        conn.commit()


def create_user(username, password_hash, email=None, role="user"):
    """Insert a new account and return its id, or None if the username exists."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users (username, email, password_hash, role, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
        """, (username, email, password_hash, role, datetime.now().isoformat()))
        conn.commit()
        user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        user_id = None
    finally:
        conn.close()
    return user_id


def get_user_by_username(username):
    """Return one account as a dict (including password_hash), or None."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id):
    """Return one account as a dict (without the hash), or None."""
    if not user_id:
        return None
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username, email, role, is_active, created_at "
                "FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_users():
    """Every account with its video count - used by the admin dashboard."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT users.id, users.username, users.email, users.role,
               users.is_active, users.created_at,
               (SELECT COUNT(*) FROM videos WHERE videos.user_id = users.id) AS video_count
        FROM users
        ORDER BY users.id ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def set_user_active(user_id, is_active):
    """Enable (1) or disable (0) an account without deleting its data."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_active = ? WHERE id = ?",
                (1 if is_active else 0, user_id))
    conn.commit()
    conn.close()


def delete_user(user_id):
    """Delete an account. Videos stay in the database (user_id becomes NULL)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE videos SET user_id = NULL WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def count_users():
    """Total number of registered accounts (shown on the admin dashboard)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users")
    total = cur.fetchone()["c"]
    conn.close()
    return total