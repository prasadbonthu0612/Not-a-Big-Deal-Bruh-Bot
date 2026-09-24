"""Phase 14 - Instagram Reels publishing using Meta's Instagram Login API."""

import asyncio
import inspect
import json
import os
import subprocess
from typing import Awaitable, Callable, Optional

import httpx
from dotenv import load_dotenv

import public_media_server

load_dotenv()

GRAPH_API_VERSION = os.getenv(
    "INSTAGRAM_API_VERSION",
    os.getenv("META_GRAPH_API_VERSION", "v26.0"),
).strip()
GRAPH_API_BASE = (
    os.getenv("INSTAGRAM_API_BASE", "https://graph.instagram.com")
    .strip()
    .rstrip("/")
)

IG_USER_ID = os.getenv(
    "INSTAGRAM_USER_ID",
    os.getenv("META_IG_USER_ID", ""),
).strip()
ACCESS_TOKEN = os.getenv(
    "INSTAGRAM_ACCESS_TOKEN",
    os.getenv("META_ACCESS_TOKEN", ""),
).strip()

SHARE_TO_FEED = (
    os.getenv(
        "INSTAGRAM_SHARE_TO_FEED",
        os.getenv("META_SHARE_TO_FEED", "true"),
    ).strip().lower()
    in {"1", "true", "yes", "on"}
)

POLL_SECONDS = max(3, int(os.getenv(
    "INSTAGRAM_PUBLISH_POLL_SECONDS",
    os.getenv("META_PUBLISH_POLL_SECONDS", "10"),
)))
POLL_TIMEOUT_SECONDS = max(30, int(os.getenv(
    "INSTAGRAM_PUBLISH_POLL_TIMEOUT_SECONDS",
    os.getenv("META_PUBLISH_POLL_TIMEOUT_SECONDS", "300"),
)))
REQUEST_TIMEOUT_SECONDS = max(30, int(os.getenv(
    "INSTAGRAM_REQUEST_TIMEOUT_SECONDS",
    os.getenv("META_REQUEST_TIMEOUT_SECONDS", "300"),
)))

ProgressCallback = Optional[Callable[[int, int, bool, str], Awaitable[None] | None]]
ContainerCallback = Optional[Callable[[str, str], Awaitable[None] | None]]
StageCallback = Optional[Callable[[str, str, bool], Awaitable[None] | None]]


class InstagramPublisherError(RuntimeError):
    """Raised when Meta rejects or cannot complete a publishing operation."""


class InstagramPublisherNotConfigured(InstagramPublisherError):
    """Raised when required Instagram publishing variables are absent."""


def is_configured() -> bool:
    return bool(IG_USER_ID and ACCESS_TOKEN and public_media_server.PUBLIC_BASE_URL)


def configuration_error() -> str:
    missing = []
    if not IG_USER_ID:
        missing.append("INSTAGRAM_USER_ID")
    if not ACCESS_TOKEN:
        missing.append("INSTAGRAM_ACCESS_TOKEN")
    if not public_media_server.PUBLIC_BASE_URL:
        missing.append("PUBLIC_BASE_URL")
    if missing:
        return "Missing Instagram publishing configuration: " + ", ".join(missing)
    return ""


def _require_credentials():
    missing = []
    if not IG_USER_ID:
        missing.append("INSTAGRAM_USER_ID")
    if not ACCESS_TOKEN:
        missing.append("INSTAGRAM_ACCESS_TOKEN")
    if missing:
        raise InstagramPublisherNotConfigured(
            "Missing Instagram Login credentials: " + ", ".join(missing)
        )


def _require_configured():
    if not is_configured():
        raise InstagramPublisherNotConfigured(configuration_error())


async def test_connection() -> dict:
    """Validate the configured Instagram Login token and user ID."""
    _require_credentials()

    timeout = httpx.Timeout(30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(
                _graph_url(f"/{IG_USER_ID}"),
                params={
                    "fields": "id,username",
                    "access_token": ACCESS_TOKEN,
                },
            )
        except httpx.HTTPError as exc:
            raise InstagramPublisherError(
                f"Instagram connection request failed: {exc}"
            ) from exc

    data = await _parse_json_response(response)
    return {
        "id": str(data.get("id", "")).strip(),
        "username": str(data.get("username", "")).strip(),
        "api_version": GRAPH_API_VERSION,
        "api_base": GRAPH_API_BASE,
    }


def _graph_url(path: str) -> str:
    return f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}{path}"


def _compact_error(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        data = response.text

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or "Meta API error"
            code = error.get("code")
            subcode = error.get("error_subcode")
            parts = [str(message)]
            if code is not None:
                parts.append(f"code={code}")
            if subcode is not None:
                parts.append(f"subcode={subcode}")
            return " | ".join(parts)[:2000]

    return str(data)[:2000]


async def _parse_json_response(response: httpx.Response) -> dict:
    if response.is_error:
        raise InstagramPublisherError(_compact_error(response))

    try:
        data = response.json()
    except Exception as exc:
        raise InstagramPublisherError(
            f"Meta returned non-JSON response (HTTP {response.status_code})."
        ) from exc

    if not isinstance(data, dict):
        raise InstagramPublisherError("Meta returned an unexpected response format.")

    if "error" in data:
        raise InstagramPublisherError(str(data["error"])[:2000])

    return data


async def _call_callback(callback, *args):
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result



def _run_ffprobe(file_path: str) -> dict:
    """Return concise media diagnostics without exposing file contents."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=format_name,duration,size,bit_rate:stream=index,codec_type,codec_name,profile,width,height,pix_fmt,r_frame_rate,avg_frame_rate,bit_rate,sample_rate,channels",
        "-of", "json",
        file_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return {"error": "ffprobe executable not found in PATH"}
    except Exception as exc:
        return {"error": f"ffprobe execution failed: {type(exc).__name__}: {exc}"}

    if result.returncode != 0:
        return {
            "error": f"ffprobe failed (exit {result.returncode})",
            "stderr": result.stderr.strip()[:2000],
        }

    try:
        return json.loads(result.stdout or "{}")
    except Exception as exc:
        return {
            "error": f"Could not parse ffprobe JSON: {exc}",
            "stdout": result.stdout[:2000],
        }


def _print_media_diagnostics(file_path: str) -> None:
    print("\n==============================")
    print("🔎 INSTAGRAM MEDIA DIAGNOSTICS")
    print(f"File: {os.path.basename(file_path)}")
    try:
        print(f"Size: {os.path.getsize(file_path)} bytes")
    except OSError:
        pass
    diagnostics = _run_ffprobe(file_path)
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2)[:12000])
    print("==============================\n")


async def _probe_public_media_url(video_url: str) -> dict:
    """Verify that our public endpoint is reachable and supports byte ranges."""
    timeout = httpx.Timeout(30)
    headers = {"Range": "bytes=0-1", "User-Agent": "InstagramAutoPoster/Phase14"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            response = await client.get(video_url, headers=headers)
        except httpx.HTTPError as exc:
            raise InstagramPublisherError(
                f"Public media URL probe failed: {type(exc).__name__}: {exc}"
            ) from exc

    result = {
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "content_length": response.headers.get("content-length", ""),
        "content_range": response.headers.get("content-range", ""),
        "accept_ranges": response.headers.get("accept-ranges", ""),
        "final_url": str(response.url),
    }
    print("\n==============================")
    print("🌍 PUBLIC MEDIA URL PROBE")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("==============================\n")

    if response.status_code not in (200, 206):
        raise InstagramPublisherError(
            f"Public media URL returned HTTP {response.status_code}: {result}"
        )
    if not response.content:
        raise InstagramPublisherError("Public media URL returned an empty response body.")
    return result


async def create_reel_container(
    video_url: str,
    caption: str,
    *,
    existing_container_id: str = "",
    on_container_created: ContainerCallback = None,
) -> dict:
    """Create an Instagram Login Reel container using a publicly reachable video URL."""
    _require_configured()

    if existing_container_id:
        return {
            "container_id": existing_container_id,
            "upload_uri": "",
            "created": False,
        }

    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true" if SHARE_TO_FEED else "false",
        "access_token": ACCESS_TOKEN,
    }

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(
                _graph_url(f"/{IG_USER_ID}/media"),
                data=payload,
            )
        except httpx.HTTPError as exc:
            raise InstagramPublisherError(
                f"Instagram container request failed: {exc}"
            ) from exc

    data = await _parse_json_response(response)
    container_id = str(data.get("id", "")).strip()
    if not container_id:
        raise InstagramPublisherError(f"Instagram created no container ID: {data}")

    await _call_callback(on_container_created, container_id, "")

    return {
        "container_id": container_id,
        "upload_uri": "",
        "created": True,
    }


async def get_container_status(container_id: str) -> dict:
    _require_configured()

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(
                _graph_url(f"/{container_id}"),
                params={
                    "fields": "status_code,status",
                    "access_token": ACCESS_TOKEN,
                },
            )
        except httpx.HTTPError as exc:
            raise InstagramPublisherError(
                f"Instagram container-status request failed: {exc}"
            ) from exc

    return await _parse_json_response(response)


async def wait_for_container(container_id: str) -> dict:
    elapsed = 0
    while elapsed <= POLL_TIMEOUT_SECONDS:
        status = await get_container_status(container_id)
        code = str(status.get("status_code", "") or status.get("status", "")).upper()

        if code == "FINISHED":
            return status
        if code == "PUBLISHED":
            return status
        if code in {"ERROR", "EXPIRED", "FAILED"}:
            print("\n==============================")
            print("❌ INSTAGRAM CONTAINER STATUS RESPONSE")
            print(json.dumps(status, ensure_ascii=False, indent=2)[:12000])
            print("==============================\n")
            detail = status.get("status") or code
            raise InstagramPublisherError(
                f"Instagram container {code}: {detail} | full_status={json.dumps(status, ensure_ascii=False)[:6000]}"
            )

        await asyncio.sleep(POLL_SECONDS)
        elapsed += POLL_SECONDS

    raise InstagramPublisherError(
        f"Instagram container {container_id} did not reach FINISHED within "
        f"{POLL_TIMEOUT_SECONDS} seconds."
    )


async def publish_container(container_id: str) -> str:
    _require_configured()

    payload = {
        "creation_id": container_id,
        "access_token": ACCESS_TOKEN,
    }

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(
                _graph_url(f"/{IG_USER_ID}/media_publish"),
                data=payload,
            )
        except httpx.HTTPError as exc:
            raise InstagramPublisherError(
                f"Instagram publish request failed: {exc}"
            ) from exc

    data = await _parse_json_response(response)
    media_id = str(data.get("id", "")).strip()
    if not media_id:
        raise InstagramPublisherError(
            f"Instagram publish succeeded without returning a media ID: {data}"
        )
    return media_id


async def publish_reel(
    file_path: str,
    caption: str,
    *,
    existing_container_id: str = "",
    existing_upload_uri: str = "",
    upload_completed: bool = False,
    on_container_created: ContainerCallback = None,
    progress_callback: ProgressCallback = None,
    stage_callback: StageCallback = None,
) -> dict:
    """Publish a local MP4 through Instagram Login's URL-based Reel flow."""
    _require_configured()

    token = ""
    try:
        # The Instagram Login API fetches video_url itself. Keep the URL alive
        # until the container has finished processing and publication succeeds.
        if not existing_container_id:
            await _call_callback(
                stage_callback,
                "🌐 Preparing public Instagram media URL",
                "Registering the temporary HTTPS media endpoint.",
                False,
            )
            _print_media_diagnostics(file_path)
            token, video_url = public_media_server.register_media(file_path)
            await _call_callback(progress_callback, 0, os.path.getsize(file_path), False, "")
            await _probe_public_media_url(video_url)

            await _call_callback(
                stage_callback,
                "📤 Creating Instagram Reel container",
                "Sending the public video URL to Instagram Login API.",
                False,
            )
            container = await create_reel_container(
                video_url,
                caption,
                on_container_created=on_container_created,
            )
        else:
            # A previously created container may survive a worker restart.
            # Resume it only while it is still usable. Expired/error containers
            # cannot be repaired, so create a fresh container instead of
            # retrying the same dead container forever.
            await _call_callback(
                stage_callback,
                "♻️ Checking saved Instagram container",
                f"Existing container: {existing_container_id}",
                False,
            )
            saved_status = await get_container_status(existing_container_id)
            saved_code = str(
                saved_status.get("status_code", "")
                or saved_status.get("status", "")
            ).upper()

            if saved_code == "PUBLISHED":
                return {
                    "container_id": existing_container_id,
                    "instagram_media_id": "",
                    "status": "ALREADY_PUBLISHED",
                }

            if saved_code in {"ERROR", "EXPIRED", "FAILED"}:
                await _call_callback(
                    stage_callback,
                    "🔄 Creating replacement Instagram Reel container",
                    f"Saved container is {saved_code}; creating a fresh container.",
                    False,
                )
                # Run the same diagnostics on replacement containers. The
                # previous diagnostic build skipped this branch.
                _print_media_diagnostics(file_path)
                token, video_url = public_media_server.register_media(file_path)
                await _call_callback(
                    progress_callback,
                    0,
                    os.path.getsize(file_path),
                    False,
                    "",
                )
                await _probe_public_media_url(video_url)
                container = await create_reel_container(
                    video_url,
                    caption,
                    on_container_created=on_container_created,
                )
            else:
                await _call_callback(
                    stage_callback,
                    "♻️ Resuming Instagram container",
                    f"Container status: {saved_code or 'UNKNOWN'}",
                    False,
                )
                container = await create_reel_container(
                    "",
                    caption,
                    existing_container_id=existing_container_id,
                    on_container_created=on_container_created,
                )

        container_id = container["container_id"]

        await _call_callback(
            stage_callback,
            "⏳ Waiting for Instagram processing",
            f"Container: {container_id}\nPolling every {POLL_SECONDS}s (timeout {POLL_TIMEOUT_SECONDS}s).",
            False,
        )

        status = await get_container_status(container_id)
        code = str(status.get("status_code", "") or status.get("status", "")).upper()

        if code == "PUBLISHED":
            return {
                "container_id": container_id,
                "instagram_media_id": "",
                "status": "ALREADY_PUBLISHED",
            }

        status = await wait_for_container(container_id)
        if str(status.get("status_code", "") or status.get("status", "")).upper() == "PUBLISHED":
            return {
                "container_id": container_id,
                "instagram_media_id": "",
                "status": "ALREADY_PUBLISHED",
            }

        await _call_callback(
            progress_callback,
            os.path.getsize(file_path),
            os.path.getsize(file_path),
            True,
            "",
        )

        await _call_callback(
            stage_callback,
            "🚀 Publishing Instagram Reel",
            f"Container {container_id} is FINISHED; publishing now.",
            False,
        )

        media_id = await publish_container(container_id)

        await _call_callback(
            stage_callback,
            "🎉 Instagram Reel published",
            f"Instagram media ID: {media_id}",
            True,
        )

        return {
            "container_id": container_id,
            "instagram_media_id": media_id,
            "status": "PUBLISHED",
        }

    finally:
        public_media_server.unregister_media(token)
