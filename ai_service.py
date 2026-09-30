"""
services/ai_service.py

All prompts sent to the OpenAI API live in this one file, as required
by the project spec (keeps app.py clean and easy to read).

Every function that calls the AI has a "Demo Mode" fallback that is
used automatically when no OPENAI_API_KEY is configured, so the
project can always be demonstrated even without internet access.
"""

import os
import json
from openai import OpenAI

from services import local_content

MODEL_NAME = "gpt-4o-mini"  # small + cheap, good enough for this project

# How long to wait for the AI before giving up (seconds)
REQUEST_TIMEOUT = 120

# A transcript shorter than this is summarised in ONE request. Longer
# transcripts are split into chunks first (see generate_study_material).
SINGLE_REQUEST_WORDS = 2500

# How much transcript text to send per chunk when chunking is needed
CHUNK_WORDS = 1200


def _get_client():
    """Create an OpenAI client, or return None if no API key is set."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or api_key == "your_api_key_here":
        return None
    return OpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT)


def is_demo_mode():
    """True when there is no usable OpenAI API key configured."""
    return _get_client() is None


def _clean_json_text(raw_text):
    """Remove markdown code fences that the model may add around JSON."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return text


def _ask_ai_for_json(system_prompt, user_prompt, want_object=True):
    """Send ONE prompt to OpenAI and parse the response as JSON.

    'want_object=True' asks the API for a JSON object (used for study
    material). 'want_object=False' is used when the answer is a JSON
    array (used for the quiz), because the API only understands the
    "json_object" response format.
    """
    client = _get_client()
    if client is None:
        raise Exception("OpenAI API key is not configured.")

    request = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
    }
    if want_object:
        # This makes the API guarantee valid JSON, so we do not have to
        # guess what the model decided to return.
        request["response_format"] = {"type": "json_object"}

    response = client.chat.completions.create(**request)
    raw_text = response.choices[0].message.content
    return json.loads(_clean_json_text(raw_text))


def _ask_ai_for_json_safely(system_prompt, user_prompt, want_object=True):
    """Same as _ask_ai_for_json but tries once more if the first call fails.

    A single retry keeps the pipeline simple while still surviving a
    temporary network hiccup, which is useful during a live demo.
    """
    try:
        return _ask_ai_for_json(system_prompt, user_prompt, want_object)
    except Exception:
        return _ask_ai_for_json(system_prompt, user_prompt, want_object)


# ---------------------------------------------------------------------
# SUMMARY + KEY POINTS + IMPORTANT TERMS  (ONE AI request)
# ---------------------------------------------------------------------

STUDY_MATERIAL_SYSTEM_PROMPT = (
    "You are an assistant that creates study notes for students from "
    "video lecture transcripts. Use ONLY information contained in the "
    "transcript provided. Return valid JSON only, with no extra text "
    "and no markdown formatting."
)


def generate_study_material(transcript, quick_mode=True, progress_callback=None):
    """Generate the summary, key points and important terms.

    IMPORTANT: this is ONE structured AI request, not three. The original
    version called the AI separately for the summary, the key points and
    the terms, which tripled the waiting time. Asking for a single JSON
    object with three keys removes two network round-trips.

    LONG VIDEOS: if the transcript is longer than SINGLE_REQUEST_WORDS it
    is first split into chunks, each chunk is turned into a few bullet
    notes, and those notes are combined into the final answer with one
    more request. That is the simple "map reduce" idea - no vector
    database needed.

    quick_mode=True  -> shorter summary, 5 key points (faster + cheaper)
    quick_mode=False -> detailed summary, 8 key points, terms, short answers

    Returns a dict:
        {"summary": str, "key_points": [...], "important_terms": [...],
         "short_answers": [...]}
    """
    if not (transcript or "").strip():
        raise Exception("There is no transcript text to build study notes from.")

    if is_demo_mode():
        # No API key configured: build the notes from THIS transcript,
        # offline. (Previously a fixed sample answer was returned here,
        # which made every video look identical.)
        return local_content.build_study_material(transcript, quick_mode)

    try:
        return _ai_study_material(transcript, quick_mode, progress_callback)
    except Exception as error:
        print(f"[ai_service] AI study notes unavailable ({error}); "
              "building them from this transcript instead")
        return local_content.build_study_material(transcript, quick_mode)


def _ai_study_material(transcript, quick_mode, progress_callback):
    """Ask the AI for the study notes (used when an API key is configured)."""
    if progress_callback:
        progress_callback(0)

    # Short transcript: a single request gives everything at once
    if len(transcript.split()) <= SINGLE_REQUEST_WORDS:
        material = _ask_ai_for_json_safely(
            STUDY_MATERIAL_SYSTEM_PROMPT,
            _build_study_material_prompt(transcript, quick_mode)
        )
        if progress_callback:
            progress_callback(100)
        return _normalise_material(material, quick_mode)

    # Long transcript: summarise chunk by chunk, then combine
    from services.transcription import split_transcript_into_chunks
    chunks = split_transcript_into_chunks(transcript, CHUNK_WORDS)

    chunk_notes = []
    for index, chunk in enumerate(chunks):
        notes = _ask_ai_for_json_safely(
            "You take notes from a part of a lecture transcript. Return valid JSON only.",
            f"""
Read this part {index + 1} of {len(chunks)} of a lecture and return a JSON
object with one key, "notes", holding a list of short bullet-point strings
covering every important idea in this part.

Transcript part:
\"\"\"{chunk}\"\"\"
"""
        )
        chunk_notes.extend(notes.get("notes", []))

        if progress_callback:
            progress_callback(int((index + 1) * 70 / len(chunks)))

    combined_notes = "\n".join(f"- {note}" for note in chunk_notes)

    material = _ask_ai_for_json_safely(
        STUDY_MATERIAL_SYSTEM_PROMPT,
        _build_study_material_prompt(
            "These are the notes of consecutive parts of one long lecture:\n" + combined_notes,
            quick_mode
        )
    )

    if progress_callback:
        progress_callback(100)

    return _normalise_material(material, quick_mode)


def _build_study_material_prompt(source_text, quick_mode):
    """Build the user prompt for the single structured study-material request."""
    if quick_mode:
        summary_rule = "a short summary of the lesson, 80 to 150 words"
        key_point_rule = "exactly 5 short bullet-point strings covering the main ideas"
        terms_count = "3"
        extra = ""
    else:
        summary_rule = "a detailed summary of the lesson, 200 to 350 words"
        key_point_rule = "8 short bullet-point strings covering the main ideas"
        terms_count = "8"
        extra = (
            '\n- "short_answers": a list of exactly 3 objects, each with "question" '
            "and \"model_answer\" keys, testing deeper understanding."
        )

    return f"""
Using ONLY the following educational material, produce study notes.

The material runs from the beginning to the end of the lesson, so make
sure the summary and the key points cover the opening topic, the middle
AND the conclusion - not only the introduction.

Return a JSON object with exactly these keys:
- "summary": {summary_rule}.
- "key_points": {key_point_rule}.
- "important_terms": a list of {terms_count} objects, each with "term" and "meaning" keys.
- "notes": study notes for the WHOLE lesson as ONE string. Use lines
  starting with "# " for the main title, "## " for section headings and
  "- " for bullet points. Organise the notes with headings, subheadings
  and bullets so no important concept of the lesson is missing.{extra}

Material:
\"\"\"{source_text}\"\"\"
"""


def _normalise_material(material, quick_mode):
    """Make sure the AI answer has all the keys we expect, whatever it returned."""
    material = material or {}
    key_points = material.get("key_points") or []
    important_terms = material.get("important_terms") or []
    short_answers = material.get("short_answers") or []

    # The AI sometimes uses "definition" instead of "meaning"
    for term in important_terms:
        if "meaning" not in term and "definition" in term:
            term["meaning"] = term["definition"]

    if quick_mode:
        key_points = key_points[:5]
        important_terms = important_terms[:3]

    notes = material.get("notes") or ""
    if notes and not isinstance(notes, str):
        notes = str(notes)

    return {
        "summary": material.get("summary") or "",
        "key_points": key_points,
        "important_terms": important_terms,
        "short_answers": short_answers if not quick_mode else [],
        "notes": notes,
    }




def generate_summary(transcript, quick_mode=True):
    """Backwards-compatible helper: study material only (summary + points + terms)."""
    material = generate_study_material(transcript, quick_mode=quick_mode)
    return {
        "summary": material["summary"],
        "key_points": material["key_points"],
        "important_terms": material["important_terms"],
    }




# ---------------------------------------------------------------------
# SHORT ANSWER QUESTIONS
# ---------------------------------------------------------------------

def generate_short_answers(transcript):
    """Generate ~3 short-answer questions with an internal model answer.

    Returns a list of dicts: { "question": str, "model_answer": str }
    """
    if is_demo_mode():
        return local_content.build_short_answers(transcript, 3)

    system_prompt = (
        "You are an educational quiz generator. Use ONLY the transcript "
        "provided. Return valid JSON only, with no extra text."
    )
    user_prompt = f"""
Using ONLY the following transcript, write 3 short-answer questions that
test understanding of the lesson. For each, also write a brief model
answer (for internal grading reference, not shown to the student before
submission).

Return a JSON object with ONE key, "short_answers", holding a list of
objects each with keys "question" and "model_answer".

Transcript:
\"\"\"{transcript}\"\"\"
"""
    try:
        answer = _ask_ai_for_json_safely(system_prompt, user_prompt)
    except Exception as error:
        print(f"[ai_service] AI short answers unavailable ({error}); "
              "building them from this transcript instead")
        return local_content.build_short_answers(transcript, 3)

    answers = answer if isinstance(answer, list) else answer.get("short_answers", [])
    if not answers:
        return local_content.build_short_answers(transcript, 3)
    return answers




# ---------------------------------------------------------------------
# MCQ QUIZ GENERATION
# ---------------------------------------------------------------------

def generate_quiz(transcript, num_questions=5, difficulty="Medium"):
    """Generate multiple-choice questions from a transcript.

    Returns a list of dicts, each with:
    question, option_a, option_b, option_c, option_d, correct_answer, explanation
    """
    if not (transcript or "").strip():
        return []

    if is_demo_mode():
        # No API key: build the questions from THIS transcript, offline
        return local_content.build_quiz(transcript, num_questions, difficulty)

    try:
        questions = _ai_quiz(transcript, num_questions, difficulty)
    except Exception as error:
        print(f"[ai_service] AI quiz unavailable ({error}); "
              "building the questions from this transcript instead")
        return local_content.build_quiz(transcript, num_questions, difficulty)

    if not questions:
        return local_content.build_quiz(transcript, num_questions, difficulty)
    return questions[:num_questions]


def _ai_quiz(transcript, num_questions, difficulty):
    """Ask the AI for the multiple-choice questions (API key path)."""
    system_prompt = (
        "You are an educational quiz generator. Using ONLY the transcript "
        "provided, generate multiple-choice questions. Do not introduce "
        "information that is not present in the transcript. Return valid "
        "JSON only, with no extra text and no markdown formatting."
    )
    user_prompt = f"""
Using ONLY the following educational transcript, generate {num_questions}
multiple-choice questions at {difficulty} difficulty.

Return a JSON object with ONE key, "questions", holding a list where each
item is an object with exactly these keys:
"question", "option_a", "option_b", "option_c", "option_d",
"correct_answer" (must be exactly "A", "B", "C" or "D"), "explanation",
"topic" (2-4 words naming the subject of the question, e.g. "Machine
Learning" or "Prompt Engineering"), and "difficulty" (exactly one of
"Easy", "Medium" or "Hard").

Questions should test understanding of the lesson, not trivial wording.
Spread the questions across the WHOLE lesson - from the introduction,
through the middle, to the final part - not only from the opening.

Transcript:
\"\"\"{transcript}\"\"\"
"""
    answer = _ask_ai_for_json_safely(system_prompt, user_prompt)

    # The API is asked for an object, so the list is inside "questions".
    # A plain list is also accepted in case the model returns one.
    if isinstance(answer, list):
        questions = answer
    else:
        questions = answer.get("questions", [])

    # Fill in topic/difficulty so the results dashboard always has data
    cleaned = []
    for q in questions:
        if not q.get("question"):
            continue
        q["topic"] = (q.get("topic") or "General").strip()
        level = (q.get("difficulty") or difficulty).strip().capitalize()
        q["difficulty"] = level if level in ("Easy", "Medium", "Hard") else "Medium"
        cleaned.append(q)

    return cleaned




# ---------------------------------------------------------------------
# DEMO TRANSCRIPT (used when a student picks "Try Demo Mode")
# ---------------------------------------------------------------------

DEMO_TRANSCRIPT = """
Welcome to this lecture on Introduction to Artificial Intelligence. Artificial
Intelligence, or AI, is the field of computer science focused on building
systems that can perform tasks that normally require human intelligence.
This includes things like recognizing images, understanding language, and
making decisions.

There are two broad categories we should understand. Narrow AI is designed
to perform one specific task very well, such as facial recognition or a
spam filter. General AI, on the other hand, is a theoretical form of AI
that would match human intelligence across a wide range of tasks. Today,
all AI systems in use are examples of narrow AI.

The most common approach used to build modern AI systems is called Machine
Learning. Instead of a programmer writing explicit rules for every possible
situation, a machine learning system learns patterns directly from data.
The more relevant, high quality data the system sees, the better it tends
to perform.

One important type of machine learning is supervised learning. In supervised
learning, a model is trained using labelled examples, meaning the correct
answer is already known and provided during training. The model uses these
examples to learn the relationship between the input and the correct output,
so that it can later make predictions on new, unseen data.

The quality of the training data is extremely important. If the data is
biased, incomplete or incorrect, the resulting model will also produce
biased or incorrect predictions, no matter how advanced the algorithm is.

Finally, let's look at some real-world applications. Voice assistants like
the ones on our phones use AI to understand spoken language. Recommendation
systems, like the ones used by streaming services, use AI to suggest content
based on our past behaviour. Self-driving cars use AI to interpret data from
cameras and sensors in order to make driving decisions in real time.

That concludes our introduction to Artificial Intelligence. In the next
lecture, we will look more closely at how neural networks work.
"""


# ---------------------------------------------------------------------
# MIND MAP  (one nested structure, rendered as an interactive tree)
# ---------------------------------------------------------------------
# The AI returns ONE JSON object shaped like a tree:
#     {"label": "Root topic",
#      "children": [{"label": "...", "children": [...]}, ...]}
# Demo Mode builds the same shape from the built-in study material, so
# the mind map page always works without an API key.

MIND_MAP_SYSTEM_PROMPT = (
    "You are an assistant that creates mind maps for students from "
    "video lecture transcripts. Use ONLY information contained in the "
    "transcript provided. Return valid JSON only, with no extra text "
    "and no markdown formatting."
)

# Safety limits so one long lecture cannot produce a huge tree
MIND_MAP_MAX_DEPTH = 4
MIND_MAP_MAX_CHILDREN = 6


def generate_mind_map(transcript, title=None, progress_callback=None):
    """Turn a transcript into a mind map tree and return the nested dict.

    Returns:
        {"label": str, "children": [{"label": str, "children": [...]}, ...]}
    """
    if not (transcript or "").strip():
        # Nothing to read: fall back to the lecture title on its own
        return local_content.build_mind_map("", title or "Video Summary")

    if is_demo_mode():
        # No API key: build the map from THIS transcript, offline
        return local_content.build_mind_map(transcript, title)

    if progress_callback:
        progress_callback(0)

    try:
        data = _ask_ai_for_json_safely(
            MIND_MAP_SYSTEM_PROMPT,
            f"""
Create a mind map for the following lecture.

Return a JSON object with exactly these keys:
- "label": the main topic of the lecture (short, 3-7 words).
- "children": a list of 3 to 5 main branches. Each branch is an object
  with "label" (short branch name, 2-6 words) and "children": a list of
  2 to 4 leaf nodes. Each leaf node is an object with "label" (a short
  phrase from the lecture) and "children": [] (an empty list).

Lecture title (may help): {title or "Untitled lecture"}
Transcript:
\"\"\"{transcript}\"\"\"
"""
        )
    except Exception as error:
        print(f"[ai_service] AI mind map unavailable ({error}); "
              "building it from this transcript instead")
        return local_content.build_mind_map(transcript, title)

    if progress_callback:
        progress_callback(100)

    return _normalise_mind_map(data, title or "Lecture Overview")


def _normalise_mind_map(data, fallback_label, _depth=0):
    """Make sure the AI answer is a valid tree with the limits we expect."""
    if not isinstance(data, dict):
        return {"label": fallback_label, "children": []}

    label = str(data.get("label") or fallback_label).strip()[:80] or fallback_label
    raw_children = data.get("children")
    if not isinstance(raw_children, list):
        raw_children = []

    children = []
    # Depth and width limits keep the page readable and the JSON small
    if _depth < MIND_MAP_MAX_DEPTH:
        for child in raw_children[:MIND_MAP_MAX_CHILDREN]:
            children.append(_normalise_mind_map(child, "Idea", _depth + 1))

    return {"label": label, "children": children}


