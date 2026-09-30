"""
services/quiz_generator.py

Plain Python logic around the quiz (no AI calls happen in this file -
all prompts live in ai_service.py):

    calculate_score()            compare answers, count right/wrong
    build_topic_scores()         score per topic      -> bar chart
    build_difficulty_scores()    score per difficulty -> bar chart
    get_topics_to_review()       topics of the wrong answers
    get_performance_message()    friendly message for the score

Every number used by the results dashboard is calculated HERE, in
Python, so the JavaScript only has to draw the charts. This also means
the dashboard appears immediately after submitting - no AI call, no
video processing, nothing to wait for.
"""


def _answer_text(question, choice):
    """Turn a choice like "B" into the readable option text."""
    if not choice or choice == "No answer":
        return "No answer"
    text = question.get(f"option_{choice.lower()}")
    return f"{choice}. {text}" if text else choice


def calculate_score(questions, student_answers):
    """Compare the student's answers with the correct answers.

    questions:       list of quiz dicts from the database
                     (each has id, question, options, correct_answer,
                      explanation, topic, difficulty)
    student_answers: dict mapping str(question_id) -> "A"/"B"/"C"/"D"

    Returns a dict with the score, the counts and the per-question
    review data used by the results page and its charts.
    """
    total_questions = len(questions)
    correct_count = 0
    review = []

    for q in questions:
        qid = str(q["id"])
        student_choice = student_answers.get(qid, "")
        is_correct = (student_choice == q["correct_answer"])

        if is_correct:
            correct_count += 1

        review.append({
            "question_id": q["id"],
            "question": q["question"],
            "option_a": q["option_a"],
            "option_b": q["option_b"],
            "option_c": q["option_c"],
            "option_d": q["option_d"],
            "student_choice": student_choice,
            "student_answer": student_choice or "No answer",
            "student_answer_text": _answer_text(q, student_choice),
            "correct_answer": q["correct_answer"],
            "correct_answer_text": _answer_text(q, q["correct_answer"]),
            "is_correct": is_correct,
            "explanation": q.get("explanation") or "",
            "topic": q.get("topic") or "General",
            "difficulty": q.get("difficulty") or "Medium",
        })

    incorrect_count = total_questions - correct_count
    percentage = round((correct_count / total_questions) * 100, 1) if total_questions > 0 else 0

    return {
        "score": correct_count,
        "correct_answers": correct_count,
        "incorrect_answers": incorrect_count,
        "total_questions": total_questions,
        "percentage": percentage,
        "review": review,
    }


def build_topic_scores(review):
    """Percentage score for each topic -> "Topic-wise Performance" chart.

    Only topics that actually appear in the generated questions are
    returned. A topic with only one question is left out, because a
    percentage based on a single question says nothing useful.
    """
    topics = {}

    for item in review:
        topic = (item.get("topic") or "General").strip()
        if not topic:
            continue
        entry = topics.setdefault(topic, {"correct": 0, "total": 0})
        entry["total"] += 1
        if item["is_correct"]:
            entry["correct"] += 1

    labels, percentages, counts = [], [], []

    for topic, entry in topics.items():
        if entry["total"] < 2:
            continue
        labels.append(topic)
        percentages.append(round((entry["correct"] / entry["total"]) * 100, 1))
        counts.append(entry["total"])

    return {"labels": labels, "percentages": percentages, "question_counts": counts}


def build_difficulty_scores(review):
    """Percentage score for Easy / Medium / Hard -> difficulty bar chart.

    Only the difficulty levels present in the quiz are returned, so an
    all-Medium quiz does not show two empty bars.
    """
    order = ["Easy", "Medium", "Hard"]
    scores = {}

    for item in review:
        level = (item.get("difficulty") or "Medium").strip().capitalize()
        if level not in order:
            level = "Medium"
        entry = scores.setdefault(level, {"correct": 0, "total": 0})
        entry["total"] += 1
        if item["is_correct"]:
            entry["correct"] += 1

    labels, percentages, counts = [], [], []

    for level in order:
        if level in scores:
            entry = scores[level]
            labels.append(level)
            percentages.append(round((entry["correct"] / entry["total"]) * 100, 1))
            counts.append(entry["total"])

    return {"labels": labels, "percentages": percentages, "question_counts": counts}


def get_topics_to_review(review):
    """Topics of the questions the student answered incorrectly.

    Duplicates are removed, so a topic the student got wrong three times
    appears once. If everything was correct the list is simply empty.
    """
    topics = []
    seen = set()

    for item in review:
        if item["is_correct"]:
            continue
        topic = (item.get("topic") or "General").strip()
        if topic and topic not in seen:
            seen.add(topic)
            topics.append(topic)

    return topics


def build_question_chart(review):
    """Data for the "Question-wise Performance" bar chart.

    X-axis: Q1, Q2, Q3 ...   Y-axis: 1 = correct, 0 = incorrect
    """
    labels = [f"Q{index + 1}" for index in range(len(review))]
    values = [1 if item["is_correct"] else 0 for item in review]
    results = ["Correct" if item["is_correct"] else "Incorrect" for item in review]
    return {"labels": labels, "values": values, "results": results}


def get_performance_message(percentage):
    """Return an encouraging educational message based on the score."""
    if percentage >= 90:
        return "Excellent! You have a strong understanding of this lesson."
    elif percentage >= 75:
        return "Good job! You understood most of the concepts."
    elif percentage >= 60:
        return "Good attempt. Review the concepts you missed and try again."
    else:
        return "Review the summary and key learning points before attempting the quiz again."