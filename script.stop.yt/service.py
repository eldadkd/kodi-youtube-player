"""Kodi service: on Stop, request yt-listener /stop-yt (not fired at normal track end)."""

import urllib.error
import urllib.request

import xbmc  # pylint: disable=import-error

ADDON_ID = "script.stop.yt"
# Same machine as Kodi (yt-listener binds 0.0.0.0:5000).
STOP_URL = "http://127.0.0.1:5000/stop-yt"


class StopYouTubeDownloadsPlayer(xbmc.Player):  # pylint: disable=too-few-public-methods
    """Forward Kodi Stop to the local stop-yt HTTP endpoint."""

    def onPlayBackStopped(self):  # pylint: disable=invalid-name
        """Kodi callback when user stops playback (not end-of-track)."""
        # A new URL replaces the playing track; if playback resumes, it was a switch, not Stop.
        # 5 s covers a stream item whose YouTube URL is still being looked up.
        xbmc.sleep(5000)
        if self.isPlaying():
            xbmc.log(f"[{ADDON_ID}] Playback resumed; not a user stop", xbmc.LOGINFO)
            return
        xbmc.log(f"[{ADDON_ID}] User stopped playback → {STOP_URL}", xbmc.LOGINFO)
        trigger_stop()

    def onPlayBackError(self):  # pylint: disable=invalid-name
        """Kodi stops the playlist when an item fails (e.g. YouTube 403): skip to the next one."""
        playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
        nxt = playlist.getposition() + 1
        if 0 < nxt < playlist.size():
            xbmc.log(f"[{ADDON_ID}] Playback failed; skipping to item {nxt}", xbmc.LOGINFO)
            self.play(playlist, startpos=nxt)


def trigger_stop():
    """GET stop-yt; log outcome (HTTP, network, or other failures)."""
    try:
        with urllib.request.urlopen(STOP_URL, timeout=3) as resp:
            xbmc.log(f"[{ADDON_ID}] stop-yt OK (HTTP {resp.status})", xbmc.LOGINFO)
    except urllib.error.HTTPError as e:
        xbmc.log(f"[{ADDON_ID}] stop-yt HTTP error: {e}", xbmc.LOGERROR)
    except urllib.error.URLError as e:
        xbmc.log(f"[{ADDON_ID}] stop-yt network error: {e}", xbmc.LOGERROR)
    except OSError as e:
        xbmc.log(f"[{ADDON_ID}] stop-yt unexpected: {e}", xbmc.LOGERROR)


# Instance must stay alive so Player callbacks work.
_player = StopYouTubeDownloadsPlayer()
_monitor = xbmc.Monitor()

while not _monitor.abortRequested():
    _monitor.waitForAbort(30)
