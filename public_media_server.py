"""Temporary public media server used by Instagram's URL-based publishing API."""

import os
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


load_dotenv()

PORT = int(os.getenv("PORT", "10000"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

_media_files = {}
_media_lock = threading.Lock()
_server_started = False
_server_lock = threading.Lock()


class PublicMediaHandler(BaseHTTPRequestHandler):
    def _timestamp(self):
        return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    def _headers(self, content_type="text/plain"):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")

    def do_HEAD(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_response(200)
            self._headers()
            self.end_headers()
            return

        if path.startswith("/media/"):
            token = path[len("/media/"):]
            print(f"🌍 [{self._timestamp()}] HEAD /media/{token}")
            file_path = get_media_path(token)
            if file_path and os.path.isfile(file_path):
                try:
                    size = os.path.getsize(file_path)
                    self.send_response(200)
                    self._headers("video/mp4")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    return
                except OSError:
                    pass

        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        path = urlsplit(self.path).path

        if path == "/health":
            self.send_response(200)
            self._headers()
            self.end_headers()
            self.wfile.write(b"Bot is healthy")
            return

        if path.startswith("/media/"):
            token = path[len("/media/"):]
            print(
                f"🌍 [{self._timestamp()}] GET /media/{token} "
                f"Range={self.headers.get('Range', '') or 'none'}"
            )
            self.serve_media(token)
            return

        self.send_response(404)
        self.end_headers()

    def serve_media(self, token):
        file_path = get_media_path(token)
        if not file_path or not os.path.isfile(file_path):
            self.send_response(404)
            self.end_headers()
            return

        try:
            file_size = os.path.getsize(file_path)
            if file_size <= 0:
                self.send_response(404)
                self.end_headers()
                return

            range_header = self.headers.get("Range", "")
            start = 0
            end = file_size - 1
            status = 200

            if range_header.startswith("bytes="):
                value = range_header[6:].split(",", 1)[0].strip()
                if "-" in value:
                    left, right = value.split("-", 1)
                    if left:
                        start = int(left)
                    if right:
                        end = int(right)
                    elif left:
                        end = file_size - 1
                    else:
                        suffix = min(int(right), file_size)
                        start = file_size - suffix
                        end = file_size - 1

                    if start < 0 or start >= file_size or end < start:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{file_size}")
                        self.end_headers()
                        return

                    end = min(end, file_size - 1)
                    status = 206

            length = end - start + 1
            self.send_response(status)
            self._headers("video/mp4")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.end_headers()

            with open(file_path, "rb") as media:
                media.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = media.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"❌ Public media serving error: {type(exc).__name__}: {exc}")

    def log_message(self, format, *args):
        return


def start_server():
    global _server_started
    with _server_lock:
        if _server_started:
            return
        server = ThreadingHTTPServer(("0.0.0.0", PORT), PublicMediaHandler)
        _server_started = True

    print(f"🌐 Public HTTP server listening on port {PORT}.")
    server.serve_forever()


def start_server_in_background():
    if not PUBLIC_BASE_URL:
        print("⚠️ PUBLIC_BASE_URL is not configured; public media URLs will be unavailable.")
    thread = threading.Thread(target=start_server, name="public-media-server", daemon=True)
    thread.start()
    return thread


def register_media(file_path):
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL is missing from .env")
    if not os.path.isfile(file_path):
        raise FileNotFoundError(file_path)

    import uuid
    token = uuid.uuid4().hex
    with _media_lock:
        _media_files[token] = file_path

    url = f"{PUBLIC_BASE_URL}/media/{token}"
    print(f"🌐 Temporary Instagram media URL created: {url}")
    return token, url


def unregister_media(token):
    if not token:
        return
    with _media_lock:
        _media_files.pop(token, None)
    print(f"🗑️ Temporary public media URL removed: {token}")


def get_media_path(token):
    with _media_lock:
        return _media_files.get(token)
