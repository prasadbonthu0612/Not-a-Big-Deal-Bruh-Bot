#bot.py
import asyncio
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import telegram_storage
import instagram_downloader
import groq_ai
import instagram_publisher
import public_media_server


# =========================
# CONFIG
# =========================

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()
POSTING_TIMEZONE = os.getenv("POSTING_TIMEZONE", "Asia/Kolkata")
try:
    DISPLAY_TIMEZONE = ZoneInfo(POSTING_TIMEZONE)
except Exception:
    DISPLAY_TIMEZONE = ZoneInfo("Asia/Kolkata")


# =========================
# PHASE 17 ADMIN ACCESS
# =========================

async def _get_admin_chat_id():
    """Resolve the persistent admin chat ID without exposing credentials."""
    if TELEGRAM_ADMIN_CHAT_ID:
        return TELEGRAM_ADMIN_CHAT_ID

    config = await telegram_storage.get_config()
    configured = str(config.get("admin_chat_id", "")).strip()
    if configured:
        return configured

    # Backward-compatible bootstrap: if existing jobs all came from exactly
    # one Telegram chat, persist that chat as the admin. This avoids requiring
    # a .env change for an already-private, single-user bot.
    jobs = await telegram_storage.get_all_jobs()
    chat_ids = sorted({
        str(job.get("status_chat_id", "")).strip()
        for job in jobs
        if str(job.get("status_chat_id", "")).strip()
    })

    if len(chat_ids) == 1:
        config = await telegram_storage.save_config({
            **config,
            "admin_chat_id": chat_ids[0],
        })
        return str(config.get("admin_chat_id", chat_ids[0])).strip()

    return ""


async def _is_storage_channel_update(update: Update):
    """Return True when an update originated from the persistent storage channel.

    The storage channel contains the bot's own [JOB], [QUEUE_MANIFEST],
    [BOT_CONFIG], and media records. Those messages must never be treated as
    admin commands or intake URLs.
    """
    chat = update.effective_chat
    if not chat or not STORAGE_CHANNEL_ID:
        return False

    return str(chat.id) == str(STORAGE_CHANNEL_ID)


async def _require_admin(update: Update):
    chat = update.effective_chat
    if not chat:
        return False

    # Never answer inside the persistent storage channel. The bot itself writes
    # Instagram URLs into [JOB] records there, so replying with an admin error
    # would pollute persistent storage and create a feedback loop.
    if await _is_storage_channel_update(update):
        return False

    admin_chat_id = await _get_admin_chat_id()
    if admin_chat_id and str(chat.id) == admin_chat_id:
        return True

    await update.effective_message.reply_text(
        "⛔ Admin access required.\n\n"
        "Set TELEGRAM_ADMIN_CHAT_ID in .env to the Telegram chat ID "
        "that is allowed to control this bot."
    )
    return False


# =========================
# /start
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "🤖 Instagram Auto Poster\n\n"
        "Bot is online.\n"
        "Telegram persistent storage is connected.\n\n"
        "Send an Instagram Reel URL to add it to the intake queue."
    )


# =========================
# /help
# =========================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "Available commands:\n\n"
        "/start - Start the bot\n"
        "/help - Show help\n"
        "/status - Full automation status\n"
        "/dashboard - Read-only operations dashboard\n"
        "/queue - Show the intake and publishing queues\n"
        "/pause - Pause automatic Instagram publishing\n"
        "/resume - Resume automatic Instagram publishing\n"
        "/interval [minutes] - Show/change publishing interval\n"
        "/limit [count] - Show/change daily publishing limit (0 = unlimited)\n"
        "/window - Show posting window\n"
        "/window on HH:MM HH:MM - Enable/change posting window\n"
        "/window off - Disable posting window\n"
        "/schedule - Show publishing schedule\n"
        "/retry JOB-XXXXXX - Retry a failed job\n"
        "/cancel JOB-XXXXXX - Cancel a waiting/ready job\n"
        "/publish_now JOB-XXXXXX - Publish a READY job as soon as allowed\n"
        "/remove JOB-XXXXXX - Remove a WAITING job\n"
        "/skip JOB-XXXXXX - Skip a WAITING job\n"
        "/test_instagram - Test Instagram Login API credentials\n"
        "/test_storage - Test storage channel\n"
        "/storage_info - Check storage channel access\n"
        "/storage_status - Show persistent automation state\n"
        "/storage_report - Audit Telegram storage usage and retained media\n"
        "/storage_cleanup - Preview reclaimable terminal-job media\n"
        "/storage_cleanup confirm - Delete reclaimable terminal-job media\n"
        "/storage_cleanup JOB-XXXXXX - Preview cleanup for one terminal job\n"
        "/storage_cleanup JOB-XXXXXX confirm - Clean one terminal job's media\n\n"
        "Processing remains automatic. Telegram is the persistent control plane."
    )


# =========================
# /test_instagram
# =========================

async def test_instagram_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """Verify the Instagram Login token/user ID without exposing the token."""
    if not await _require_admin(update):
        return

    status_message = await update.message.reply_text(
        "🔎 Testing Instagram Login API connection..."
    )

    try:
        result = await instagram_publisher.test_connection()
        await status_message.edit_text(
            "✅ INSTAGRAM API CONNECTION WORKS!\n\n"
            f"Username: @{result.get('username') or '(not returned)'}\n"
            f"Instagram User ID: {result.get('id') or '(not returned)'}\n"
            f"API version: {result.get('api_version')}\n"
            f"API base: {result.get('api_base')}\n\n"
            "🔐 Access token was not displayed."
        )
    except Exception as e:
        await status_message.edit_text(
            "❌ INSTAGRAM API TEST FAILED\n\n"
            f"Error: {type(e).__name__}\n"
            f"{str(e)[:1500]}"
        )


# =========================
# /storage_info
# =========================

async def storage_info(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    if not STORAGE_CHANNEL_ID:
        await update.message.reply_text(
            "❌ STORAGE_CHANNEL_ID is missing from .env"
        )
        return

    try:
        chat = await context.bot.get_chat(
            chat_id=int(STORAGE_CHANNEL_ID)
        )

        await update.message.reply_text(
            "✅ Storage channel found!\n\n"
            f"Name: {chat.title}\n"
            f"ID: {chat.id}\n"
            f"Type: {chat.type}"
        )

    except Exception as e:
        print(
            f"❌ Storage info error: {e}"
        )

        await update.message.reply_text(
            "❌ Telegram cannot access "
            "the storage channel.\n\n"
            f"Error: {e}"
        )


# =========================
# /test_storage
# =========================

async def test_storage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    if not STORAGE_CHANNEL_ID:
        await update.message.reply_text(
            "❌ STORAGE_CHANNEL_ID is missing from .env"
        )
        return

    try:
        await context.bot.send_message(
            chat_id=int(STORAGE_CHANNEL_ID),
            text=(
                "🧪 STORAGE TEST 001\n\n"
                "Telegram storage connection is working."
            )
        )

        await update.message.reply_text(
            "✅ Storage channel test successful."
        )

    except Exception as e:
        print(
            f"❌ Storage test error: {e}"
        )

        await update.message.reply_text(
            "❌ Storage test failed.\n\n"
            f"{e}"
        )


# =========================
# /interval
# =========================

async def interval_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    try:
        if not context.args:
            schedule = await telegram_storage.get_publishing_schedule()
            await update.message.reply_text(
                "⏱️ PUBLISHING INTERVAL\n\n"
                f"Current interval: {schedule['interval_minutes']} minutes\n"
                f"Next scheduled post: {schedule['next_post_at'] or 'Not scheduled'}\n\n"
                "Use /interval <minutes> to change it."
            )
            return

        if len(context.args) != 1:
            await update.message.reply_text(
                "Usage: /interval <minutes>\n\nExample: /interval 30"
            )
            return

        try:
            minutes = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "❌ Interval must be a whole number of minutes."
            )
            return

        await telegram_storage.set_publishing_interval(minutes)
        schedule = await telegram_storage.get_publishing_schedule()

        await update.message.reply_text(
            "✅ Publishing interval updated and persisted.\n\n"
            f"Interval: every {schedule['interval_minutes']} minutes\n"
            f"Next scheduled post: {schedule['next_post_at'] or 'Not scheduled'}"
        )

    except Exception as e:
        print(f"❌ Interval command error: {e}")
        await update.message.reply_text(
            "❌ Failed to update the publishing interval.\n\n"
            f"Error: {e}"
        )


# =========================
# /schedule
# =========================

async def schedule_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """Show the active interval and daily posting window."""
    if not await _require_admin(update):
        return

    try:
        config = await telegram_storage.get_config()
        schedule = await telegram_storage.get_publishing_schedule()

        window_enabled = str(
            config.get("posting_window_enabled", "true")
        ).lower() == "true"

        window_text = (
            f"{config.get('posting_window_start', '04:00')} - "
            f"{config.get('posting_window_end', '23:30')} "
            f"({config.get('posting_timezone', 'Asia/Kolkata')})"
            if window_enabled
            else "Disabled"
        )

        try:
            daily_limit = int(str(config.get("daily_post_limit", "0")))
        except (TypeError, ValueError):
            daily_limit = 0

        limit_text = "Unlimited" if daily_limit <= 0 else str(daily_limit)

        enabled = str(config.get("publishing_enabled", "true")).lower() == "true"
        await update.message.reply_text(
            "⏰ PUBLISHING SCHEDULE\n\n"
            f"Publishing: {'Enabled' if enabled else 'Paused'}\n"
            f"Interval: every {schedule['interval_minutes']} minutes\n"
            f"Posting window: {window_text}\n"
            f"Daily limit: {limit_text}\n"
            f"Next scheduled post: {schedule['next_post_at'] or 'Not scheduled'}"
        )
    except Exception as e:
        print(f"❌ Schedule command error: {e}")
        await update.message.reply_text(
            "❌ Failed to read publishing schedule.\n\n"
            f"Error: {e}"
        )


# =========================
# PHASE 19 DASHBOARD
# =========================

async def dashboard_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """Show a read-only operational dashboard in Telegram."""
    if not await _require_admin(update):
        return

    status_message = await update.message.reply_text(
        "🔄 Building automation dashboard..."
    )

    try:
        dashboard = await telegram_storage.get_dashboard_summary(
            recent_limit=5,
            queue_limit=10,
        )

        summary = dashboard["summary"]
        config = dashboard["config"]
        state = dashboard["state"]
        schedule = dashboard["schedule"]
        storage = dashboard["storage"]

        enabled = (
            str(config.get("publishing_enabled", "true")).lower()
            == "true"
        )
        window_enabled = telegram_storage.posting_window_enabled(config)
        window_text = (
            f"{config.get('posting_window_start', '04:00')} - "
            f"{config.get('posting_window_end', '23:30')} "
            f"({config.get('posting_timezone', 'Asia/Kolkata')})"
            if window_enabled
            else "Disabled"
        )

        limit = telegram_storage.daily_post_limit_from_config(config)
        local_date = telegram_storage.publishing_window_status(config)["local_date"]
        used = telegram_storage.daily_posts_used(state, local_date)
        limit_text = "Unlimited" if limit == 0 else f"{used}/{limit}"

        intake = dashboard["intake_queue_preview"]
        publishing = dashboard["publishing_queue_preview"]

        recent_lines = []
        for job in dashboard["recent_jobs"]:
            status = job["status"] or "UNKNOWN"
            line = f"• {job['job_id']} — {status}"
            if job.get("error"):
                line += f" — {str(job['error'])[:80]}"
            recent_lines.append(line)
        if not recent_lines:
            recent_lines.append("• None")

        intake_text = ", ".join(intake) if intake else "Empty"
        publishing_text = ", ".join(publishing) if publishing else "Empty"

        dashboard_text = (
            "📊 INSTAGRAM AUTO POSTER DASHBOARD\n\n"
            "SYSTEM\n"
            f"• Publishing: {'🟢 ENABLED' if enabled else '⏸️ PAUSED'}\n"
            f"• Worker: {summary['worker_status']}\n"
            f"• Mode: {summary['mode']}\n"
            f"• Last worker tick: {summary['last_tick_at'] or 'Never'}\n\n"

            "QUEUES\n"
            f"• Intake: {len(summary['intake_queue'])}\n"
            f"• Processing: {summary['processing_job'] or 'None'}\n"
            f"• Publishing: {len(summary['publishing_queue'])}\n"
            f"• Currently publishing: {summary['publishing_job'] or 'None'}\n"
            f"• Intake preview: {intake_text}\n"
            f"• Publishing preview: {publishing_text}\n\n"

            "SCHEDULE\n"
            f"• Interval: {schedule['interval_minutes']} min\n"
            f"• Daily limit: {limit_text}\n"
            f"• Window: {window_text}\n"
            f"• Next post: {schedule['next_post_at'] or 'Not scheduled'}\n\n"

            "JOBS\n"
            f"• Total: {summary['job_count']}\n"
            f"• Waiting: {summary['waiting_jobs']}\n"
            f"• Processing: {summary['processing_jobs']}\n"
            f"• Ready: {summary['ready_jobs']}\n"
            f"• Publishing: {summary['publishing_jobs']}\n"
            f"• Published: {summary['published_jobs']}\n"
            f"• Failed: {summary['failed_jobs']}\n"
            f"• Skipped: {summary['skipped_jobs']}\n\n"

            "STORAGE\n"
            f"• Channel messages: {storage['channel_message_count']}\n"
            f"• Media messages: {storage['media_message_count']}\n"
            f"• Total media: {telegram_storage.format_storage_bytes(storage['total_media_bytes'])}\n"
            f"• Referenced media: {storage['referenced_media_count']} "
            f"({telegram_storage.format_storage_bytes(storage['referenced_media_bytes'])})\n"
            f"• Missing referenced media: {storage['missing_referenced_media_count']}\n"
            f"• Orphan media: {storage['orphan_media_count']}\n"
            f"• Reclaimable jobs: {storage['reclaimable_jobs']} "
            f"({telegram_storage.format_storage_bytes(storage['reclaimable_media_bytes'])})\n\n"

            "RECENT JOBS\n"
            + "\n".join(recent_lines)
        )

        # Keep a hard safety margin below Telegram's 4096-character limit.
        if len(dashboard_text) > 3900:
            dashboard_text = dashboard_text[:3890] + "\n…"

        await status_message.edit_text(dashboard_text)

    except Exception as e:
        print(f"❌ Dashboard command error: {e}")
        await status_message.edit_text(
            "❌ Failed to build automation dashboard.\n\n"
            f"Error: {e}"
        )


# =========================
# PHASE 17 TELEGRAM CONTROLS
# =========================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    status_message = await update.message.reply_text(
        "🔄 Reading automation status..."
    )

    try:
        summary = await telegram_storage.get_storage_summary()
        config = await telegram_storage.get_config()
        state = await telegram_storage.get_state()
        schedule = await telegram_storage.get_publishing_schedule()

        enabled = str(config.get("publishing_enabled", "true")).lower() == "true"
        limit = telegram_storage.daily_post_limit_from_config(config)
        window_enabled = telegram_storage.posting_window_enabled(config)
        window = (
            f"{config.get('posting_window_start', '04:00')} - "
            f"{config.get('posting_window_end', '23:30')} "
            f"({config.get('posting_timezone', 'Asia/Kolkata')})"
            if window_enabled else "Disabled"
        )

        local_date = telegram_storage.publishing_window_status(config)["local_date"]
        used = telegram_storage.daily_posts_used(state, local_date)
        limit_text = "Unlimited" if limit == 0 else f"{used}/{limit}"

        await status_message.edit_text(
            "📊 INSTAGRAM AUTO POSTER STATUS\n\n"
            f"Publishing: {'🟢 ENABLED' if enabled else '⏸️ PAUSED'}\n"
            f"Worker: {summary['worker_status']}\n"
            f"Mode: {summary['mode']}\n\n"
            "Queues\n"
            f"• Intake: {len(summary['intake_queue'])}\n"
            f"• Processing: {summary['processing_job'] or 'None'}\n"
            f"• Publishing queue: {len(summary['publishing_queue'])}\n"
            f"• Currently publishing: {summary['publishing_job'] or 'None'}\n\n"
            "Publishing rules\n"
            f"• Interval: {schedule['interval_minutes']} min\n"
            f"• Daily limit: {limit_text}\n"
            f"• Window: {window}\n"
            f"• Next post: {schedule['next_post_at'] or 'Not scheduled'}\n\n"
            "Jobs\n"
            f"• Total: {summary['job_count']}\n"
            f"• Waiting: {summary['waiting_jobs']}\n"
            f"• Ready: {summary['ready_jobs']}\n"
            f"• Published: {summary['published_jobs']}\n"
            f"• Failed: {summary['failed_jobs']}\n"
            f"• Skipped: {summary['skipped_jobs']}"
        )
    except Exception as e:
        print(f"❌ Status command error: {e}")
        await status_message.edit_text(
            "❌ Failed to read automation status.\n\n"
            f"Error: {e}"
        )


async def pause_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    try:
        config = await telegram_storage.set_publishing_enabled(False)
        await update.message.reply_text(
            "⏸️ Publishing paused.\n\n"
            "New Reels can still be processed and placed in the publishing queue. "
            "Instagram publishing will remain paused until /resume.\n\n"
            f"Persistent setting: {config.get('publishing_enabled')}"
        )
    except Exception as e:
        print(f"❌ Pause command error: {e}")
        await update.message.reply_text(f"❌ Failed to pause publishing.\n\nError: {e}")


async def resume_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    try:
        config = await telegram_storage.set_publishing_enabled(True)
        next_post = await telegram_storage.ensure_publishing_schedule()
        await update.message.reply_text(
            "▶️ Publishing resumed.\n\n"
            f"Next scheduled post: {next_post or 'Not scheduled'}\n"
            f"Persistent setting: {config.get('publishing_enabled')}"
        )
    except Exception as e:
        print(f"❌ Resume command error: {e}")
        await update.message.reply_text(f"❌ Failed to resume publishing.\n\nError: {e}")


async def limit_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    try:
        config = await telegram_storage.get_config()
        if not context.args:
            limit = telegram_storage.daily_post_limit_from_config(config)
            await update.message.reply_text(
                "📈 DAILY POST LIMIT\n\n"
                f"Current limit: {'Unlimited' if limit == 0 else limit}\n\n"
                "Use /limit <count> where 0 means unlimited."
            )
            return

        if len(context.args) != 1:
            raise ValueError("Usage: /limit <count>\nExample: /limit 10\nUse 0 for unlimited.")

        limit = int(context.args[0])
        await telegram_storage.set_daily_post_limit(limit)
        config = await telegram_storage.get_config()
        await update.message.reply_text(
            "✅ Daily publishing limit updated and persisted.\n\n"
            f"Limit: {'Unlimited' if telegram_storage.daily_post_limit_from_config(config) == 0 else config.get('daily_post_limit')}"
        )
    except Exception as e:
        print(f"❌ Limit command error: {e}")
        await update.message.reply_text(f"❌ Failed to update daily limit.\n\nError: {e}")


async def window_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    try:
        config = await telegram_storage.get_config()
        if not context.args:
            enabled = telegram_storage.posting_window_enabled(config)
            text = (
                f"{config.get('posting_window_start', '04:00')} - "
                f"{config.get('posting_window_end', '23:30')} "
                f"({config.get('posting_timezone', 'Asia/Kolkata')})"
                if enabled else "Disabled"
            )
            await update.message.reply_text(
                "⏰ POSTING WINDOW\n\n"
                f"Current: {text}\n\n"
                "Enable/change: /window on HH:MM HH:MM\n"
                "Disable: /window off"
            )
            return

        mode = context.args[0].lower()
        if mode == "off":
            if len(context.args) != 1:
                raise ValueError("Usage: /window off")
            await telegram_storage.set_posting_window(
                False,
                config.get("posting_window_start", "04:00"),
                config.get("posting_window_end", "23:30"),
            )
        elif mode == "on":
            if len(context.args) != 3:
                raise ValueError("Usage: /window on HH:MM HH:MM")
            start, end = context.args[1], context.args[2]
            _validate_clock(start)
            _validate_clock(end)
            await telegram_storage.set_posting_window(True, start, end)
        elif len(context.args) == 2:
            start, end = context.args
            _validate_clock(start)
            _validate_clock(end)
            await telegram_storage.set_posting_window(True, start, end)
        else:
            raise ValueError("Usage: /window on HH:MM HH:MM or /window off")

        config = await telegram_storage.get_config()
        await update.message.reply_text(
            "✅ Posting window updated and persisted.\n\n"
            f"Window: {config.get('posting_window_start')} - {config.get('posting_window_end')}\n"
            f"Enabled: {config.get('posting_window_enabled')}\n"
            f"Timezone: {config.get('posting_timezone')}"
        )
    except Exception as e:
        print(f"❌ Window command error: {e}")
        await update.message.reply_text(f"❌ Failed to update posting window.\n\nError: {e}")


def _validate_clock(value):
    raw = str(value or "").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        raise ValueError("Time must use HH:MM format, for example 04:00.")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Time must use HH:MM format, for example 04:00.")


async def retry_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /retry JOB-000004")
        return

    job_id = context.args[0].strip().upper()
    try:
        job = await telegram_storage.retry_job(job_id)
        await update.message.reply_text(
            f"🔁 {job_id} queued for manual retry.\n\n"
            f"New status: {job.get('status')}"
        )
    except Exception as e:
        print(f"❌ Retry command error: {e}")
        await update.message.reply_text(f"❌ Retry failed.\n\nError: {e}")


async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /cancel JOB-000004")
        return

    job_id = context.args[0].strip().upper()
    try:
        job = await telegram_storage.cancel_job(job_id)
        await update.message.reply_text(
            f"🛑 {job_id} cancelled.\n\nStatus: {job.get('status')}"
        )
    except Exception as e:
        print(f"❌ Cancel command error: {e}")
        await update.message.reply_text(f"❌ Cancel failed.\n\nError: {e}")


async def publish_now_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /publish_now JOB-000004")
        return

    job_id = context.args[0].strip().upper()
    try:
        job = await telegram_storage.request_publish_now(job_id)
        await update.message.reply_text(
            f"🚀 {job_id} marked for immediate publishing.\n\n"
            "The normal posting window and daily limit still apply.\n"
            f"Status: {job.get('status')}"
        )
    except Exception as e:
        print(f"❌ Publish-now command error: {e}")
        await update.message.reply_text(f"❌ Publish-now failed.\n\nError: {e}")


# =========================
# /storage_status
# =========================

async def storage_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    status_message = await update.message.reply_text(
        "🔄 Reading persistent Telegram storage..."
    )

    try:
        summary = (
            await telegram_storage
            .get_storage_summary()
        )

        intake = summary["intake_queue"]
        publishing = summary["publishing_queue"]

        intake_text = (
            ", ".join(intake)
            if intake
            else "Empty"
        )

        publishing_text = (
            ", ".join(publishing)
            if publishing
            else "Empty"
        )

        await status_message.edit_text(
            "📦 PERSISTENT STORAGE STATUS\n\n"

            "Jobs\n"
            f"• Total: {summary['job_count']}\n"
            f"• Waiting: {summary['waiting_jobs']}\n"
            f"• Processing: {summary['processing_jobs']}\n"
            f"• Ready: {summary['ready_jobs']}\n"
            f"• Publishing: {summary['publishing_jobs']}\n"
            f"• Published: {summary['published_jobs']}\n"
            f"• Failed: {summary['failed_jobs']}\n"
            f"• Skipped: {summary['skipped_jobs']}\n\n"

            "Queues\n"
            f"• Intake: {intake_text}\n"
            f"• Publishing: {publishing_text}\n"
            f"• Processing job: "
            f"{summary['processing_job'] or 'None'}\n"
            f"• Publishing job: "
            f"{summary['publishing_job'] or 'None'}\n\n"

            "Configuration\n"
            f"• Mode: {summary['mode']}\n"
            f"• Interval: "
            f"{summary['publishing_interval_minutes']} min\n"
            f"• Daily limit: "
            f"{summary['daily_post_limit']}\n\n"

            "Worker\n"
            f"• Status: "
            f"{summary['worker_status']}\n"
            f"• Next post: "
            f"{summary['next_post_at'] or 'Not scheduled'}\n"
            f"• Last tick: "
            f"{summary['last_tick_at'] or 'Never'}"
        )

    except Exception as e:
        print(
            f"❌ Storage status error: {e}"
        )

        await status_message.edit_text(
            "❌ Failed to read persistent storage.\n\n"
            f"Error: {e}"
        )


# =========================
# PHASE 18 STORAGE MANAGEMENT
# =========================

async def storage_report_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    status_message = await update.message.reply_text(
        "🔄 Auditing Telegram storage..."
    )

    try:
        report = await telegram_storage.get_storage_management_report()
        fmt = telegram_storage.format_storage_bytes

        missing = report["missing_referenced_media_ids"]
        orphans = report["orphan_media_ids"]
        reclaimable = report["reclaimable_jobs"]

        missing_text = (
            ", ".join(str(item) for item in missing[:10])
            if missing else "None"
        )
        orphan_text = (
            ", ".join(str(item) for item in orphans[:10])
            if orphans else "None"
        )

        if len(missing) > 10:
            missing_text += f" (+{len(missing) - 10} more)"
        if len(orphans) > 10:
            orphan_text += f" (+{len(orphans) - 10} more)"

        reclaimable_bytes = report["reclaimable_media_bytes"]
        reclaimable_lines = []
        for item in reclaimable[:15]:
            reclaimable_lines.append(
                f"• {item['job_id']} ({item['status']}) — "
                f"{fmt(item['media_size'])}"
            )
        if len(reclaimable) > 15:
            reclaimable_lines.append(
                f"• ...and {len(reclaimable) - 15} more"
            )
        if not reclaimable_lines:
            reclaimable_lines.append("None")

        await status_message.edit_text(
            "📦 TELEGRAM STORAGE REPORT\n\n"
            "Inventory\n"
            f"• Channel messages: {report['channel_message_count']}\n"
            f"• JOB records: {report['job_record_count']}\n"
            f"• Media messages: {report['media_message_count']}\n"
            f"• Total media: {fmt(report['total_media_bytes'])}\n"
            f"• Referenced media: {report['referenced_media_count']} "
            f"({fmt(report['referenced_media_bytes'])})\n"
            f"• Config records: {report['config_message_count']}\n"
            f"• State records: {report['state_message_count']}\n"
            f"• Queue manifests: {report['queue_manifest_count']}\n\n"
            "Integrity\n"
            f"• Missing referenced media: {len(missing)}\n"
            f"  IDs: {missing_text}\n"
            f"• Unreferenced/orphan media: {len(orphans)}\n"
            f"  IDs: {orphan_text}\n\n"
            "Reclaimable terminal-job media\n"
            f"• Jobs: {len(reclaimable)}\n"
            f"• Recoverable space: {fmt(reclaimable_bytes)}\n"
            + "\n".join(reclaimable_lines)
            + "\n\n"
            "Only PUBLISHED/SKIPPED job media is reclaimable. "
            "FAILED and active jobs are protected."
        )
    except Exception as e:
        print(f"❌ Storage report error: {e}")
        await status_message.edit_text(
            "❌ Failed to audit Telegram storage.\n\n"
            f"Error: {e}"
        )


async def storage_cleanup_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    args = [str(arg).strip() for arg in context.args]
    confirm = False
    job_id = ""

    if args == ["confirm"]:
        confirm = True
    elif len(args) == 1:
        job_id = args[0].upper()
    elif len(args) == 2 and args[1].lower() == "confirm":
        job_id = args[0].upper()
        confirm = True
    elif args:
        await update.message.reply_text(
            "Usage:\n"
            "/storage_cleanup\n"
            "/storage_cleanup confirm\n"
            "/storage_cleanup JOB-000005\n"
            "/storage_cleanup JOB-000005 confirm"
        )
        return

    try:
        result = await telegram_storage.cleanup_reclaimable_storage_media(
            job_id=job_id,
            confirm=confirm,
        )

        candidates = result["candidates"]
        orphans = result["orphan_media_ids"]

        if not confirm:
            if job_id:
                if candidates:
                    item = candidates[0]
                    await update.message.reply_text(
                        "🧹 STORAGE CLEANUP PREVIEW\n\n"
                        f"Job: {item['job_id']}\n"
                        f"Status: {item['status']}\n"
                        f"Media: {telegram_storage.format_storage_bytes(item['media_size'])}\n\n"
                        "Run the same command with `confirm` to delete this media."
                    )
                return

            total_bytes = sum(
                int(item.get("media_size", 0))
                for item in candidates
            )
            lines = [
                "🧹 STORAGE CLEANUP PREVIEW",
                "",
                f"Reclaimable jobs: {len(candidates)}",
                f"Reclaimable media: {telegram_storage.format_storage_bytes(total_bytes)}",
            ]
            for item in candidates[:20]:
                lines.append(
                    f"• {item['job_id']} ({item['status']}) — "
                    f"{telegram_storage.format_storage_bytes(item['media_size'])}"
                )
            if len(candidates) > 20:
                lines.append(f"• ...and {len(candidates) - 20} more")

            lines.extend([
                "",
                "No media was deleted.",
                "Use /storage_cleanup confirm to delete only these terminal-job media files.",
                f"Unreferenced/orphan media left untouched: {len(orphans)}",
            ])
            await update.message.reply_text("\n".join(lines))
            return

        deleted = result["deleted"]
        failed = result["failed"]
        deleted_bytes = sum(
            int(item.get("media_size", 0))
            for item in deleted
        )

        lines = [
            "🧹 STORAGE CLEANUP COMPLETE",
            "",
            f"Deleted media: {len(deleted)}",
            f"Space reclaimed: {telegram_storage.format_storage_bytes(deleted_bytes)}",
            f"Failed deletions: {len(failed)}",
        ]

        if deleted:
            lines.append("")
            lines.append("Deleted:")
            for item in deleted[:20]:
                lines.append(
                    f"• {item['job_id']} — Telegram message "
                    f"{item['telegram_message_id']}"
                )

        if failed:
            lines.append("")
            lines.append("Failed:")
            for item in failed[:10]:
                lines.append(
                    f"• {item['job_id']}: {item['error'][:250]}"
                )

        lines.extend([
            "",
            f"Unreferenced/orphan media left untouched: {len(orphans)}",
            "JOB records, configuration, state, and queue manifests were not deleted."
        ])
        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        print(f"❌ Storage cleanup error: {e}")
        await update.message.reply_text(
            "❌ Storage cleanup failed.\n\n"
            f"Error: {e}"
        )


# =========================
# /queue
# =========================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    status_message = await update.message.reply_text(
        "🔄 Reading queues..."
    )

    try:
        queue = await telegram_storage.get_queue()
        jobs = await telegram_storage.get_all_jobs()

        jobs_by_id = {
            job.get("job_id"): job
            for job in jobs
        }

        lines = [
            "📋 QUEUE STATUS",
            "",
            "📥 INTAKE QUEUE",
        ]

        intake = queue["intake_queue"]

        if intake:
            for position, job_id in enumerate(
                intake,
                start=1,
            ):
                job = jobs_by_id.get(job_id, {})
                url = job.get("source_url", "")
                lines.append(
                    f"{position}. {job_id}"
                )
                if url:
                    lines.append(
                        f"   {url}"
                    )
        else:
            lines.append("Empty")

        lines.extend([
            "",
            "⚙️ PROCESSING",
            queue["processing_job"] or "None",
            "",
            "📤 PUBLISHING QUEUE",
        ])

        publishing = queue["publishing_queue"]

        if publishing:
            for position, job_id in enumerate(
                publishing,
                start=1,
            ):
                lines.append(
                    f"{position}. {job_id}"
                )
        else:
            lines.append("Empty")

        schedule = await telegram_storage.get_publishing_schedule()

        config = await telegram_storage.get_config()
        window_enabled = str(
            config.get("posting_window_enabled", "true")
        ).lower() == "true"
        window_text = (
            f"{config.get('posting_window_start', '04:00')} - "
            f"{config.get('posting_window_end', '23:30')} "
            f"({config.get('posting_timezone', 'Asia/Kolkata')})"
            if window_enabled
            else "Disabled"
        )

        lines.extend([
            "",
            "📤 CURRENTLY PUBLISHING",
            queue["publishing_job"] or "None",
            "",
            "⏱️ NEXT SCHEDULED POST",
            schedule["next_post_at"] or "Not scheduled",
            f"Interval: {schedule['interval_minutes']} minutes",
            f"Window: {window_text}",
        ])

        await status_message.edit_text(
            "\n".join(lines)
        )

    except Exception as e:
        print(
            f"❌ Queue error: {e}"
        )

        await status_message.edit_text(
            "❌ Failed to read queue.\n\n"
            f"Error: {e}"
        )


# =========================
# /remove and /skip
# =========================

async def remove_job_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not await _require_admin(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/remove JOB-000002"
        )
        return

    job_id = context.args[0].strip().upper()

    if not job_id.startswith("JOB-"):
        await update.message.reply_text(
            "❌ Invalid job ID.\n\n"
            "Example: /remove JOB-000002"
        )
        return

    try:
        job = await telegram_storage.get_job(
            job_id
        )

        if not job:
            await update.message.reply_text(
                f"❌ Job not found: {job_id}"
            )
            return

        if job.get("status") != telegram_storage.STATUS_WAITING:
            await update.message.reply_text(
                f"❌ {job_id} cannot be removed.\n\n"
                f"Current status: {job.get('status')}\n\n"
                "Only WAITING jobs in the intake queue "
                "can be removed."
            )
            return

        result = (
            await telegram_storage
            .remove_job_from_intake(job_id)
        )

        if not result["removed"]:
            await update.message.reply_text(
                f"❌ {job_id} is not currently in the intake queue."
            )
            return

        await update.message.reply_text(
            f"🗑️ {job_id} removed from the intake queue.\n\n"
            "Status: SKIPPED"
        )

    except Exception as e:
        print(
            f"❌ Remove job error: {e}"
        )

        await update.message.reply_text(
            "❌ Failed to remove job.\n\n"
            f"Error: {e}"
        )


# =========================
# INSTAGRAM URL INTAKE
# =========================

async def handle_instagram_urls(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    # Storage records can contain Instagram source URLs. Never re-intake those
    # URLs as new jobs and never send admin-denied replies into the storage
    # channel.
    if await _is_storage_channel_update(update):
        return

    if not await _require_admin(update):
        return

    message = update.message

    if not message or not message.text:
        return

    urls = telegram_storage.extract_instagram_urls(
        message.text
    )

    if not urls:
        return

    status_message = await message.reply_text(
        f"📥 Found {len(urls)} Instagram URL(s).\n\n"
        "⏳ Adding to intake queue..."
    )

    results = []

    try:
        for url in urls:
            progress_message = await message.reply_text(
                "🤖 INSTAGRAM AUTO POSTER\n\n"
                "⏳ Preparing job..."
            )

            result = (
                await telegram_storage
                .enqueue_instagram_url(
                    url,
                    status_chat_id=message.chat_id,
                    status_message_id=progress_message.message_id,
                )
            )
            results.append(result)

            if result.get("reason") != "CREATED":
                try:
                    await progress_message.delete()
                except Exception:
                    pass

        lines = [
            "📥 INTAKE QUEUE UPDATE",
            "",
        ]

        for result in results:
            reason = result["reason"]
            url = result["url"]

            if reason == "CREATED":
                job = result["job"]
                lines.extend([
                    "✅ Added",
                    f"Job: {job['job_id']}",
                    f"Position: #{result['position']}",
                    f"URL: {url}",
                    "",
                ])

            elif reason == "DUPLICATE":
                job = result["job"]
                lines.extend([
                    "♻️ Duplicate ignored",
                    f"Existing job: {job.get('job_id', 'Unknown')}",
                    f"Status: {job.get('status', 'Unknown')}",
                    f"URL: {url}",
                    "",
                ])

            else:
                lines.extend([
                    "❌ Invalid Instagram URL",
                    f"URL: {url}",
                    "",
                ])

        await status_message.edit_text(
            "\n".join(lines).rstrip()
        )

    except Exception as e:
        print(
            f"❌ Instagram intake error: {e}"
        )

        await status_message.edit_text(
            "❌ Failed to add the URL(s) to the queue.\n\n"
            f"Error: {e}"
        )


# =========================
# VIDEO STORAGE HANDLER
# =========================

async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    # Videos already stored in the persistent storage channel must never be
    # copied back into the same channel or trigger an admin reply.
    if await _is_storage_channel_update(update):
        return

    if not await _require_admin(update):
        return

    if not STORAGE_CHANNEL_ID:
        await update.message.reply_text(
            "❌ STORAGE_CHANNEL_ID is missing from .env"
        )
        return

    message = update.message

    if not message:
        return

    status_message = await message.reply_text(
        "📥 Video received.\n\n"
        "⏳ Saving to private storage..."
    )

    try:
        copied_message = (
            await context.bot.copy_message(
                chat_id=int(STORAGE_CHANNEL_ID),
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
        )

        storage_message_id = (
            copied_message.message_id
        )

        print("\n==============================")
        print("🎬 VIDEO STORED")
        print(
            f"Source chat ID: "
            f"{message.chat_id}"
        )
        print(
            f"Source message ID: "
            f"{message.message_id}"
        )
        print(
            f"Storage channel ID: "
            f"{STORAGE_CHANNEL_ID}"
        )
        print(
            f"Storage message ID: "
            f"{storage_message_id}"
        )
        print("==============================\n")

        await status_message.edit_text(
            "✅ Video stored successfully!\n\n"
            f"📦 Storage message ID: "
            f"{storage_message_id}\n"
            "📁 Private Telegram storage: OK"
        )

    except Exception as e:
        print("\n==============================")
        print("❌ VIDEO STORAGE ERROR")
        print(e)
        print("==============================\n")

        await status_message.edit_text(
            "❌ Failed to store video.\n\n"
            f"Error: {e}"
        )




# =========================
# PROCESSING STATUS UI
# =========================

def _format_bytes(value):
    value = float(value or 0)
    units = ("B", "KB", "MB", "GB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.2f} {units[index]}"


def _progress_bar(percent, length=20):
    percent = max(0.0, min(100.0, float(percent)))
    filled = int((percent / 100.0) * length)
    return "█" * filled + "░" * (length - filled)


# In-memory timing is used for live diagnostics. The persistent JOB record remains
# the source of truth for queue/recovery state, so losing these timers on a restart
# does not affect publishing.
_JOB_TIMERS = {}


def _timestamp():
    return datetime.now(timezone.utc).astimezone(DISPLAY_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


def _duration_text(seconds):
    seconds = max(float(seconds or 0), 0.0)
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {remainder:.1f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h {minutes}m {remainder:.1f}s"


def _start_stage_timer(job_id, stage):
    now = time.monotonic()
    wall = _timestamp()
    state = _JOB_TIMERS.setdefault(
        job_id,
        {
            "job_started_monotonic": now,
            "job_started_at": wall,
            "active_stage": "",
            "active_stage_started_monotonic": now,
            "active_stage_started_at": wall,
        },
    )

    if state.get("active_stage") == stage:
        return state, False

    previous = state.get("active_stage")
    if previous:
        elapsed = now - state.get("active_stage_started_monotonic", now)
        print(
            f"⏱️ [{wall}] DONE   {job_id} | {previous} | "
            f"duration={_duration_text(elapsed)}"
        )

    state["active_stage"] = stage
    state["active_stage_started_monotonic"] = now
    state["active_stage_started_at"] = wall
    print(f"⏱️ [{wall}] START  {job_id} | {stage}")
    return state, True


def _finish_stage_timer(job_id, stage):
    state = _JOB_TIMERS.get(job_id)
    now = time.monotonic()
    wall = _timestamp()
    if not state:
        state, _ = _start_stage_timer(job_id, stage)
    if state.get("active_stage") != stage:
        state, _ = _start_stage_timer(job_id, stage)

    elapsed = now - state.get("active_stage_started_monotonic", now)
    print(
        f"⏱️ [{wall}] DONE   {job_id} | {stage} | "
        f"duration={_duration_text(elapsed)}"
    )
    state["active_stage"] = ""
    state["active_stage_started_monotonic"] = now
    state["active_stage_started_at"] = wall
    return state, wall, elapsed


def _job_total_elapsed(job_id, job=None):
    # Prefer the persistent processing timestamp so the total survives a
    # process restart. Fall back to the in-memory timer for a newly created job.
    if job:
        start_value = str(
            job.get("processing_started_at")
            or job.get("created_at")
            or ""
        ).strip()
        if start_value:
            try:
                started = datetime.fromisoformat(start_value)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
                return max(elapsed, 0.0)
            except ValueError:
                pass

    state = _JOB_TIMERS.get(job_id)
    if not state:
        return None
    return time.monotonic() - state.get("job_started_monotonic", time.monotonic())


def _forget_job_timer(job_id):
    _JOB_TIMERS.pop(job_id, None)


async def update_processing_status(
    job,
    stage,
    detail="",
    current=None,
    total=None,
    finished=False,
    error="",
):
    """Edit the processing/publishing status message and emit stage timings."""
    job_id = str(job.get("job_id", "")).strip() or "UNKNOWN"
    state, _ = _start_stage_timer(job_id, stage)
    stage_started_at = state.get("active_stage_started_at", _timestamp())

    completed_at = ""
    stage_duration = None
    total_elapsed = None
    if finished:
        _, completed_at, stage_duration = _finish_stage_timer(job_id, stage)
        total_elapsed = _job_total_elapsed(job_id, job)

    chat_id = str(job.get("status_chat_id", "")).strip()
    message_id = str(job.get("status_message_id", "")).strip()

    lines = [
        "🤖 INSTAGRAM AUTO POSTER",
        "",
        f"🎬 Job: {job_id}",
        f"📌 Stage: {stage}",
        f"🕒 Started: {stage_started_at}",
    ]

    if detail:
        lines.extend(["", detail])

    if total is not None:
        total = max(int(total or 0), 1)
        current = max(0, min(int(current or 0), total))
        percent = current / total * 100
        lines.extend([
            "",
            f"Progress: [{_progress_bar(percent)}] {percent:5.1f}%",
            f"Transferred: {_format_bytes(current)} / {_format_bytes(total)}",
        ])

    if finished:
        lines.extend([
            "",
            "✅ Stage complete",
            f"🕒 Completed: {completed_at}",
            f"⏱️ Stage duration: {_duration_text(stage_duration)}",
        ])
        if total_elapsed is not None and (
            "complete" in stage.lower()
            or "published" in stage.lower()
            or stage.startswith("❌")
        ):
            lines.append(f"🏁 Total elapsed (processing → now): {_duration_text(total_elapsed)}")

    if error:
        lines.extend(["", "❌ Error:", str(error)[:1000]])

    if not chat_id or not message_id:
        return

    if bot_application is None:
        print(
            f"⚠️ Could not update bot progress for {job_id}: "
            "Telegram application is not initialized."
        )
        return

    text = "\n".join(lines)
    try:
        await bot_application.bot.edit_message_text(
            chat_id=int(chat_id),
            message_id=int(message_id),
            text=text,
        )
    except Exception as exc:
        # Telegram raises BadRequest when the requested edit is byte-for-byte
        # identical. That is harmless and should not clutter the logs.
        if "Message is not modified" in str(exc):
            return
        print(
            f"⚠️ Could not update bot progress for {job_id}: {exc}"
        )


# =========================
# PHASE 5 PROCESSING WORKER
# =========================

processing_worker_task = None
publishing_worker_task = None
heartbeat_worker_task = None
bot_application = None


async def process_one_job(job):
    """
    Download one claimed Instagram job, upload the resulting video
    to persistent Telegram storage, mark it READY, and automatically
    place it in the persistent publishing queue.
    """
    job_id = job["job_id"]
    source_url = job["source_url"]

    try:
        await update_processing_status(
            job,
            "⬇️ Downloading Instagram Reel",
            f"URL: {source_url}",
        )

        download_result = await asyncio.to_thread(
            instagram_downloader.download_instagram_video,
            source_url,
            job_id,
        )

        file_path = download_result["file_path"]

        file_size = os.path.getsize(file_path)
        await update_processing_status(
            job,
            "📥 Download complete",
            f"File: {os.path.basename(file_path)}\nSize: {_format_bytes(file_size)}\n\n📝 Extracting metadata...",
            finished=True,
        )

        await update_processing_status(
            job,
            "📝 Extracting Instagram metadata",
        )

        # Phase 6: persist the original Instagram metadata returned by
        # the same yt-dlp request used for the download.
        metadata = instagram_downloader.extract_instagram_metadata(
            download_result.get("info", {})
        )

        await telegram_storage.save_instagram_metadata(
            job_id,
            metadata,
        )

        # Phase 8: rewrite the caption/hashtags with Groq.
        await update_processing_status(
            job,
            "🤖 Groq AI processing",
            "Rewriting caption and hashtags...",
        )

        # AI failures intentionally fall back to the original metadata so
        # a temporary API/rate-limit/configuration problem never loses a job.
        ai_result = await asyncio.to_thread(
            groq_ai.rewrite_instagram_metadata,
            metadata.get("original_title", ""),
            metadata.get("original_caption", ""),
            metadata.get("original_hashtags", ""),
        )

        await telegram_storage.save_ai_metadata(
            job_id,
            ai_result,
        )

        print("\n==============================")
        print("🤖 GROQ AI PROCESSING")
        print(f"Job: {job_id}")
        print(f"AI status: {ai_result.get('status', 'UNKNOWN')}")
        print(
            "AI caption: "
            f"{ai_result.get('caption', '')[:300] or '(empty)'}"
        )
        print(
            "AI hashtags: "
            f"{ai_result.get('hashtags', '') or '(none)'}"
        )
        if ai_result.get("error"):
            print(f"AI fallback/error: {ai_result['error']}")
        print("==============================\n")

        await update_processing_status(
            job,
            "🔐 Calculating media fingerprint",
            "Generating SHA-256 and checking for duplicates...",
        )

        # Phase 7: hash the actual downloaded media and check whether
        # identical bytes already exist in another processed job.
        media_hash = await asyncio.to_thread(
            instagram_downloader.calculate_media_hash,
            file_path,
        )

        await telegram_storage.save_media_hash(
            job_id,
            media_hash,
        )

        duplicate_job = await telegram_storage.find_duplicate_media_hash(
            media_hash,
            exclude_job_id=job_id,
        )

        if duplicate_job:
            await telegram_storage.finish_processing_duplicate(
                job_id,
                duplicate_job.get("job_id", ""),
                media_hash,
            )

            instagram_downloader.cleanup_download(job_id)

            print(
                "\n=============================="
            )
            print("♻️ DUPLICATE MEDIA DETECTED")
            print(f"Job: {job_id}")
            print(
                "Existing job: "
                f"{duplicate_job.get('job_id', 'Unknown')}"
            )
            print(f"SHA-256: {media_hash}")
            print("Status: SKIPPED")
            print("==============================\n")
            return

        print(
            "\n=============================="
        )
        print("📝 METADATA EXTRACTED")
        print(f"Job: {job_id}")
        print(
            f"Title: {metadata.get('original_title', '') or '(empty)'}"
        )
        print(
            "Caption: "
            f"{metadata.get('original_caption', '')[:300] or '(empty)'}"
        )
        print(
            "Hashtags: "
            f"{metadata.get('original_hashtags', '') or '(none)'}"
        )
        print("==============================\n")

        await update_processing_status(
            job,
            "📤 Uploading to Telegram storage",
            "Watch this message for live upload progress.",
            current=0,
            total=file_size,
        )

        async def upload_progress(current, total, finished=False, error=""):
            elapsed_text = ""
            if finished:
                elapsed_text = "Upload finished successfully."
            elif error:
                elapsed_text = "Upload failed."
            else:
                elapsed_text = "Telegram storage upload in progress..."

            await update_processing_status(
                job,
                "📤 Uploading to Telegram storage",
                elapsed_text,
                current=current,
                total=total,
                finished=finished,
                error=error,
            )

        try:
            storage_message_id = (
                await telegram_storage.store_downloaded_media(
                    job_id,
                    file_path,
                    progress_callback=upload_progress,
                )
            )
        finally:
            instagram_downloader.cleanup_download(job_id)

        completed_job = await telegram_storage.finish_processing_success(
            job_id,
            storage_message_id,
        )

        queue_after_processing = await telegram_storage.get_queue()
        publishing_queue = queue_after_processing.get("publishing_queue", [])
        try:
            publishing_position = publishing_queue.index(job_id) + 1
        except ValueError:
            publishing_position = None

        if publishing_position:
            queue_detail = (
                f"Telegram storage message: {storage_message_id}\n\n"
                "Status: READY\n"
                "🤖 Automatic mode: ON\n"
                f"📤 Added to publishing queue: #{publishing_position}"
            )
        else:
            queue_detail = (
                f"Telegram storage message: {storage_message_id}\n\n"
                "Status: READY\n"
                "🤖 Automatic mode: ON\n"
                "📤 Publishing queue: queued"
            )

        await update_processing_status(
            completed_job or job,
            "✅ Processing complete",
            queue_detail,
            finished=True,
        )

        print(
            "\n=============================="
        )
        print("✅ PROCESSING COMPLETE")
        print(f"Job: {job_id}")
        print(
            f"Storage message ID: {storage_message_id}"
        )
        print("==============================\n")

    except instagram_downloader.ReelNotProcessableError as e:
        # Instagram could not expose downloadable media for this Reel.
        # This is an expected per-job failure: do not retry the Reel and do
        # not stop the processing worker. Release the processing slot so the
        # next WAITING job can be claimed immediately.
        print(
            "\n=============================="
        )
        print("❌ REEL CANNOT BE PROCESSED")
        print(f"Job: {job_id}")
        print(f"Error: {e}")
        print("➡️ Releasing job and continuing the queue.")
        print("==============================\n")

        await update_processing_status(
            job,
            "❌ Cannot process this Reel",
            "Instagram did not provide downloadable media for this Reel.",
            finished=True,
        )

        try:
            instagram_downloader.cleanup_download(job_id)
        except Exception:
            pass

        await telegram_storage.finish_processing_failure(
            job_id,
            str(e),
        )

        # Return normally. processing_worker() immediately loops and
        # attempts the next queued job.
        return

    except Exception as e:
        print(
            "\n=============================="
        )
        print("❌ PROCESSING FAILED")
        print(f"Job: {job_id}")
        print(f"Error: {e}")
        print("==============================\n")

        await update_processing_status(
            job,
            "❌ Processing failed",
            error=str(e),
            finished=True,
        )

        try:
            instagram_downloader.cleanup_download(job_id)
        except Exception:
            pass

        await telegram_storage.finish_processing_failure(
            job_id,
            str(e),
        )


async def update_publishing_status(
    job,
    stage,
    detail="",
    current=None,
    total=None,
    finished=False,
    error="",
):
    """Reuse the job's bot-chat status message for publishing progress."""
    await update_processing_status(
        job,
        stage,
        detail,
        current=current,
        total=total,
        finished=finished,
        error=error,
    )


async def publish_one_job(job):
    """Download a READY job from Telegram storage and publish it to Instagram."""
    job_id = job["job_id"]
    publishing_root = os.path.join("downloads", "publishing", job_id)
    os.makedirs(publishing_root, exist_ok=True)
    local_path = os.path.join(publishing_root, f"{job_id}.mp4")

    try:
        await update_publishing_status(
            job,
            "📥 Downloading from Telegram storage",
            f"Storage message: {job.get('storage_message_id', '')}",
        )

        async def download_progress(current, total, finished=False, error=""):
            detail = "Downloading video from private Telegram storage..."
            if finished:
                detail = "Telegram storage download complete."
            await update_publishing_status(
                job,
                "📥 Downloading from Telegram storage",
                detail,
                current=current,
                total=total,
                finished=finished,
                error=error,
            )

        await telegram_storage.download_stored_media(
            job_id,
            local_path,
            progress_callback=download_progress,
        )

        file_size = os.path.getsize(local_path)
        caption = str(job.get("ai_caption", "") or "").strip()
        hashtags = str(job.get("ai_hashtags", "") or "").strip()
        if hashtags:
            caption = f"{caption}\n\n{hashtags}" if caption else hashtags
        caption = caption[:2200]

        await update_publishing_status(
            job,
            "📤 Uploading to Instagram",
            "Using Meta's resumable Reel upload.",
            current=0,
            total=file_size,
        )

        async def on_container_created(container_id, upload_uri):
            # Persist the container immediately. If Render crashes after this
            # point, the next process can resume the existing Meta container.
            await telegram_storage.update_job(
                job_id,
                {
                    "instagram_container_id": container_id,
                    "instagram_upload_uri": upload_uri or "",
                    "publishing_upload_completed": "false",
                    "publishing_error": "",
                },
            )
            job["instagram_container_id"] = container_id
            job["instagram_upload_uri"] = upload_uri or ""
            job["publishing_upload_completed"] = "false"

        async def upload_progress(current, total, finished=False, error=""):
            detail = "Preparing the Instagram URL-based Reel upload..."
            if finished:
                detail = "Instagram accepted the public media URL; waiting for processing..."
            await update_publishing_status(
                job,
                "📤 Uploading to Instagram",
                detail,
                current=current,
                total=total,
                finished=finished,
                error=error,
            )

        async def publisher_stage(stage, detail="", finished=False):
            await update_publishing_status(
                job,
                stage,
                detail,
                finished=finished,
            )

        result = await instagram_publisher.publish_reel(
            local_path,
            caption,
            existing_container_id=str(job.get("instagram_container_id", "")).strip(),
            existing_upload_uri=str(job.get("instagram_upload_uri", "")).strip(),
            upload_completed=str(job.get("publishing_upload_completed", "false")).lower() == "true",
            on_container_created=on_container_created,
            progress_callback=upload_progress,
            stage_callback=publisher_stage,
        )

        media_id = str(result.get("instagram_media_id", "")).strip()
        container_id = str(result.get("container_id", "")).strip()

        if result.get("status") == "ALREADY_PUBLISHED":
            # This case means the API container itself reports PUBLISHED. The
            # media ID may be recoverable separately, but the post is already
            # live, so do not call media_publish a second time.
            media_id = str(job.get("instagram_media_id", "")).strip()

        completed_job = await telegram_storage.finish_publishing_success(
            job_id,
            media_id,
            container_id=container_id,
        )

        await update_publishing_status(
            completed_job or job,
            "✅ Instagram publishing complete",
            f"Instagram media ID: {media_id}\n\nTelegram media will now be deleted from persistent storage.",
            finished=True,
        )

        # Deleting storage is deliberately after Instagram has confirmed the
        # publication. A cleanup failure must not turn a successful Instagram
        # publication back into a failed job.
        try:
            await telegram_storage.delete_stored_media(
                job_id,
                clear_reference=True,
            )
        except Exception as cleanup_error:
            print(
                f"⚠️ Telegram media cleanup failed for {job_id}: {cleanup_error}"
            )

        print("\n==============================")
        print("✅ INSTAGRAM PUBLISH COMPLETE")
        print(f"Job: {job_id}")
        print(f"Instagram media ID: {media_id}")
        print("==============================\n")

    except instagram_publisher.InstagramPublisherNotConfigured as e:
        # Configuration is a deployment problem, not a failed Reel. Keep the
        # job READY and let the worker retry after credentials are configured.
        print(f"⚠️ Instagram publisher not configured: {e}")
        await update_publishing_status(
            job,
            "❌ Instagram publishing is not configured",
            error=str(e),
            finished=True,
        )
        recovery = await telegram_storage.finish_publishing_failure(
            job_id,
            str(e),
            retry=True,
        )
        if recovery and recovery.get("publishing_retry_scheduled") == "true":
            await update_publishing_status(
                recovery,
                "🔁 Instagram publishing retry scheduled",
                f"Retry {recovery.get('publishing_retry_count', '0')} of {recovery.get('publishing_max_retries', '3')} "
                f"in {recovery.get('publishing_retry_delay_minutes', '0')} minutes.",
                finished=True,
            )

    except Exception as e:
        print("\n==============================")
        print("❌ INSTAGRAM PUBLISH FAILED")
        print(f"Job: {job_id}")
        print(f"Error: {e}")
        print("==============================\n")

        await update_publishing_status(
            job,
            "❌ Instagram publishing failed",
            error=str(e),
            finished=True,
        )

        recovery = await telegram_storage.finish_publishing_failure(
            job_id,
            str(e),
            retry=True,
        )
        if recovery and recovery.get("publishing_retry_scheduled") == "true":
            await update_publishing_status(
                recovery,
                "🔁 Instagram publishing retry scheduled",
                f"Retry {recovery.get('publishing_retry_count', '0')} of {recovery.get('publishing_max_retries', '3')} "
                f"in {recovery.get('publishing_retry_delay_minutes', '0')} minutes.",
                finished=True,
            )
        elif recovery and recovery.get("status") == telegram_storage.STATUS_FAILED:
            await update_publishing_status(
                recovery,
                "⛔ Instagram publishing permanently failed",
                f"Retries exhausted or failure is non-retryable. "
                f"Attempts: {recovery.get('publishing_retry_count', '0')}.",
                finished=True,
                error=str(e),
            )

    finally:
        try:
            if os.path.isdir(publishing_root):
                for filename in os.listdir(publishing_root):
                    path = os.path.join(publishing_root, filename)
                    if os.path.isfile(path):
                        os.remove(path)
                os.rmdir(publishing_root)
        except Exception as cleanup_error:
            print(
                f"⚠️ Local publishing cleanup failed for {job_id}: {cleanup_error}"
            )


async def worker_heartbeat():
    """Persist a throttled worker heartbeat for the operational dashboard."""
    print("💓 Worker heartbeat started.")

    while True:
        try:
            await telegram_storage.update_tick_state()
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            print("🛑 Worker heartbeat stopped.")
            raise
        except Exception as e:
            print(f"❌ Worker heartbeat error: {e}")
            await asyncio.sleep(15)


async def publishing_worker():
    """Independent persistent publishing worker."""
    print("📤 Phase 14 Instagram publishing worker started.")

    while True:
        try:
            if not instagram_publisher.is_configured():
                await asyncio.sleep(15)
                continue

            config = await telegram_storage.get_config()
            if str(config.get("publishing_enabled", "true")).strip().lower() != "true":
                await asyncio.sleep(5)
                continue

            # Ensure a missing schedule is created before trying to claim.
            await telegram_storage.ensure_publishing_schedule()
            job = await telegram_storage.claim_next_publishing_job()

            if job:
                await publish_one_job(job)
                continue

            await asyncio.sleep(5)

        except asyncio.CancelledError:
            print("🛑 Phase 14 Instagram publishing worker stopped.")
            raise

        except Exception as e:
            print(f"❌ Publishing worker loop error: {e}")
            await asyncio.sleep(10)


async def processing_worker():
    """
    Single-worker loop.

    It always claims at most one WAITING job. When that job finishes,
    the next WAITING job can be claimed.
    """
    print("⚙️ Phase 5 processing worker started.")

    while True:
        try:
            job = await telegram_storage.claim_next_intake_job()

            if job:
                await process_one_job(job)
                continue

            await asyncio.sleep(3)

        except asyncio.CancelledError:
            print("🛑 Phase 5 processing worker stopped.")
            raise

        except Exception as e:
            print(
                f"❌ Processing worker loop error: {e}"
            )
            await asyncio.sleep(5)


# =========================
# POST INITIALIZATION
# =========================

async def post_init(
    application: Application
):
    global bot_application

    bot_application = application

    print(
        "📦 Initializing Telegram "
        "persistent storage..."
    )

    try:
        await telegram_storage.initialize_storage()

        recovered = (
            await telegram_storage
            .recover_interrupted_processing()
        )

        if recovered:
            print(
                "♻️ Processing state checked/recovered: "
                f"{recovered.get('job_id')}"
            )

        global processing_worker_task
        global publishing_worker_task

        processing_worker_task = asyncio.create_task(
            processing_worker()
        )

        publishing_worker_task = asyncio.create_task(
            publishing_worker()
        )

        global heartbeat_worker_task
        heartbeat_worker_task = asyncio.create_task(
            worker_heartbeat()
        )

        if instagram_publisher.is_configured():
            print("📤 Instagram publishing configuration detected.")
        else:
            print(
                "⚠️ Instagram publishing is not configured yet. "
                f"{instagram_publisher.configuration_error()}"
            )

        print(
            "✅ Telegram persistent "
            "storage initialized."
        )

    except Exception as e:
        print(
            "❌ Persistent storage "
            f"initialization failed: {e}"
        )
        raise


# =========================
# POST SHUTDOWN
# =========================

async def post_shutdown(
    application: Application
):
    global processing_worker_task
    global publishing_worker_task
    global heartbeat_worker_task
    global bot_application

    if processing_worker_task:
        processing_worker_task.cancel()

        try:
            await processing_worker_task
        except asyncio.CancelledError:
            pass

        processing_worker_task = None

    if publishing_worker_task:
        publishing_worker_task.cancel()

        try:
            await publishing_worker_task
        except asyncio.CancelledError:
            pass

        publishing_worker_task = None

    if heartbeat_worker_task:
        heartbeat_worker_task.cancel()

        try:
            await heartbeat_worker_task
        except asyncio.CancelledError:
            pass

        heartbeat_worker_task = None

    bot_application = None

    await telegram_storage.close_storage()

    print(
        "🔌 Telegram storage connection closed."
    )


# =========================
# MAIN
# =========================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing from .env"
        )

    if not STORAGE_CHANNEL_ID:
        raise RuntimeError(
            "STORAGE_CHANNEL_ID is missing from .env"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("queue", queue_command)
    )

    application.add_handler(
        CommandHandler("dashboard", dashboard_command)
    )

    application.add_handler(
        CommandHandler("status", status_command)
    )

    application.add_handler(
        CommandHandler("pause", pause_command)
    )

    application.add_handler(
        CommandHandler("resume", resume_command)
    )

    application.add_handler(
        CommandHandler("remove", remove_job_command)
    )

    application.add_handler(
        CommandHandler("skip", remove_job_command)
    )

    application.add_handler(
        CommandHandler("test_storage", test_storage)
    )

    application.add_handler(
        CommandHandler("storage_info", storage_info)
    )

    application.add_handler(
        CommandHandler("storage_status", storage_status)
    )

    application.add_handler(
        CommandHandler("storage_report", storage_report_command)
    )

    application.add_handler(
        CommandHandler("storage_cleanup", storage_cleanup_command)
    )

    application.add_handler(
        CommandHandler("interval", interval_command)
    )

    application.add_handler(
        CommandHandler("limit", limit_command)
    )

    application.add_handler(
        CommandHandler("window", window_command)
    )

    application.add_handler(
        CommandHandler("retry", retry_command)
    )

    application.add_handler(
        CommandHandler("cancel", cancel_command)
    )

    application.add_handler(
        CommandHandler("publish_now", publish_now_command)
    )

    application.add_handler(
        CommandHandler("schedule", schedule_command)
    )

    application.add_handler(
        CommandHandler("test_instagram", test_instagram_command)
    )

    # Video handler
    application.add_handler(
        MessageHandler(
            filters.VIDEO,
            handle_video
        )
    )

    # Instagram URL intake.
    # Commands and videos are handled by the handlers above.
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & (~filters.COMMAND)
            & filters.Regex(
                telegram_storage.INSTAGRAM_URL_PATTERN
            ),
            handle_instagram_urls,
        )
    )

    # Start the local HTTP server that Instagram Login uses to fetch the
    # temporary public video URL. Cloudflare Tunnel should forward to this
    # port (normally 10000).
    public_media_server.start_server_in_background()

    print(
        "🤖 Telegram bot starting..."
    )

    application.run_polling()


# =========================
# ENTRY POINT
# =========================

if __name__ == "__main__":
    main()
