"""yt-dlp integration: JS runtime flags and cancellable subprocess downloads (matches shell scripts)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import List, Optional, Tuple

_VIDEO_ID_LINE = re.compile(r"^[0-9A-Za-z_-]{11}$")

# Set during subprocess runs so SIGTERM can kill the active yt-dlp (matches bash child kill).
_ACTIVE_DOWNLOADER: Optional[subprocess.Popen] = None


def kill_active_downloader() -> None:
    """Terminate the current yt-dlp child, if any."""
    global _ACTIVE_DOWNLOADER
    kill_process_tree(_ACTIVE_DOWNLOADER)
    _ACTIVE_DOWNLOADER = None


def _set_active(proc: Optional[subprocess.Popen]) -> None:
    global _ACTIVE_DOWNLOADER
    _ACTIVE_DOWNLOADER = proc


def js_runtimes_argv() -> List[str]:
    """
    Build argv fragment [--js-runtimes, NAME] when a runtime is available.

    Same rules as the old yt-dlp-js.sh: env YT_DLP_JS_RUNTIMES, else first of deno/node/bun on PATH.
    """
    env = os.environ.get("YT_DLP_JS_RUNTIMES", "").strip()
    if env:
        return ["--js-runtimes", env]
    for name in ("deno", "node", "bun"):
        if shutil.which(name):
            return ["--js-runtimes", name]
    return []


def ytdlp_executable() -> str:
    exe = shutil.which("yt-dlp")
    if not exe:
        raise FileNotFoundError("yt-dlp not found on PATH")
    return exe


def _base_cmd() -> List[str]:
    return [ytdlp_executable(), *js_runtimes_argv()]


def kill_process_tree(proc: Optional[subprocess.Popen], grace_s: float = 2.0) -> None:
    """SIGTERM then SIGKILL on the process group when possible (matches bash setsid behavior)."""
    if proc is None or proc.poll() is not None:
        return
    pid = proc.pid
    try:
        os.killpg(os.getpgid(pid), 15)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    t0 = time.monotonic()
    while time.monotonic() - t0 < grace_s:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(os.getpgid(pid), 9)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def fetch_flat_entries(
    url: str,
    *,
    cancel_path: str,
    log,
    max_items: Optional[int] = None,
) -> List[Tuple[str, str]]:
    """(video id, title) pairs of a playlist or single video (no download). Respects cancel file."""
    cmd = [*_base_cmd(), "--flat-playlist", "--print", "%(id)s\t%(title)s"]
    if max_items:
        cmd += ["--playlist-items", f"1:{max_items}"]
    cmd.append(url)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    _set_active(proc)
    try:
        while proc.poll() is None:
            if os.path.isfile(cancel_path):
                kill_process_tree(proc)
                proc.communicate(timeout=30)
                log.info("Cancelled during playlist fetch.")
                return []
            time.sleep(0.4)
        out, err = proc.communicate(timeout=60)
        lines: List[Tuple[str, str]] = []
        for line in (out or b"").decode("utf-8", errors="replace").splitlines():
            vid, _, title = line.strip().partition("\t")
            if _VIDEO_ID_LINE.match(vid):
                lines.append((vid, title if title not in ("", "NA") else vid))
        if proc.returncode != 0:
            log.warning(
                "yt-dlp playlist fetch exit %s: %s",
                proc.returncode,
                (err or b"").decode(errors="replace")[:500],
            )
        return lines
    finally:
        _set_active(None)


def fetch_first_video_id(url: str, *, cancel_path: str, log) -> Optional[str]:
    """First entry only (party seed from playlist URL)."""
    cmd = [
        *_base_cmd(),
        "--flat-playlist",
        "--playlist-items",
        "1",
        "--print",
        "id",
        url,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    _set_active(proc)
    try:
        while proc.poll() is None:
            if os.path.isfile(cancel_path):
                kill_process_tree(proc)
                proc.communicate(timeout=30)
                return None
            time.sleep(0.4)
        out, _err = proc.communicate(timeout=60)
        vid = ""
        for line in (out or b"").decode("utf-8", errors="replace").splitlines():
            s = line.strip()
            if _VIDEO_ID_LINE.match(s):
                vid = s
                break
        if len(vid) != 11:
            log.error("Failed to resolve first video id from URL.")
            return None
        return vid
    finally:
        _set_active(None)


def fetch_video_duration_seconds(
    video_id: str,
    *,
    cancel_path: str,
    log,
) -> Optional[float]:
    """
    Duration in seconds from yt-dlp metadata only (no download).
    Returns None if unavailable (live, error) or on cancel.
    """
    url = f"https://youtu.be/{video_id}"
    cmd = [
        *_base_cmd(),
        "--no-download",
        "--print",
        "%(duration)s",
        url,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    _set_active(proc)
    try:
        while proc.poll() is None:
            if os.path.isfile(cancel_path):
                log.info("Cancelled during duration fetch.")
                kill_process_tree(proc)
                proc.communicate(timeout=30)
                return None
            time.sleep(0.4)
        out, err = proc.communicate(timeout=60)
        if proc.returncode != 0:
            log.debug(
                "Duration fetch exit %s for %s: %s",
                proc.returncode,
                video_id,
                (err or b"").decode(errors="replace")[:300],
            )
            return None
        raw = (out or b"").decode("utf-8", errors="replace").strip().splitlines()
        if not raw:
            return None
        last = raw[-1].strip()
        if last.upper() in ("NA", "NONE", "NULL", ""):
            return None
        try:
            sec = float(last)
        except ValueError:
            return None
        if sec <= 0:
            return None
        return sec
    finally:
        _set_active(None)


def download_mp3(
    video_id: str,
    output_template: str,
    *,
    cancel_path: str,
    log,
) -> Optional[str]:
    """
    Download audio as MP3; print final path via after_move:filepath (same as bash).

    Returns filesystem path or None on failure/cancel.
    """
    log.info("Downloading video ID: %s", video_id)
    url = f"https://youtu.be/{video_id}"
    cmd = [
        *_base_cmd(),
        "-x",
        "--audio-format",
        "mp3",
        "--no-windows-filenames",
        # mtime = download time (not upload date) so cache pruning deletes the oldest downloads.
        "--no-mtime",
        "-o",
        output_template,
        "--print",
        "after_move:filepath",
        url,
    ]
    fd, out_capture = tempfile.mkstemp(prefix="ytdlp-path-")
    os.close(fd)
    # Unnamed temp file (not a pipe) so a chatty yt-dlp can't block on a full stderr buffer.
    errf = tempfile.TemporaryFile()
    try:
        with open(out_capture, "wb") as outf:
            proc = subprocess.Popen(
                cmd,
                stdout=outf,
                stderr=errf,
                start_new_session=True,
            )
        _set_active(proc)
        try:
            while proc.poll() is None:
                if os.path.isfile(cancel_path):
                    log.info("Stop requested; terminating active download...")
                    kill_process_tree(proc)
                    proc.wait(timeout=30)
                    return None
                time.sleep(0.4)
            rc = proc.wait(timeout=60)
            with open(out_capture, encoding="utf-8", errors="replace") as f:
                text = f.read().strip()
            lines = [x.strip() for x in text.splitlines() if x.strip()]
            path_line = lines[-1] if lines else ""
            if rc != 0 or not path_line:
                errf.seek(0)
                err = errf.read().decode("utf-8", errors="replace").strip()
                log.error("yt-dlp failed (exit %s) for %s: %s", rc, video_id, err[-500:])
                return None
            if os.path.isfile(path_line):
                return path_line
            log.error("yt-dlp reported path but file missing: %s", path_line)
            return None
        finally:
            _set_active(None)
    finally:
        errf.close()
        try:
            os.remove(out_capture)
        except OSError:
            pass


def fetch_audio_url(video_id: str, *, log) -> Optional[str]:
    """Direct (expiring) URL of the best audio stream, for Kodi to play without downloading."""
    cmd = [*_base_cmd(), "-f", "bestaudio", "-g", f"https://youtu.be/{video_id}"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    except subprocess.TimeoutExpired:
        log.error("yt-dlp audio URL lookup timed out for %s", video_id)
        return None
    url = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
    if res.returncode != 0 or not url.startswith("http"):
        log.error("yt-dlp audio URL lookup failed for %s: %s", video_id, res.stderr.strip()[-500:])
        return None
    return url
