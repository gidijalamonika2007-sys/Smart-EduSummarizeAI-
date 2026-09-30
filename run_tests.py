"""
run_tests.py  (temporary test harness - can be deleted afterwards)

End-to-end tests for the upgraded EduSummarize AI:

  1. URL validation + SSRF protection + URL normalisation/hash
  2. Demo Mode (instant, no AI/Whisper calls)
  3. REAL upload pipeline: ffmpeg audio -> Whisper -> study notes -> quiz
     (AI calls are monkeypatched so no API key is needed; everything else
      - ffmpeg, Whisper, threading, database - is real)
  4. Cache test: submitting the same file again must NOT reprocess
  5. Quiz submit -> performance dashboard data -> retake -> attempt history
  6. URL job error handling (graceful failure)
  7. Every page renders (UI test)

Run:  .\\venv\\Scripts\\python.exe run_tests.py
"""

import os
import shutil
import sys
import time
import copy

# Use a THROWAWAY database so the student's real data is untouched
TEST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_database.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

import database.db as db
db.DB_PATH = TEST_DB          # must happen BEFORE app is imported

from app import app
from services import ai_service, quiz_generator, url_processor

app.config["TESTING"] = True
client = app.test_client()

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}")


# =====================================================================
print("\n=== 1. URL VALIDATION + SSRF PROTECTION ===")
# =====================================================================

cases = [
    ("", False, "empty"),
    ("ftp://youtube.com/watch?v=abc", False, "ftp protocol"),
    ("file:///C:/videos/x.mp4", False, "file protocol"),
    ("javascript:alert(1)", False, "javascript protocol"),
    ("http://localhost/video.mp4", False, "localhost"),
    ("http://127.0.0.1:5000/video.mp4", False, "loopback IP"),
    ("http://192.168.1.10/video.mp4", False, "private IP"),
    ("http://10.0.0.5/video.mp4", False, "private 10.x"),
    ("https://example.com/video.mp4", False, "unsupported host"),
    ("https://www.youtube.com/", False, "youtube home page, not a video"),
    ("https://www.youtube.com/watch?v=aircAruvnKk", True, "youtube watch link"),
    ("https://youtu.be/aircAruvnKk", True, "youtu.be short link"),
    ("https://vimeo.com/76979871", True, "vimeo link"),
    ("https://www.ted.com/talks/ken_robinson_says_schools_kill_creativity", True, "ted link"),
]
for url, expected_valid, label in cases:
    is_valid, message = url_processor.validate_video_url(url)
    check(f"validate: {label}", is_valid == expected_valid,
          f"got valid={is_valid}, msg={message!r}")

# SSRF: make sure private addresses are rejected even with a sneaky host name
is_valid, _ = url_processor.validate_video_url("http://[::1]/video.mp4")
check("validate: IPv6 loopback rejected", not is_valid)

# normalisation: tracking parameters must not change the hash
h1 = url_processor.get_url_hash("https://www.youtube.com/watch?v=aircAruvnKk")
h2 = url_processor.get_url_hash("https://www.youtube.com/watch?v=aircAruvnKk&si=xyz123&t=42s")
h3 = url_processor.get_url_hash("https://www.YouTube.com/watch?v=aircAruvnKk&")
check("hash stable across tracking params + case", h1 == h2 == h3)
check("hash is 64 hex chars", len(h1) == 64)

# =====================================================================
print("\n=== 2. QUIZ ANALYTICS (pure Python, feeds the dashboard) ===")
# =====================================================================

fake_questions = []
spec = [
    ("ML Easy 1", "Machine Learning", "Easy", "A"),
    ("ML Easy 2", "Machine Learning", "Easy", "B"),
    ("NN Med", "Neural Networks", "Medium", "C"),
    ("NN Hard", "Neural Networks", "Hard", "A"),
]
for i, (text, topic, level, answer) in enumerate(spec):
    fake_questions.append({
        "id": 100 + i, "question": text, "option_a": "A text", "option_b": "B text",
        "option_c": "C text", "option_d": "D text", "correct_answer": answer,
        "explanation": "Because.", "topic": topic, "difficulty": level,
    })

answers = {"100": "A", "101": "C", "102": "C", "103": "D"}   # 2 of 4 correct
score = quiz_generator.calculate_score(fake_questions, answers)
check("score = 2/4", score["score"] == 2 and score["total_questions"] == 4)
check("percentage = 50.0", score["percentage"] == 50.0)
check("incorrect = 2", score["incorrect_answers"] == 2)
check("review has 4 items", len(score["review"]) == 4)
check("student answer text readable", score["review"][1]["student_answer_text"] == "C. C text")

topics = quiz_generator.build_topic_scores(score["review"])
check("topic chart: 2 topics", topics["labels"] == ["Machine Learning", "Neural Networks"],
      str(topics))
check("topic percentages", topics["percentages"] == [50.0, 50.0])

diffs = quiz_generator.build_difficulty_scores(score["review"])
check("difficulty chart: Easy/Medium/Hard", diffs["labels"] == ["Easy", "Medium", "Hard"])
check("difficulty percentages", diffs["percentages"] == [50.0, 100.0, 0.0])

check("topics to review = both topics",
      quiz_generator.get_topics_to_review(score["review"]) == ["Machine Learning", "Neural Networks"])
check("perfect score -> no review topics",
      quiz_generator.get_topics_to_review(quiz_generator.calculate_score(
          fake_questions, {"100": "A", "101": "B", "102": "C", "103": "A"})["review"]) == [])
check("question chart values", quiz_generator.build_question_chart(score["review"])["values"] == [1, 0, 1, 0])
check("90% message", "Excellent" in quiz_generator.get_performance_message(95))
check("50% message", "Review the summary" in quiz_generator.get_performance_message(50))
