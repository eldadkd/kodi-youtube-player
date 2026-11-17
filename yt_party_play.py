#!/usr/bin/env python3
"""
YouTube → Kodi: play, add, play-next, and party (YouTube Mix of the seed).

Invoked by yt-listener.py as:
  yt_party_play.py <url>              # party (default action)
  yt_party_play.py <url> <action>     # play | add | play-next
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import urllib.parse

from instance_control import (
    InstanceSession,
    YT_CANCEL,
    cancel_requested,
    clear_cancel_flag,
    wait_until_cancel_or,
)
from kodi_helper import play_media, send_notification
from kodi_jsonrpc import KodiJsonRpc, load_kodi_env
from logger_helper import get_logger
from party_cache import (
    normalize_playback_path,
    paths_mean_different_track,
    prune_cache,
)
from ytdlp_helper import (
    download_mp3,
    fetch_first_video_id,
    fetch_flat_entries,
    fetch_video_duration_seconds,
    kill_active_downloader,
    ytdlp_executable,
)
from stream_cache import resolve_audio_url, stream_url, video_id_from_stream_url

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
logger = get_logger(__name__)

PARTY_MAX_NEXT_SECONDS = 600  # skip downloading next track if longer than this (party mode)
PARTY_MIX_SIZE = 50  # Mix entries per fetch; YouTube drifts off-topic further down
DEFAULT_CACHE_DIR = "/tmp"
DEFAULT_CACHE_MAX_MB = 250
OUT_NAME = "ytm-%(title)s [%(id)s].%(ext)s"  # id keeps same-titled videos apart


def parse_args(argv: list[str]) -> argparse.Namespace:
    """CLI: URL plus optional action (party when omitted, as started by the listener)."""
    parser = argparse.ArgumentParser(
        description="Download YouTube audio with yt-dlp and queue or play in Kodi."
    )
    parser.add_argument("url", help="Video or playlist URL")
    parser.add_argument(
        "action",
        nargs="?",
        choices=("play", "add", "play-next", "party"),
        default="party",
        help="play (replace queue), add, play-next, or party (default when omitted)",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="play/add/play-next: download MP3s instead of streaming",
    )
    parser.add_argument(
        "--max-mb",
        type=int,
        default=None,
        help=f"Download cache cap in MB (default: env YT_CACHE_MAX_MB or {DEFAULT_CACHE_MAX_MB})",
    )
    return parser.parse_args(argv)


def _cache_max_bytes(args: argparse.Namespace) -> int:
    if args.max_mb is not None:
        return max(1, args.max_mb) * 1024 * 1024
    env = os.environ.get("YT_CACHE_MAX_MB", str(DEFAULT_CACHE_MAX_MB)).strip()
    try:
        mb = int(env)
    except ValueError:
        mb = DEFAULT_CACHE_MAX_MB
    return max(1, mb) * 1024 * 1024


def _cache_dir() -> str:
    return os.environ.get("YT_CACHE_DIR", DEFAULT_CACHE_DIR).strip() or DEFAULT_CACHE_DIR


def remove_ytm_mp3s(directory: str, keep: str) -> None:
    """Remove ytm-*.mp3 in directory except keep (play / party reset)."""
    keep = normalize_playback_path(keep)
    for fp in glob.glob(os.path.join(directory, "ytm-*.mp3")):
        if normalize_playback_path(fp) == keep:
            continue
        try:
            os.remove(fp)
        except OSError:
            pass


def replace_queue_and_play(rpc: KodiJsonRpc, cache_dir: str, path: str) -> None:
    """
    Swap Kodi's music queue for path. Called only once path is downloaded, so the old
    track keeps playing during the download. PlayMedia replaces it (no Player.Stop).
    """
    try:
        rpc.playlist_clear_music()
    except RuntimeError as e:
        logger.warning("Playlist clear: %s", e)
    play_media(path)
    remove_ytm_mp3s(cache_dir, keep=path)


def insert_play_next(rpc: KodiJsonRpc, path: str, after: str) -> None:
    """
    Insert path right after `after` (the previous play-next track) so a playlist keeps
    its order; otherwise right after the current track.
    """
    items = [normalize_playback_path(f) for f in rpc.playlist_item_files()]
    prev = normalize_playback_path(after)
    if prev and prev in items:
        position = items.index(prev) + 1
    else:
        position = rpc.current_playlist_position() + 1
    rpc.playlist_insert_file(position, path)


def make_room(rpc: KodiJsonRpc, cache_dir: str, max_bytes: int) -> bool:
    """
    Before a download: delete the oldest cached tracks Kodi no longer needs until the cache
    is under max_bytes. If only current/queued tracks remain, wait for playback to move on.
    Returns False on cancel.
    """
    while True:
        needed = rpc.upcoming_playlist_files()
        if prune_cache(cache_dir, max_bytes, needed) < max_bytes or not needed:
            return True
        playing = rpc.get_current_playing_file()
        logger.info("Cache full (%d MB); waiting for the current track to finish...",
                    max_bytes // (1024 * 1024))
        if not wait_until_cancel_or(
            lambda: rpc.get_current_playing_file() != playing,
            cancel_path=YT_CANCEL,
            interval_s=2.0,
        ):
            return False


def playlist_contains_file(rpc: KodiJsonRpc, local_path: str) -> bool:
    """True if the music playlist already references this on-disk path."""
    target = normalize_playback_path(local_path)
    if not target:
        return False
    for item in rpc.playlist_item_files():
        if normalize_playback_path(item) == target:
            return True
    return False


def mix_url(video_id: str) -> str:
    """YouTube's auto-generated Mix playlist for video_id."""
    return f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"


def pick_next_party_track(
    mix: list[str], current_id: str, seen_ids: set[str], cancel_path: str
) -> str | None:
    """
    Pop the next unplayed track from mix (YouTube Mix ids, in order).
    When mix runs out, refill it once from current_id's Mix.
    Skips tracks longer than PARTY_MAX_NEXT_SECONDS when duration is known.
    """
    refilled = False
    while True:
        if not mix:
            if refilled:
                return None
            refilled = True
            entries = fetch_flat_entries(
                mix_url(current_id),
                cancel_path=cancel_path,
                log=logger,
                max_items=PARTY_MIX_SIZE,
            )
            mix.extend(vid for vid, _ in entries)
            continue
        cand = mix.pop(0)
        if cand in seen_ids:
            continue
        seen_ids.add(cand)
        dur = fetch_video_duration_seconds(cand, cancel_path=cancel_path, log=logger)
        if cancel_requested():
            return None
        if dur is not None and dur > PARTY_MAX_NEXT_SECONDS:
            logger.info(
                "Skipping next (too long: %.0fs > %ds): %s",
                dur,
                PARTY_MAX_NEXT_SECONDS,
                cand,
            )
            continue
        return cand


def wait_until_track_changes(
    rpc: KodiJsonRpc, previous_local_path: str, cancel_path: str
) -> bool:
    """True when Kodi is playing a different file than previous_local_path; False on cancel."""

    def _changed() -> bool:
        cur = rpc.get_current_playing_file()
        return bool(cur) and paths_mean_different_track(cur, previous_local_path)

    return wait_until_cancel_or(_changed, cancel_path=cancel_path, interval_s=1.0)


def run_party(url: str, rpc: KodiJsonRpc, cache_dir: str, max_bytes: int) -> int:
    """Party: play the seed, then its YouTube Mix (download while playing → Playlist.Add)."""
    logger.info("Party mode: resolving first video...")
    seed_id = fetch_first_video_id(url, cancel_path=YT_CANCEL, log=logger)
    if not seed_id:
        if cancel_requested():
            clear_cancel_flag()
            logger.info("Party stopped during resolve (cancel).")
            return 0
        send_notification("YouTube Party", "Could not resolve first video")
        return 1

    out_template = os.path.join(cache_dir, OUT_NAME)

    send_notification("YouTube Party", "Downloading first track...")
    current_id = seed_id
    seen_ids: set[str] = {current_id}
    mix: list[str] = []

    current_file = download_mp3(
        current_id, out_template, cancel_path=YT_CANCEL, log=logger
    )
    if not current_file:
        if cancel_requested():
            clear_cancel_flag()
            return 0
        send_notification("YouTube Party", "Download failed")
        return 1

    logger.info("Replacing Kodi queue and starting playback: %s", current_file)
    send_notification("YouTube Party", "Playing")
    replace_queue_and_play(rpc, cache_dir, current_file)

    while True:
        if cancel_requested():
            logger.info("Stop requested (cancel file).")
            clear_cancel_flag()
            return 0

        logger.info("Picking next Mix track...")
        next_id = pick_next_party_track(mix, current_id, seen_ids, YT_CANCEL)
        logger.info("Next Mix track: %s", next_id)
        if not next_id:
            logger.info("Mix exhausted; party mode ends.")
            send_notification("YouTube Party", "No more Mix tracks")
            return 0

        if not make_room(rpc, cache_dir, max_bytes):
            logger.info("Cancelled while waiting for cache space.")
            clear_cancel_flag()
            return 0

        send_notification("YouTube Party", "Loading next...")
        logger.info("Downloading next: %s", next_id)
        next_path = download_mp3(
            next_id, out_template, cancel_path=YT_CANCEL, log=logger
        )
        if not next_path:
            if cancel_requested():
                clear_cancel_flag()
                return 0
            # Private/deleted/blocked videos are common in Mixes: skip, don't end the party.
            logger.warning("Next download failed, skipping: %s", next_id)
            send_notification("YouTube Party", "Next download failed — skipping")
            continue
        logger.info("Downloaded next: %s", next_path)
        seen_ids.add(next_id)
        if playlist_contains_file(rpc, next_path):
            logger.info("Kodi playlist already has %s; skipping add, fetching another.", next_path)
            try:
                os.remove(next_path)
            except OSError:
                pass
            current_id = next_id
            continue

        if rpc.is_playback_idle():
            logger.info(
                "Playback idle before next was ready; starting playback instead of queue."
            )
            send_notification("YouTube Party", "Playing")
            play_media(next_path)
            current_id = next_id
            current_file = next_path
            continue

        logger.info("Queueing next: %s", next_path)
        try:
            rpc.playlist_add_file(next_path)
        except RuntimeError as e:
            logger.error("Playlist.Add failed, skipping %s: %s", next_path, e)
            continue

        logger.info("Waiting for playback to leave %s ...", current_file)
        if not wait_until_track_changes(rpc, current_file, YT_CANCEL):
            logger.info("Cancelled while waiting for track change.")
            clear_cancel_flag()
            return 0

        current_id = next_id
        current_file = next_path


def run_standard(
    action: str, url: str, rpc: KodiJsonRpc, cache_dir: str, max_bytes: int
) -> int:
    """
    play: download the first track, then replace Kodi's music queue with it.
    add / play-next: leave playback and playlist as-is.
    Before each later download, make_room keeps the cache under max_bytes.
    """
    out_template = os.path.join(cache_dir, OUT_NAME)

    if action == "play":
        logger.info("Kodi queue will be replaced once the first track is downloaded...")
    elif action == "add":
        logger.info("Appending to Kodi playlist (no clear, no stop)...")
    else:
        logger.info("Inserting after the current track (play-next)...")

    logger.info("Fetching playlist metadata...")
    ids = [vid for vid, _ in fetch_flat_entries(url, cancel_path=YT_CANCEL, log=logger)]
    if not ids:
        if cancel_requested():
            clear_cancel_flag()
            logger.info("Stopped during playlist fetch (cancel).")
            return 0
        logger.error("Failed to fetch playlist (empty or error).")
        return 1

    first = True
    last_path = ""
    failed = 0
    for vid in ids:
        if cancel_requested():
            logger.info("Stop requested before next download.")
            clear_cancel_flag()
            return 0

        replacing = action == "play" and first
        # The old queue's files are deleted by replace_queue_and_play, so no need to wait for room.
        if not replacing and not make_room(rpc, cache_dir, max_bytes):
            logger.info("Cancelled while waiting for cache space.")
            clear_cancel_flag()
            return 0

        send_notification("YouTube", f"Downloading: {vid}")
        path = download_mp3(vid, out_template, cancel_path=YT_CANCEL, log=logger)
        if not path:
            if cancel_requested():
                clear_cancel_flag()
                return 0
            # Private/deleted videos are common in playlists: skip, don't abort.
            failed += 1
            logger.warning("Download failed, skipping: %s", vid)
            send_notification("YouTube", f"Download failed: {vid}")
            continue

        if replacing:
            logger.info("Replacing Kodi queue and starting playback: %s", path)
            replace_queue_and_play(rpc, cache_dir, path)
            first = False
        elif rpc.is_playback_idle():
            logger.info(
                "Playback idle before track was ready; starting playback instead of queue."
            )
            play_media(path)
        else:
            try:
                if action == "play-next":
                    insert_play_next(rpc, path, after=last_path)
                else:
                    rpc.playlist_add_file(path)
            except RuntimeError as e:
                logger.error("Adding to Kodi playlist failed: %s", e)
                return 1
        last_path = path

    logger.info("Finished queue load (%s tracks, %s failed).", len(ids), failed)
    return 1 if failed == len(ids) else 0


def in_play_order(rpc: KodiJsonRpc, entries: list) -> list:
    """
    entries in the order Kodi will play them (it may be shuffled), starting at the current
    track, so pre-resolving reaches the next songs first.
    """
    try:
        order = [video_id_from_stream_url(urllib.parse.unquote(f)) for f in rpc.playlist_item_files()]
    except RuntimeError:
        return entries
    pos = max(rpc.current_playlist_position(), 0)
    rank: dict[str, int] = {}
    for i, vid in enumerate(order[pos:] + order[:pos]):
        if vid:
            rank.setdefault(vid, i)
    return sorted(entries, key=lambda e: rank.get(e[0], len(order)))


def run_stream(action: str, url: str, rpc: KodiJsonRpc, cache_dir: str) -> int:
    """
    play / add / play-next without downloading. Kodi gets listener /stream URLs, which
    redirect to direct YouTube audio. The first track's URL is looked up before touching
    Kodi (old playback continues meanwhile); the rest are looked up afterwards in queue
    order so skipping to the next track is instant.
    """
    logger.info("Fetching playlist metadata (stream mode)...")
    entries = fetch_flat_entries(url, cancel_path=YT_CANCEL, log=logger)
    if not entries:
        if cancel_requested():
            clear_cancel_flag()
            logger.info("Stopped during playlist fetch (cancel).")
            return 0
        logger.error("Failed to fetch playlist (empty or error).")
        return 1

    # Drop leading unplayable videos (private/deleted) so playback starts on a good one.
    while entries and not resolve_audio_url(entries[0][0], log=logger):
        send_notification("YouTube", f"Unavailable: {entries[0][1]}")
        entries.pop(0)
        if cancel_requested():
            clear_cancel_flag()
            return 0
    if not entries:
        return 1

    files = [stream_url(vid, title) for vid, title in entries]
    try:
        idle = rpc.is_playback_idle()
        if action == "play":
            rpc.playlist_clear_music()
            position = 0
            rpc.playlist_add_files(files)
        elif action == "play-next":
            position = rpc.current_playlist_position() + 1
            rpc.playlist_insert_files(position, files)
        else:
            position = len(rpc.playlist_item_files())
            rpc.playlist_add_files(files)
        if action == "play" or idle:
            # Kodi's shuffle may have moved the resolved first track: swap it back to position.
            items = [urllib.parse.unquote(f) for f in rpc.playlist_item_files()]
            first = urllib.parse.unquote(files[0])
            if first in items and items.index(first) != position:
                rpc.playlist_swap(items.index(first), position)
            rpc.play_playlist_position(position)
    except RuntimeError as e:
        logger.error("Queueing in Kodi failed: %s", e)
        return 1
    if action == "play":
        remove_ytm_mp3s(cache_dir, keep="")  # downloads from the replaced queue
    logger.info("Queued %s tracks (%s); pre-resolving stream URLs...", len(files), action)

    for vid, title in in_play_order(rpc, entries[1:]):
        if cancel_requested():
            logger.info("Stop requested; remaining URLs resolve when Kodi plays them.")
            clear_cancel_flag()
            return 0
        if not resolve_audio_url(vid, log=logger):
            # Kodi stops the whole playlist on a failed item, so drop it before it gets there.
            logger.warning("No stream URL for %s (%s); removing it from Kodi.", vid, title)
            remove_from_playlist(rpc, vid)
    logger.info("All stream URLs resolved.")
    return 0


def remove_from_playlist(rpc: KodiJsonRpc, video_id: str) -> None:
    """Remove video_id's stream entry from Kodi's music playlist (not if it is playing)."""
    items = [video_id_from_stream_url(urllib.parse.unquote(f)) for f in rpc.playlist_item_files()]
    if video_id not in items:
        return
    position = items.index(video_id)
    if position == rpc.current_playlist_position():
        return
    try:
        rpc.playlist_remove(position)
    except RuntimeError as e:
        logger.warning("Playlist.Remove failed for %s: %s", video_id, e)


def main() -> int:
    """
    play — download first track, then replace music queue with it; Playlist.Add the rest.
    add — append downloads with Playlist.Add.
    play-next — each track via Playlist.Insert after the current track (order kept).
    party — first playlist entry only as seed; play its YouTube Mix; prune ytm cache.
    Without --download, play / add / play-next stream instead (see run_stream).
    """
    args = parse_args(sys.argv[1:])

    try:
        ytdlp_executable()
    except FileNotFoundError as e:
        logger.error("%s", e)
        return 1

    try:
        user, password, jsonrpc_url = load_kodi_env(SCRIPT_DIR)
    except RuntimeError as e:
        logger.error("%s", e)
        return 1

    rpc = KodiJsonRpc(jsonrpc_url, user, password)
    action = args.action

    kill_others = action in ("play", "party")
    session = InstanceSession(os.getpid(), kill_active_downloader)
    session.start(kill_others_first=kill_others)
    clear_cancel_flag()

    try:
        if action == "party":
            return run_party(args.url, rpc, _cache_dir(), _cache_max_bytes(args))
        if args.download:
            return run_standard(action, args.url, rpc, _cache_dir(), _cache_max_bytes(args))
        return run_stream(action, args.url, rpc, _cache_dir())
    finally:
        logger.info("--- yt_party_play.py finished (action=%s) ---", action)


if __name__ == "__main__":
    raise SystemExit(main())
