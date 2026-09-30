// ==========================================================
// EduSummarize AI - script.js
// Plain JavaScript, no frameworks. Two jobs:
//
//   1. UPLOAD PAGE    - drag & drop, tab awareness, quick checks
//   2. PROCESSING PAGE - polls /api/status/<id> every 1.5 s and
//      reveals each result (transcript, summary, quiz) the moment
//      it becomes available, plus an elapsed-time timer.
// ==========================================================

// ------------------------- shared helpers -------------------------

function formatFileSize(bytes) {
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function formatClock(totalSeconds) {
    const m = Math.floor(totalSeconds / 60);
    const s = totalSeconds % 60;
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

// ------------------------- upload page -------------------------

function initUploadPage() {
    const dropZone = document.getElementById('dropZone');
    const videoInput = document.getElementById('videoInput');
    const fileInfo = document.getElementById('fileInfo');
    const fileName = document.getElementById('fileName');
    const fileSize = document.getElementById('fileSize');
    const videoPreview = document.getElementById('videoPreview');
    const processBtn = document.getElementById('processBtn');
    const uploadForm = document.getElementById('uploadForm');
    const urlInput = document.getElementById('videoUrlInput');
    const inputType = document.getElementById('inputType');

    // Remember which tab the student is on, so Flask receives the
    // matching input_type ("upload" or "url").
    const uploadTabBtn = document.getElementById('tabBtn-upload');
    const urlTabBtn = document.getElementById('tabBtn-url');
    if (uploadTabBtn && inputType) {
        uploadTabBtn.addEventListener('shown.bs.tab', () => { inputType.value = 'upload'; });
        urlTabBtn.addEventListener('shown.bs.tab', () => { inputType.value = 'url'; });
    }

    if (!dropZone) return; // not on the upload page

    // Clicking anywhere in the drop zone opens the file picker
    dropZone.addEventListener('click', () => videoInput.click());

    ['dragenter', 'dragover'].forEach(evt => {
        dropZone.addEventListener(evt, (e) => {
            e.preventDefault();
            dropZone.classList.add('dragover');
        });
    });

    ['dragleave', 'drop'].forEach(evt => {
        dropZone.addEventListener(evt, (e) => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
        });
    });

    dropZone.addEventListener('drop', (e) => {
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            videoInput.files = files;
            handleFileSelect(files[0]);
        }
    });

    videoInput.addEventListener('change', () => {
        if (videoInput.files.length > 0) {
            handleFileSelect(videoInput.files[0]);
        }
    });

    function handleFileSelect(file) {
        fileName.textContent = file.name;
        fileSize.textContent = formatFileSize(file.size);
        fileInfo.classList.remove('d-none');

        // Show a preview when the browser can play this format
        if (videoPreview && file.type.startsWith('video')) {
            videoPreview.src = URL.createObjectURL(file);
            videoPreview.classList.remove('d-none');
        } else if (videoPreview) {
            videoPreview.classList.add('d-none');
        }
    }

    // One last client-side check before submitting
    if (uploadForm) {
        uploadForm.addEventListener('submit', (e) => {
            const usingUrl = inputType && inputType.value === 'url';

            if (usingUrl) {
                if (!urlInput.value.trim()) {
                    e.preventDefault();
                    alert('Please paste a video link first, or switch to the Upload File tab.');
                    return;
                }
                processBtn.disabled = true;
                processBtn.innerHTML =
                    '<span class="spinner-border spinner-border-sm"></span> Checking link...';
            } else {
                if (!videoInput.files.length) {
                    e.preventDefault();
                    alert('Please choose a video file first, or switch to the Video URL tab.');
                    return;
                }
                processBtn.disabled = true;
                processBtn.innerHTML =
                    '<span class="spinner-border spinner-border-sm"></span> Uploading...';
            }
        });
    }
}

// ------------------------- processing page -------------------------

// The five steps shown on the processing page, in order
const STEP_IDS = ['step-received', 'step-audio', 'step-transcript',
                  'step-summary', 'step-quiz'];

// Which steps count as "done" for each status returned by the API
const STEPS_DONE_BY_STATUS = {
    'UPLOADED':           ['step-received'],
    'PREPARING_AUDIO':    ['step-received'],
    'TRANSCRIBING':       ['step-received', 'step-audio'],
    'TRANSCRIPT_READY':   ['step-received', 'step-audio', 'step-transcript'],
    'GENERATING_SUMMARY': ['step-received', 'step-audio', 'step-transcript'],
    'SUMMARY_READY':      ['step-received', 'step-audio', 'step-transcript', 'step-summary'],
    'GENERATING_QUIZ':    ['step-received', 'step-audio', 'step-transcript', 'step-summary'],
    'COMPLETED':          STEP_IDS,
    'CANCELLED':          [],
    'FAILED':             [],
};

function initProcessingPage() {
    const progressBar = document.getElementById('progressBar');
    const progressPercent = document.getElementById('progressPercent');
    const statusMessage = document.getElementById('statusMessage');
    const elapsedTimer = document.getElementById('elapsedTimer');
    const errorBox = document.getElementById('errorBox');
    const errorMessage = document.getElementById('errorMessage');
    const readyBox = document.getElementById('readyBox');
    const readyText = document.getElementById('readyText');
    const readyButtons = document.getElementById('readyButtons');
    const videoDuration = document.getElementById('videoDuration');
    const videoTitle = document.getElementById('videoTitle');

    if (!progressBar) return; // not on the processing page

    // The video id is the last part of the URL: /processing/<id>
    const videoId = window.location.pathname.split('/').pop();

    // ---- elapsed-time clock. It only ever shows how long the work has
    //      ACTUALLY been running - no invented time promises. ----
    let elapsedSeconds = 0;
    const timerId = setInterval(() => {
        elapsedSeconds += 1;
        if (elapsedTimer) elapsedTimer.textContent = formatClock(elapsedSeconds);
    }, 1000);

    // Makes sure each "ready" banner is shown only once
    const announced = { transcript: false, summary: false, quiz: false };

    function setStepStates(doneIds, activeId) {
        STEP_IDS.forEach(id => {
            const el = document.getElementById(id);
            if (!el) return;
            const icon = el.querySelector('i');
            if (doneIds.includes(id)) {
                el.classList.remove('active-step', 'text-muted');
                icon.className = 'bi bi-check-circle-fill text-success';
            } else if (id === activeId) {
                el.classList.add('active-step');
                icon.className = 'bi bi-arrow-repeat text-primary';
            } else {
                el.classList.add('text-muted');
                icon.className = 'bi bi-circle';
            }
        });
    }

    function showReady(html, buttonsHtml) {
        readyBox.classList.remove('d-none');
        readyText.innerHTML = html;
        readyButtons.innerHTML = buttonsHtml || '';
    }

    function showVideoDuration(seconds) {
        if (!videoDuration || !seconds) return;
        const total = Math.round(seconds);
        const m = Math.floor(total / 60);
        const s = total % 60;
        videoDuration.textContent = m + ':' + String(s).padStart(2, '0');
    }

    // ---- the polling loop (every 1.5 s - deliberately not faster) ----
    const pollId = setInterval(async () => {
        let data;
        try {
            const response = await fetch('/api/status/' + videoId);
            if (!response.ok) throw new Error('status request failed');
            data = await response.json();
        } catch (err) {
            return; // temporary network problem - try again on the next tick
        }

        if (data.title && videoTitle) videoTitle.textContent = data.title;
        showVideoDuration(data.duration);

        // progress bar + status message
        progressBar.style.width = data.progress + '%';
        if (progressPercent) progressPercent.textContent = data.progress + '%';
        if (statusMessage) statusMessage.textContent = data.message || 'Processing...';

        // step list
        const done = STEPS_DONE_BY_STATUS[data.status] || [];
        let active = null;
        if (data.status === 'PREPARING_AUDIO') active = 'step-audio';
        else if (data.status === 'TRANSCRIBING') active = 'step-transcript';
        else if (data.status === 'GENERATING_SUMMARY') active = 'step-summary';
        else if (data.status === 'GENERATING_QUIZ') active = 'step-quiz';
        setStepStates(done, active);

        // ---- progressive results: each one appears the moment it exists ----
        if (data.transcript_ready && !announced.transcript) {
            announced.transcript = true;
            showReady(
                '<i class="bi bi-check-circle-fill text-success"></i> <strong>Transcript Ready!</strong>',
                '<a href="/transcript/' + videoId + '" class="btn btn-success btn-sm">' +
                '<i class="bi bi-text-left"></i> View Transcript</a>'
            );
        }

        if (data.summary_ready && !announced.summary) {
            announced.summary = true;
            showReady(
                '<i class="bi bi-check-circle-fill text-success"></i> ' +
                '<strong>AI Summary Ready!</strong> You can start reading while the quiz is prepared.',
                '<a href="/summary/' + videoId + '" class="btn btn-success btn-sm">' +
                '<i class="bi bi-journal-text"></i> View Study Notes</a>'
            );
        }

        if (data.quiz_ready && !announced.quiz) {
            announced.quiz = true;
            showReady(
                '<i class="bi bi-check-circle-fill text-success"></i> ' +
                '<strong>Study material complete!</strong> Your quiz is ready.',
                '<a href="/quiz/' + videoId + '" class="btn btn-primary btn-sm">' +
                '<i class="bi bi-lightning-charge"></i> Start Quiz</a> ' +
                '<a href="/summary/' + videoId + '" class="btn btn-outline-success btn-sm">' +
                '<i class="bi bi-journal-text"></i> Study Notes</a>'
            );
        }

        // ---- end states: stop polling ----
        if (data.completed) {
            clearInterval(pollId);
            clearInterval(timerId);
            progressBar.style.width = '100%';
            progressBar.classList.remove('progress-bar-animated');
            if (progressPercent) progressPercent.textContent = '100%';
            setStepStates(STEP_IDS, null);
            return;
        }

        if (data.status === 'FAILED' || data.error) {
            clearInterval(pollId);
            clearInterval(timerId);
            progressBar.classList.remove('progress-bar-animated');
            progressBar.classList.add('bg-danger');
            if (errorBox && errorMessage) {
                errorBox.classList.remove('d-none');
                errorMessage.textContent = data.error ||
                    'Something went wrong while processing this video.';
            }
            const progressSection = document.getElementById('progressSection');
            if (progressSection) progressSection.classList.add('d-none');
            return;
        }

        if (data.cancelled || data.status === 'CANCELLED') {
            clearInterval(pollId);
            clearInterval(timerId);
            if (statusMessage) statusMessage.textContent = 'Processing cancelled.';
            progressBar.classList.remove('progress-bar-animated');
        }
    }, 1500);
}

// ------------------------- boot -------------------------

document.addEventListener('DOMContentLoaded', () => {
    initUploadPage();
    initProcessingPage();
});
