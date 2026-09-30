"""
services/transliterate.py

TELUGU SCRIPT -> ENGLISH LETTERS (romanisation only - never a translation).

WHY THIS FILE EXISTS:

    Whisper writes Telugu speech as Telugu UNICODE text. The project rule
    is: "If the video is Telugu, write ALL generated content in Telugu
    using ENGLISH LETTERS only ... Never translate Telugu into unrelated
    English meaning."

    Every Telugu syllable is therefore RE-WRITTEN with English letters,
    keeping the exact same words and meaning ("gurinchi" stays
    "gurinchi"). English words the speaker mixed in pass through
    untouched, which also satisfies the mixed-speech rule.

HOW IT WORKS:

    Telugu is a syllabic script: a consonant carries an inherent "a"
    sound, a vowel sign (matra) replaces that "a", and the virama removes
    it. We walk the text once:

        క  -> "ka"   (inherent a kept)     కి -> "ki"  (sign replaces a)
        గా -> "ga"                        క్ -> "k"   (virama removes a)

    A small dictionary of common loan-words that Telugu speakers write
    in Telugu script ("వీడియో" -> "video") is applied first, so the
    result reads like natural Telugu-in-English-letters.

    Only the SCRIPT changes - never the language, never the meaning.
"""

import re

# Zero-width characters Telugu typewriters insert inside loan-words
_ZERO_WIDTH = "\u200c\u200d\u200b\ufeff"

# Common loan-words typed in Telugu script (applied BEFORE syllable
# conversion, longest first; keys must not contain zero-width chars
# because the text is normalised first).
_LOANWORDS = [
    ("వీడియో", "video"),
    ("ఆడియో", "audio"),
    ("డేటాబేస్", "database"),
    ("కంప్యూటర్", "computer"),
    ("ఇంటర్నెట్", "internet"),
    ("నెట్వర్క్", "network"),
    ("సాఫ్ట్వేర్", "software"),
    ("హార్డ్వేర్", "hardware"),
    ("పాస్వర్డ్", "password"),
    ("ప్రోగ్రామ్", "program"),
    ("ప్రాజెక్ట్", "project"),
    ("అల్గోరిథం", "algorithm"),
    ("ఫంక్షన్", "function"),
    ("వేరియబుల్", "variable"),
    ("అప్లికేషన్", "application"),
    ("ఎక్స్ప్లెయిన్", "explain"),
    ("క్లియర్", "clear"),
    ("సర్వర్", "server"),
    ("సిస్టమ్", "system"),
    ("డెవలపర్", "developer"),
    ("మెషీన్", "machine"),
    ("లాంగ్వేజ్", "language"),
    ("ఇంగ్లీష్", "english"),
    ("తెలుగు", "telugu"),
    ("స్టూడెంట్", "student"),
    ("టీచర్", "teacher"),
    ("ఈమెయిల్", "email"),
    ("బ్రౌజర్", "browser"),
    ("సెర్చ్", "search"),
    ("లాగిన్", "login"),
    ("యూజర్", "user"),
    ("ఫైల్", "file"),
    ("ఫోల్డర్", "folder"),
    ("సర్వీస్", "service"),
    ("స్ట్రింగ్", "string"),
    ("నంబర్", "number"),
    ("టేబుల్", "table"),
    ("క్వెరీ", "query"),
    ("స్కీమా", "schema"),
    ("ఇమేజ్", "image"),
    ("వెబ్", "web"),
    ("యాప్", "app"),
    ("డేటా", "data"),
    ("వాల్యూ", "value"),
]

# Consonant cores (WITHOUT the inherent "a")
_CONSONANTS = {
    "క": "k", "ఖ": "kh", "గ": "g", "ఘ": "gh", "ఙ": "ng",
    "చ": "ch", "ఛ": "ch", "జ": "j", "ఝ": "jh", "ఞ": "ny",
    "ట": "t", "ఠ": "th", "డ": "d", "ఢ": "dh", "ణ": "n",
    "త": "t", "థ": "th", "ద": "d", "ధ": "dh", "న": "n",
    "ప": "p", "ఫ": "ph", "బ": "b", "భ": "bh", "మ": "m",
    "య": "y", "ర": "r", "ల": "l", "వ": "v", "శ": "sh",
    "ష": "sh", "స": "s", "హ": "h", "ళ": "l", "ఱ": "r",
}

# Vowel signs (matras) that REPLACE the inherent "a"
_VOWEL_SIGNS = {
    "\u0c3e": "a",    # long aa is written "a" in natural Telugu-English letters
    "\u0c3f": "i",
    "\u0c40": "ee",
    "\u0c41": "u",
    "\u0c42": "oo",
    "\u0c43": "r",
    "\u0c46": "e",
    "\u0c47": "e",
    "\u0c48": "ai",
    "\u0c4a": "o",
    "\u0c4b": "o",
    "\u0c4c": "au",
}

# Independent vowels
_VOWELS = {
    "\u0c05": "a", "\u0c06": "aa", "\u0c07": "i", "\u0c08": "ee",
    "\u0c09": "u", "\u0c0a": "oo", "\u0c0b": "ri", "\u0c0e": "e",
    "\u0c0f": "ee", "\u0c10": "ai", "\u0c12": "o", "\u0c13": "o",
    "\u0c14": "au",
}

_VIRAMA = "\u0c4d"          # removes the inherent vowel
_ANUSVARA = "\u0c02"        # sounds like a following n/m
_CHANDRABINDU = "\u0c01"
_VISARGA = "\u0c03"

# Frequent 3-letter clusters handled as a unit (better spellings)
_CLUSTERS = {
    "క్ష": "ksha", "జ్ఞ": "jna", "శ్ర": "sra", "స్ర": "sra",
    "ప్ర": "pra", "బ్ర": "bra", "త్ర": "tra", "ద్ర": "dra",
    "గ్ర": "gra", "క్ర": "kra", "స్థ": "stha",
}

_TELUGU_DIGITS = {"\u0ce6": "0", "\u0ce7": "1", "\u0ce8": "2", "\u0ce9": "3",
                  "\u0cea": "4", "\u0ceb": "5", "\u0cec": "6", "\u0ced": "7",
                  "\u0cee": "8", "\u0cef": "9"}


def contains_telugu(text):
    """True when the text holds at least one Telugu-script character."""
    return any("\u0c00" <= ch <= "\u0c7f" for ch in (text or ""))


def _normalise(text):
    # Telugu typewriters insert zero-width joiners inside loan-words
    for char in _ZERO_WIDTH:
        text = text.replace(char, "")
    return text


def telugu_to_english(text):
    """Convert Telugu script to English letters.

    Non-Telugu text is copied character for character, so mixed
    Telugu + English speech stays mixed - only the Telugu SCRIPT is
    rewritten, never the words and never the meaning.
    """
    if not text or not contains_telugu(text):
        return text

    text = _normalise(text)

    # 1. well-known loan-words first, so "వీడియో" becomes "video"
    for telugu_word, english_word in _LOANWORDS:
        if telugu_word in text:
            text = text.replace(telugu_word, english_word)

    out = []
    index = 0
    length = len(text)

    while index < length:
        char = text[index]

        if not ("\u0c00" <= char <= "\u0c7f"):
            out.append(char)              # Latin text, punctuation, spaces...
            index += 1
            continue

        if char in _TELUGU_DIGITS:
            out.append(_TELUGU_DIGITS[char])
            index += 1
            continue

        # 2. frequent clusters as one unit: క్ష -> ksha
        cluster = text[index:index + 3]
        if cluster in _CLUSTERS:
            out.append(_CLUSTERS[cluster])
            index += 3
            continue

        # 3. consonant + optional vowel sign / virama
        if char in _CONSONANTS:
            core = _CONSONANTS[char]
            following = text[index + 1] if index + 1 < length else ""
            if following in _VOWEL_SIGNS:
                out.append(core + _VOWEL_SIGNS[following])
                index += 2
            elif following == _VIRAMA:
                out.append(core)          # vowel silenced: క్ -> k
                index += 2
            else:
                out.append(core + "a")    # inherent vowel: క -> ka
                index += 1
            continue

        # 4. independent vowels
        if char in _VOWELS:
            out.append(_VOWELS[char])
            index += 1
            continue

        # 5. vowel sign with no consonant in front (start of a word)
        if char in _VOWEL_SIGNS:
            out.append(_VOWEL_SIGNS[char])
            index += 1
            continue

        # 6. marks
        if char in (_ANUSVARA, _CHANDRABINDU):
            out.append("n")               # గురించి -> gurinchi (ం -> n)
            index += 1
            continue
        if char == _VISARGA:
            out.append("h")
            index += 1
            continue
        if char == _VIRAMA:
            index += 1                    # stray virama: nothing to silence
            continue

        index += 1                         # unknown Telugu char: skip, no guess

    converted = "".join(out)

    # 7. capitalise the first letter of each sentence for readability
    parts = re.split(r"([.!?]\s*)", converted)
    for i in range(0, len(parts), 2):
        part = parts[i]
        if part:
            stripped = part.lstrip()
            if stripped:
                offset = part.index(stripped[0])
                parts[i] = part[:offset] + stripped[0].upper() + stripped[1:]
    return "".join(parts)


def romanise_transcript(text):
    """Public entry point used by the transcription pipeline.

    Returns (transcript, changed). `changed` is True when Telugu script
    was present and has been rewritten in English letters - the pipeline
    uses it to tell the user what happened.
    """
    if not contains_telugu(text):
        return text, False
    return telugu_to_english(text), True
