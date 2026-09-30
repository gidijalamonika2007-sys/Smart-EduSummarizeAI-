"""
services/url_processor.py

Everything needed to work with a VIDEO LINK instead of an uploaded file:

    validate_video_url()      -> is this a safe, supported public video link?
    normalise_url()           -> clean version of the link (used for hashing)
    get_url_hash()            -> unique identifier so a link is never processed twice
    get_url_metadata()        -> title + duration, WITHOUT downloading anything
    download_audio_from_url() -> download ONLY the audio stream with yt-dlp

IMPORTANT (speed): we never download the full high-resolution video when
we only need the sound. yt-dlp is asked for the best AUDIO-ONLY stream,
which is a fraction of the size, so the download is much faster.

IMPORTANT (security): the app must not be tricked into requesting
internal addresses. validate_video_url() therefore blocks
localhost / 127.0.0.1 / private network IPs / file:// and ftp:// links,
and only allows hosts belonging to explicitly supported video providers.
This is the standard defence against SSRF (Server-Side Request Forgery).

IMPORTANT (legal/ethical): this module does not bypass logins, paywalls,
DRM, private-video settings or any other platform access control. If a
link cannot be used, the student is asked to upload the video instead.
"""

import hashlib
import ipaddress
import os
import socket
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from services import video_processor

# ---------------------------------------------------------------------
# Which websites are supported?
# ---------------------------------------------------------------------
# Only well known public video providers are allowed by default, because
# yt-dlp only knows how to read media from sites it has extractors for.
# A host also matches its sub-domains (www.youtube.com matches youtube.com).
SUPPORTED_DOMAINS = [
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "vimeo.com",
    "dailymotion.com",
    "dai.ly",
    "ted.com",
    "archive.org",
    "soundcloud.com",
]

# Query parameters that only track the visitor or change playback position.
# They are thrown away before hashing so the SAME video always produces the
# SAME hash, no matter which link the student copied.
TRACKING_PARAMETERS = {
    "si", "feature", "ref", "fbclid", "gclid", "t", "list",
    "index", "start_radio", "rv", "pp", "ab_channel",
}

# Friendly message used for every "we could not use this link" case
URL_PROCESSING_ERROR = (
    "This video link could not be processed. Please upload the video file instead."
)

# Set this environment variable to 1 if you really want to try other hosts
ALLOW_ANY_HOST = os.environ.get("ALLOW_ANY_VIDEO_HOST") == "1"

try:
    import yt_dlp
except ImportError:          # only if the install step was skipped
    yt_dlp = None


# ---------------------------------------------------------------------
# STEP 1: VALIDATION (security + "is this really a video page?")
# ---------------------------------------------------------------------

def _is_blocked_ip(ip_text):
    """True for addresses we must never contact (SSRF protection).

    Blocks loopback (127.0.0.1, ::1), private ranges (10.x, 192.168.x,
    172.16-31.x), link-local (169.254.x), reserved, multicast and
    carrier-grade NAT addresses.
    """
    ip = ipaddress.ip_address(ip_text)
    if (ip.is_private or ip.is_loopback or ip.is_link_local or
            ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        return True
    if getattr(ip, "is_shared", False):     # 100.64.0.0/10
        return True
    return False


def _host_is_public(hostname):
    """Resolve a hostname and make sure every address behind it is public."""
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False

    if not addresses:
        return False

    for info in addresses:
        ip_text = info[4][0]
        try:
            if _is_blocked_ip(ip_text):
                return False
        except ValueError:
            return False
    return True


def _host_is_supported(hostname):
    """True when the host is one of our supported video providers."""
    if ALLOW_ANY_HOST:
        return True
    for domain in SUPPORTED_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


def _looks_like_media_page(parsed):
    """Small sanity check so obvious non-video links are rejected early."""
    hostname = (parsed.hostname or "").lower()
    path = (parsed.path or "").strip("/")
    query = dict(parse_qsl(parsed.query))

    # youtu.be/<video-id>
    if hostname.endswith("youtu.be"):
        return len(path) >= 6

    # youtube.com/watch?v=<video-id>
    if "youtube.com" in hostname and path.startswith("watch"):
        return bool(query.get("v"))

    # Everything else needs at least some path (e.g. vimeo.com/123456789)
    return len(path) > 0


def validate_video_url(url):
    """Check a video link before any expensive work starts.

    Returns a tuple:  (is_valid, message)
        (True,  "")                    -> the link may be processed
        (False, "why it was rejected") -> show this message to the student
    """
    if not url or not url.strip():
        return False, "Please paste a video link first."

    url = url.strip()

    # --- protocol check: only ordinary web pages are allowed ---
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "Invalid or unsupported video link. Please check the URL or upload the video directly."

    if parsed.scheme.lower() not in ("http", "https"):
        return False, "Invalid or unsupported video link. Please check the URL or upload the video directly."

    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        return False, "Invalid or unsupported video link. Please check the URL or upload the video directly."

    # --- no "user:password@" inside the link ---
    if parsed.username or parsed.password:
        return False, "Invalid or unsupported video link. Please check the URL or upload the video directly."

    # --- supported provider? ---
    if not _host_is_supported(hostname):
        return (
            False,
            "This website is not supported. Please use a public video link from a "
            "supported provider, or upload the video file directly.",
        )

    # --- the link must not point at a private / internal address ---
    if not _host_is_public(hostname):
        return False, "Invalid or unsupported video link. Please check the URL or upload the video directly."

    # --- does the link look like a video page and not just a home page? ---
    if not _looks_like_media_page(parsed):
        return (
            False,
            "This link does not look like a video page. Please paste the full link "
            "of the video you want to study.",
        )

    return True, ""


# ---------------------------------------------------------------------
# STEP 2: NORMALISE + HASH (so the same video is never processed twice)
# ---------------------------------------------------------------------

def normalise_url(url):
    """Return a clean, comparable version of a video link.

    Removes tracking parameters, the "#" fragment and the "www." prefix,
    and lower-cases the scheme + host. Two different copies of the same
    link then produce the same hash, which is what makes caching work.
    """
    parsed = urlparse(url.strip())
    hostname = (parsed.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    # Keep only parameters that actually describe WHICH video is playing
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() not in TRACKING_PARAMETERS and not key.lower().startswith("utm_")
    ]
    kept.sort()

    netloc = hostname
    if parsed.port:
        netloc = f"{hostname}:{parsed.port}"

    path = parsed.path.rstrip("/")

    return urlunparse((
        parsed.scheme.lower(),
        netloc,
        path,
        "",                       # params (rarely used)
        urlencode(kept),
        "",                       # no fragment
    ))


def get_url_hash(url):
    """Return a SHA-256 hash of the normalised link - the URL's identifier."""
    return hashlib.sha256(normalise_url(url).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# STEP 3: METADATA (fast: nothing is downloaded)
# ---------------------------------------------------------------------

def _base_options():
    """Shared yt-dlp options used for every request."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,       # a playlist link gives one video, not fifty
        "socket_timeout": 30,
        "retries": 2,
    }
    ffmpeg = video_processor.find_ffmpeg()
    if ffmpeg:
        options["ffmpeg_location"] = ffmpeg
    return options


def get_url_metadata(url):
    """Read the video title and duration WITHOUT downloading the video.

    yt-dlp fetches only the small info page, which takes a second or two.
    Knowing the length up front means we can refuse an extremely long
    video before spending time downloading or transcribing it.

    Returns a dict: {"title": str, "duration": float|None, "uploader": str}
    """
    if yt_dlp is None:
        raise Exception("yt-dlp is not installed. Run: pip install yt-dlp")

    options = _base_options()
    options["skip_download"] = True

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        raise Exception(URL_PROCESSING_ERROR)

    if not info:
        raise Exception(URL_PROCESSING_ERROR)

    # A playlist search result may wrap the video in an "entries" list
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise Exception(URL_PROCESSING_ERROR)
        info = entries[0]

    return {
        "title": info.get("title") or "Video link",
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel") or "",
    }


# ---------------------------------------------------------------------
# STEP 4: DOWNLOAD ONLY THE AUDIO
# ---------------------------------------------------------------------

def download_audio_from_url(url, output_dir, progress_callback=None):
    """Download ONLY the audio stream of a video link.

    yt-dlp is asked for "bestaudio" (an audio-only stream such as m4a or
    webm/opus). That file is a small percentage of the size of the full
    video, so both the download and the rest of the pipeline are faster.

    progress_callback(percent) is called while downloading, if provided,
    so the processing page can show real download progress.

    Returns (audio_file_path, info_dict).
    Raises an Exception with a readable message when it fails.
    """
    if yt_dlp is None:
        raise Exception("yt-dlp is not installed. Run: pip install yt-dlp")

    os.makedirs(output_dir, exist_ok=True)

    def hook(progress):
        if progress_callback and progress.get("status") == "downloading":
            total = progress.get("total_bytes") or progress.get("total_bytes_estimate")
            if total:
                percent = int(progress.get("downloaded_bytes", 0) * 100 / total)
                progress_callback(min(percent, 99))

    options = _base_options()
    options.update({
        "format": "bestaudio/best",       # audio only - never the full HD video
        "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
        "restrictfilenames": True,
        "progress_hooks": [hook],
    })

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            audio_path = ydl.prepare_filename(info)
    except Exception:
        raise Exception(URL_PROCESSING_ERROR)

    # yt-dlp sometimes picks a slightly different extension than the filename
    # it reports, so look for the real file if the expected one is missing.
    if not os.path.exists(audio_path):
        video_id = info.get("id", "")
        candidates = [
            os.path.join(output_dir, name)
            for name in os.listdir(output_dir)
            if video_id and name.startswith(video_id)
        ]
        if not candidates:
            raise Exception(URL_PROCESSING_ERROR)
        audio_path = candidates[0]

    if os.path.getsize(audio_path) == 0:
        raise Exception(URL_PROCESSING_ERROR)

    return audio_path, info