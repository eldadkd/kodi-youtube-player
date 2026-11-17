"""kodi-send helpers (notifications, PlayMedia). Playlist append uses JSON-RPC in kodi_jsonrpc."""

import subprocess


def _escape_kodi_string(path: str) -> str:
    """Escape for Kodi action strings that wrap paths in double quotes."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(title: str, message: str) -> None:
    """Show a Kodi toast via kodi-send."""
    subprocess.run(
        ["kodi-send", "--action", f"Notification({title}, {message})"],
        check=False,
    )


def play_media(media_path: str) -> None:
    """Start playback of a local file (quoted path for spaces)."""
    esc = _escape_kodi_string(media_path)
    subprocess.run(
        ["kodi-send", "--action", f'PlayMedia("{esc}")'],
        check=False,
    )

