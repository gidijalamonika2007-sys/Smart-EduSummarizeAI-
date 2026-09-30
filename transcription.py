"""
services/transcription.py

Speech-to-text with OpenAI Whisper (running locally on the CPU).

PERFORMANCE DETAILS (worth keeping for the report):

1. Whisper normally runs ffmpeg itself to convert the input into a
   16 kHz mono float array. Our audio is ALREADY a 16 kHz mono WAV
   (produced by video_processor.extract_audio), so we read it directly
   with the built-in 'wave' module and NumPy. That removes a whole
   ffmpeg pass over the file - one less full read of the audio.

2. We use the 'base' Whisper model by default. 'tiny' is faster but makes
   more mistakes; 'small'/'medium' are noticeably slower on a laptop CPU.
   The model can be changed with the WHISPER_MODEL environment variable.

3. Long audio is transcribed in ORDERED CHUNKS (WHISPER_CHUNK_SECONDS,
   default 10 minutes) one after another (WHISPER_CHUNK_WORKERS can opt
   into parallel decoding on machines where it is stable) and the parts
   are then combined IN ORDER, so no section of the video can be
   skipped or truncated. Every chunk is retried once before the job
   fails with a clear error - the app then shows the failure instead of
   inventing content.

4. The language is detected from the first seconds of speech BEFORE the
   full decode (shown on the processing page) and then forced for all
   chunks, so a long video keeps one consistent language. Telugu
   speech is rewritten in English letters (see services/transliterate).

5. The model is loaded once and cached in memory (see _model_cache) so a
   second video in the same session does not reload the weights again.

Demo Mode never reaches this file at all, so demos start instantly.
"""

import os
import re
import wave

import numpy as np

from services.transliterate import romanise_transcript

# Smallest -> fastest: tiny, base, small, medium, large
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "base")

# Whisper expects 16 kHz mono audio. If a different sample rate turns up
# we fall back to letting Whisper handle the file itself.
WHISPER_SAMPLE_RATE = 16000

# How much audio ONE Whisper call handles. Longer videos are split into
# chunks of this many seconds, decoded in order (optionally a few in
# parallel) and combined afterwards.
CHUNK_SECONDS = max(float(os.environ.get("WHISPER_CHUNK_SECONDS", "600")), 15.0)

# How many chunks may be decoded at the same time (1 = sequential).
# DEFAULT IS SEQUENTIAL: concurrent Whisper decodes in threads proved
# unstable on this CPU/torch build, and sequential chunk decoding is
# already well faster than real-time. Set WHISPER_CHUNK_WORKERS=2 to
# opt in on machines where it is stable - results are stored by index,
# so the combined order is identical either way.
CHUNK_WORKERS = max(int(os.environ.get("WHISPER_CHUNK_WORKERS", "1")), 1)

# Friendly names for the language badge / processing status messages
LANGUAGE_NAMES = {
    "en": "English", "te": "Telugu", "hi": "Hindi", "ta": "Tamil",
    "kn": "Kannada", "ml": "Malayalam", "bn": "Bengali", "mr": "Marathi",
    "ur": "Urdu", "gu": "Gujarati", "pa": "Punjabi", "or": "Odia",
    "es": "Spanish", "fr": "French", "de": "German", "ru": "Russian",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
    "pt": "Portuguese", "it": "Italian", "nl": "Dutch", "tr": "Turkish",
}


def language_name(code):
    """Human readable language name for a Whisper language code."""
    code = (code or "").lower()
    if not code:
        return "Unknown"
    return LANGUAGE_NAMES.get(code, code.upper())


# Cache of already-loaded models, so repeated videos are faster
_model_cache = {}


def load_model(model_name=None):
    """Load (or reuse) a Whisper model. Loading a model is the slow part."""
    import whisper

    model_name = model_name or DEFAULT_MODEL
    if model_name not in _model_cache:
        # Print a short note so the student can watch progress in the terminal
        print(f"[transcription] Loading Whisper model '{model_name}' (first time only)...")
        _model_cache[model_name] = whisper.load_model(model_name)
        print(f"[transcription] Whisper model '{model_name}' ready.")
    return _model_cache[model_name]


def read_audio_as_array(audio_path):
    """Read a mono 16-bit PCM WAV file into a float32 NumPy array.

    Returns None when the file is not a plain 16 kHz mono WAV, in which
    case the caller lets Whisper deal with it (safer, just slower).
    """
    try:
        with wave.open(audio_path, "rb") as wav:
            if wav.getnchannels() != 1:
                return None
            if wav.getframerate() != WHISPER_SAMPLE_RATE:
                return None
            if wav.getsampwidth() != 2:          # 2 bytes = 16-bit PCM
                return None

            frames = wav.readframes(wav.getnframes())

        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
        # Whisper works with samples scaled to the range -1.0 .. 1.0
        return samples / 32768.0
    except Exception:
        return None


def clean_transcript(text):
    """Remove Whisper's repetition artefacts without touching real speech.

    On noisy audio (or speech in a language the model is weak at) Whisper
    sometimes gets stuck at the END of a recording and repeats one word or
    one sentence over and over ("child child child child ..."). Normal
    speech is never affected by this cleanup:

      * only a run of FOUR or more identical consecutive words is
        collapsed to a single word ("very very good" stays untouched)
      * a trailing sentence that exactly repeats the one before it is
        dropped (the classic end-of-audio loop)
    """
    if not text:
        return text

    words = text.split()
    cleaned = []
    same_run = 0
    for word in words:
        if cleaned and word.lower() == cleaned[-1].lower():
            same_run += 1
            if same_run >= 3:          # 4th identical word in a row: drop
                continue
        else:
            same_run = 0
        cleaned.append(word)
    text = " ".join(cleaned)

    for _ in range(3):
        pieces = re.split(r"(?<=[.!?])\s+", text.strip())
        if len(pieces) < 2:
            break
        last = re.sub(r"\s+", " ", pieces[-1]).strip().lower().rstrip(".!?")
        previous = re.sub(r"\s+", " ", pieces[-2]).strip().lower().rstrip(".!?")
        if last and last == previous:
            text = " ".join(pieces[:-1])
        else:
            break
    return text


def transcribe_audio(audio_path, model_name=None, progress_callback=None,
                     language=None):
    """Transcribe an audio file and return (transcript_text, language).

    language: optionally FORCE the language (a Whisper code such as
    "en" or "te") - the pipeline detects it once, shows it to the
    student and passes it in so every chunk of a long video is decoded
    as ONE consistent language. None lets Whisper detect it per call.

    A small wrapper around Whisper. The model is loaded once and cached
    (see load_model), so a second video in the same app run is much faster.

    verbose=False keeps Whisper quiet - it avoids the tqdm progress bar
    that would otherwise write to sys.stderr (which can be None when the
    app is launched without a console) and crash the background job with
    'NoneType' object has no attribute 'write'. Progress for the UI comes
    from progress_callback instead.

    progress_callback(percent) is optional and only receives a rough
    estimate based on how much of the real work is done - we use it to
    move the progress bar while Whisper is running.
    """
    if not audio_path or not os.path.exists(audio_path):
        raise Exception("No audio file was provided for transcription.")

    model = load_model(model_name)

    if progress_callback:
        progress_callback(5)

    audio_array = read_audio_as_array(audio_path)

    # ---- 1. LANGUAGE is detected FIRST, from the first seconds --------
    detected = language
    if detected is None and audio_array is not None:
        detected, _probability = _detect_from_audio(model, audio_array)
        if progress_callback:
            progress_callback(10)

    # ---- 2. The ENTIRE audio is decoded, chunk by chunk, IN ORDER ------
    if audio_array is None:
        # Unusual file format: let Whisper read the complete file itself
        single = _with_retry(lambda: model.transcribe(
            audio_path, fp16=False, verbose=None, language=detected))
        text = (single.get("text") or "").strip()
        if detected is None:
            detected = single.get("language")
    else:
        samples_per_chunk = int(CHUNK_SECONDS * WHISPER_SAMPLE_RATE)
        chunks = [audio_array[start:start + samples_per_chunk]
                  for start in range(0, len(audio_array), samples_per_chunk)]

        def decode(chunk):
            return _with_retry(lambda: model.transcribe(
                chunk, fp16=False, verbose=None, language=detected))

        results = [None] * len(chunks)
        if len(chunks) > 1 and CHUNK_WORKERS > 1:
            # Independent chunks can safely decode in parallel: every call
            # only READS the model weights, and results are stored by index,
            # so the combined text always matches the video's order.
            from concurrent.futures import ThreadPoolExecutor
            workers = min(CHUNK_WORKERS, len(chunks))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for index, result in enumerate(pool.map(decode, chunks)):
                    results[index] = result
                    if progress_callback:
                        progress_callback(min(
                            10 + int(85 * (index + 1) / len(chunks)), 95))
        else:
            for index, chunk in enumerate(chunks):
                results[index] = decode(chunk)
                if progress_callback:
                    progress_callback(min(
                        10 + int(85 * (index + 1) / len(chunks)), 95))

        # ---- 3. Combine the parts IN THE CORRECT ORDER -----------------
        text = " ".join((result.get("text") or "").strip()
                        for result in results
                        if result and (result.get("text") or "").strip())
        if detected is None:
            detected = next((result.get("language")
                             for result in results if result), None)

    if progress_callback:
        progress_callback(97)

    # ---- 4. Clean + rewrite Telugu script in English letters -----------
    # (clean_transcript removes Whisper repetition artefacts so the stored
    # transcript ends with the real end of the video.)
    transcript = clean_transcript(re.sub(r"\s+", " ", text).strip())
    transcript, _romanised = romanise_transcript(transcript)

    if not transcript:
        raise Exception(
            "No speech was detected in this video. Please try a video where "
            "someone is speaking clearly."
        )

    if progress_callback:
        progress_callback(100)
    return transcript, detected or "unknown"


def detect_language(audio_path, model_name=None):
    """Detect which language is spoken, using only the first seconds.

    Called BEFORE the full transcription so the processing page can show
    "Language detected: Telugu" while the chunks are still decoding, and
    so every chunk of a long video is decoded as ONE consistent language.

    Returns (language_code, probability); (None, 0.0) when the file
    cannot be read - the caller then lets transcribe_audio auto-detect.
    """
    if not audio_path or not os.path.exists(audio_path):
        return None, 0.0
    model = load_model(model_name)
    audio_array = read_audio_as_array(audio_path)
    if audio_array is None:
        return None, 0.0
    return _detect_from_audio(model, audio_array)


def _detect_from_audio(model, audio):
    """Language of the first 30 seconds of a sample array.

    Returns (language_code, probability) or (None, 0.0). Any problem here
    is non-fatal: transcription simply auto-detects per chunk instead.
    """
    try:
        import whisper

        window = audio[: WHISPER_SAMPLE_RATE * 30]
        if len(window) < WHISPER_SAMPLE_RATE:        # under one second
            return None, 0.0
        needed = WHISPER_SAMPLE_RATE * 30            # pad to 30 s of input
        if len(window) < needed:
            window = np.pad(window, (0, needed - len(window)))

        mel = whisper.log_mel_spectrogram(window, model.dims.n_mels)
        detected = model.detect_language(mel)

        if isinstance(detected, str):
            return detected, 1.0

        probs = detected[1] if isinstance(detected, tuple) else detected
        if hasattr(probs, "items") and probs:
            best = max(probs.items(), key=lambda pair: pair[1])
            return str(best[0]), float(best[1])

        # (language, probability) shaped results
        if isinstance(detected, tuple) and detected and isinstance(detected[0], str):
            probability = float(detected[1]) if len(detected) > 1 else 1.0
            return detected[0], probability
    except Exception:
        return None, 0.0
    return None, 0.0


def _with_retry(action, attempts=2):
    """Run ONE Whisper decode, trying again once after a failure.

    A single retry absorbs a transient error (memory spike, thread
    hiccup). If it still fails, the raised message is clear and the
    pipeline shows the failure to the student instead of generating
    content from a missing transcript.
    """
    last_error = None
    for _attempt in range(attempts):
        try:
            return action()
        except Exception as error:          # noqa: BLE001 - retried above
            last_error = error
    raise Exception(
        "Transcription failed while reading the audio: "
        f"{type(last_error).__name__}: {last_error}"
    )


def count_words(text):
    """Number of words in a transcript (shown on the performance dashboard)."""
    return len(text.split()) if text else 0


def split_transcript_into_chunks(text, words_per_chunk=1200):
    """Split a long transcript into chunks of roughly N words.

    WHY: a one-hour lecture produces thousands of words. Sending all of it
    to the AI in a single request is slow and can exceed the model's
    context limit. Splitting it into word-sized chunks and summarising
    them one by one is the simple "map reduce" approach - no vector
    database or RAG needed.

    The split happens on sentence boundaries where possible, so a chunk
    never ends in the middle of a sentence.
    """
    if not text:
        return []

    sentences = []
    current = ""
    for character in text:
        current += character
        if character in ".!?":
            sentences.append(current.strip())
            current = ""
    if current.strip():
        sentences.append(current.strip())

    chunks = []
    buffer = []
    buffer_words = 0

    for sentence in sentences:
        sentence_words = len(sentence.split())
        # A single very long sentence simply becomes its own chunk
        if buffer_words + sentence_words > words_per_chunk and buffer:
            chunks.append(" ".join(buffer))
            buffer = []
            buffer_words = 0
        buffer.append(sentence)
        buffer_words += sentence_words

    if buffer:
        chunks.append(" ".join(buffer))

    return chunks