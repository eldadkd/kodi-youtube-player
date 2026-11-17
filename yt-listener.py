"""HTTP server for YouTube play URLs and stop-yt.

GET ?url=...&action=play|add|play-next|party[&mode=download] runs yt_party_play.py.
GET/HEAD /stream/<video_id>/<title> relays the YouTube audio to Kodi (Range supported).
GET /stop-yt or /yt-stop stops downloads.
GET /stop-after stops downloads and makes Kodi stop when the current song ends.
GET /log or /logs returns the last 20 lines of the listener log file.

Module file is named yt-listener.py (not snake_case) for systemd/scripts.
"""
# pylint: disable=invalid-name
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import subprocess
import sys
import urllib.parse

from instance_control import YT_CANCEL, register_instance, stop_all_instances
from kodi_jsonrpc import KodiJsonRpc, load_kodi_env
from logger_helper import LOG_FILE, get_logger
from stream_cache import (
    LISTENER_PORT,
    audio_size_and_type,
    cached_audio_url,
    drop_cached_url,
    read_range,
    resolve_audio_url,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
YT_PLAY_SCRIPT = os.path.join(SCRIPT_DIR, "yt_party_play.py")

log = get_logger(__name__)

_STREAM_PATH = re.compile(r"/stream/([0-9A-Za-z_-]{11})(?:/.*)?")
_RANGE = re.compile(r"bytes=(\d+)-(\d*)")


def _byte_range(header, size: int) -> tuple[int, int]:
    """(start, end) inclusive for a 'bytes=S-' / 'bytes=S-E' header; whole file otherwise."""
    m = _RANGE.fullmatch((header or "").strip())
    if not m:
        return 0, size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    return start, min(end, size - 1)

ACTION_REPLY = {
    "add": b"Added to queue. Loading on Kodi...",
    "play-next": b"Added to play next. Loading on Kodi...",
    "play": b"Playing on Kodi. Loading...",
    "party": b"Party mode started. Loading on Kodi...",
}


def _last_log_lines(path, n=20):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=n))
    except FileNotFoundError:
        return []


def _normalize_request_path(path: str) -> str:
    """Single leading slash, collapse duplicate slashes, drop trailing slash (except root)."""
    if not path:
        return "/"
    p = path if path.startswith("/") else "/" + path
    parts = [seg for seg in p.split("/") if seg]
    return "/" + "/".join(parts) if parts else "/"


def _chain(first: bytes, rest):
    yield first
    yield from rest


class RequestHandler(BaseHTTPRequestHandler):
    """Serve play URLs and /stop-yt; log access lines via module logger."""

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        if self.command == "HEAD":
            return  # Kodi probes the playing stream item every ~1.5 s; don't flood the log
        log.info("%s - %s", self.address_string(), format % args)

    def _plain(self, code, body: bytes):
        self.send_response(code)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def _open_stream(self, video_id: str):
        """
        (url, size, mime, start, end, chunks) for the requested Range, with the first chunk
        already fetched so a rejected URL can be replaced (once) before headers are sent.
        """
        for attempt in (1, 2):
            url = resolve_audio_url(video_id, log=log)
            if not url:
                return None
            size, mime = audio_size_and_type(url)
            if size is None:
                return url, None, mime, 0, 0, iter(())
            start, end = _byte_range(self.headers.get("Range"), size)
            chunks = read_range(url, start, end)
            try:
                first = next(chunks, b"")
            except OSError as e:
                log.warning("stream %s: YouTube rejected URL (attempt %s): %s", video_id, attempt, e)
                drop_cached_url(video_id)
                continue
            return url, size, mime, start, end, _chain(first, chunks)
        return None

    def _stream(self, video_id: str):
        """Relay audio to Kodi in bounded upstream ranges (YouTube 403s Kodi's open ranges)."""
        opened = self._open_stream(video_id)
        if not opened:
            return self._plain(502, b"Could not get YouTube audio.")
        url, size, mime, start, end, chunks = opened
        if size is None:  # no length in the URL: let Kodi fetch it directly
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()
            return None
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        partial = bool(self.headers.get("Range"))
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        pos, retried = start, False
        while True:
            try:
                for chunk in chunks:
                    self.wfile.write(chunk)
                    pos += len(chunk)
                return None
            except (BrokenPipeError, ConnectionResetError):
                return None  # Kodi closed the connection (seek, skip, stop)
            except OSError as e:
                # YouTube sometimes rejects a URL mid-song: get a fresh one, continue at pos.
                log.warning("stream %s: YouTube rejected URL at byte %s: %s", video_id, pos, e)
                drop_cached_url(video_id)
                url = None if retried else resolve_audio_url(video_id, log=log)
                if not url:
                    return None
                retried = True
                chunks = read_range(url, pos, end)

    def do_HEAD(self):  # noqa: N802 pylint: disable=invalid-name
        """
        Kodi probes stream items with HEAD, also for every item added to the playlist and
        every ~1.5 s for the playing one. Answer from the cache only, never look up a URL
        here (~10 s each would make queueing a long playlist take minutes).
        """
        m = _STREAM_PATH.fullmatch(urllib.parse.urlparse(self.path).path)
        if not m:
            self.send_response(404)
            self.end_headers()
            return None
        size, mime = audio_size_and_type(cached_audio_url(m.group(1)) or "")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        if size is not None:
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
        self.end_headers()
        return None

    def do_GET(self):  # noqa: N802 pylint: disable=invalid-name
        """Handle GET: /stream/..., /stop-yt, /log or ?url=...&action=..."""
        parsed = urllib.parse.urlparse(self.path)
        m = _STREAM_PATH.fullmatch(parsed.path)
        if m:
            return self._stream(m.group(1))
        path = _normalize_request_path(parsed.path or "/")
        pl = path.lower()
        # Aliases: /yt-stop is common; /logs mirrors /log. Match case-insensitively for clients
        # that alter path casing.
        if pl in ("/log", "/logs"):
            return self._log_tail()
        if pl in ("/stop-yt", "/yt-stop"):
            return self._stop_yt()
        if pl == "/stop-after":
            return self._stop_after()

        q = urllib.parse.parse_qs(parsed.query)
        if "url" not in q:
            log.warning("Request missing 'url' parameter")
            return self._plain(400, b"Error: No 'url' parameter provided.")

        url, action = q["url"][0], (q.get("action") or ["play"])[0].strip().lower()
        if action not in ACTION_REPLY:
            action = "play"
        download = (q.get("mode") or [""])[0].strip().lower() == "download"
        log.info("URL received: %s (action=%s%s)", url, action, ", download" if download else "")

        play_script = YT_PLAY_SCRIPT
        if not os.path.isfile(play_script):
            log.error("%s missing", play_script)
            return self._plain(500, b"Server error: play script not found.")

        env = {**os.environ, "YT_LISTENER_LOG": LOG_FILE}
        try:
            # Run via the same interpreter as this listener so a bad/shebang or CRLF
            # on yt_party_play.py cannot trigger ENOEXEC (Exec format error).
            proc_args = (
                [sys.executable, play_script, url]
                if action == "party"
                else [sys.executable, play_script, url, action]
            )
            if download:
                proc_args.append("--download")
            # Do not use Popen as context manager: exiting would terminate the child.
            proc = subprocess.Popen(  # pylint: disable=consider-using-with
                proc_args,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Register PID immediately so /stop-yt can SIGTERM before yt_party_play.py
            # reaches its own echo $$ (otherwise stop only sets cancel and downloads continue).
            try:
                register_instance(proc.pid)
            except OSError as e:
                log.warning("Could not register pid %s: %s", proc.pid, e)
        except OSError as e:
            log.exception("Failed to start yt_party_play.py: %s", e)
            return self._plain(500, b"Server error: failed to start play script.")

        log.info("Started %s for: %s (action=%s)", os.path.basename(play_script), url, action)
        self._plain(202, ACTION_REPLY[action])
        return None

    def _log_tail(self):
        lines = _last_log_lines(LOG_FILE, 20)
        text = "(log file empty or not found)\n" if not lines else "".join(lines)
        self._plain(200, text.encode("utf-8"))

    def _stop_yt(self):
        log.info("stop-yt: from %s", self.address_string())
        killed = _stop_downloads()
        self._plain(
            200,
            b"Stopped all YouTube download/queue processes."
            if killed
            else b"No YouTube download processes were running (cancel flag set for any new run).",
        )

    def _stop_after(self):
        """Stop downloads/party mode (they would queue more songs) and drop the rest of the queue."""
        log.info("stop-after: from %s", self.address_string())
        _stop_downloads()
        try:
            user, password, jsonrpc_url = load_kodi_env(SCRIPT_DIR)
            removed = KodiJsonRpc(jsonrpc_url, user, password).stop_after_current()
        except RuntimeError as e:
            log.error("stop-after: %s", e)
            return self._plain(500, b"Could not reach Kodi.")
        log.info("stop-after: removed %s queued items", removed)
        return self._plain(200, b"Kodi will stop after this song.")


def _stop_downloads() -> list:
    """Set the cancel flag and kill every yt_party_play.py; returns the killed pids."""
    try:
        with open(YT_CANCEL, "w", encoding="utf-8") as f:
            f.write("1")
    except OSError as e:
        log.warning("stop-yt: cancel file: %s", e)
    killed = stop_all_instances()
    log.info("stop-yt: finished (killed pids=%s)", killed)
    return killed


if __name__ == "__main__":
    try:
        load_kodi_env(SCRIPT_DIR)  # exports YT_DLP_JS_RUNTIMES for /stream lookups
    except RuntimeError as e:
        log.warning("%s", e)
    log.info("Listening on port %s (log: %s)", LISTENER_PORT, LOG_FILE)
    # Threaded: a /stream lookup takes seconds and must not block /stop-yt or other items.
    server = ThreadingHTTPServer(("0.0.0.0", LISTENER_PORT), RequestHandler)
    server.daemon_threads = True  # don't wait for open Kodi streams on shutdown
    server.serve_forever()
