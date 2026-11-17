"""Kodi JSON-RPC over HTTP (same contract as the former bash play scripts)."""

from __future__ import annotations

import base64
import json
import os
import shlex
import urllib.error
import urllib.request
from typing import Any, Optional


def load_kodi_env(script_dir: str) -> tuple[str, str, str]:
    """
    Read KODI_JSONRPC_USER / KODI_JSONRPC_PASS from kodi-jsonrpc.env next to scripts.
    YT_DLP_JS_RUNTIMES there is exported to os.environ (a real env var wins).

    Values are written by setup.sh using bash %q; we parse with shlex like a shell word.
    """
    path = os.path.join(script_dir, "kodi-jsonrpc.env")
    user, password = "", ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw = line.partition("=")
                key, raw = key.strip(), raw.strip()
                try:
                    parts = shlex.split(raw, posix=True)
                    val = parts[0] if parts else ""
                except ValueError:
                    val = raw
                if key == "KODI_JSONRPC_USER":
                    user = val
                elif key == "KODI_JSONRPC_PASS":
                    password = val
                elif key == "YT_DLP_JS_RUNTIMES":
                    os.environ.setdefault(key, val)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Missing {path}. Run ./setup.sh to create kodi-jsonrpc.env."
        ) from e
    if not user:
        raise RuntimeError(
            f"Kodi JSON-RPC username missing in {path}. Run ./setup.sh."
        )
    base = os.environ.get("KODI_JSONRPC_URL", "http://localhost:8080/jsonrpc").rstrip("/")
    if not base.endswith("/jsonrpc"):
        base = base.rstrip("/") + "/jsonrpc"
    return user, password, base


class KodiJsonRpc:
    """Minimal JSON-RPC client for playlist and player queries used by the play scripts."""

    def __init__(self, jsonrpc_url: str, user: str, password: str, timeout: float = 30.0):
        self._url = jsonrpc_url
        self._timeout = timeout
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}",
        }

    def call(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=payload, headers=self._headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Kodi JSON-RPC HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"Kodi JSON-RPC request failed: {e}") from e
        if "error" in data:
            raise RuntimeError(f"Kodi JSON-RPC error: {data['error']}")
        return data

    def playlist_clear_music(self) -> None:
        self.call("Playlist.Clear", {"playlistid": 0})

    def playlist_add_file(self, filepath: str) -> None:
        self.call(
            "Playlist.Add",
            {"playlistid": 0, "item": {"file": filepath}},
        )

    def playlist_add_files(self, files: list[str]) -> None:
        """Append many items in one call (order kept)."""
        self.call(
            "Playlist.Add",
            {"playlistid": 0, "item": [{"file": f} for f in files]},
        )

    def playlist_insert_files(self, position: int, files: list[str]) -> None:
        """Insert many items at position in one call (order kept)."""
        self.call(
            "Playlist.Insert",
            {"playlistid": 0, "position": position, "item": [{"file": f} for f in files]},
        )

    def playlist_swap(self, position1: int, position2: int) -> None:
        self.call(
            "Playlist.Swap",
            {"playlistid": 0, "position1": position1, "position2": position2},
        )

    def playlist_remove(self, position: int) -> None:
        self.call("Playlist.Remove", {"playlistid": 0, "position": position})

    def stop_after_current(self) -> int:
        """
        Make Kodi stop when the current track ends: turn repeat off and remove every
        music playlist item after the current one. Returns how many were removed.
        """
        pid = self.get_active_player_id_for_audio()
        position = self.current_playlist_position()
        if pid is None or position < 0:
            return 0
        self.call("Player.SetRepeat", {"playerid": pid, "repeat": "off"})
        count = len(self.playlist_item_files())
        for p in range(count - 1, position, -1):  # from the end so positions stay valid
            self.playlist_remove(p)
        return count - 1 - position

    def play_playlist_position(self, position: int) -> None:
        self.call("Player.Open", {"item": {"playlistid": 0, "position": position}})

    def playlist_insert_file(self, position: int, filepath: str) -> None:
        self.call(
            "Playlist.Insert",
            {"playlistid": 0, "position": position, "item": {"file": filepath}},
        )

    def current_playlist_position(self) -> int:
        """Index of the playing item on the music playlist, or -1 when nothing plays."""
        pid = self.get_active_player_id_for_audio()
        if pid is None:
            return -1
        try:
            data = self.call(
                "Player.GetProperties", {"playerid": pid, "properties": ["position"]}
            )
        except RuntimeError:
            return -1
        return int((data.get("result") or {}).get("position", -1))

    def get_active_player_id_for_audio(self) -> Optional[int]:
        """Prefer audio player; if none, use first active player (MP3 may use video player)."""
        try:
            data = self.call("Player.GetActivePlayers", {})
        except RuntimeError:
            return None
        rows = data.get("result") or []
        for p in rows:
            if p.get("type") == "audio":
                pid = p.get("playerid")
                return int(pid) if pid is not None else None
        if rows:
            pid = rows[0].get("playerid")
            return int(pid) if pid is not None else None
        return None

    def get_player_file(self, playerid: int) -> str:
        data = self.call(
            "Player.GetItem",
            {"playerid": playerid, "properties": ["file"]},
        )
        item = (data.get("result") or {}).get("item") or {}
        return (item.get("file") or "").strip()

    def get_current_playing_file(self) -> str:
        """Kodi file string (may be file:// or percent-encoded)."""
        pid = self.get_active_player_id_for_audio()
        if pid is None:
            return ""
        return self.get_player_file(pid)

    def is_playback_idle(self) -> bool:
        """
        True when nothing is actively playing (no audio player or empty current item).
        Used to start the next download with PlayMedia instead of appending when the
        previous track ended before the download finished.
        """
        if self.get_active_player_id_for_audio() is None:
            return True
        cur = self.get_current_playing_file()
        return not cur.strip()

    def playlist_item_files(self) -> list[str]:
        """All file paths on music playlist id 0 (for duplicate checks)."""
        try:
            data = self.call(
                "Playlist.GetItems",
                {"playlistid": 0, "properties": ["file"]},
            )
        except RuntimeError:
            return []
        out: list[str] = []
        for it in (data.get("result") or {}).get("items") or []:
            f = (it.get("file") or "").strip()
            if f:
                out.append(f)
        return out

    def upcoming_playlist_files(self) -> list[str]:
        """
        Files Kodi still needs: the current track and everything after it on the music playlist.
        Empty when nothing is playing.
        """
        pid = self.get_active_player_id_for_audio()
        if pid is None:
            return []
        position = self.current_playlist_position()
        try:
            current = self.get_player_file(pid)
        except RuntimeError:
            current = ""
        if position < 0:
            # Unknown position: keep the whole playlist rather than risk deleting queued tracks.
            return [current, *self.playlist_item_files()]
        return [current, *self.playlist_item_files()[position:]]
