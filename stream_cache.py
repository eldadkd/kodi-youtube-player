"""Stream mode: Kodi queues listener URLs; direct YouTube audio URLs are cached on disk.

yt_party_play.py pre-resolves URLs in queue order; the listener's /stream handler reads
the cache (or resolves on demand) and relays the audio to Kodi. Both processes share the files.
"""

from __future__ import annotations

import os
import time
import urllib.parse
import urllib.request
from typing import Iterator, Optional

from ytdlp_helper import fetch_audio_url

LISTENER_PORT = 5000
STREAM_BASE = f"http://127.0.0.1:{LISTENER_PORT}/stream"
URL_CACHE_DIR = "/tmp/yt-stream-urls"
# Treat URLs as stale this long before YouTube's expiry so a song can finish playing.
EXPIRY_MARGIN_S = 30 * 60
# Used when a URL has no expire= parameter.
DEFAULT_LIFETIME_S = 60 * 60
# YouTube answers 403 to open-ended ranges (what Kodi sends) for most clients,
# so the listener relays the audio using bounded requests of this size.
CHUNK_BYTES = 1024 * 1024


def stream_url(video_id: str, title: str) -> str:
    """Kodi playlist entry; the title in the path becomes Kodi's label."""
    return f"{STREAM_BASE}/{video_id}/{urllib.parse.quote(title.replace('/', '-'), safe='')}"


def video_id_from_stream_url(url: str) -> Optional[str]:
    """Inverse of stream_url (None for other playlist entries)."""
    _, sep, rest = url.partition(f"{STREAM_BASE}/")
    return rest[:11] if sep and len(rest) >= 11 else None


def _cache_path(video_id: str) -> str:
    return os.path.join(URL_CACHE_DIR, video_id)


def _expires_at(url: str, fetched_at: float) -> float:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    try:
        return float(query["expire"][0])
    except (KeyError, IndexError, ValueError):
        return fetched_at + DEFAULT_LIFETIME_S


def cached_audio_url(video_id: str) -> Optional[str]:
    """Cached direct URL if it is still valid for at least EXPIRY_MARGIN_S."""
    path = _cache_path(video_id)
    try:
        with open(path, encoding="utf-8") as f:
            url = f.read().strip()
        fetched_at = os.path.getmtime(path)
    except OSError:
        return None
    if not url or _expires_at(url, fetched_at) - time.time() < EXPIRY_MARGIN_S:
        return None
    return url


def drop_cached_url(video_id: str) -> None:
    """Forget a URL YouTube rejected, so the next lookup fetches a fresh one."""
    try:
        os.remove(_cache_path(video_id))
    except OSError:
        pass


def audio_size_and_type(url: str) -> tuple[Optional[int], str]:
    """Total bytes (clen=) and MIME type (mime=) from a direct audio URL."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    try:
        size: Optional[int] = int(query["clen"][0])
    except (KeyError, IndexError, ValueError):
        size = None
    return size, query.get("mime", ["audio/webm"])[0]


def read_range(url: str, start: int, end: int) -> Iterator[bytes]:
    """Yield bytes start..end (inclusive) of url using bounded CHUNK_BYTES requests."""
    pos = start
    while pos <= end:
        stop = min(pos + CHUNK_BYTES - 1, end)
        req = urllib.request.Request(url, headers={"Range": f"bytes={pos}-{stop}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if not data:
            raise OSError(f"empty response for bytes {pos}-{stop}")
        yield data
        pos += len(data)


def _url_works(url: str) -> bool:
    """YouTube occasionally hands out a URL that is rejected from the first byte; probe 1 KB."""
    try:
        return bool(next(read_range(url, 0, 1023), b""))
    except OSError:
        return False


def resolve_audio_url(video_id: str, *, log) -> Optional[str]:
    """Cached direct URL, or look one up with yt-dlp (~10 s on a Pi), check it works and cache it."""
    url = cached_audio_url(video_id)
    if url:
        return url
    for attempt in (1, 2):
        url = fetch_audio_url(video_id, log=log)
        if not url:
            return None
        if _url_works(url):
            break
        log.warning("Audio URL for %s rejected by YouTube (attempt %s)", video_id, attempt)
    else:
        return None
    os.makedirs(URL_CACHE_DIR, exist_ok=True)
    tmp = f"{_cache_path(video_id)}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(url)
    os.replace(tmp, _cache_path(video_id))  # atomic: the other process never reads half a URL
    return url
