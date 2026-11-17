"""Download cache: path normalization and ytm-*.mp3 pruning under a size cap."""

from __future__ import annotations

import glob
import os
import urllib.parse
from typing import Iterable


def normalize_playback_path(path: str) -> str:
    """Map Kodi file string to local path for comparison with yt-dlp output."""
    if not path:
        return ""
    p = path.strip()
    if p.startswith("file://"):
        p = p[7:]
    p = urllib.parse.unquote(p)
    try:
        return os.path.realpath(p)
    except OSError:
        return os.path.normpath(p)


def paths_mean_different_track(kodi_reported: str, local_path: str) -> bool:
    """True when Kodi's current item is not the same on-disk file as local_path."""
    a = normalize_playback_path(kodi_reported)
    b = normalize_playback_path(local_path)
    if not a or not b:
        return False
    return a != b


def prune_cache(cache_dir: str, max_bytes: int, keep_paths: Iterable[str]) -> int:
    """
    Delete the oldest ytm-*.mp3 (by mtime) in cache_dir until the total is under max_bytes.

    Never deletes files in keep_paths (Kodi file strings or local paths).
    Returns the remaining total size in bytes.
    """
    keep = {normalize_playback_path(p) for p in keep_paths}
    files: list[tuple[float, int, str]] = []
    for fp in glob.glob(os.path.join(cache_dir, "ytm-*.mp3")):
        try:
            st = os.stat(fp)
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, fp))
    total = sum(size for _, size, _ in files)
    for _, size, fp in sorted(files):
        if total < max_bytes:
            break
        if normalize_playback_path(fp) in keep:
            continue
        try:
            os.remove(fp)
            total -= size
        except OSError:
            pass
    return total
