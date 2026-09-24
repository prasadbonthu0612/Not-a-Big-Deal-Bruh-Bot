#instagram_downloader.py
import hashlib
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yt_dlp
from dotenv import load_dotenv


load_dotenv()

HASHTAG_PATTERN = re.compile(
    r"(?<!\w)#[\w\u0080-\uffff]+",
    re.UNICODE,
)


DOWNLOAD_ROOT = Path(
    os.getenv("DOWNLOAD_ROOT", "downloads")
)

INSTAGRAM_COOKIES_FILE = os.getenv(
    "INSTAGRAM_COOKIES_FILE",
    "instagram_cookies.txt",
)

INSTAGRAM_USER_AGENT = os.getenv(
    "INSTAGRAM_USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
)

# Download strategy configuration.
DOWNLOAD_RETRIES = int(os.getenv("INSTAGRAM_DOWNLOAD_RETRIES", "2"))
DOWNLOAD_BACKOFF_SECONDS = float(
    os.getenv("INSTAGRAM_DOWNLOAD_BACKOFF_SECONDS", "3")
)
ENABLE_YTDLP_IMPERSONATION = (
    os.getenv("INSTAGRAM_YTDLP_IMPERSONATION", "true").lower()
    in {"1", "true", "yes", "on"}
)
ENABLE_INSTALOADER_FALLBACK = (
    os.getenv("INSTAGRAM_INSTALOADER_FALLBACK", "true").lower()
    in {"1", "true", "yes", "on"}
)

# Keep the failure message useful but avoid dumping enormous extractor traces
# into Telegram persistent state.
MAX_ERROR_LENGTH = 1200


class ReelNotProcessableError(RuntimeError):
    """
    Raised when all configured Instagram download strategies are exhausted.

    This is a per-job failure. The bot treats it separately so one Reel that
    Instagram will not expose does not stop the intake worker from moving to
    the next WAITING job.
    """


def _clean_error(value):
    """Convert an exception into a compact, persistent error message."""
    text = " ".join(str(value).split())

    if len(text) > MAX_ERROR_LENGTH:
        text = text[:MAX_ERROR_LENGTH - 3] + "..."

    return text or value.__class__.__name__


def _normalize_instagram_url(source_url):
    """
    Return the canonical Instagram URL without query/fragment data.

    The original URL is still preserved as the first download candidate.
    Query parameters such as stkn/igsh are therefore not blindly discarded
    before the downloader gets a chance to try them.
    """
    parsed = urlsplit(source_url)

    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            "",
        )
    )


def _build_url_candidates(source_url):
    """
    Build a small, deterministic set of URLs to try.

    Candidate 1 keeps the exact URL supplied by the user.
    Candidate 2 removes query/fragment parameters.
    """
    candidates = []

    for candidate in (
        source_url,
        _normalize_instagram_url(source_url),
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    return candidates


def _extract_shortcode(source_url):
    """Extract an Instagram post/reel shortcode from a URL."""
    match = re.search(
        r"/(?:reel|reels|p|tv)/([^/?#]+)",
        source_url,
        re.IGNORECASE,
    )

    if not match:
        return ""

    return match.group(1).strip()


def _find_downloaded_video(job_dir):
    """
    Find the final video produced by a downloader.

    Temporary .part/.ytdl files are ignored. MP4 is preferred.
    Recursive search is used because Instaloader may create nested
    target directories depending on its version/configuration.
    """
    if not job_dir.exists():
        return None

    candidates = [
        path
        for path in job_dir.rglob("*")
        if path.is_file()
        and not path.name.endswith(".part")
        and not path.name.endswith(".ytdl")
        and path.suffix.lower()
        in {".mp4", ".mkv", ".webm", ".mov"}
    ]

    if not candidates:
        return None

    mp4_files = [
        path
        for path in candidates
        if path.suffix.lower() == ".mp4"
    ]

    candidates = mp4_files or candidates

    return max(
        candidates,
        key=lambda path: path.stat().st_size,
    )


def _clean_job_directory(job_dir):
    """Remove stale files/folders from a previous attempt."""
    if not job_dir.exists():
        return

    for path in sorted(
        job_dir.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if path.is_file() or path.is_symlink():
            try:
                path.unlink()
            except OSError:
                pass
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def _validate_downloaded_file(file_path):
    """Validate that a downloader actually produced usable media."""
    if not file_path:
        raise RuntimeError(
            "Downloader completed but no video file was found."
        )

    path = Path(file_path)

    if not path.is_file():
        raise RuntimeError(
            f"Downloader returned a non-file path: {path}"
        )

    if path.stat().st_size <= 0:
        raise RuntimeError(
            "Downloader produced an empty video file."
        )

    return path


def _cookie_path():
    """Return the configured cookie file if it exists."""
    path = Path(INSTAGRAM_COOKIES_FILE)

    if path.is_file():
        return path

    return None


def _base_ytdlp_options(job_dir):
    """Create the common yt-dlp configuration."""
    output_template = str(
        job_dir / f"{job_dir.name}.%(ext)s"
    )

    options = {
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,

        # Prefer a single MP4 stream so normal Reel downloads do not
        # require ffmpeg merely for format merging.
        "format": "best[ext=mp4]/best",

        "outtmpl": output_template,
        "restrictfilenames": True,

        # yt-dlp has its own network retries. We additionally perform
        # a small number of fresh extractor attempts below.
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,

        "http_headers": {
            "User-Agent": INSTAGRAM_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },

        "merge_output_format": "mp4",
    }

    cookies_path = _cookie_path()

    if cookies_path:
        options["cookiefile"] = str(cookies_path)

    return options


def _run_ytdlp_attempt(
    source_url,
    job_dir,
    *,
    attempt_number,
    impersonate=False,
):
    """
    Execute one isolated yt-dlp attempt.

    A fresh YoutubeDL object is created for every attempt so a failed
    extractor/session does not contaminate the next strategy.
    """
    _clean_job_directory(job_dir)

    options = _base_ytdlp_options(job_dir)

    if impersonate:
        # curl_cffi is optional. If unavailable, do not pretend this
        # strategy is active.
        try:
            import curl_cffi  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "yt-dlp browser impersonation requested, but curl_cffi "
                "is not installed."
            ) from exc

        # True tells yt-dlp to use any available curl_cffi
        # impersonation target. A hard-coded "chrome" target can fail with
        # an AssertionError when the installed curl_cffi/yt-dlp target map
        # does not expose that exact alias.
        options["impersonate"] = True

    label = (
        f"yt-dlp{' + Chrome impersonation' if impersonate else ''}"
    )

    print(
        f"\n🔄 Download attempt {attempt_number}: {label}"
    )
    print(f"🌐 URL: {source_url}")

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            source_url,
            download=True,
        )

    file_path = _validate_downloaded_file(
        _find_downloaded_video(job_dir)
    )

    return {
        "file_path": str(file_path),
        "info": info or {},
        "method": label,
    }


def _load_instaloader():
    """Import Instaloader only when the fallback is actually needed."""
    try:
        import instaloader
    except ImportError as exc:
        raise RuntimeError(
            "Instaloader fallback is enabled but the 'instaloader' "
            "package is not installed."
        ) from exc

    return instaloader


def _run_instaloader_fallback(source_url, job_dir):
    """
    Try downloading a single Instagram post/reel using Instaloader.

    Instaloader officially supports single-post downloads by shortcode
    and Reels/video downloads. It is deliberately used only after
    yt-dlp strategies fail.
    """
    shortcode = _extract_shortcode(source_url)

    if not shortcode:
        raise RuntimeError(
            "Could not extract an Instagram shortcode for Instaloader."
        )

    instaloader = _load_instaloader()

    _clean_job_directory(job_dir)

    print(
        "\n🛟 FALLBACK DOWNLOAD: Instaloader"
    )
    print(f"🔑 Shortcode: {shortcode}")

    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        quiet=False,
    )

    post = instaloader.Post.from_shortcode(
        loader.context,
        shortcode,
    )

    if not post.is_video:
        raise RuntimeError(
            "Instagram post was fetched, but it is not a video."
        )

    loader.download_post(
        post,
        target=str(job_dir),
    )

    file_path = _validate_downloaded_file(
        _find_downloaded_video(job_dir)
    )

    caption = (post.caption or "").strip()

    # Instaloader does not expose the same "title" field as yt-dlp.
    # Use the first caption line as a best-effort title while preserving
    # the full caption in "description".
    title = ""
    if caption:
        title = caption.splitlines()[0].strip()

    info = {
        "id": shortcode,
        "display_id": shortcode,
        "webpage_url": source_url,
        "title": title,
        "description": caption,
        "caption": caption,
        "tags": [
            f"#{tag}"
            for tag in (getattr(post, "caption_hashtags", []) or [])
        ],
        "extractor": "Instaloader",
    }

    return {
        "file_path": str(file_path),
        "info": info,
        "method": "Instaloader fallback",
    }


def _is_retryable_network_error(error_text):
    """
    Identify failures where another extractor strategy is sensible.

    Instagram's extractor can return an empty media response even for
    public Reels, so this category intentionally includes that known
    extractor failure.
    """
    text = error_text.lower()

    retryable_markers = (
        "instagram sent an empty media response",
        "unable to extract video url",
        "video info extraction failed",
        "http error 404",
        "http error 403",
        "http error 429",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
        "too many requests",
        "rate limit",
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "network",
    )

    return any(marker in text for marker in retryable_markers)


def _format_attempt_summary(attempts):
    """Create a compact human-readable summary for persistent state."""
    if not attempts:
        return "No download attempts were recorded."

    lines = ["Download attempts:"]

    for attempt in attempts:
        status = attempt.get("status", "UNKNOWN")
        method = attempt.get("method", "unknown")
        error = attempt.get("error", "")

        if status == "SUCCESS":
            lines.append(f"✅ {method}")
        else:
            lines.append(f"❌ {method}: {error}")

    return "\n".join(lines)


def download_instagram_video(source_url, job_id):
    """
    Download one Instagram post/reel using multiple fallback strategies.

    Strategy order:
        1. yt-dlp with the original URL
        2. yt-dlp with the normalized URL
        3. yt-dlp with Chrome impersonation, if curl_cffi is installed
        4. Instaloader fallback

    A failed strategy never escapes immediately. All strategies are tried
    before the final exception is raised.

    Returns:
        {
            "file_path": str,
            "info": dict,
            "download_method": str,
            "download_attempts": list[dict],
        }

    The final exception contains a compact attempt summary so the caller
    can mark only this job as failed while the processing worker continues.
    """
    job_dir = DOWNLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    cookie_path = _cookie_path()

    if cookie_path:
        print(
            f"🍪 Instagram cookies available: {cookie_path}"
        )
    else:
        print(
            "🍪 No Instagram cookies file configured/found. "
            "Trying public access."
        )

    print("\n==============================")
    print("⬇️ INSTAGRAM DOWNLOAD")
    print(f"Job: {job_id}")
    print(f"URL: {source_url}")
    print("==============================\n")

    attempts = []
    candidates = _build_url_candidates(source_url)

    # --------------------------------------------------------
    # STRATEGY 1/2: yt-dlp
    # --------------------------------------------------------
    for candidate_index, candidate_url in enumerate(candidates, start=1):
        for retry_index in range(DOWNLOAD_RETRIES + 1):
            attempt_number = len(attempts) + 1
            method = (
                f"yt-dlp normal "
                f"(URL variant {candidate_index}, retry {retry_index + 1})"
            )

            try:
                result = _run_ytdlp_attempt(
                    candidate_url,
                    job_dir,
                    attempt_number=attempt_number,
                    impersonate=False,
                )

                attempts.append(
                    {
                        "method": result["method"],
                        "status": "SUCCESS",
                        "url": candidate_url,
                    }
                )

                print(
                    f"\n✅ Download succeeded with "
                    f"{result['method']}"
                )

                return {
                    "file_path": result["file_path"],
                    "info": result["info"],
                    "download_method": result["method"],
                    "download_attempts": attempts,
                }

            except Exception as exc:
                error_text = _clean_error(exc)

                attempts.append(
                    {
                        "method": method,
                        "status": "FAILED",
                        "url": candidate_url,
                        "error": error_text,
                    }
                )

                print(
                    f"⚠️ {method} failed: {error_text}"
                )

                # For clearly permanent URL/access errors, don't waste
                # several identical retries. We still proceed to the
                # alternate extractor below.
                if not _is_retryable_network_error(error_text):
                    break

                if retry_index < DOWNLOAD_RETRIES:
                    delay = DOWNLOAD_BACKOFF_SECONDS * (2 ** retry_index)
                    print(
                        f"⏳ Waiting {delay:.1f}s before fresh retry..."
                    )
                    time.sleep(delay)

    # --------------------------------------------------------
    # STRATEGY 3: yt-dlp with browser impersonation
    # --------------------------------------------------------
    if ENABLE_YTDLP_IMPERSONATION:
        impersonation_available = True

        try:
            import curl_cffi  # noqa: F401
        except ImportError:
            impersonation_available = False
            print(
                "\nℹ️ curl_cffi is not installed. "
                "Skipping yt-dlp Chrome impersonation."
            )

        if impersonation_available:
            for candidate_index, candidate_url in enumerate(
                candidates,
                start=1,
            ):
                attempt_number = len(attempts) + 1
                method = (
                    "yt-dlp + Chrome impersonation "
                    f"(URL variant {candidate_index})"
                )

                try:
                    result = _run_ytdlp_attempt(
                        candidate_url,
                        job_dir,
                        attempt_number=attempt_number,
                        impersonate=True,
                    )

                    attempts.append(
                        {
                            "method": result["method"],
                            "status": "SUCCESS",
                            "url": candidate_url,
                        }
                    )

                    print(
                        f"\n✅ Download succeeded with "
                        f"{result['method']}"
                    )

                    return {
                        "file_path": result["file_path"],
                        "info": result["info"],
                        "download_method": result["method"],
                        "download_attempts": attempts,
                    }

                except Exception as exc:
                    error_text = _clean_error(exc)

                    attempts.append(
                        {
                            "method": method,
                            "status": "FAILED",
                            "url": candidate_url,
                            "error": error_text,
                        }
                    )

                    print(
                        f"⚠️ {method} failed: {error_text}"
                    )

    # --------------------------------------------------------
    # STRATEGY 4: Instaloader fallback
    # --------------------------------------------------------
    if ENABLE_INSTALOADER_FALLBACK:
        try:
            # Instaloader needs only the shortcode, so the normalized URL
            # is the cleanest input for this strategy.
            fallback_url = _normalize_instagram_url(source_url)

            result = _run_instaloader_fallback(
                fallback_url,
                job_dir,
            )

            attempts.append(
                {
                    "method": result["method"],
                    "status": "SUCCESS",
                    "url": fallback_url,
                }
            )

            print(
                "\n✅ Download succeeded with "
                "Instaloader fallback."
            )

            return {
                "file_path": result["file_path"],
                "info": result["info"],
                "download_method": result["method"],
                "download_attempts": attempts,
            }

        except Exception as exc:
            error_text = _clean_error(exc)

            attempts.append(
                {
                    "method": "Instaloader fallback",
                    "status": "FAILED",
                    "url": _normalize_instagram_url(source_url),
                    "error": error_text,
                }
            )

            print(
                f"❌ Instaloader fallback failed: {error_text}"
            )
    else:
        print(
            "\nℹ️ Instaloader fallback is disabled."
        )

    # --------------------------------------------------------
    # All strategies failed.
    # --------------------------------------------------------
    summary = _format_attempt_summary(attempts)

    print("\n==============================")
    print("❌ INSTAGRAM DOWNLOAD FAILED")
    print(f"Job: {job_id}")
    print(summary)
    print("==============================\n")

    # Clean temporary partial files before handing the failure to the
    # caller. The persistent Telegram job record remains untouched.
    _clean_job_directory(job_dir)

    raise ReelNotProcessableError(
        "Instagram download failed after all available strategies.\n"
        + summary
    )


# ============================================================
# PHASE 6 METADATA EXTRACTION
# ============================================================


def _clean_metadata_text(value):
    """Convert downloader metadata values into safe single strings."""
    if value is None:
        return ""

    if isinstance(value, (list, tuple)):
        value = " ".join(
            str(item)
            for item in value
            if item
        )

    return str(value).strip()


def extract_instagram_metadata(info):
    """
    Extract the original Instagram title/caption/hashtags from downloader
    metadata.

    This works for both yt-dlp metadata and the compatible metadata object
    returned by the Instaloader fallback. No additional network request is
    made here.
    """
    info = info or {}

    title = _clean_metadata_text(info.get("title"))

    caption = _clean_metadata_text(
        info.get("description")
        or info.get("caption")
    )

    # Instagram captions commonly contain hashtags directly in the
    # description. Keep their original order and remove duplicates.
    hashtag_pattern = HASHTAG_PATTERN
    hashtags = []
    seen = set()

    for match in hashtag_pattern.findall(caption):
        if match.lower() not in seen:
            seen.add(match.lower())
            hashtags.append(match)

    # Some extractors expose tags separately. Add those only when they
    # are not already present in the caption.
    tags = info.get("tags") or []

    if isinstance(tags, str):
        tags = [tags]

    for tag in tags:
        tag = _clean_metadata_text(tag)

        if not tag:
            continue

        if not tag.startswith("#"):
            tag = "#" + tag

        if tag.lower() not in seen:
            seen.add(tag.lower())
            hashtags.append(tag)

    return {
        "original_title": title,
        "original_caption": caption,
        "original_hashtags": " ".join(hashtags),
    }


# ============================================================
# PHASE 7 MEDIA HASHING
# ============================================================

MEDIA_HASH_ALGORITHM = "sha256"
MEDIA_HASH_CHUNK_SIZE = 1024 * 1024


def calculate_media_hash(file_path):
    """
    Calculate a SHA-256 hash of the complete downloaded media file.

    The file is read incrementally so large videos do not need to be
    loaded into RAM all at once.

    Returns:
        lowercase hexadecimal SHA-256 digest string.
    """
    path = Path(file_path)

    if not path.is_file():
        raise FileNotFoundError(file_path)

    hasher = hashlib.sha256()

    with path.open("rb") as file:
        while True:
            chunk = file.read(MEDIA_HASH_CHUNK_SIZE)

            if not chunk:
                break

            hasher.update(chunk)

    media_hash = hasher.hexdigest()

    if not media_hash:
        raise RuntimeError(
            "Failed to calculate media SHA-256 hash."
        )

    print(
        f"🔐 Media SHA-256: {media_hash}"
    )

    return media_hash


def cleanup_download(job_id):
    """
    Delete temporary local files for a completed/failed job.
    """
    job_dir = DOWNLOAD_ROOT / job_id

    if not job_dir.exists():
        return

    _clean_job_directory(job_dir)

    try:
        job_dir.rmdir()
    except OSError:
        pass
