#telegram_storage.py file
import asyncio
import os
import re
import inspect
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import MessageNotModifiedError


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID"))

# Render cannot answer Telethon's interactive phone/code prompts.
# For production, provide a pre-authorized user session through this env var.
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
SESSION_NAME = "storage_test_session"

if TELEGRAM_SESSION_STRING:
    client = TelegramClient(
        StringSession(TELEGRAM_SESSION_STRING),
        API_ID,
        API_HASH,
    )
else:
    # Local-development fallback: use the existing .session file.
    client = TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
    )


# Serialize all queue read-modify-write operations in this process.
# Telegram is the persistent store, but its message edits are not transactional.
_queue_lock = asyncio.Lock()
# Serialize persistent BOT_STATE read-modify-write operations.
_state_lock = asyncio.Lock()

# Limit heartbeat writes so the Telegram state message is not edited every few seconds.
HEARTBEAT_WRITE_INTERVAL_SECONDS = 30
_last_heartbeat_write_monotonic = 0.0


# ============================================================
# JOB STATUS
# ============================================================

STATUS_WAITING = "WAITING"
STATUS_PROCESSING = "PROCESSING"
STATUS_READY = "READY"
STATUS_PUBLISHING = "PUBLISHING"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
STATUS_REJECTED = "REJECTED"


# ============================================================
# MARKERS
# ============================================================

JOB_MARKER = "[JOB]"
QUEUE_MARKER = "[QUEUE_MANIFEST]"
CONFIG_MARKER = "[BOT_CONFIG]"
STATE_MARKER = "[BOT_STATE]"


# ============================================================
# DEFAULT CONFIGURATION
# ============================================================

DEFAULT_CONFIG = {
    "version": "1",

    "mode": "AUTO",

    # These values are initialized from .env only when the persistent
    # Telegram configuration is first created. Phase 17 controls then own
    # the persistent values so Telegram commands are not overwritten by .env
    # on every read.
    "publishing_interval_minutes": os.getenv("PUBLISHING_INTERVAL_MINUTES", "30"),

    # 0 means unlimited.
    "daily_post_limit": os.getenv("DAILY_POST_LIMIT", "0"),

    "posting_window_enabled": os.getenv("POSTING_WINDOW_ENABLED", "true"),
    "posting_window_start": os.getenv("POSTING_WINDOW_START", "04:00"),
    "posting_window_end": os.getenv("POSTING_WINDOW_END", "23:30"),
    "posting_timezone": os.getenv("POSTING_TIMEZONE", "Asia/Kolkata"),

    "auto_publish": "true",
    "publishing_enabled": "true",
    "admin_chat_id": "",

    "ai_enabled": "true",

    "duplicate_detection": "true",

    "max_retries": "3",
}


# ============================================================
# DEFAULT BOT STATE
# ============================================================

DEFAULT_STATE = {
    "version": "1",

    "worker_status": "IDLE",

    "processing_job": "",
    "publishing_job": "",

    "processing_lease_until": "",
    "publishing_lease_until": "",

    "last_tick_at": "",

    "last_successful_processing_at": "",
    "last_successful_publish_at": "",

    "next_post_at": "",

    "daily_posts_count": "0",
    "daily_posts_date": "",

    "last_error": "",
}


# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


async def ensure_client():
    """
    Make sure the Telethon user client is connected and authorized.

    Render has no interactive stdin, so production must use a pre-authorized
    TELEGRAM_SESSION_STRING instead of calling client.start() and prompting
    for a phone number, login code, or 2FA password.
    """

    if not client.is_connected():
        await client.connect()

    if not await client.is_user_authorized():
        if os.getenv("RENDER", "").lower() == "true":
            raise RuntimeError(
                "Telegram user session is not authorized on Render. "
                "Set TELEGRAM_SESSION_STRING in the Render environment "
                "using a session string generated locally."
            )
        raise RuntimeError(
            "Telegram user session is not authorized. "
            "Generate/login to the local Telethon session before starting the bot."
        )


def parse_list(value):
    if not value:
        return []

    return [
        item.strip()
        for item in value.split(",")
        if item.strip()
    ]


def format_list(items):
    return ",".join(items)


def clean_value(value):
    """
    Keep Telegram state records one-line-per-field.
    """

    if value is None:
        return ""

    return str(value).replace("\n", "\\n")



def format_storage_bytes(value):
    """Format a byte count for Telegram storage-management output."""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0

    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.2f} {units[index]}"


# ============================================================
# INSTAGRAM URL HELPERS
# ============================================================

INSTAGRAM_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/[A-Za-z0-9_-]+(?:\?[^\s]+)?",
    re.IGNORECASE,
)


def extract_instagram_urls(text):
    """Extract Instagram post/reel URLs from arbitrary text."""

    if not text:
        return []

    urls = INSTAGRAM_URL_PATTERN.findall(text)

    # Preserve order while removing duplicates from the same message.
    seen = set()
    result = []

    for url in urls:
        normalized = normalize_instagram_url(url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)

    return result


def normalize_instagram_url(url):
    """Normalize a supported Instagram URL for duplicate detection."""

    if not url:
        return None

    url = url.strip().strip("<>[](){}")

    try:
        parsed = urlsplit(url)
    except ValueError:
        return None

    hostname = (parsed.hostname or "").lower()

    if hostname not in {"instagram.com", "www.instagram.com"}:
        return None

    path = parsed.path.rstrip("/")
    parts = [part for part in path.split("/") if part]

    if len(parts) < 2:
        return None

    content_type = parts[0].lower()
    post_id = parts[1]

    if content_type == "reels":
        content_type = "reel"

    if content_type not in {"reel", "p", "tv"}:
        return None

    return urlunsplit(
        (
            "https",
            "www.instagram.com",
            f"/{content_type}/{post_id}/",
            "",
            "",
        )
    )


def instagram_post_id_from_url(url):
    """Return the Instagram shortcode from a normalized URL."""

    normalized = normalize_instagram_url(url)

    if not normalized:
        return ""

    parsed = urlsplit(normalized)
    parts = [part for part in parsed.path.split("/") if part]

    if len(parts) >= 2:
        return parts[1]

    return ""


# ============================================================
# JOB PARSING
# ============================================================

def parse_job(text):
    if not text:
        return None

    lines = text.splitlines()

    if not lines or lines[0].strip() != JOB_MARKER:
        return None

    job = {}

    for line in lines[1:]:
        line = line.strip()

        if not line or "=" not in line:
            continue

        key, value = line.split("=", 1)

        job[key.strip()] = value.strip()

    return job


# Telegram Bot API text messages are limited to 4096 characters.
# Keep persistent JOB records comfortably below that limit because these
# records are repeatedly edited as a job moves through the pipeline.
JOB_MESSAGE_MAX_LENGTH = 3800

# Long metadata fields are intentionally bounded in the persistent JOB record.
# The downloaded media itself remains in Telegram storage, while the AI
# caption/hashtags are retained at useful lengths for later publishing.
JOB_FIELD_MAX_LENGTHS = {
    "original_title": 200,
    "original_caption": 150,
    "original_hashtags": 150,
    "ai_caption": 2100,
    "ai_hashtags": 150,
    "ai_error": 100,
    "publishing_error": 100,
    "error": 100,
}


def _job_field_value(job, field):
    value = clean_value(job.get(field, ""))
    limit = JOB_FIELD_MAX_LENGTHS.get(field)

    if limit is None or len(value) <= limit:
        return value

    suffix = "...[truncated]"
    if limit <= len(suffix):
        return value[:limit]

    return value[:limit - len(suffix)] + suffix


def format_job(job):
    fields = [
        "job_id",
        "source_url",
        "source_platform",
        "source_post_id",

        "status",
        "queue_position",
        "status_chat_id",
        "status_message_id",

        "original_title",
        "original_caption",
        "original_hashtags",

        "ai_caption",
        "ai_hashtags",
        "ai_status",
        "ai_processed_at",
        "ai_error",

        "storage_message_id",
        "media_hash",
        "media_deleted_at",

        "created_at",
        "updated_at",

        "processing_started_at",
        "processing_completed_at",
        "processing_lease_until",

        "publish_after",

        "publishing_started_at",
        "publishing_completed_at",
        "publishing_lease_until",

        "published_at",
        "instagram_media_id",
        "instagram_container_id",
        "instagram_upload_uri",
        "publishing_upload_completed",
        "publishing_retry_count",
        "publishing_error",

        "approval_status",

        "retry_count",
        "error",
    ]

    lines = [JOB_MARKER]

    for field in fields:
        lines.append(
            f"{field}={_job_field_value(job, field)}"
        )

    text = "\n".join(lines)

    # A second safety net protects the record if a future field is added
    # without a specific limit above.
    if len(text) > JOB_MESSAGE_MAX_LENGTH:
        truncatable_fields = [
            "ai_caption",
            "original_caption",
            "original_hashtags",
            "ai_hashtags",
            "ai_error",
            "publishing_error",
            "error",
            "original_title",
        ]

        values = {
            field: _job_field_value(job, field)
            for field in fields
        }

        excess = len(text) - JOB_MESSAGE_MAX_LENGTH

        for field in truncatable_fields:
            if excess <= 0:
                break

            current = values[field]
            minimum = 40 if field != "ai_caption" else 200
            removable = max(0, len(current) - minimum)

            if removable <= 0:
                continue

            cut = min(removable, excess)
            new_length = len(current) - cut

            suffix = "...[truncated]"
            if new_length > len(suffix):
                values[field] = current[:new_length - len(suffix)] + suffix
            else:
                values[field] = current[:new_length]

            excess -= cut

        lines = [JOB_MARKER]
        for field in fields:
            lines.append(f"{field}={values[field]}")

        text = "\n".join(lines)

    # Never allow the persistent JOB edit to exceed Telegram's text limit.
    # The normal field limits above should make this unnecessary, but this
    # final guard keeps future schema changes from breaking processing.
    if len(text) > JOB_MESSAGE_MAX_LENGTH:
        # Preserve every field name and truncate only the final value area.
        overflow = len(text) - JOB_MESSAGE_MAX_LENGTH
        last_index = text.rfind("\n")
        if last_index > 0:
            tail = text[last_index + 1:]
            keep = max(0, len(tail) - overflow)
            text = text[:last_index + 1] + tail[:keep]

    return text


# ============================================================
# QUEUE PARSING
# ============================================================

def parse_queue_manifest(text):
    if not text:
        return None

    lines = text.splitlines()

    if not lines or lines[0].strip() != QUEUE_MARKER:
        return None

    queue = {
        "version": "1",
        "intake_queue": [],
        "processing_job": "",
        "publishing_queue": [],
        "publishing_job": "",
        "updated_at": "",
    }

    for line in lines[1:]:
        line = line.strip()

        if not line or "=" not in line:
            continue

        key, value = line.split("=", 1)

        key = key.strip()
        value = value.strip()

        if key == "intake_queue":
            queue[key] = parse_list(value)

        elif key == "publishing_queue":
            queue[key] = parse_list(value)

        else:
            queue[key] = value

    return queue


def format_queue_manifest(queue):
    return "\n".join([
        QUEUE_MARKER,
        f"version={clean_value(queue.get('version', '1'))}",
        f"intake_queue={format_list(queue.get('intake_queue', []))}",
        f"processing_job={clean_value(queue.get('processing_job', ''))}",
        f"publishing_queue={format_list(queue.get('publishing_queue', []))}",
        f"publishing_job={clean_value(queue.get('publishing_job', ''))}",
        f"updated_at={utc_now()}",
    ])


# ============================================================
# CONFIG PARSING
# ============================================================

def parse_config(text):
    if not text:
        return None

    lines = text.splitlines()

    if not lines or lines[0].strip() != CONFIG_MARKER:
        return None

    config = {}

    for line in lines[1:]:
        line = line.strip()

        if not line or "=" not in line:
            continue

        key, value = line.split("=", 1)

        config[key.strip()] = value.strip()

    return config


def format_config(config):
    fields = [
        "version",
        "mode",

        "publishing_interval_minutes",

        "daily_post_limit",

        "posting_window_enabled",
        "posting_window_start",
        "posting_window_end",
        "posting_timezone",

        "auto_publish",
        "publishing_enabled",
        "admin_chat_id",

        "ai_enabled",

        "duplicate_detection",

        "max_retries",

        "updated_at",
    ]

    lines = [CONFIG_MARKER]

    for field in fields:
        lines.append(
            f"{field}={clean_value(config.get(field, ''))}"
        )

    return "\n".join(lines)


# ============================================================
# BOT STATE PARSING
# ============================================================

def parse_state(text):
    if not text:
        return None

    lines = text.splitlines()

    if not lines or lines[0].strip() != STATE_MARKER:
        return None

    state = {}

    for line in lines[1:]:
        line = line.strip()

        if not line or "=" not in line:
            continue

        key, value = line.split("=", 1)

        state[key.strip()] = value.strip()

    return state


def format_state(state):
    fields = [
        "version",

        "worker_status",

        "processing_job",
        "publishing_job",

        "processing_lease_until",
        "publishing_lease_until",

        "last_tick_at",

        "last_successful_processing_at",
        "last_successful_publish_at",

        "next_post_at",

        "daily_posts_count",
        "daily_posts_date",

        "last_error",

        "updated_at",
    ]

    lines = [STATE_MARKER]

    for field in fields:
        lines.append(
            f"{field}={clean_value(state.get(field, ''))}"
        )

    return "\n".join(lines)


# ============================================================
# STORAGE MESSAGES
# ============================================================

async def get_storage_messages(limit=None):
    """
    Read messages from the private Telegram storage channel.
    """

    await ensure_client()

    channel = await client.get_entity(
        STORAGE_CHANNEL_ID
    )

    messages = []

    async for message in client.iter_messages(
        channel,
        limit=limit,
    ):
        messages.append(message)

    return messages


# ============================================================
# SAFE TELEGRAM EDIT HELPER
# ============================================================

async def _edit_message_if_changed(message_id, text):
    """Edit a persistent Telegram message without failing on a no-op edit."""
    try:
        return await client.edit_message(
            STORAGE_CHANNEL_ID,
            message_id,
            text,
        )
    except MessageNotModifiedError:
        return None


# ============================================================
# JOB FUNCTIONS
# ============================================================

async def get_all_jobs():
    messages = await get_storage_messages()

    jobs = []

    for message in messages:
        job = parse_job(
            message.text or ""
        )

        if job:
            job["_telegram_message_id"] = message.id
            jobs.append(job)

    return jobs


async def get_job(job_id):
    """
    Find one job by internal JOB ID.
    """

    jobs = await get_all_jobs()

    for job in jobs:
        if job.get("job_id") == job_id:
            return job

    return None


async def get_next_job_id():
    jobs = await get_all_jobs()

    highest = 0

    for job in jobs:
        job_id = job.get("job_id", "")

        if not job_id.startswith("JOB-"):
            continue

        try:
            number = int(
                job_id.replace("JOB-", "")
            )

            highest = max(
                highest,
                number,
            )

        except ValueError:
            continue

    return f"JOB-{highest + 1:06d}"


async def create_job(
    source_url,
    source_platform="instagram",
    source_post_id="",
    status_chat_id="",
    status_message_id="",
):
    """
    Create a new WAITING job.
    """

    await ensure_client()

    normalized_url = normalize_instagram_url(source_url)

    if not normalized_url:
        raise ValueError(
            "Unsupported or invalid Instagram URL."
        )

    if not source_post_id:
        source_post_id = instagram_post_id_from_url(
            normalized_url
        )

    source_url = normalized_url

    job_id = await get_next_job_id()
    now = utc_now()

    job = {
        "job_id": job_id,

        "source_url": source_url,
        "source_platform": source_platform,
        "source_post_id": source_post_id,

        "status": STATUS_WAITING,
        "queue_position": "",
        "status_chat_id": clean_value(status_chat_id),
        "status_message_id": clean_value(status_message_id),

        "original_title": "",
        "original_caption": "",
        "original_hashtags": "",

        "ai_caption": "",
        "ai_hashtags": "",
        "ai_status": "PENDING",
        "ai_processed_at": "",
        "ai_error": "",

        "storage_message_id": "",
        "media_hash": "",
        "media_deleted_at": "",

        "created_at": now,
        "updated_at": now,

        "processing_started_at": "",
        "processing_completed_at": "",
        "processing_lease_until": "",

        "publish_after": "",

        "publishing_started_at": "",
        "publishing_completed_at": "",
        "publishing_lease_until": "",

        "published_at": "",
        "instagram_media_id": "",
        "instagram_container_id": "",
        "instagram_upload_uri": "",
        "publishing_upload_completed": "false",
        "publishing_retry_count": "0",
        "publishing_error": "",

        "approval_status": "PENDING",

        "retry_count": "0",
        "error": "",
    }

    message = await client.send_message(
        STORAGE_CHANNEL_ID,
        format_job(job),
    )

    job["_telegram_message_id"] = message.id

    return job


async def update_job(job_id, updates):
    """
    Update an existing [JOB] record.
    """

    await ensure_client()

    job = await get_job(job_id)

    if not job:
        raise ValueError(
            f"Job not found: {job_id}"
        )

    telegram_message_id = job.get(
        "_telegram_message_id"
    )

    job.update(updates)
    job["updated_at"] = utc_now()

    await _edit_message_if_changed(
        telegram_message_id,
        format_job(job),
    )

    job["_telegram_message_id"] = (
        telegram_message_id
    )

    return job


async def save_instagram_metadata(job_id, metadata):
    """
    Persist metadata extracted by yt-dlp into the [JOB] record.

    Metadata is saved before media upload so that a later upload failure
    does not discard successfully extracted source information.
    """
    metadata = metadata or {}

    return await update_job(
        job_id,
        {
            "original_title": str(
                metadata.get("original_title", "")
            ).strip(),
            "original_caption": str(
                metadata.get("original_caption", "")
            ).strip(),
            "original_hashtags": str(
                metadata.get("original_hashtags", "")
            ).strip(),
        },
    )


async def save_ai_metadata(job_id, ai_result):
    """
    Persist Phase 8 Groq output into the permanent [JOB] record.

    AI processing is deliberately non-fatal: when Groq is unavailable,
    the helper returns FALLBACK with the original caption/hashtags.
    """
    ai_result = ai_result or {}

    status = str(ai_result.get("status", "FALLBACK")).strip().upper()
    caption = str(ai_result.get("caption", "")).strip()
    hashtags = str(ai_result.get("hashtags", "")).strip()
    error = str(ai_result.get("error", "")).strip()

    return await update_job(
        job_id,
        {
            "ai_caption": caption,
            "ai_hashtags": hashtags,
            "ai_status": status,
            "ai_processed_at": utc_now(),
            "ai_error": error[:1500],
        },
    )


async def find_duplicate_job(source_url, source_post_id=""):
    """
    Find an existing job with the same normalized Instagram URL
    or Instagram shortcode.
    """

    normalized_url = normalize_instagram_url(source_url)

    if not normalized_url:
        return None

    if not source_post_id:
        source_post_id = instagram_post_id_from_url(
            normalized_url
        )

    jobs = await get_all_jobs()

    for job in jobs:
        existing_url = normalize_instagram_url(
            job.get("source_url", "")
        )

        if existing_url == normalized_url:
            return job

        existing_post_id = job.get(
            "source_post_id", ""
        )

        if source_post_id and existing_post_id:
            if existing_post_id == source_post_id:
                return job

    return None


async def find_duplicate_media_hash(media_hash, exclude_job_id=""):
    """
    Find an existing job containing the same downloaded media.

    Media hashes are SHA-256 values calculated from the actual video
    bytes. This catches duplicate content even when the submitted
    Instagram URLs/shortcodes are different.

    Failed jobs are ignored so a transient processing failure does not
    permanently block a later retry of the same media.
    """

    media_hash = str(media_hash or "").strip().lower()

    if not media_hash:
        return None

    jobs = await get_all_jobs()

    duplicate_statuses = {
        STATUS_PROCESSING,
        STATUS_READY,
        STATUS_PUBLISHING,
        STATUS_PUBLISHED,
        STATUS_SKIPPED,
    }

    for job in jobs:
        if job.get("job_id") == exclude_job_id:
            continue

        existing_hash = str(
            job.get("media_hash", "")
        ).strip().lower()

        if not existing_hash:
            continue

        if job.get("status") not in duplicate_statuses:
            continue

        if existing_hash == media_hash:
            return job

    return None


async def save_media_hash(job_id, media_hash):
    """
    Persist the SHA-256 hash of the downloaded media.
    """

    media_hash = str(media_hash or "").strip().lower()

    if not media_hash:
        raise ValueError("Media hash cannot be empty.")

    return await update_job(
        job_id,
        {
            "media_hash": media_hash,
        },
    )


# ============================================================
# QUEUE FUNCTIONS
# ============================================================

async def find_queue_manifest():
    messages = await get_storage_messages()

    for message in messages:
        text = message.text or ""

        if text.startswith(
            QUEUE_MARKER
        ):
            return message

    return None


async def get_queue():
    message = await find_queue_manifest()

    if not message:
        return {
            "version": "1",
            "intake_queue": [],
            "processing_job": "",
            "publishing_queue": [],
            "publishing_job": "",
            "updated_at": utc_now(),
        }

    queue = parse_queue_manifest(
        message.text
    )

    if not queue:
        raise RuntimeError(
            "QUEUE_MANIFEST exists but "
            "could not be parsed."
        )

    queue["_telegram_message_id"] = (
        message.id
    )

    return queue


async def _save_queue_unlocked(queue):
    """Persist a queue manifest while the caller holds _queue_lock."""
    await ensure_client()

    text = format_queue_manifest(queue)
    existing = await find_queue_manifest()

    if existing:
        await _edit_message_if_changed(
            existing.id,
            text,
        )

        queue["_telegram_message_id"] = existing.id

    else:
        message = await client.send_message(
            STORAGE_CHANNEL_ID,
            text,
        )

        queue["_telegram_message_id"] = message.id

    return queue


async def save_queue(queue):
    """Persist a queue manifest safely for callers outside a queue lock."""
    async with _queue_lock:
        return await _save_queue_unlocked(queue)

async def add_job_to_intake(job_id):
    async with _queue_lock:
        queue = await get_queue()

        if job_id not in queue["intake_queue"]:
            queue["intake_queue"].append(job_id)

        return await _save_queue_unlocked(queue)

async def refresh_intake_positions():
    """
    Keep queue_position on every WAITING job synchronized with
    its current position in the persistent intake queue.
    """

    queue = await get_queue()
    jobs = await get_all_jobs()

    jobs_by_id = {
        job.get("job_id"): job
        for job in jobs
    }

    for position, job_id in enumerate(
        queue["intake_queue"],
        start=1,
    ):
        job = jobs_by_id.get(job_id)

        if not job:
            continue

        if (
            job.get("status") == STATUS_WAITING
            and job.get("queue_position")
            != str(position)
        ):
            await update_job(
                job_id,
                {
                    "queue_position": str(position),
                },
            )

    return queue


async def enqueue_instagram_url(
    source_url,
    status_chat_id="",
    status_message_id="",
):
    """
    Create a WAITING job and append it to the intake queue.

    Returns a result dictionary describing whether the URL was
    created or was already present as a duplicate.
    """

    normalized_url = normalize_instagram_url(
        source_url
    )

    if not normalized_url:
        return {
            "ok": False,
            "reason": "INVALID_URL",
            "url": source_url,
        }

    source_post_id = instagram_post_id_from_url(
        normalized_url
    )

    duplicate = await find_duplicate_job(
        normalized_url,
        source_post_id,
    )

    if duplicate:
        return {
            "ok": False,
            "reason": "DUPLICATE",
            "url": normalized_url,
            "job": duplicate,
        }

    job = await create_job(
        source_url=normalized_url,
        source_platform="instagram",
        source_post_id=source_post_id,
        status_chat_id=status_chat_id,
        status_message_id=status_message_id,
    )

    await add_job_to_intake(
        job["job_id"]
    )

    queue = await refresh_intake_positions()

    position = queue["intake_queue"].index(
        job["job_id"]
    ) + 1

    return {
        "ok": True,
        "reason": "CREATED",
        "url": normalized_url,
        "job": job,
        "position": position,
    }


async def remove_job_from_intake(job_id, mark_skipped=True):
    async with _queue_lock:
        queue = await get_queue()

        if job_id not in queue["intake_queue"]:
            return {
                "removed": False,
                "queue": queue,
            }

        queue["intake_queue"] = [
            item
            for item in queue["intake_queue"]
            if item != job_id
        ]

        queue = await _save_queue_unlocked(queue)

        if mark_skipped:
            await update_job(
                job_id,
                {
                    "status": STATUS_SKIPPED,
                    "queue_position": "",
                },
            )

    queue = await refresh_intake_positions()

    return {
        "removed": True,
        "queue": queue,
    }

async def set_processing_job(job_id):
    async with _queue_lock:
        queue = await get_queue()
        queue["processing_job"] = job_id
        return await _save_queue_unlocked(queue)

async def clear_processing_job():
    async with _queue_lock:
        queue = await get_queue()
        queue["processing_job"] = ""
        return await _save_queue_unlocked(queue)

async def add_job_to_publishing(job_id):
    async with _queue_lock:
        queue = await get_queue()

        if job_id not in queue["publishing_queue"]:
            queue["publishing_queue"].append(job_id)

        return await _save_queue_unlocked(queue)

async def remove_job_from_publishing(job_id):
    async with _queue_lock:
        queue = await get_queue()

        queue["publishing_queue"] = [
            item
            for item in queue["publishing_queue"]
            if item != job_id
        ]

        return await _save_queue_unlocked(queue)

async def set_publishing_job(job_id):
    async with _queue_lock:
        queue = await get_queue()
        queue["publishing_job"] = job_id
        return await _save_queue_unlocked(queue)

async def clear_publishing_job():
    async with _queue_lock:
        queue = await get_queue()
        queue["publishing_job"] = ""
        return await _save_queue_unlocked(queue)


# ============================================================
# PUBLISHING WORKER FUNCTIONS
# ============================================================

PUBLISHING_LEASE_MINUTES = 30


def publishing_lease_until(minutes=PUBLISHING_LEASE_MINUTES):
    return (
        datetime.now(timezone.utc)
        + timedelta(minutes=minutes)
    ).isoformat()


async def claim_next_publishing_job():
    """
    Claim the first READY/PUBLISHING job whose persistent publish_after slot
    is due and whose posting-window/daily-limit rules allow publication.

    The queue entry stays in publishing_queue while publishing is in progress.
    publishing_job is the persistent single-publisher lock.
    """
    async with _queue_lock:
        queue = await get_queue()
        config = await get_config()
        state = await get_state()

        if str(config.get("publishing_enabled", "true")).strip().lower() != "true":
            state["worker_status"] = "PAUSED"
            await save_state(state)
            return None

        if queue.get("publishing_job"):
            existing = await get_job(queue["publishing_job"])
            if existing and existing.get("status") == STATUS_PUBLISHING:
                lease = parse_utc_datetime(existing.get("publishing_lease_until", ""))
                if lease is not None and lease > datetime.now(timezone.utc):
                    return None
                # The previous process/worker lease expired. Reclaim it below.
                queue["publishing_job"] = ""
                await _save_queue_unlocked(queue)
            else:
                queue["publishing_job"] = ""
                await _save_queue_unlocked(queue)

        publishing_queue = list(queue.get("publishing_queue", []))
        if not publishing_queue:
            return None

        now = datetime.now(timezone.utc)
        first_job_id = publishing_queue[0]
        job = await get_job(first_job_id)

        if not job:
            queue["publishing_queue"] = publishing_queue[1:]
            await _save_queue_unlocked(queue)
            state["next_post_at"] = ""
            await save_state(state)
            return None

        status = str(job.get("status", ""))

        # A PUBLISHING job can survive a process restart. Reclaim it only if
        # its lease has expired; the publisher will resume the saved container.
        if status == STATUS_PUBLISHING:
            lease = parse_utc_datetime(job.get("publishing_lease_until", ""))
            if lease is not None and lease > now:
                return None

        elif status != STATUS_READY:
            # Keep the persistent queue clean without destroying PUBLISHED jobs.
            queue["publishing_queue"] = [
                item for item in publishing_queue if item != first_job_id
            ]
            await _save_queue_unlocked(queue)
            if state.get("next_post_at"):
                state["next_post_at"] = ""
                await save_state(state)
            return None

        scheduled_at = parse_utc_datetime(job.get("publish_after", ""))
        if scheduled_at is None:
            # Missing schedule: create one and wait for it.
            await _save_queue_unlocked(queue)
            # Cannot call ensure_publishing_schedule under the same lock.
            return None

        if scheduled_at > now:
            return None

        window = publishing_window_status(config, now)
        if not window["allowed"]:
            next_slot = window["next_window_start_utc"]
            next_iso = next_slot.isoformat()
            await update_job(first_job_id, {"publish_after": next_iso})
            state["next_post_at"] = next_iso
            await save_state(state)
            return None

        local_date = window["local_date"]
        limit = daily_post_limit_from_config(config)
        used = daily_posts_used(state, local_date)
        if limit > 0 and used >= limit:
            # Wait for the next local day. The next worker tick will apply the
            # posting-window start if midnight itself is outside the window.
            tomorrow = window["local_now"].date() + timedelta(days=1)
            offset = posting_timezone_offset()
            midnight_local_as_utc = datetime(
                tomorrow.year,
                tomorrow.month,
                tomorrow.day,
                0,
                0,
                tzinfo=timezone.utc,
            ) - offset
            next_iso = midnight_local_as_utc.isoformat()
            await update_job(first_job_id, {"publish_after": next_iso})
            state["next_post_at"] = next_iso
            await save_state(state)
            return None

        now_iso = now.isoformat()
        updates = {
            "status": STATUS_PUBLISHING,
            "publishing_started_at": job.get("publishing_started_at") or now_iso,
            "publishing_lease_until": publishing_lease_until(),
            "publishing_error": "",
            "error": "",
        }
        job = await update_job(first_job_id, updates)

        queue["publishing_job"] = first_job_id
        await _save_queue_unlocked(queue)

        state["publishing_job"] = first_job_id
        state["publishing_lease_until"] = job["publishing_lease_until"]
        state["worker_status"] = "PUBLISHING"
        state["last_error"] = ""
        await save_state(state)

        return job


async def mark_publishing_upload_completed(job_id):
    """Persist that Meta accepted the binary upload for the saved container."""
    return await update_job(
        job_id,
        {"publishing_upload_completed": "true"},
    )


async def finish_publishing_success(job_id, instagram_media_id, container_id=""):
    """Persist a successful Instagram publication and advance the queue."""
    job = await get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")

    now = utc_now()
    await update_job(
        job_id,
        {
            "status": STATUS_PUBLISHED,
            "publishing_completed_at": now,
            "publishing_lease_until": "",
            "published_at": now,
            "instagram_media_id": str(instagram_media_id or ""),
            "instagram_container_id": str(container_id or job.get("instagram_container_id", "")),
            "publishing_upload_completed": "true",
            "publishing_error": "",
            "error": "",
            "publish_after": "",
        },
    )

    async with _queue_lock:
        queue = await get_queue()
        queue["publishing_queue"] = [
            item for item in queue.get("publishing_queue", [])
            if item != job_id
        ]
        if queue.get("publishing_job") == job_id:
            queue["publishing_job"] = ""
        await _save_queue_unlocked(queue)

    state = await get_state()
    offset = posting_timezone_offset()
    local_date = (datetime.now(timezone.utc) + offset).date().isoformat()
    previous_count = daily_posts_used(state, local_date)
    state["daily_posts_date"] = local_date
    state["daily_posts_count"] = str(previous_count + 1)
    state["publishing_job"] = ""
    state["publishing_lease_until"] = ""
    state["last_successful_publish_at"] = now
    state["last_error"] = ""
    state["worker_status"] = "IDLE"
    state["next_post_at"] = ""
    await save_state(state)

    await ensure_publishing_schedule()
    return await get_job(job_id)


PUBLISH_RETRY_BACKOFF_MINUTES = (5, 15, 60)


def classify_publishing_failure(error_text):
    """Classify an Instagram publishing error for Phase 15 recovery."""
    text = str(error_text or "").lower()

    # Deployment/configuration/authentication failures should not be retried
    # repeatedly because the same bad configuration will keep failing.
    permanent_markers = (
        "not configured",
        "invalid oauth",
        "oauthexception",
        "invalid access token",
        "access token has expired",
        "permissions error",
        "permission error",
        "does not have permission",
        "unsupported get request",
        "invalid user",
        "instagram account",
    )
    if any(marker in text for marker in permanent_markers):
        return "PERMANENT"

    # A malformed/unsupported media file is normally deterministic. The
    # publisher's container ERROR is intentionally treated as retryable here
    # because Meta does not always expose the underlying processing reason.
    media_markers = (
        "unsupported format",
        "invalid video",
        "invalid media",
        "video format",
        "codec",
        "resolution",
        "duration",
    )
    if any(marker in text for marker in media_markers):
        return "PERMANENT"

    return "RETRYABLE"


async def finish_publishing_failure(job_id, error_message, retry=True):
    """
    Persist a publishing failure and apply Phase 15 bounded recovery.

    Retryable failures are retried at 5, 15, and 60 minute backoff slots.
    After the configured max_retries (default 3) the job becomes FAILED and
    is removed from the active publishing queue. Permanent configuration or
    media errors fail immediately. Telegram remains the source of truth.
    """
    job = await get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")

    error_text = str(error_message)[:1500]
    current_retries = 0
    try:
        current_retries = int(str(job.get("publishing_retry_count", "0")))
    except (TypeError, ValueError):
        current_retries = 0

    config = await get_config()
    try:
        max_retries = max(0, int(str(config.get("max_retries", "3"))))
    except (TypeError, ValueError):
        max_retries = 3

    failure_class = classify_publishing_failure(error_text)
    next_retries = current_retries + 1
    should_retry = bool(retry) and failure_class == "RETRYABLE" and next_retries <= max_retries

    if should_retry:
        index = min(next_retries - 1, len(PUBLISH_RETRY_BACKOFF_MINUTES) - 1)
        delay_minutes = PUBLISH_RETRY_BACKOFF_MINUTES[index]
        retry_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        retry_iso = retry_at.isoformat()
        status = STATUS_READY
    else:
        delay_minutes = 0
        retry_iso = ""
        status = STATUS_FAILED

    await update_job(
        job_id,
        {
            "status": status,
            "publishing_lease_until": "",
            "publishing_retry_count": str(next_retries),
            "publishing_error": error_text,
            "error": error_text,
            "publish_after": retry_iso,
        },
    )

    async with _queue_lock:
        queue = await get_queue()
        if status == STATUS_READY:
            if job_id not in queue.get("publishing_queue", []):
                queue.setdefault("publishing_queue", []).insert(0, job_id)
        else:
            queue["publishing_queue"] = [
                item for item in queue.get("publishing_queue", [])
                if item != job_id
            ]
        if queue.get("publishing_job") == job_id:
            queue["publishing_job"] = ""
        await _save_queue_unlocked(queue)

    state = await get_state()
    state["publishing_job"] = ""
    state["publishing_lease_until"] = ""
    state["worker_status"] = "ERROR" if status == STATUS_FAILED else "WAITING"
    state["last_error"] = error_text
    state["next_post_at"] = retry_iso
    await save_state(state)

    result = await get_job(job_id)
    if result is None:
        return None

    result["publishing_failure_class"] = failure_class
    result["publishing_retry_scheduled"] = "true" if should_retry else "false"
    result["publishing_retry_delay_minutes"] = str(delay_minutes)
    result["publishing_max_retries"] = str(max_retries)
    return result


# ============================================================
# PROCESSING WORKER FUNCTIONS
# ============================================================

PROCESSING_LEASE_MINUTES = 60


async def reconcile_queue_state():
    """
    Repair queue/job inconsistencies caused by a crash or concurrent
    Telegram message edits.

    WAITING jobs are guaranteed to appear in intake_queue exactly once.
    Jobs that are no longer WAITING are removed from intake_queue.
    The processing/publishing markers are preserved.
    """
    async with _queue_lock:
        queue = await get_queue()
        jobs = await get_all_jobs()

        waiting_jobs = [
            job for job in jobs
            if job.get("status") == STATUS_WAITING
        ]
        waiting_by_id = {
            job.get("job_id"): job
            for job in waiting_jobs
            if job.get("job_id")
        }

        current_intake = queue.get("intake_queue", [])
        repaired_intake = []
        seen = set()

        # Preserve the existing queue order for valid WAITING jobs.
        for job_id in current_intake:
            if job_id in waiting_by_id and job_id not in seen:
                repaired_intake.append(job_id)
                seen.add(job_id)

        # Append WAITING jobs that were lost from the queue manifest.
        missing = [
            job for job in waiting_jobs
            if job.get("job_id") not in seen
        ]
        missing.sort(
            key=lambda job: (
                job.get("created_at", ""),
                job.get("job_id", ""),
            )
        )

        for job in missing:
            job_id = job.get("job_id")
            if job_id:
                repaired_intake.append(job_id)
                seen.add(job_id)

        # Phase 9: READY jobs automatically belong to the publishing queue.
        ready_jobs = [job for job in jobs if job.get("status") == STATUS_READY]
        current_publishing = queue.get("publishing_queue", [])
        repaired_publishing = []
        publishing_seen = set()
        jobs_by_id = {job.get("job_id"): job for job in jobs if job.get("job_id")}

        for job_id in current_publishing:
            job = jobs_by_id.get(job_id)
            if job and job.get("status") in (STATUS_READY, STATUS_PUBLISHING) and job_id not in publishing_seen:
                repaired_publishing.append(job_id)
                publishing_seen.add(job_id)

        missing_publishing = [job for job in ready_jobs if job.get("job_id") not in publishing_seen]
        missing_publishing.sort(key=lambda job: (job.get("created_at", ""), job.get("job_id", "")))
        for job in missing_publishing:
            job_id = job.get("job_id")
            if job_id:
                repaired_publishing.append(job_id)
                publishing_seen.add(job_id)

        changed = repaired_intake != current_intake or repaired_publishing != current_publishing

        if changed:
            queue["intake_queue"] = repaired_intake
            queue["publishing_queue"] = repaired_publishing
            await _save_queue_unlocked(queue)

    # Schedule READY publishing jobs after queue reconciliation.
    await ensure_publishing_schedule()
    return await get_queue()

def processing_lease_until(minutes=PROCESSING_LEASE_MINUTES):
    from datetime import timedelta
    return (
        datetime.now(timezone.utc)
        + timedelta(minutes=minutes)
    ).isoformat()


async def claim_next_intake_job():
    """
    Atomically claim the first valid WAITING job for the single
    processing worker.

    The persistent queue manifest is the source of truth. Queue
    mutations are serialized so a new enqueue cannot be overwritten
    by a worker reading an older queue snapshot.
    """
    async with _queue_lock:
        # Repair any WAITING jobs that are present in Telegram storage
        # but missing from the persistent intake queue.
        queue = await get_queue()
        jobs = await get_all_jobs()

        waiting_jobs = [
            job for job in jobs
            if job.get("status") == STATUS_WAITING
        ]
        waiting_by_id = {
            job.get("job_id"): job
            for job in waiting_jobs
            if job.get("job_id")
        }

        current_intake = queue.get("intake_queue", [])
        repaired_intake = []
        seen = set()

        for job_id in current_intake:
            if job_id in waiting_by_id and job_id not in seen:
                repaired_intake.append(job_id)
                seen.add(job_id)

        missing = [
            job for job in waiting_jobs
            if job.get("job_id") not in seen
        ]
        missing.sort(
            key=lambda job: (
                job.get("created_at", ""),
                job.get("job_id", ""),
            )
        )

        for job in missing:
            job_id = job.get("job_id")
            if job_id:
                repaired_intake.append(job_id)
                seen.add(job_id)

        if repaired_intake != current_intake:
            queue["intake_queue"] = repaired_intake
            await _save_queue_unlocked(queue)

        if queue.get("processing_job"):
            return None

        while queue["intake_queue"]:
            job_id = queue["intake_queue"][0]
            queue["intake_queue"] = queue["intake_queue"][1:]

            job = await get_job(job_id)

            if not job:
                continue

            if job.get("status") != STATUS_WAITING:
                continue

            now = utc_now()

            await update_job(
                job_id,
                {
                    "status": STATUS_PROCESSING,
                    "queue_position": "",
                    "processing_started_at": now,
                    "processing_lease_until": processing_lease_until(),
                    "error": "",
                },
            )

            queue["processing_job"] = job_id
            await _save_queue_unlocked(queue)

            claimed_job = await get_job(job_id)
            state = await get_state()
            state["processing_job"] = job_id
            state["processing_lease_until"] = claimed_job.get(
                "processing_lease_until", ""
            )
            state["worker_status"] = "PROCESSING"
            state["last_error"] = ""
            await save_state(state)

            # Refresh the remaining WAITING job positions.
            # This only edits JOB records, not the queue manifest.
            await refresh_intake_positions()

            return await get_job(job_id)

        await _save_queue_unlocked(queue)
        return None


def parse_utc_datetime(value):
    """Parse an ISO-8601 UTC timestamp stored in Telegram state."""
    value = str(value or "").strip()
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def publishing_interval_minutes_from_config(config):
    """Return a safe positive publishing interval in minutes."""
    try:
        minutes = int(str(config.get("publishing_interval_minutes", "30")).strip())
    except (TypeError, ValueError):
        minutes = 30

    return max(1, min(minutes, 1440))


# ============================================================
# PUBLISHING WINDOW / SCHEDULING HELPERS
# ============================================================

IST_OFFSET = timedelta(hours=5, minutes=30)


def posting_timezone_offset():
    """Return the configured posting timezone offset.

    The project is configured for Asia/Kolkata by default. Windows Python
    installations may not ship with the IANA tz database, so we deliberately
    use the fixed +05:30 offset for India instead of requiring zoneinfo/tzdata.
    """
    config_timezone = str(os.getenv("POSTING_TIMEZONE", "Asia/Kolkata")).strip()
    if config_timezone in {"Asia/Kolkata", "IST", "UTC+05:30", "GMT+05:30"}:
        return IST_OFFSET
    if config_timezone in {"UTC", "Etc/UTC", "GMT"}:
        return timedelta(0)
    # Unknown timezone: keep the project safe and deterministic.
    raise ValueError(
        f"Unsupported POSTING_TIMEZONE '{config_timezone}'. "
        "Use Asia/Kolkata or UTC."
    )


def parse_clock_time(value, default):
    """Parse HH:MM from configuration, falling back to a safe default."""
    raw = str(value or default).strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour * 60 + minute
    except (TypeError, ValueError):
        hour_text, minute_text = default.split(":", 1)
        return int(hour_text) * 60 + int(minute_text)


def posting_window_enabled(config):
    return str(config.get("posting_window_enabled", "true")).strip().lower() == "true"


def daily_post_limit_from_config(config):
    """Return 0 for unlimited daily publishing."""
    try:
        limit = int(str(config.get("daily_post_limit", "0")).strip())
    except (TypeError, ValueError):
        limit = 0
    return max(0, limit)


def daily_posts_used(state, local_date):
    if str(state.get("daily_posts_date", "")) != local_date:
        return 0
    try:
        return max(0, int(str(state.get("daily_posts_count", "0"))))
    except (TypeError, ValueError):
        return 0


def publishing_window_status(config, now_utc=None):
    """Return whether publishing is currently allowed and the next window start."""
    now_utc = now_utc or datetime.now(timezone.utc)
    offset = posting_timezone_offset()
    local_now = now_utc + offset

    start_minute = parse_clock_time(config.get("posting_window_start", "04:00"), "04:00")
    end_minute = parse_clock_time(config.get("posting_window_end", "23:30"), "23:30")
    current_minute = local_now.hour * 60 + local_now.minute
    current_second = local_now.second

    if not posting_window_enabled(config):
        return {
            "allowed": True,
            "local_now": local_now,
            "local_date": local_now.date().isoformat(),
            "next_window_start_utc": now_utc,
        }

    if start_minute < end_minute:
        inside = (
            start_minute <= current_minute < end_minute
            or (current_minute == end_minute and current_second == 0)
        )
        if inside:
            return {
                "allowed": True,
                "local_now": local_now,
                "local_date": local_now.date().isoformat(),
                "next_window_start_utc": now_utc,
            }
        if current_minute < start_minute:
            target_date = local_now.date()
        else:
            target_date = local_now.date() + timedelta(days=1)
    else:
        # Supports a window crossing midnight, e.g. 22:00 -> 02:00.
        inside = current_minute >= start_minute or current_minute < end_minute
        if inside:
            return {
                "allowed": True,
                "local_now": local_now,
                "local_date": local_now.date().isoformat(),
                "next_window_start_utc": now_utc,
            }
        target_date = local_now.date()

    target_local = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        start_minute // 60,
        start_minute % 60,
        tzinfo=timezone.utc,
    ) - offset

    return {
        "allowed": False,
        "local_now": local_now,
        "local_date": local_now.date().isoformat(),
        "next_window_start_utc": target_local,
    }


def next_publish_slot(config, state=None, now_utc=None, after_utc=None):
    """Calculate the next eligible UTC publication slot."""
    now_utc = now_utc or datetime.now(timezone.utc)
    candidate = after_utc or now_utc
    if candidate < now_utc:
        candidate = now_utc

    interval = publishing_interval_minutes_from_config(config)
    window = publishing_window_status(config, candidate)

    if not window["allowed"]:
        candidate = window["next_window_start_utc"]

    # A scheduled timestamp exactly at the window end is outside a normal
    # [start, end) window, so advance to the next valid window.
    for _ in range(3):
        window = publishing_window_status(config, candidate)
        if window["allowed"]:
            return candidate
        candidate = window["next_window_start_utc"]

    return candidate


async def ensure_publishing_schedule():
    """
    Ensure the first queued READY job has a persistent publish_after time.

    The schedule is persisted in Telegram so a restart cannot reset it.
    Posting-window and daily-limit rules are applied here as well as by the
    future publishing worker.
    """
    async with _queue_lock:
        queue = await get_queue()
        publishing_queue = list(queue.get("publishing_queue", []))
        config = await get_config()

        if not publishing_queue:
            state = await get_state()
            if state.get("next_post_at"):
                state["next_post_at"] = ""
                await save_state(state)
            return None

        state = await get_state()
        existing_next = parse_utc_datetime(state.get("next_post_at", ""))

        first_job_id = publishing_queue[0]
        first_job = await get_job(first_job_id)

        if not first_job:
            queue["publishing_queue"] = publishing_queue[1:]
            await _save_queue_unlocked(queue)
            state["next_post_at"] = ""
            await save_state(state)
            return None

        existing_job_schedule = parse_utc_datetime(first_job.get("publish_after", ""))
        if existing_job_schedule is not None:
            if existing_next is None or existing_next != existing_job_schedule:
                state["next_post_at"] = existing_job_schedule.isoformat()
                await save_state(state)
            return existing_job_schedule.isoformat()

        limit = daily_post_limit_from_config(config)
        offset = posting_timezone_offset()
        local_now = datetime.now(timezone.utc) + offset
        local_date = local_now.date().isoformat()
        used = daily_posts_used(state, local_date)

        if limit > 0 and used >= limit:
            # Daily limit is retained for compatibility, but 0 is the normal
            # project setting for unlimited publishing.
            tomorrow = local_now.date() + timedelta(days=1)
            target_local = datetime(
                tomorrow.year, tomorrow.month, tomorrow.day,
                0, 0, tzinfo=timezone.utc,
            ) - offset
            scheduled_at = target_local
        else:
            # The interval is measured from the moment the job enters the
            # publishing schedule. This keeps the test value (e.g. 2 min)
            # meaningful and preserves the normal 30-minute production plan.
            first_candidate = datetime.now(timezone.utc) + timedelta(
                minutes=publishing_interval_minutes_from_config(config)
            )
            scheduled_at = next_publish_slot(
                config,
                state=state,
                after_utc=first_candidate,
            )

        scheduled_iso = scheduled_at.isoformat()
        await update_job(first_job_id, {"publish_after": scheduled_iso})

        state["next_post_at"] = scheduled_iso
        await save_state(state)
        return scheduled_iso


async def reschedule_after_publish():
    """Clear the completed slot and schedule the next queued job."""
    async with _queue_lock:
        state = await get_state()
        state["next_post_at"] = ""
        await save_state(state)
    return await ensure_publishing_schedule()


async def get_publishing_schedule():
    """Return the current persistent publishing schedule."""
    state = await get_state()
    queue = await get_queue()
    return {
        "next_post_at": state.get("next_post_at", ""),
        "publishing_queue": list(queue.get("publishing_queue", [])),
        "interval_minutes": publishing_interval_minutes_from_config(await get_config()),
    }


async def finish_processing_success(job_id, storage_message_id):
    """Mark a processed job READY, auto-approve it, and queue it for publishing."""
    job = await get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")

    now = utc_now()
    await update_job(job_id, {
        "status": STATUS_READY,
        "storage_message_id": str(storage_message_id),
        "processing_completed_at": now,
        "processing_lease_until": "",
        "approval_status": "APPROVED",
        "error": "",
    })

    async with _queue_lock:
        queue = await get_queue()
        if queue.get("processing_job") == job_id:
            queue["processing_job"] = ""
        if job_id not in queue.get("publishing_queue", []):
            queue.setdefault("publishing_queue", []).append(job_id)
        await _save_queue_unlocked(queue)

    # Persist the next publishing slot immediately. This means the schedule
    # survives a restart even before the future Instagram publisher exists.
    await ensure_publishing_schedule()

    state = await get_state()
    state["processing_job"] = ""
    state["processing_lease_until"] = ""
    state["last_successful_processing_at"] = now
    state["worker_status"] = "IDLE"
    state["last_error"] = ""
    await save_state(state)
    return await get_job(job_id)

async def finish_processing_failure(job_id, error_message):
    """
    Mark a processing failure and release the processing slot.

    Retry policy is intentionally left for the later failure-recovery
    phase. A failed job remains in Telegram storage for inspection.
    """
    job = await get_job(job_id)

    if not job:
        raise ValueError(f"Job not found: {job_id}")

    now = utc_now()

    await update_job(
        job_id,
        {
            "status": STATUS_FAILED,
            "processing_lease_until": "",
            "error": str(error_message)[:1500],
        },
    )

    async with _queue_lock:
        queue = await get_queue()

        if queue.get("processing_job") == job_id:
            queue["processing_job"] = ""

        await _save_queue_unlocked(queue)

    state = await get_state()
    state["processing_job"] = ""
    state["processing_lease_until"] = ""
    state["worker_status"] = "ERROR"
    state["last_error"] = str(error_message)[:1500]
    await save_state(state)

    return await get_job(job_id)

async def finish_processing_duplicate(
    job_id,
    duplicate_job_id,
    media_hash,
):
    """
    Mark a downloaded job as SKIPPED because the exact media already
    exists in another job.

    The duplicate media is not uploaded a second time. The processing
    slot is released so the next intake job can continue immediately.
    """

    job = await get_job(job_id)

    if not job:
        raise ValueError(f"Job not found: {job_id}")

    now = utc_now()
    duplicate_job_id = str(duplicate_job_id or "").strip()
    media_hash = str(media_hash or "").strip().lower()

    error_message = (
        f"Duplicate media detected. "
        f"Existing job: {duplicate_job_id}"
    )

    await update_job(
        job_id,
        {
            "status": STATUS_SKIPPED,
            "media_hash": media_hash,
            "processing_completed_at": now,
            "processing_lease_until": "",
            "error": error_message,
        },
    )

    async with _queue_lock:
        queue = await get_queue()

        if queue.get("processing_job") == job_id:
            queue["processing_job"] = ""

        await _save_queue_unlocked(queue)

    state = await get_state()
    state["processing_job"] = ""
    state["processing_lease_until"] = ""
    state["worker_status"] = "IDLE"
    state["last_error"] = ""
    await save_state(state)

    return await get_job(job_id)

async def store_downloaded_media(
    job_id,
    file_path,
    progress_callback=None,
):
    """
    Upload downloaded media to persistent Telegram storage.

    Performance notes:
    - Telethon's upload chunk is explicitly set to the maximum supported
      512 KiB to reduce request overhead for larger videos.
    - ``cryptg`` should be installed in the environment. Telethon will then
      use native AES-IGE encryption instead of the much slower pure-Python
      fallback.
    - ``upload_file`` is used first and the resulting remote file handle is
      passed to ``send_file``. This keeps the upload operation explicit and
      lets us control the chunk size without changing the resulting Telegram
      storage message.

    If progress_callback is supplied, progress is reported to the caller
    (the bot chat) instead of creating a progress message in the storage
    channel. The callback may be async.
    """
    await ensure_client()

    job = await get_job(job_id)

    if not job:
        raise ValueError(f"Job not found: {job_id}")

    if not os.path.isfile(file_path):
        raise FileNotFoundError(file_path)

    file_size = os.path.getsize(file_path)
    last_update_monotonic = 0.0
    last_percent = -1
    upload_started_monotonic = time.monotonic()

    def format_bytes(value):
        value = float(value or 0)
        units = ("B", "KB", "MB", "GB")
        index = 0
        while value >= 1024 and index < len(units) - 1:
            value /= 1024
            index += 1
        return f"{value:.2f} {units[index]}"

    def format_speed(bytes_per_second):
        return f"{format_bytes(bytes_per_second)}/s"

    async def report(current, total, finished=False, error=""):
        if progress_callback is None:
            return
        try:
            result = progress_callback(
                int(current or 0),
                int(total or file_size or 1),
                finished,
                error,
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            print(
                f"⚠️ Bot upload progress callback failed for {job_id}: {exc}"
            )

    async def telethon_progress(current, total):
        nonlocal last_update_monotonic, last_percent

        total = int(total or file_size or 1)
        current = min(int(current or 0), total)
        percent = int((current / total) * 100) if total else 100
        now_monotonic = time.monotonic()

        should_update = (
            percent >= 100
            or percent // 5 > last_percent // 5
            or now_monotonic - last_update_monotonic >= 1.0
        )

        if not should_update:
            return

        elapsed = max(now_monotonic - upload_started_monotonic, 0.001)
        speed = current / elapsed
        speed_text = format_speed(speed)

        if percent >= 100:
            print(
                f"🚀 Telegram upload speed: {speed_text} "
                f"({format_bytes(current)} / {format_bytes(total)})"
            )

        await report(current, total, finished=percent >= 100)
        last_update_monotonic = now_monotonic
        last_percent = percent

    caption = (
        "🎬 DOWNLOADED MEDIA\n"
        f"job_id={job_id}\n"
        f"source_url={job.get('source_url', '')}"
    )

    try:
        await report(0, file_size)

        print("\n==============================")
        print("📤 TELEGRAM STORAGE UPLOAD")
        print(f"Job: {job_id}")
        print(f"File: {os.path.basename(file_path)}")
        print(f"Size: {format_bytes(file_size)}")
        print("Upload chunk size: 512 KiB")

        try:
            import cryptg  # noqa: F401
            print("Encryption accelerator: cryptg ✅")
        except ImportError:
            print(
                "Encryption accelerator: cryptg ❌ "
                "(Telethon will use the slower pure-Python fallback)"
            )

        print("Live progress: bot chat")
        print("==============================\n")

        # Upload the file explicitly with the largest supported chunk size.
        # The returned handle is then attached to the storage message without
        # uploading the file a second time.
        uploaded_file = await client.upload_file(
            file_path,
            part_size_kb=512,
            file_size=file_size,
            file_name=os.path.basename(file_path),
            progress_callback=telethon_progress,
        )

        message = await client.send_file(
            STORAGE_CHANNEL_ID,
            uploaded_file,
            file_size=file_size,
            caption=caption,
            supports_streaming=True,
        )

        await report(file_size, file_size, finished=True)

        elapsed = max(time.monotonic() - upload_started_monotonic, 0.001)
        average_speed = file_size / elapsed

        print("\n==============================")
        print("✅ TELEGRAM STORAGE UPLOAD COMPLETE")
        print(f"Job: {job_id}")
        print(f"Storage message ID: {message.id}")
        print(f"Elapsed: {elapsed:.2f} seconds")
        print(f"Average upload speed: {format_speed(average_speed)}")
        print("==============================\n")

        return message.id

    except Exception as exc:
        await report(0, file_size, finished=False, error=str(exc))
        raise


async def download_stored_media(job_id, destination_path, progress_callback=None):
    """
    Download a job's stored Telegram video to a temporary local path.

    Telegram remains the source of truth. The local file is temporary and is
    deleted by the publishing worker after the Instagram API operation ends.
    """
    await ensure_client()

    job = await get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")

    message_id = str(job.get("storage_message_id", "")).strip()
    if not message_id:
        raise ValueError(f"{job_id} has no storage_message_id.")

    try:
        message_id_int = int(message_id)
    except ValueError as exc:
        raise ValueError(
            f"Invalid storage_message_id for {job_id}: {message_id}"
        ) from exc

    destination = os.path.abspath(str(destination_path))
    os.makedirs(os.path.dirname(destination), exist_ok=True)

    message = await client.get_messages(
        STORAGE_CHANNEL_ID,
        ids=message_id_int,
    )

    if not message:
        raise FileNotFoundError(
            f"Telegram storage message {message_id_int} was not found for {job_id}."
        )

    if not getattr(message, "media", None):
        raise ValueError(
            f"Telegram storage message {message_id_int} has no downloadable media."
        )

    print("\n==============================")
    print("📥 TELEGRAM STORAGE DOWNLOAD")
    print(f"Job: {job_id}")
    print(f"Storage message ID: {message_id_int}")
    print("==============================\n")

    last_percent = -1
    last_update = 0.0

    async def telethon_progress(current, total):
        nonlocal last_percent, last_update
        total = int(total or 1)
        current = min(int(current or 0), total)
        percent = int((current / total) * 100) if total else 100
        now = time.monotonic()

        if (
            percent >= 100
            or percent // 5 > last_percent // 5
            or now - last_update >= 1.0
        ):
            last_percent = percent
            last_update = now
            if progress_callback is not None:
                try:
                    result = progress_callback(current, total, percent >= 100, "")
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    print(
                        f"⚠️ Storage download progress callback failed for {job_id}: {exc}"
                    )

    result = await client.download_media(
        message,
        file=destination,
        progress_callback=telethon_progress,
    )

    if not result or not os.path.isfile(destination):
        raise RuntimeError(
            f"Telegram storage download completed without creating {destination}."
        )

    file_size = os.path.getsize(destination)
    if file_size <= 0:
        raise RuntimeError("Telegram storage download produced an empty file.")

    if progress_callback is not None:
        try:
            callback_result = progress_callback(file_size, file_size, True, "")
            if inspect.isawaitable(callback_result):
                await callback_result
        except Exception as exc:
            print(
                f"⚠️ Final storage download progress callback failed for {job_id}: {exc}"
            )

    print(f"✅ Telegram storage download complete: {file_size} bytes")
    return destination


async def delete_stored_media(job_id, clear_reference=True):
    """
    Delete the downloaded media message for a job from Telegram storage.

    This is intended to be called only after Instagram publishing has been
    confirmed successful. The [JOB] record is preserved as permanent state;
    only the large media message is deleted. The historical media_hash is
    intentionally retained so duplicate detection continues to work after
    the video is gone.

    Returns a result dictionary. If the job has no storage_message_id, the
    function treats the media as already absent and does not fail.
    """
    await ensure_client()

    job = await get_job(job_id)

    if not job:
        raise ValueError(f"Job not found: {job_id}")

    message_id = str(job.get("storage_message_id", "")).strip()

    if not message_id:
        return {
            "deleted": False,
            "already_absent": True,
            "job": job,
        }

    try:
        message_id_int = int(message_id)
    except ValueError as exc:
        raise ValueError(
            f"Invalid storage_message_id for {job_id}: {message_id}"
        ) from exc

    # revoke=True removes the message for all channel viewers, not merely
    # hiding it locally for the authenticated Telethon account.
    await client.delete_messages(
        STORAGE_CHANNEL_ID,
        [message_id_int],
        revoke=True,
    )

    now = utc_now()

    updates = {
        "media_deleted_at": now,
    }

    if clear_reference:
        updates["storage_message_id"] = ""

    updated_job = await update_job(job_id, updates)

    return {
        "deleted": True,
        "already_absent": False,
        "telegram_message_id": message_id_int,
        "job": updated_job,
    }


async def recover_interrupted_processing():
    """
    Recover a processing job left behind by a restart and repair any
    WAITING jobs that were lost from the intake queue.

    If media was already stored, finish the job as READY.
    Otherwise put the interrupted job back at the front of intake.
    """
    async with _queue_lock:
        queue = await get_queue()
        processing_job_id = queue.get("processing_job", "")

        if not processing_job_id:
            # Also recover an orphan PROCESSING record if the queue manifest
            # was not updated before a process restart.
            jobs = await get_all_jobs()
            processing_jobs = [
                job for job in jobs
                if job.get("status") == STATUS_PROCESSING
            ]

            if processing_jobs:
                processing_jobs.sort(
                    key=lambda job: job.get(
                        "processing_started_at", ""
                    )
                )
                processing_job_id = processing_jobs[0].get("job_id")

                queue["processing_job"] = processing_job_id
                await _save_queue_unlocked(queue)

        if processing_job_id:
            job = await get_job(processing_job_id)

            if not job:
                queue["processing_job"] = ""
                await _save_queue_unlocked(queue)
            elif job.get("status") != STATUS_PROCESSING:
                queue["processing_job"] = ""
                await _save_queue_unlocked(queue)
            elif job.get("storage_message_id"):
                # Do not call finish_processing_success while holding the
                # queue lock; it acquires the same lock.
                pass
            else:
                queue["intake_queue"] = [
                    processing_job_id,
                    *[
                        item
                        for item in queue["intake_queue"]
                        if item != processing_job_id
                    ],
                ]
                queue["processing_job"] = ""
                await _save_queue_unlocked(queue)

                await update_job(
                    processing_job_id,
                    {
                        "status": STATUS_WAITING,
                        "queue_position": "1",
                        "processing_started_at": "",
                        "processing_lease_until": "",
                        "error": "",
                    },
                )

                processing_job_id = ""

    # Complete a previously uploaded job outside the queue lock.
    if processing_job_id:
        job = await get_job(processing_job_id)
        if job and job.get("status") == STATUS_PROCESSING:
            if job.get("storage_message_id"):
                return await finish_processing_success(
                    processing_job_id,
                    job["storage_message_id"],
                )

    await reconcile_queue_state()

    if processing_job_id:
        return await get_job(processing_job_id)

    return None

# ============================================================
# CONFIG FUNCTIONS
# ============================================================

async def find_config_message():
    messages = await get_storage_messages()

    for message in messages:
        text = message.text or ""

        if text.startswith(
            CONFIG_MARKER
        ):
            return message

    return None


async def get_config():
    message = await find_config_message()

    if not message:
        config = DEFAULT_CONFIG.copy()

        return await save_config(
            config
        )

    config = parse_config(
        message.text
    )

    if not config:
        raise RuntimeError(
            "BOT_CONFIG exists but "
            "could not be parsed."
        )

    config["_telegram_message_id"] = (
        message.id
    )

    return {
        **DEFAULT_CONFIG,
        **config,
    }


async def save_config(config):
    await ensure_client()

    config = {
        **DEFAULT_CONFIG,
        **config,
    }

    text = format_config(
        config
    )

    existing = await find_config_message()

    if existing:
        await _edit_message_if_changed(
            existing.id,
            text,
        )

        config["_telegram_message_id"] = (
            existing.id
        )

    else:
        message = await client.send_message(
            STORAGE_CHANNEL_ID,
            text,
        )

        config["_telegram_message_id"] = (
            message.id
        )

    return config


# ============================================================
# BOT STATE FUNCTIONS
# ============================================================

async def find_state_message():
    messages = await get_storage_messages()

    for message in messages:
        text = message.text or ""

        if text.startswith(
            STATE_MARKER
        ):
            return message

    return None


async def get_state():
    message = await find_state_message()

    if not message:
        state = DEFAULT_STATE.copy()

        return await save_state(
            state
        )

    state = parse_state(
        message.text
    )

    if not state:
        raise RuntimeError(
            "BOT_STATE exists but "
            "could not be parsed."
        )

    state["_telegram_message_id"] = (
        message.id
    )

    return {
        **DEFAULT_STATE,
        **state,
    }


async def save_state(state):
    """Persist BOT_STATE with serialized read-modify-write protection."""
    async with _state_lock:
        await ensure_client()

        state = {
            **DEFAULT_STATE,
            **state,
        }

        text = format_state(
            state
        )

        existing = await find_state_message()

        if existing:
            await _edit_message_if_changed(
                existing.id,
                text,
            )

            state["_telegram_message_id"] = (
                existing.id
            )

        else:
            message = await client.send_message(
                STORAGE_CHANNEL_ID,
                text,
            )

            state["_telegram_message_id"] = (
                message.id
            )

        return state


async def update_tick_state():
    """
    Record a worker heartbeat without changing the current worker status.

    Heartbeats are throttled so the persistent Telegram state message is not
    edited on every worker loop iteration.
    """
    global _last_heartbeat_write_monotonic

    now_monotonic = time.monotonic()
    if (
        _last_heartbeat_write_monotonic
        and now_monotonic - _last_heartbeat_write_monotonic
        < HEARTBEAT_WRITE_INTERVAL_SECONDS
    ):
        return await get_state()

    async with _state_lock:
        # Re-check after acquiring the lock because another worker may have
        # written the heartbeat while this coroutine was waiting.
        now_monotonic = time.monotonic()
        if (
            _last_heartbeat_write_monotonic
            and now_monotonic - _last_heartbeat_write_monotonic
            < HEARTBEAT_WRITE_INTERVAL_SECONDS
        ):
            return await get_state()

        await ensure_client()
        state = await get_state()
        state["last_tick_at"] = utc_now()

        state = {
            **DEFAULT_STATE,
            **state,
        }
        text = format_state(state)
        existing = await find_state_message()

        if existing:
            await _edit_message_if_changed(
                existing.id,
                text,
            )
            state["_telegram_message_id"] = existing.id
        else:
            message = await client.send_message(
                STORAGE_CHANNEL_ID,
                text,
            )
            state["_telegram_message_id"] = message.id

        _last_heartbeat_write_monotonic = now_monotonic
        return state


# ============================================================
# INITIALIZE STORAGE
# ============================================================

async def initialize_storage():
    """
    Make sure the persistent configuration,
    queue and state records exist.

    Existing JOB records are preserved.
    """

    await ensure_client()

    config = await get_config()

    # The project is fully automatic. Migrate the older approval defaults.
    if (str(config.get("mode", "")).upper() != "AUTO" or
            str(config.get("auto_publish", "")).lower() != "true"):
        config = await save_config({**config, "mode": "AUTO", "auto_publish": "true"})

    queue = await get_queue()
    state = await get_state()

    # Repair WAITING jobs that may have been left out of the queue
    # manifest by a concurrent edit or process restart.
    queue = await reconcile_queue_state()

    return {
        "config": config,
        "queue": queue,
        "state": state,
    }


# ============================================================
# STORAGE SUMMARY
# ============================================================

async def get_storage_summary():
    """
    Return a compact summary of the
    persistent Telegram database.
    """

    storage = await initialize_storage()

    jobs = await get_all_jobs()

    config = storage["config"]
    queue = storage["queue"]
    state = storage["state"]

    return {
        "job_count": len(jobs),

        "waiting_jobs": sum(
            1
            for job in jobs
            if job.get("status")
            == STATUS_WAITING
        ),

        "processing_jobs": sum(
            1
            for job in jobs
            if job.get("status")
            == STATUS_PROCESSING
        ),

        "ready_jobs": sum(
            1
            for job in jobs
            if job.get("status")
            == STATUS_READY
        ),

        "publishing_jobs": sum(
            1
            for job in jobs
            if job.get("status")
            == STATUS_PUBLISHING
        ),

        "published_jobs": sum(
            1
            for job in jobs
            if job.get("status")
            == STATUS_PUBLISHED
        ),

        "failed_jobs": sum(
            1
            for job in jobs
            if job.get("status")
            == STATUS_FAILED
        ),

        "skipped_jobs": sum(
            1
            for job in jobs
            if job.get("status")
            == STATUS_SKIPPED
        ),

        "intake_queue": queue[
            "intake_queue"
        ],

        "publishing_queue": queue[
            "publishing_queue"
        ],

        "processing_job": queue[
            "processing_job"
        ],

        "publishing_job": queue[
            "publishing_job"
        ],

        "mode": config["mode"],

        "publishing_enabled": config.get("publishing_enabled", "true"),

        "publishing_interval_minutes":
            config[
                "publishing_interval_minutes"
            ],

        "daily_post_limit":
            config["daily_post_limit"],

        "next_post_at":
            state["next_post_at"],

        "worker_status":
            state["worker_status"],

        "last_tick_at":
            state["last_tick_at"],
    }


async def get_dashboard_summary(recent_limit=5, queue_limit=10):
    """
    Return a read-only operational dashboard snapshot for the Telegram admin.

    Telegram remains the source of truth. This helper only reads persistent
    configuration, state, queues, JOB records, and storage inventory; it does
    not change job status, queues, scheduling, or stored media.
    """
    recent_limit = max(1, min(int(recent_limit), 10))
    queue_limit = max(1, min(int(queue_limit), 20))

    summary = await get_storage_summary()
    config = await get_config()
    state = await get_state()
    schedule = await get_publishing_schedule()
    storage_report = await get_storage_management_report()
    jobs = await get_all_jobs()

    jobs_sorted = sorted(
        jobs,
        key=lambda job: str(
            job.get("updated_at") or job.get("created_at") or ""
        ),
        reverse=True,
    )

    recent_jobs = []
    for job in jobs_sorted[:recent_limit]:
        recent_jobs.append({
            "job_id": job.get("job_id", ""),
            "status": job.get("status", ""),
            "created_at": job.get("created_at", ""),
            "updated_at": job.get("updated_at", ""),
            "publish_after": job.get("publish_after", ""),
            "published_at": job.get("published_at", ""),
            "error": job.get("error", "") or job.get("publishing_error", ""),
        })

    return {
        "summary": summary,
        "config": config,
        "state": state,
        "schedule": schedule,
        "recent_jobs": recent_jobs,
        "intake_queue_preview": list(summary["intake_queue"][:queue_limit]),
        "publishing_queue_preview": list(summary["publishing_queue"][:queue_limit]),
        "storage": {
            "channel_message_count": storage_report["channel_message_count"],
            "media_message_count": storage_report["media_message_count"],
            "total_media_bytes": storage_report["total_media_bytes"],
            "referenced_media_count": storage_report["referenced_media_count"],
            "referenced_media_bytes": storage_report["referenced_media_bytes"],
            "missing_referenced_media_count": len(
                storage_report["missing_referenced_media_ids"]
            ),
            "orphan_media_count": len(storage_report["orphan_media_ids"]),
            "reclaimable_jobs": len(storage_report["reclaimable_jobs"]),
            "reclaimable_media_bytes": storage_report["reclaimable_media_bytes"],
        },
    }


# ============================================================
# PHASE 17 TELEGRAM CONTROLS
# ============================================================

async def set_publishing_enabled(enabled):
    """Persist whether automatic Instagram publishing is enabled."""
    config = await get_config()
    value = "true" if bool(enabled) else "false"
    return await save_config({**config, "publishing_enabled": value})


async def set_publishing_interval(minutes):
    minutes = int(minutes)
    if minutes < 1 or minutes > 1440:
        raise ValueError("Publishing interval must be between 1 and 1440 minutes.")

    config = await get_config()
    updated = await save_config({
        **config,
        "publishing_interval_minutes": str(minutes),
    })

    queue = await get_queue()
    if queue.get("publishing_queue") and not queue.get("publishing_job"):
        first_job_id = queue["publishing_queue"][0]
        first_job = await get_job(first_job_id)
        if first_job and first_job.get("status") == STATUS_READY:
            await update_job(first_job_id, {"publish_after": ""})
        state = await get_state()
        state["next_post_at"] = ""
        await save_state(state)
        await ensure_publishing_schedule()

    return updated


async def set_daily_post_limit(limit):
    limit = int(limit)
    if limit < 0 or limit > 1000:
        raise ValueError("Daily post limit must be between 0 and 1000. Use 0 for unlimited.")

    config = await get_config()
    updated = await save_config({
        **config,
        "daily_post_limit": str(limit),
    })

    await reschedule_publishing_queue()
    return updated


async def set_posting_window(enabled, start="04:00", end="23:30"):
    def strict_clock(value):
        raw = str(value or "").strip()
        try:
            hour_text, minute_text = raw.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except (TypeError, ValueError):
            raise ValueError("Time must use HH:MM format, for example 04:00.")
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Time must use HH:MM format, for example 04:00.")
        return hour * 60 + minute

    start_minutes = strict_clock(start)
    end_minutes = strict_clock(end)

    def normalized_clock(minutes):
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    config = await get_config()
    updated = await save_config({
        **config,
        "posting_window_enabled": "true" if enabled else "false",
        "posting_window_start": normalized_clock(start_minutes),
        "posting_window_end": normalized_clock(end_minutes),
    })

    await reschedule_publishing_queue()
    return updated


async def reschedule_publishing_queue():
    """Recalculate the first READY publishing slot after a schedule change."""
    queue = await get_queue()
    if queue.get("publishing_job"):
        return (await get_state()).get("next_post_at", "")

    publishing_queue = list(queue.get("publishing_queue", []))
    if not publishing_queue:
        state = await get_state()
        state["next_post_at"] = ""
        await save_state(state)
        return None

    first_job_id = publishing_queue[0]
    first_job = await get_job(first_job_id)
    if not first_job or first_job.get("status") != STATUS_READY:
        return await ensure_publishing_schedule()

    await update_job(first_job_id, {"publish_after": ""})
    state = await get_state()
    state["next_post_at"] = ""
    await save_state(state)
    return await ensure_publishing_schedule()


async def prioritize_publishing_job(job_id):
    """Move a READY job to the front of the persistent publishing queue."""
    async with _queue_lock:
        queue = await get_queue()
        if job_id not in queue.get("publishing_queue", []):
            queue.setdefault("publishing_queue", []).insert(0, job_id)
        else:
            queue["publishing_queue"] = [
                job_id,
                *[item for item in queue["publishing_queue"] if item != job_id],
            ]
        return await _save_queue_unlocked(queue)


async def retry_job(job_id):
    """Manually retry a FAILED processing or publishing job."""
    job = await get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")

    if job.get("status") != STATUS_FAILED:
        raise ValueError(
            f"{job_id} is {job.get('status')}, not FAILED. Only FAILED jobs can be manually retried."
        )

    # A stored media message means processing succeeded previously and the
    # failure happened in the publishing stage. Resume from READY.
    if str(job.get("storage_message_id", "")).strip():
        updated = await update_job(
            job_id,
            {
                "status": STATUS_READY,
                "publishing_lease_until": "",
                "publishing_error": "",
                "publishing_retry_count": "0",
                "error": "",
                "publish_after": "",
            },
        )
        await prioritize_publishing_job(job_id)
        await ensure_publishing_schedule()
        return updated

    # No stored media means the processing stage failed. Put the original URL
    # back at the front of the intake queue.
    updated = await update_job(
        job_id,
        {
            "status": STATUS_WAITING,
            "queue_position": "",
            "processing_started_at": "",
            "processing_completed_at": "",
            "processing_lease_until": "",
            "retry_count": "0",
            "error": "",
        },
    )

    async with _queue_lock:
        queue = await get_queue()
        queue["intake_queue"] = [
            job_id,
            *[item for item in queue.get("intake_queue", []) if item != job_id],
        ]
        await _save_queue_unlocked(queue)

    await refresh_intake_positions()
    return updated


async def cancel_job(job_id):
    """Cancel a non-active job and remove it from its active queue."""
    job = await get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")

    status = job.get("status")
    if status in (STATUS_PROCESSING, STATUS_PUBLISHING):
        raise ValueError(f"{job_id} is currently {status} and cannot be cancelled safely.")
    if status == STATUS_PUBLISHED:
        raise ValueError(f"{job_id} is already PUBLISHED and cannot be cancelled.")
    if status == STATUS_SKIPPED:
        return job

    if status in (STATUS_WAITING, STATUS_FAILED):
        await remove_job_from_intake(job_id, mark_skipped=False)
    if status in (STATUS_READY, STATUS_FAILED):
        await remove_job_from_publishing(job_id)

    updated = await update_job(
        job_id,
        {
            "status": STATUS_SKIPPED,
            "queue_position": "",
            "publishing_lease_until": "",
            "publish_after": "",
            "error": "Cancelled by Telegram admin.",
        },
    )

    if status == STATUS_READY:
        await reschedule_publishing_queue()
    return updated


async def request_publish_now(job_id):
    """Move a READY job to the front and make its slot immediately due."""
    job = await get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")
    if job.get("status") != STATUS_READY:
        raise ValueError(f"{job_id} must be READY to publish now; current status is {job.get('status')}.")

    config = await get_config()
    if str(config.get("publishing_enabled", "true")).lower() != "true":
        raise ValueError("Automatic publishing is paused. Use /resume before /publish_now.")

    await prioritize_publishing_job(job_id)
    updated = await update_job(
        job_id,
        {"publish_after": datetime.now(timezone.utc).isoformat()},
    )
    state = await get_state()
    state["next_post_at"] = updated.get("publish_after", "")
    await save_state(state)
    return updated


# ============================================================
# PHASE 18 STORAGE MANAGEMENT
# ============================================================

async def _message_media_size(message):
    """Return a Telegram message media size when Telegram exposes one."""
    try:
        file_obj = getattr(message, "file", None)
        size = getattr(file_obj, "size", None) if file_obj else None
        if size is not None:
            return int(size)
    except (TypeError, ValueError):
        pass
    return 0


async def get_storage_management_report():
    """
    Audit the private Telegram storage channel without deleting anything.

    Telegram remains the source of truth. This report distinguishes persistent
    JOB records from media messages and identifies media that is still needed,
    safely reclaimable, missing, or not referenced by any JOB record.
    """
    await ensure_client()

    messages = await get_storage_messages()
    jobs = []
    referenced_media_ids = set()
    media_messages = []
    total_media_bytes = 0

    for message in messages:
        text = message.text or ""
        job = parse_job(text)
        if job:
            job["_telegram_message_id"] = message.id
            jobs.append(job)

        if getattr(message, "media", None):
            size = await _message_media_size(message)
            media_messages.append({
                "message_id": int(message.id),
                "size": size,
                "date": getattr(message, "date", None),
            })
            total_media_bytes += size

    for job in jobs:
        storage_message_id = str(
            job.get("storage_message_id", "")
        ).strip()
        if storage_message_id:
            try:
                referenced_media_ids.add(int(storage_message_id))
            except ValueError:
                pass

    all_message_ids = {int(message.id) for message in messages}
    existing_media_ids = {
        item["message_id"] for item in media_messages
    }
    missing_media_ids = sorted(
        referenced_media_ids - all_message_ids
    )
    orphan_media_ids = sorted(
        existing_media_ids - referenced_media_ids
    )

    reclaimable_jobs = []
    protected_media_ids = set()
    active_statuses = {
        STATUS_WAITING,
        STATUS_PROCESSING,
        STATUS_READY,
        STATUS_PUBLISHING,
    }

    for job in jobs:
        storage_message_id = str(
            job.get("storage_message_id", "")
        ).strip()
        if not storage_message_id:
            continue

        try:
            media_id = int(storage_message_id)
        except ValueError:
            continue

        status = job.get("status", "")
        if status in active_statuses:
            protected_media_ids.add(media_id)
            continue

        # FAILED jobs can still be manually retried. Their media must remain
        # available, so they are deliberately excluded from cleanup.
        if status == STATUS_FAILED:
            protected_media_ids.add(media_id)
            continue

        # Only terminal jobs are considered reclaimable. This includes
        # PUBLISHED and SKIPPED jobs whose media was not cleaned up previously.
        if status in {STATUS_PUBLISHED, STATUS_SKIPPED}:
            # A missing Telegram media message is an integrity issue, not a
            # deletion candidate. Leave it in the missing-media report.
            if media_id not in existing_media_ids:
                continue

            reclaimable_jobs.append({
                "job_id": job.get("job_id", ""),
                "status": status,
                "storage_message_id": storage_message_id,
                "media_size": next(
                    (
                        item["size"]
                        for item in media_messages
                        if item["message_id"] == media_id
                    ),
                    0,
                ),
            })

    reclaimable_ids = {
        int(item["storage_message_id"])
        for item in reclaimable_jobs
        if str(item.get("storage_message_id", "")).isdigit()
    }

    return {
        "channel_message_count": len(messages),
        "job_record_count": len(jobs),
        "media_message_count": len(media_messages),
        "total_media_bytes": total_media_bytes,
        "referenced_media_count": len(referenced_media_ids),
        "referenced_media_bytes": sum(
            item["size"]
            for item in media_messages
            if item["message_id"] in referenced_media_ids
        ),
        "missing_referenced_media_ids": missing_media_ids,
        "orphan_media_ids": orphan_media_ids,
        "protected_media_count": len(protected_media_ids),
        "reclaimable_jobs": reclaimable_jobs,
        "reclaimable_media_count": len(reclaimable_ids),
        "reclaimable_media_bytes": sum(
            item["media_size"] for item in reclaimable_jobs
        ),
        "config_message_count": sum(
            1 for message in messages
            if (message.text or "").startswith(CONFIG_MARKER)
        ),
        "state_message_count": sum(
            1 for message in messages
            if (message.text or "").startswith(STATE_MARKER)
        ),
        "queue_manifest_count": sum(
            1 for message in messages
            if (message.text or "").startswith(QUEUE_MARKER)
        ),
    }


async def cleanup_reclaimable_storage_media(
    job_id="",
    confirm=False,
):
    """
    Delete only media belonging to terminal jobs that no longer need it.

    Safety rules:
    - dry-run unless confirm=True;
    - only PUBLISHED or SKIPPED jobs are eligible;
    - WAITING/PROCESSING/READY/PUBLISHING/FAILED media is never deleted;
    - queue/config/state records are never deleted;
    - the existing delete_stored_media() path records media_deleted_at and
      clears the media reference after successful deletion.
    """
    report = await get_storage_management_report()
    candidates = list(report["reclaimable_jobs"])

    if job_id:
        normalized = str(job_id).strip().upper()
        candidates = [
            item for item in candidates
            if str(item.get("job_id", "")).upper() == normalized
        ]
        if not candidates:
            job = await get_job(normalized)
            if not job:
                raise ValueError(f"Job not found: {normalized}")
            raise ValueError(
                f"{normalized} is not a terminal storage-cleanup candidate. "
                "Only PUBLISHED or SKIPPED jobs with retained media can be cleaned."
            )

    if not confirm:
        return {
            "confirmed": False,
            "deleted": [],
            "failed": [],
            "candidates": candidates,
            "orphan_media_ids": report["orphan_media_ids"],
        }

    deleted = []
    failed = []

    for candidate in candidates:
        current_job_id = candidate["job_id"]
        try:
            result = await delete_stored_media(
                current_job_id,
                clear_reference=True,
            )
            deleted.append({
                "job_id": current_job_id,
                "telegram_message_id": result.get("telegram_message_id"),
                "media_size": candidate.get("media_size", 0),
            })
        except Exception as exc:
            failed.append({
                "job_id": current_job_id,
                "error": str(exc),
            })

    return {
        "confirmed": True,
        "deleted": deleted,
        "failed": failed,
        "candidates": candidates,
        "orphan_media_ids": report["orphan_media_ids"],
    }



# ============================================================
# CLEAN SHUTDOWN
# ============================================================

async def close_storage():
    if client.is_connected():
        await client.disconnect()
