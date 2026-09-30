"""
services/local_content.py

OFFLINE study-material generator.

Normally the app asks the OpenAI API to write the summary, key points,
important terms, short-answer questions, quiz and mind map.

When no API key is configured (or an API request fails) this module takes
over and builds ALL of those from the CURRENT transcript using plain
Python. This replaces the old behaviour, where the fallback returned one
fixed sample answer about "Introduction to AI" for every video - which
made every upload look identical.

Everything here is computed from the text it is handed, so two different
videos always produce two different result sets.

HOW IT WORKS (extractive summarisation - no machine learning needed):

    1. split the transcript into sentences
    2. score each sentence by how often its words occur in the whole
       transcript (word frequency) plus a small bonus for early
       sentences, which normally carry the lecture's main ideas
    3. summary    = highest scoring sentences, kept in reading order
    4. key points = next highest scoring, de-duplicated sentences
    5. terms      = most frequent meaningful words / short phrases, each
                    explained using the sentence it came from
    6. quiz       = fill-in-the-blank and "what is X?" questions built
                    from those sentences and terms, where the wrong
                    options are taken from OTHER terms of the SAME
                    transcript
    7. mind map   = a small tree built from the terms and key points
    8. short answers = the same material phrased as practice questions

Every public function only ever looks at the transcript it is given, so
results can never leak from one video to another.
"""

import hashlib
import math
import random
import re
from collections import Counter

# Sentences outside this word range are ignored when building points
# (too short = fragments, too long = hard to read as a bullet point)
MIN_SENTENCE_WORDS = 6
MAX_SENTENCE_WORDS = 40

# Very common English words that carry no topic meaning
STOP_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and",
    "any", "are", "as", "at", "be", "because", "been", "before", "being", "below",
    "between", "both", "but", "by", "can", "cannot", "could", "did", "do", "does",
    "doing", "down", "during", "each", "every", "few", "for", "from", "further", "get",
    "got", "had", "has", "have", "having", "he", "her", "here", "hers", "herself",
    "him", "himself", "his", "how", "i", "if", "in", "into", "is", "it", "its",
    "itself", "just", "me", "more", "most", "my", "myself", "no", "nor", "not",
    "of", "off", "on", "once", "only", "or", "other", "ought", "our", "ours",
    "ourselves", "out", "over", "own", "same", "she", "should", "so", "some",
    "such", "than", "that", "the", "their", "theirs", "them", "themselves", "then",
    "there", "these", "they", "this", "those", "through", "to", "too", "under",
    "until", "up", "very", "was", "we", "were", "what", "when", "where", "which",
    "while", "who", "whom", "why", "will", "with", "would", "you", "your",
    "yours", "yourself", "yourselves",
    # filler words that appear in spoken lectures
    "actually", "also", "anyway", "basically", "course", "everyone", "going",
    "gonna", "kind", "know", "like", "look", "lot", "maybe", "much", "need",
    "okay", "really", "right", "said", "say", "see", "sure", "take", "thing",
    "things", "think", "today", "told", "want", "way", "well",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

# Words that are too general to be a useful "important term" on their own.
# They are still allowed inside a multi-word term ("process management").
GENERIC_WORDS = {
    "air", "another", "anything", "area", "basic", "called", "case", "certain",
    "class", "day",
    "different", "essentially", "everything", "example", "first", "form",
    "general", "group", "idea", "important", "inside", "kind", "last", "lecture", "level",
    "list", "main", "matters", "next", "nothing", "number", "part", "people", "person",
    "place", "point", "process", "question", "result", "second", "simple",
    "something", "sort", "specific", "stage", "step", "student", "teacher",
    "term", "third", "time", "topic", "type", "using", "value", "video", "word", "year",
    "answer",
}


def _stem(word):
    """Very small stemmer: 'devices' and 'device' should look the same."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        # "boxes" -> "box" / "matches" -> "match" / "addresses" -> "address"
        if word.endswith("es"):
            without_es = word[:-2]
            if without_es.endswith(("s", "z", "x", "ch", "sh")):
                return without_es
        return word[:-1]          # "devices" -> "device"
    return word

# Sentence starts that only introduce the next idea; removed from bullets
_LEADING_FILLERS = (
    "so", "and", "but", "now", "okay", "ok", "well", "basically", "actually",
    "right", "also", "therefore", "however", "then", "first", "second",
    "third", "next", "finally", "in fact", "of course", "you know",
    "let's", "lets", "as i said", "as we said", "remember",
)

# "X is/are/means Y" - used to write a term's meaning from its sentence
_DEFINITION_RE = re.compile(
    r"^(?:the\s+|a\s+|an\s+)?(?P<term>[A-Za-z0-9][\w\s\-']{1,60}?)\s+"
    r"(?:is|are|was|were|means|refers to|describes|is called|is defined as|"
    r"can be defined as)\s+(?P<meaning>.+)$",
    re.IGNORECASE,
)


def _words(text):
    """All words in a piece of text, lower case."""
    return [word.lower() for word in _WORD_RE.findall(text or "")]


def _content_words(text):
    """Meaningful words only (very common words removed)."""
    return [word for word in _words(text) if word not in STOP_WORDS and len(word) > 2]


def split_sentences(transcript):
    """Split a transcript into usable sentences.

    Very short fragments are dropped because they cannot become a useful
    key point or quiz question. Over-long sentences are shortened so that
    they stay readable.
    """
    text = (transcript or "").replace("\r", " ")
    # Join wrapped lines first: a line break in a transcript does not mean
    # the sentence ended, so "…make food\nfrom water." must stay one sentence.
    text = re.sub(r"\s+", " ", text)

    sentences = []
    seen = set()

    for piece in _SENTENCE_SPLIT_RE.split(text):
        candidate = re.sub(r"\s+", " ", piece).strip()
        # Whisper sometimes prefixes a line with ">>" or a dash
        candidate = re.sub(r"^[>\u00bb\-\u2013\u2014]+\s*", "", candidate)

        if not candidate:
            continue

        word_count = len(_words(candidate))
        if word_count < MIN_SENTENCE_WORDS:
            continue

        if word_count > MAX_SENTENCE_WORDS:
            shortened = " ".join(candidate.split()[:MAX_SENTENCE_WORDS])
            candidate = shortened.rstrip(" ,;:") + "."

        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        sentences.append(candidate)

    return sentences


def _sentence_scores(sentences):
    """Give every sentence a score based on how central its words are."""
    frequencies = Counter()
    for sentence in sentences:
        frequencies.update(set(_content_words(sentence)))

    scores = []
    for index, sentence in enumerate(sentences):
        words = _content_words(sentence)
        if not words:
            scores.append(0.0)
            continue
        score = sum(frequencies[word] for word in set(words)) / math.sqrt(len(words))
        if index < 3:
            score *= 1.25          # opening lines normally state the topic
        scores.append(score)
    return scores


def _rank(sentences):
    """Return sentence indexes best-first, plus the score list."""
    scores = _sentence_scores(sentences)
    order = sorted(range(len(sentences)), key=lambda i: scores[i], reverse=True)
    return order, scores


def _bucket_orders(sentences, parts=3):
    """Rank sentences separately inside equal positional segments.

    Returns (scores, [segment_0_ranked, segment_1_ranked, ...]).

    WHY THIS EXISTS: ranking purely by word frequency lets the opening of
    a long lecture dominate the results, because the introduction repeats
    the topic word most often. The notes then cover only the first part of
    the video.

    The caller passes how many items it needs (parts), so there is one
    segment per item: picking the best sentence of EVERY segment then
    guarantees the results are spread evenly over the whole video, from
    beginning to end - even when the real topic sections do not align
    with exact thirds.
    """
    order, scores = _rank(sentences)
    total = len(sentences)
    parts = max(min(parts, total), 1)
    buckets = [[] for _ in range(parts)]
    for index in order:
        segment = min(index * parts // total, parts - 1)
        buckets[segment].append(index)
    return scores, buckets


def _trim_words(text, max_words):
    """Shorten a sentence to at most max_words words."""
    words = (text or "").split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(" ,;:") + "..."


def _overlap(first_words, second_words):
    """Jaccard similarity (0-1) between two sets of words."""
    if not first_words or not second_words:
        return 0.0
    return len(first_words & second_words) / len(first_words | second_words)


def _fingerprint(text):
    """A stable number for a piece of text (used to seed the randomiser)."""
    digest = hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()
    return int(digest[:8], 16)


def summarise(transcript, quick_mode=True):
    """Write a short summary using the transcript's own sentences.

    One strong sentence is taken from the opening, the middle AND the
    closing third of the transcript first, then the remaining room is
    filled with the best sentences overall. The summary therefore always
    reflects the WHOLE video, never only its introduction.
    """
    sentences = split_sentences(transcript)
    if not sentences:
        # Too short to summarise - return the cleaned-up original text
        return re.sub(r"\s+", " ", (transcript or "")).strip()[:600]

    max_words = 130 if quick_mode else 220
    wanted = 6 if quick_mode else 9

    # one segment per wanted sentence -> even coverage of the whole video
    _scores, buckets = _bucket_orders(sentences, wanted)

    picked = []
    total_words = 0
    picked_words = []

    def try_pick(index):
        nonlocal total_words
        sentence_words = len(_words(sentences[index]))
        if picked and total_words + sentence_words > max_words:
            return False          # the word budget always wins
        words = set(_content_words(sentences[index]))
        if any(_overlap(words, used) > 0.55 for used in picked_words):
            return False          # nearly the same as one already picked
        picked.append(index)
        total_words += sentence_words
        if words:
            picked_words.append(words)
        return True

    # 1. one strong sentence from each third of the video
    for bucket in buckets:
        for index in bucket:
            if try_pick(index):
                break

    # 2. keep filling ROUND-ROBIN over the thirds (skipping near
    #    duplicates), so every third of the video contributes and the
    #    summary never turns into a recap of the introduction only
    progressed = True
    while len(picked) < wanted and progressed:
        progressed = False
        for bucket in buckets:
            if len(picked) >= wanted:
                break
            for index in bucket:
                if index in picked:
                    continue
                if try_pick(index):
                    progressed = True
                    break

    picked.sort()                      # back into reading order
    return " ".join(sentences[index] for index in picked)


def _as_key_point(sentence, max_words=24):
    """Turn a sentence into a tidy bullet point."""
    text = (sentence or "").strip()

    lowered = text.lower()
    for filler in _LEADING_FILLERS:
        if lowered.startswith(filler + " "):
            text = text[len(filler):].strip(" ,;:")
            break

    text = _trim_words(text, max_words).strip().rstrip(" ,;:")
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if not text.endswith("."):
        text += "."
    return text


def key_points(transcript, quick_mode=True):
    """Pick the most central sentences as key learning points.

    Candidates are taken round-robin from the opening, middle and closing
    thirds of the transcript, so the points always span the whole video.
    """
    wanted = 5 if quick_mode else 8
    sentences = split_sentences(transcript)
    if not sentences:
        return []

    # one segment per wanted point -> points spread over the whole video
    _scores, buckets = _bucket_orders(sentences, wanted)
    points = []
    used_word_sets = []

    def try_point(index):
        sentence = sentences[index]
        words = set(_content_words(sentence))
        if not words:
            return False
        # Skip a sentence that says almost the same as one we already have
        if any(_overlap(words, used) > 0.55 for used in used_word_sets):
            return False
        point = _as_key_point(sentence)
        if not point:
            return False
        points.append(point)
        used_word_sets.append(words)
        return True

    progressed = True
    while len(points) < wanted and progressed:
        progressed = False
        for bucket in buckets:
            if len(points) >= wanted:
                break
            for index in list(bucket):
                if try_point(index):
                    bucket.remove(index)
                    progressed = True
                    break

    return points


def _finish_sentence(text):
    """Capitalise a fragment and give it a full stop."""
    text = (text or "").strip().strip(" ,;:")
    text = text.rstrip(".")
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text + "."


def _title_case(term):
    """'machine learning' -> 'Machine Learning' (existing capitals kept)."""
    if any(char.isupper() for char in term):
        return term
    return " ".join(word.capitalize() for word in term.split())


def meaning_of(term, sentences, scores):
    """Explain one term using the best sentence that mentions it."""
    pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)

    best_sentence = None
    best_score = -1.0
    for index, sentence in enumerate(sentences):
        if not pattern.search(sentence):
            continue
        if scores[index] > best_score:
            best_sentence = sentence
            best_score = scores[index]

    if not best_sentence:
        return ""

    # "X is/are/means Y" -> the part after the verb is a ready-made meaning
    match = _DEFINITION_RE.match(best_sentence.strip())
    if match and term.lower() in match.group("term").lower():
        meaning = match.group("meaning").strip()
        if len(meaning.split()) >= 3:
            return _finish_sentence(_trim_words(meaning, 30))

    # otherwise use the sentence itself, which shows the term in context
    return _finish_sentence(_trim_words(best_sentence.strip(), 32))


def important_terms(transcript, quick_mode=True, sentences=None, count=None):
    """Find the terms this lecture keeps coming back to.

    A term is either a frequent meaningful word ("photosynthesis"), a
    frequent word pair ("machine learning") or an acronym that appears
    more than once ("TCP"). The meaning always comes from the transcript.
    """
    wanted = count or (6 if quick_mode else 8)
    sentences = sentences if sentences is not None else split_sentences(transcript)
    if not sentences:
        return []

    _order, scores = _rank(sentences)

    single_words = Counter()
    word_pairs = Counter()
    acronyms = Counter()

    for sentence in sentences:
        tokens = _words(sentence)
        single_words.update(word for word in tokens
                            if word not in STOP_WORDS and len(word) > 2)

        # Only pair words that are actually next to each other in the text,
        # so "machine learning" is a candidate but "learning systems" is not.
        for first, second in zip(tokens, tokens[1:]):
            if first in STOP_WORDS or second in STOP_WORDS:
                continue
            if len(first) < 3 or len(second) < 3:
                continue
            word_pairs[f"{first} {second}"] += 1

        for token in _WORD_RE.findall(sentence):
            if len(token) > 1 and token.isupper() and token.lower() not in STOP_WORDS:
                acronyms[token] += 1

    candidates = []
    for term, frequency in single_words.items():
        if term in GENERIC_WORDS:
            continue
        if frequency >= 2 or (len(term) >= 8 and frequency >= 1):
            candidates.append((frequency + min(len(term), 10) / 10.0, term))
    for term, frequency in word_pairs.items():
        if frequency >= 2:
            candidates.append((frequency * 2.5, term))       # phrases are precise
    for term, frequency in acronyms.items():
        if frequency >= 2:
            candidates.append((frequency * 2.0, term))

    candidates.sort(key=lambda item: item[0], reverse=True)

    # Which positional segment is each candidate's strongest sentence in?
    # (Keeps the term list spread evenly over the whole video, instead of
    # being made up entirely of intro terms.)
    total = len(sentences)
    parts = max(min(count, total), 1)

    def _bucket_of(term):
        # A term belongs to the segment where it is FIRST introduced in
        # the video - that is its natural position, and it guarantees
        # early topics are attributed to the opening segment.
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        for index, sentence in enumerate(sentences):
            if pattern.search(sentence):
                return min(index * parts // total, parts - 1)
        return 0

    candidate_bucket = {term: _bucket_of(term) for _score, term in candidates}

    def _meaning_segment(term):
        # Segment of the sentence meaning_of() will quote for this term
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        best_index, best_score = None, -1.0
        for index, sentence in enumerate(sentences):
            if pattern.search(sentence) and scores[index] > best_score:
                best_index, best_score = index, scores[index]
        if best_index is None:
            return None
        return min(best_index * parts // total, parts - 1)

    terms = []
    used_stems = set()

    def try_term(term):
        stems = {_stem(word) for word in term.split()}
        # Skip a term that repeats a word we already used: this drops
        # "devices" when "device" is listed, and "protocol addresses" when
        # "internet protocol" is listed.
        if stems & used_stems:
            return False
        meaning = meaning_of(term, sentences, scores)
        if not meaning:
            return False
        terms.append({"term": _title_case(term), "meaning": meaning})
        used_stems.update(stems)      # method call: keeps it closure-friendly
        return True

    # 1. the best candidate of each segment, so the list spans the video.
    #    A candidate whose meaning is quoted from the SAME segment is
    #    preferred, so every term really is explained with its own part
    #    of the lecture; otherwise any meaning from the transcript counts.
    for wanted_bucket in range(parts):
        if len(terms) >= wanted:
            break
        for anchored in (True, False):
            picked_this_segment = False
            for _score, term in candidates:
                if candidate_bucket[term] != wanted_bucket:
                    continue
                if anchored and _meaning_segment(term) != wanted_bucket:
                    continue
                if try_term(term):
                    picked_this_segment = True
                    break
            if picked_this_segment:
                break

    # 2. fill the rest with the highest scoring candidates
    for _score, term in candidates:
        if len(terms) >= wanted:
            break
        try_term(term)

    return terms


# ---------------------------------------------------------------------
# QUIZ  (built from the sentences and terms of THIS transcript)
# ---------------------------------------------------------------------

def _difficulty_for(kind, requested):
    """Use the requested difficulty, or vary it when "Mixed" is asked for."""
    cleaned = (requested or "").strip().capitalize()
    if cleaned in ("Easy", "Medium", "Hard"):
        return cleaned
    return "Easy" if kind == "definition" else "Medium"


def _wrong_options(terms, sentence, rng, wanted=3):
    """Wrong options: other terms of the same transcript, not in this sentence."""
    lowered = sentence.lower()
    pool = [term for term in terms if term.lower() not in lowered]
    if len(pool) < wanted:
        return []
    return rng.sample(pool, wanted)


def _spread_order(order, sentences, parts=6):
    """Re-order ranked sentence indexes by interleaving positional parts.

    The best sentence of part 0, the best of part 1, ... then the second
    best of each part, and so on. Quiz questions built from this order
    therefore come from DIFFERENT PARTS of the video (beginning, middle
    and end) instead of clustering in the introduction.
    """
    total = len(sentences)
    if total <= 1:
        return list(order)
    parts = max(min(parts, total), 1)
    buckets = [[] for _ in range(parts)]
    for index in order:
        buckets[min(index * parts // total, parts - 1)].append(index)

    spread = []
    remaining = True
    while remaining:
        remaining = False
        for bucket in buckets:
            if bucket:
                spread.append(bucket.pop(0))
                remaining = True
    return spread


def build_quiz(transcript, num_questions=5, difficulty="Medium"):
    """Create multiple-choice questions from one transcript.

    Two question styles are produced:

      * definition questions - "According to this lecture, what is X?"
      * fill-in-the-blank questions built from a sentence of the lecture

    Each wrong option is a different term taken from the SAME transcript,
    and every explanation quotes the sentence the answer came from, so the
    questions always match the video they belong to.
    """
    sentences = split_sentences(transcript)
    if not sentences:
        return []

    order, scores = _rank(sentences)
    # Questions must come from different parts of the video, so the
    # candidates are interleaved across positional segments first.
    order = _spread_order(order, sentences, 6)
    terms = [item["term"] for item in important_terms(
        transcript, quick_mode=False, sentences=sentences,
        count=max(8, num_questions + 4))]

    # NOTE: we no longer give up when the lecture has fewer than four
    # terms - styles 2 and 3 below can still build questions, so the quiz
    # always reaches the requested question count when there is content.

    rng = random.Random(_fingerprint(transcript))
    questions = []
    used_sentences = set()
    used_terms = set()

    # ---- style 1: definition questions -------------------------------
    for term in terms:
        if len(questions) >= num_questions:
            break
        if term.lower() in used_terms:
            continue

        correct = meaning_of(term, sentences, scores)
        if not correct or len(correct.split()) < 4:
            continue

        distractors = []
        for other in terms:
            if other.lower() == term.lower() or other.lower() in used_terms:
                continue
            text = meaning_of(other, sentences, scores)
            if not text or _overlap(set(_words(text)), set(_words(correct))) > 0.5:
                continue
            distractors.append(text)
            if len(distractors) == 3:
                break
        if len(distractors) < 3:
            continue

        options = distractors + [correct]
        rng.shuffle(options)
        questions.append({
            "question": f'According to this lecture, what is "{term}"?',
            "option_a": options[0],
            "option_b": options[1],
            "option_c": options[2],
            "option_d": options[3],
            "correct_answer": "ABCD"[options.index(correct)],
            "explanation": f'From the transcript: "{_trim_words(correct, 30)}"',
            "topic": term,
            "difficulty": _difficulty_for("definition", difficulty),
        })
        used_terms.add(term.lower())

    # ---- style 2: fill-in-the-blank questions ------------------------
    for index in order:
        if len(questions) >= num_questions:
            break
        if index in used_sentences:
            continue

        sentence = sentences[index]
        target = None
        for term in terms:
            if re.search(r"\b" + re.escape(term) + r"\b", sentence, re.IGNORECASE):
                target = term
                break
        if not target or target.lower() in used_terms:
            continue

        wrong = _wrong_options(terms, sentence, rng, 3)
        if len(wrong) < 3:
            continue

        blanked = re.sub(r"\b" + re.escape(target) + r"\b", "______",
                         sentence, flags=re.IGNORECASE)
        options = wrong + [target]
        rng.shuffle(options)
        questions.append({
            "question": 'Which term completes this statement from the lecture? '
                        f'"{blanked}"',
            "option_a": options[0],
            "option_b": options[1],
            "option_c": options[2],
            "option_d": options[3],
            "correct_answer": "ABCD"[options.index(target)],
            "explanation": f'The lecture says: "{_trim_words(sentence, 30)}"',
            "topic": target,
            "difficulty": _difficulty_for("blank", difficulty),
        })
        used_sentences.add(index)
        used_terms.add(target.lower())

    # ---- style 3: general fill-in-the-blank (tops the quiz up) --------
    # Any high-information sentence can become a question: blank one of
    # its distinctive words and use OTHER content words of the SAME
    # transcript as the wrong options. This guarantees the full question
    # count even when the lecture has too few formal terms for styles 1+2.
    word_frequency = Counter()
    for sentence in sentences:
        for word in _content_words(sentence):
            if len(word) >= 4:
                word_frequency[word] += 1
    pool = [word for word, _count in word_frequency.most_common(40)]

    for index in order:
        if len(questions) >= num_questions:
            break
        if index in used_sentences:
            continue

        sentence = sentences[index]
        targets = [word for word in _words(sentence)
                   if len(word) >= 4 and word in word_frequency]
        if not targets:
            continue

        target = max(targets, key=lambda w: (word_frequency[w], len(w)))
        if target in used_terms:
            alternatives = [word for word in targets if word not in used_terms]
            if not alternatives:
                continue
            target = max(alternatives,
                         key=lambda w: (word_frequency[w], len(w)))

        lowered = sentence.lower()
        wrong = []
        for word in pool:
            if word == target or word in used_terms:
                continue
            if word in lowered:
                continue          # already part of the sentence
            wrong.append(word)
            if len(wrong) == 3:
                break
        if len(wrong) < 3:
            continue

        blanked = re.sub(r"\b" + re.escape(target) + r"\b", "______",
                         sentence, flags=re.IGNORECASE)
        options = wrong + [target]
        rng.shuffle(options)
        questions.append({
            "question": 'Which word completes this statement from the '
                        f'lecture? "{blanked}"',
            "option_a": options[0],
            "option_b": options[1],
            "option_c": options[2],
            "option_d": options[3],
            "correct_answer": "ABCD"[options.index(target)],
            "explanation": f'The lecture says: "{_trim_words(sentence, 30)}"',
            "topic": target.capitalize(),
            "difficulty": _difficulty_for("blank", difficulty),
        })
        used_sentences.add(index)
        used_terms.add(target)

    return questions[:num_questions]


# ---------------------------------------------------------------------
# SHORT ANSWER PRACTICE
# ---------------------------------------------------------------------

def build_short_answers(transcript, count=3):
    """Practice questions whose model answers come from the transcript."""
    sentences = split_sentences(transcript)
    if not sentences:
        return []

    _order, scores = _rank(sentences)
    terms = important_terms(transcript, quick_mode=True, sentences=sentences,
                            count=max(count, 4))

    answers = []
    for item in terms:
        if len(answers) >= count:
            break
        meaning = meaning_of(item["term"], sentences, scores)
        if not meaning:
            continue
        answers.append({
            "question": f'What does the lecture say about "{item["term"]}"?',
            "model_answer": meaning,
        })
    return answers


# ---------------------------------------------------------------------
# MIND MAP
# ---------------------------------------------------------------------

def _short_label(sentence, max_words=8):
    """Turn a sentence into a very short branch label."""
    label = _as_key_point(sentence, max_words=max_words).rstrip(".")
    return label or (sentence or "")[:60]


def _guess_topic(transcript):
    """Best guess at the lecture topic: the strongest term of the transcript."""
    terms = important_terms(transcript, quick_mode=True, count=1)
    if terms:
        return terms[0]["term"]
    sentences = split_sentences(transcript)
    if sentences:
        return _short_label(sentences[0], 6)
    return "Video Summary"


def build_mind_map(transcript, title=None):
    """Build the mind map tree from this transcript only."""
    sentences = split_sentences(transcript)
    root_label = (title or "").strip() or _guess_topic(transcript)

    if not sentences:
        return {"label": root_label[:80], "children": []}

    points = key_points(transcript, quick_mode=False)[:5]
    terms = [item["term"] for item in
             important_terms(transcript, quick_mode=False, count=5)]

    branches = []
    if points:
        branches.append({"label": "Main Ideas",
                         "children": [{"label": point.rstrip(".")[:70], "children": []}
                                      for point in points]})
    if terms:
        branches.append({"label": "Important Terms",
                         "children": [{"label": term[:60], "children": []}
                                      for term in terms]})

    # "Lecture Flow" should show the path through the WHOLE lecture, not
    # only its first sentences: one label from each third plus the ending.
    total = len(sentences)
    cut = max(total // 3, 1)
    flow_indexes = []
    for index in (0, cut, 2 * cut, total - 1):
        if 0 <= index < total and index not in flow_indexes:
            flow_indexes.append(index)
    opening = [_short_label(sentences[index])
               for index in sorted(flow_indexes)[:4]]
    if opening:
        branches.append({"label": "Lecture Flow",
                         "children": [{"label": label, "children": []}
                                      for label in opening]})

    return {"label": root_label[:80], "children": branches}


# ---------------------------------------------------------------------
# DETAILED STUDY NOTES  (headings + subheadings + bullet points)
# ---------------------------------------------------------------------

def build_notes(transcript, title=None):
    """Build well-organised study notes from the WHOLE transcript.

    Returns a plain-text document with Markdown-style markers that the
    summary page renders as real headings and bullets:

        # <topic> - Study Notes       main title
        ## <number>. <heading>        one section per part of the video
        - bullet                      key sentences of that section
        ## Key Terms
        - Term: meaning

    Sections come from POSITIONAL segments of the transcript, so the
    notes cover the beginning, middle and end of the video, and every
    line is taken from the transcript itself - whatever language it is
    written in (including Telugu-in-English-letters).
    """
    sentences = split_sentences(transcript)
    if not sentences:
        return ""

    topic = (title or "").strip() or _guess_topic(transcript)
    lines = [f"# {topic} - Study Notes"]

    total = len(sentences)
    if total >= 16:
        section_count = 4
    elif total >= 9:
        section_count = 3
    elif total >= 5:
        section_count = 2
    else:
        section_count = 1

    _scores, sections = _bucket_orders(sentences, section_count)
    picked_words = []

    for number, section in enumerate(sections, start=1):
        if not section:
            continue
        heading = _short_label(sentences[section[0]], 9)
        heading = heading or _as_key_point(sentences[section[0]], max_words=9)
        lines.append("")
        lines.append(f"## {number}. {heading.rstrip('.')}")

        added = 0
        for index in section:
            if added >= 3:
                break
            words = set(_content_words(sentences[index]))
            if not words:
                continue
            if any(_overlap(words, used) > 0.55 for used in picked_words):
                continue
            point = _as_key_point(sentences[index], max_words=26)
            if not point:
                continue
            lines.append(f"- {point}")
            picked_words.append(words)
            added += 1

        if added == 0:
            # every sentence of this section overlapped earlier picks -
            # still show the best one so no part of the video is missing
            point = _as_key_point(sentences[section[0]], max_words=26)
            if point:
                lines.append(f"- {point}")

    terms = important_terms(transcript, quick_mode=False, sentences=sentences,
                            count=6)
    if terms:
        lines.append("")
        lines.append("## Key Terms")
        for item in terms:
            lines.append(f"- {item['term']}: {item['meaning']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------
# EVERYTHING TOGETHER  (this is what the pipeline calls)
# ---------------------------------------------------------------------

def build_study_material(transcript, quick_mode=True):
    """Summary + key points + terms + short answers for ONE transcript.

    The Quick/Detailed limits match the ones used for the AI answers:
        Quick    -> 5 key points, 3 terms, no short answers
        Detailed -> 8 key points, 8 terms, 3 short answers
    """
    sentences = split_sentences(transcript)
    term_count = 3 if quick_mode else 8

    return {
        "summary": summarise(transcript, quick_mode),
        "key_points": key_points(transcript, quick_mode),
        "important_terms": important_terms(transcript, quick_mode=True,
                                           sentences=sentences, count=term_count),
        "short_answers": [] if quick_mode else build_short_answers(transcript, 3),
        "notes": build_notes(transcript),
    }
