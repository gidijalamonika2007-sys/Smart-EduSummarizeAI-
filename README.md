# EduSummarize AI
### AI-Powered Educational Video Summarizer and Quiz Generator

## 1. Introduction

EduSummarize AI is a web application that turns an educational video into
study material automatically. A student uploads a lecture video, and the
app produces a transcript, a short summary, key learning points, important
terms, and an interactive multiple-choice quiz — complete with instant
scoring and feedback.

## 2. Problem Statement

Long educational videos take a lot of time to watch and revise. Students
need a fast way to turn a video into readable notes and to check their
own understanding afterwards.

## 3. Objectives

- Convert speech in a video into a text transcript.
- Summarize the transcript into short study notes using AI.
- Automatically generate a multiple-choice quiz from the content.
- Score the quiz and give instant, explained feedback.
- Track quiz history and simple performance analytics.

## 4. Features

- Drag-and-drop video upload (MP4, MOV, AVI, MKV, WEBM)
- Automatic audio extraction (ffmpeg, with imageio-ffmpeg fallback)
- Speech-to-text transcription (OpenAI Whisper, runs locally)
- AI-generated summary, key points, important terms and short-answer questions
- AI-generated multiple-choice quiz, configurable by question count and difficulty
- **AI-generated mind map** (collapsible tree, with a Regenerate button)
- Instant scoring with a per-question explanation and correct/incorrect review
- Doughnut chart + progress bar visualizing quiz results (Chart.js)
- Learning history table of every past attempt
- Simple analytics: videos processed, quizzes completed, average/best score
- **User accounts**: registration, login, logout, session-based auth (hashed passwords)
- **User dashboard**: personal stats, saved videos and saved quiz results
- **Admin area**: separate admin login, admin dashboard with system stats,
  user management (enable/disable/delete accounts)
- **Demo Mode**: works fully offline with a sample "Introduction to AI" lecture, no API key required

### Default administrator account

Created automatically on first run (password is stored hashed):

| Field    | Value     |
|----------|-----------|
| URL      | `/admin/login` |
| Username | `admin`   |
| Password | `admin123`|

## 5. Technology Stack

| Layer          | Technology                  |
|----------------|------------------------------|
| Frontend       | HTML5, CSS3, Bootstrap 5, JavaScript |
| Backend        | Python, Flask                |
| Database       | SQLite                       |
| AI             | OpenAI API (gpt-4o-mini)     |
| Speech-to-Text | OpenAI Whisper (local model) |
| Video/Audio    | MoviePy                      |
| Charts         | Chart.js                     |
| Templates      | Jinja2                       |

## 6. System Architecture

```
Student
   │
   ▼
HTML / Bootstrap UI
   │
   ▼
Flask (Python backend)
   │
   ▼
Video Processing  →  Whisper (Speech → Text)  →  AI Model (Summary + Quiz)
   │
   ▼
SQLite database
   │
   ▼
Results Dashboard
```

## 7. Project Workflow

Upload Video → Extract Audio → Whisper Transcription → Generate Transcript
→ AI Summary → Key Points → Quiz Generation → Student Takes Quiz
→ Calculate Score → Feedback → Results Dashboard

## 8. Project Structure

```
educational-video-ai/
│
├── app.py                     # Flask routes (the "traffic controller")
├── requirements.txt
├── .env.example
├── .env                       # your real config (not committed)
├── database.db                # created automatically on first run
│
├── templates/                 # Jinja2 HTML templates
│   ├── base.html
│   ├── index.html
│   ├── upload.html
│   ├── summary.html
│   ├── quiz.html
│   ├── results.html
│   ├── history.html
│   ├── about.html
│   └── error.html
│
├── static/
│   ├── css/style.css
│   └── js/script.js
│
├── uploads/                    # uploaded videos are saved here temporarily
├── audio/                      # extracted audio is saved here temporarily
│
├── services/
│   ├── video_processor.py      # extract_audio()
│   ├── transcription.py        # transcribe_audio() using Whisper
│   ├── ai_service.py           # all AI prompts + Demo Mode data
│   └── quiz_generator.py       # calculate_score(), performance message
│
└── database/
    └── db.py                   # all SQLite queries
```

## 9. Installation

### Windows
```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### macOS / Linux
```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> **Note on Whisper & MoviePy:** these need `ffmpeg` installed on your system.
> - Windows: download from ffmpeg.org and add it to PATH (or `choco install ffmpeg`)
> - macOS: `brew install ffmpeg`
> - Linux: `sudo apt install ffmpeg`

### Verified setup on this machine (Windows)

A ready-to-use virtual environment already exists in `venv/` (Python 3.12).
To run the app, you do **not** need to install anything again — just use:

```
.\venv\Scripts\python.exe app.py
```

If you ever need to rebuild the environment from scratch, these are the
exact steps that were verified to work here:

```
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install "setuptools<81" wheel
.\venv\Scripts\python.exe -m pip install --no-build-isolation -r requirements.txt
```

Why the two extra steps are needed:
- **Python 3.12, not 3.13** — `openai-whisper` (and `moviepy`) are older
  packages; their build step imports `pkg_resources`, which behaves more
  predictably on 3.12.
- **`setuptools<81` + `--no-build-isolation`** — `openai-whisper==20231117`
  imports `pkg_resources` while building. setuptools 81+ removed it, so pip's
  isolated build environment fails with
  `ModuleNotFoundError: No module named 'pkg_resources'`. Pinning an older
  setuptools and reusing it (`--no-build-isolation`) avoids this.

`ffmpeg` is installed and on PATH (installed via
`winget install --id Gyan.FFmpeg -e`). Open a **new** terminal after
installing it so the updated PATH is picked up.

> The Whisper `base` model (~139 MB) is downloaded automatically the first
> time a real video is transcribed and is then cached locally, so only the
> very first upload is slow.

## 10. API Configuration

1. Copy `.env.example` to `.env`.
2. Add your key: `OPENAI_API_KEY=sk-...`
3. If you skip this step, the app automatically runs in **Demo Mode**
   (a sample AI lecture is used instead of a real API call), so it can
   still be fully demonstrated without internet access or a paid key.

## 11. How to Run

The virtual environment lives inside the project folder (`educational-video-ai/educational-video-ai/venv`).
Activate it first, then start the app. No extra installs are needed.

> **Cloned this repo from GitHub?** The `venv/` folder is deliberately not
> committed (it is large and machine-specific). Create it once with the four
> commands in **section 9 (Installation)**, then follow the steps below.

Windows (PowerShell, from the project folder that contains `app.py`):
```
.\venv\Scripts\Activate.ps1
python app.py
```

macOS / Linux:
```
source venv/bin/activate
python app.py
```

Then open your browser at: **http://127.0.0.1:5000**

Quick tour:
1. **Home** - http://127.0.0.1:5000/
2. **Register** a student account (or **Login**) from the navbar
3. Open your **Dashboard** to see personal stats and saved results
4. **Upload Video** (or try Demo Mode) - transcript, summary, quiz and
   mind map are generated automatically
5. **Admin**: http://127.0.0.1:5000/admin/login  (`admin` / `admin123`)

Run the test suites:
```
python run_tests.py      # URL validation + quiz analytics
python smoke_test.py     # every feature end-to-end (uses a throwaway database)
```

## 12. How to Upload a Video

1. Go to the "Upload Video" page.
2. Drag a video file into the box, or click "Choose Video".
3. Click "Process Video" and wait for processing to finish.
4. (No video handy? Click "Try Demo Mode" instead.)

### First upload takes longer (one-time Whisper model download)

The first real video is always the slowest one: Whisper downloads its
speech model (~140 MB, `base`) into `%USERPROFILE%\.cache\whisper` once
and caches it on disk. Every upload after that reuses the cached model,
so transcription starts straight away and processing is much faster.

### Running without a console window (pythonw.exe)

`app.py` protects itself against a Windows quirk. When a Python program is
started with `pythonw.exe` (no console window) both `sys.stdout` and
`sys.stderr` are `None`, and any library that writes a progress bar
straight to `stderr` - Whisper's `tqdm` bars for the model download and
for transcription - fails the background job with:

```
'NoneType' object has no attribute 'write'
```

At startup `app.py` therefore points a missing `stdout`/`stderr` at the
null device (`os.devnull`), so uploads are processed correctly whether the
server is started with `python.exe` (normal, logs on screen) or with
`pythonw.exe` (windowless). Whisper is also called with `verbose=False`.

## 13. How Summarization Works

The extracted transcript is sent to the AI model with a strict instruction
to use **only** information present in the transcript. The model returns a
short summary (100–200 words), 5–8 key points, and 3–8 important terms with
their meanings, all as structured JSON that the app parses and stores.

## 14. How Quiz Generation Works

A second prompt asks the AI model to generate multiple-choice questions
(5 by default, configurable to 10, at Easy/Medium/Hard difficulty) strictly
from the transcript, each with 4 options, the correct answer, and an
explanation. The JSON response is parsed and saved to the `quizzes` table.

## 15. Database Description

| Table    | Purpose                                              |
|----------|-------------------------------------------------------|
| videos   | uploaded video, transcript, summary, key points, terms |
| quizzes  | generated MCQs linked to a video                       |
| results  | each completed quiz attempt, score and answers          |

## 16. Testing

Manually verified: video upload, audio extraction, transcription, summary
generation, quiz generation and JSON parsing, quiz submission and scoring,
database saving, history page, and error handling (missing file, wrong
format, missing API key).

## 17. Limitations

- Processing is synchronous — a very long video can take a while with no
  live progress bar (a simple spinner is shown instead).
- Whisper's "base" model is a speed/accuracy tradeoff; accents or noisy
  audio may reduce transcript quality.
- No user accounts — history is shared across whoever runs the app locally.

## 18. Future Enhancements

- Background processing with progress updates
- User accounts and per-user history
- Support for longer videos via chunked transcription
- Exportable PDF study notes
