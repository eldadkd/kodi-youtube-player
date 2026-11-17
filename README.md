# YouTube → Kodi listener (OSMC)

Server that receives a YouTube playlist or video URL, downloads audio with yt-dlp, and plays it in Kodi.

## Flow

1. **yt-listener.py** – HTTP server on port 5000. GET `?url=<youtube_url>&action=play|add|play-next|party` starts **`yt_party_play.py`** in the background and **returns 202 Accepted immediately** (so clients like HTTP Shortcut don’t timeout). Download and playback continue on the server.
   - **GET `/stop-yt`** or **`/yt-stop`** (same behavior; paths are matched case-insensitively) stops **every** running **`yt_party_play.py`** (e.g. a playlist still downloading in the background after you used **play-next**, which starts a second process). Kodi’s JSON-RPC cannot tell “user pressed Stop” from “first track ended while the next is still downloading”, so automatic stop is done by calling `/stop-yt` when the user stops playback (see **Stop downloads when you press Stop in Kodi** below).
   - **GET `/log`** or **`/logs`** returns **plain text**: the **last 20 lines** of the listener log file (same file as **Logging** below; override path with env **`YT_LISTENER_LOG`**).
2. **`yt_party_play.py`** – All play actions (invoked by the listener; not run directly):
   - **Streaming (default for play / add / play-next)**: nothing is downloaded. The play script reads the playlist, looks up the first song's direct YouTube audio URL (~10 s on a Pi; the current song keeps playing meanwhile), then queues **all** songs in Kodi at once as `http://127.0.0.1:5000/stream/<id>/<title>` and starts the first one (~25 s after sending the URL). Kodi shows each title; Next/Previous, jumping and seeking work as usual. The remaining URLs are looked up in the background in Kodi’s play order (shuffle-aware) and cached in `/tmp/yt-stream-urls` until shortly before YouTube expires them (~6 h), so skips take ~2–3 s. Songs not looked up yet take ~10 s to start.
   - The listener **relays** the audio to Kodi instead of redirecting: YouTube rejects the open-ended byte ranges Kodi requests, so the listener fetches 1 MB ranges and maps Kodi’s `Range` requests onto them. New URLs are checked before caching, and a URL YouTube rejects mid-song is replaced and the song continues at the same byte.
   - Add **`&mode=download`** to use the download mode below instead (MP3 files, plays offline once downloaded).
   - **play** (with `mode=download`): Stops any older download, downloads the first track with yt-dlp as MP3 to `/tmp/`, and **only then** clears Kodi’s music playlist and plays it (the current song keeps playing meanwhile). The rest of the playlist is downloaded and queued at the end (JSON-RPC `Playlist.Add`).
   - **Disk cache**: downloads are capped by size, not track count — **250 MB** by default (env **`YT_CACHE_MAX_MB`**, directory env **`YT_CACHE_DIR`**, default `/tmp`). Before each download the **oldest** `ytm-*.mp3` that Kodi no longer needs (already played) are deleted; if only current/queued tracks remain, the download waits until the current track ends.
   - **add**: Does not stop or clear; appends the same downloads to the end of Kodi’s playlist (JSON-RPC `Playlist.Add`).
   - **play-next**: Does not stop or clear; inserts the downloads right **after the current track**, in playlist order (JSON-RPC `Playlist.Insert`).
   - On **download failure**, shows an on-screen notification in Kodi via **kodi-send** `Notification(title, message)` and skips to the next video (yt-dlp’s error is logged).
3. **Party mode** (`action=party`):
   - After the first track is downloaded, clears the **music** playlist and removes previous **`ytm-*.mp3`** files (same as **play**; party and normal mode don’t mix).
   - If the URL is a **playlist**, only the **first** video is used as the seed.
   - Plays the seed, then repeatedly: takes the next track from the seed’s **YouTube Mix** (`list=RD<id>`, first 50 entries; refilled from the current track’s Mix when used up), **downloads the next track while the current one plays**, and appends it with JSON-RPC `Playlist.Add`.
   - Uses the same size-capped disk cache as **play** (see above).
   - Stops with **`/stop-yt`** or **`/yt-stop`** (same PID/instance file as other actions). If the Mix can’t be resolved, party mode exits cleanly after the current queue ends.

## Prerequisites (OSMC)

- **Python 3** – usually preinstalled
- **yt-dlp** – `sudo apt update && sudo apt install yt-dlp` (or [install from upstream](https://github.com/yt-dlp/yt-dlp))
- **JavaScript runtime for YouTube (recommended)** – yt-dlp warns and may miss formats without **Deno**, **Node.js**, or **Bun** (see [EJS / JS runtimes](https://github.com/yt-dlp/yt-dlp/wiki/EJS)). **`yt_party_play.py`** passes `--js-runtimes` when one is on `PATH`, or you can set **`YT_DLP_JS_RUNTIMES`** in `kodi-jsonrpc.env`. Many videos still download without a runtime; installing one avoids warnings and future breakage. **Node.js must be v20+**: OSMC’s apt package is v12, which yt-dlp can’t use (downloads then fail with HTTP 403). On 32-bit OSMC, unpack the official `linux-armv7l` Node 22 build to `/opt/node22` and add `YT_DLP_JS_RUNTIMES=node:/opt/node22/bin/node` to `kodi-jsonrpc.env`.
- **Kodi HTTP control** – In Kodi: *Settings → Services → Control*: enable *Allow remote control via HTTP*, port **8080** (used to stop/clear playlist when using **play**). **`setup.sh` asks for that username and password** and stores them in `$INSTALL_DIR/kodi-jsonrpc.env` (mode 600). `yt_party_play.py` loads that file automatically; credentials are not kept in the repo scripts.
- **Kodi EventClient (kodi-send)** – In Kodi: *Settings → Services → Control*: enable *Allow programs on this system to control Kodi* (UDP port 9777). Used to play/queue media and to show a notification when a download fails. The `kodi-send` command must be on `PATH` (often provided by the OSMC/Kodi package).

## Install

```bash
git clone https://github.com/eldadkd/kodi-youtube-player.git
cd kodi-youtube-player
chmod +x setup.sh
./setup.sh
sudo systemctl start yt-listener
```

By default, `setup.sh` installs the scripts to `/yt-service`, prompts for Kodi HTTP JSON-RPC username/password, writes `kodi-jsonrpc.env` there, installs the systemd unit, and copies the **`script.stop.yt`** Kodi add-on into `~/.kodi/addons/`. **Restart Kodi**, then **enable** the add-on under *My add-ons → Services*. Edit `INSTALL_DIR` in `setup.sh` to use another path.

## Usage

From phone or browser:

| Action | Query | Behavior |
|--------|--------|----------|
| **Play** (default) | `?url=<youtube_url>` or `?url=...&action=play` | Replace the playlist and stream it (first song starts in ~10 s). |
| **Add to playlist** | `?url=<youtube_url>&action=add` | Append to current Kodi playlist (no clear/stop). |
| **Play next** | `?url=<youtube_url>&action=play-next` | Insert after the current song, in order. |
| **Download instead of stream** | add `&mode=download` to play / add / play-next | Download MP3s first (old behavior). |
| **Party mode** (autoplay chain) | `?url=<youtube_url>&action=party` | Clear music playlist; play first video only if URL is a playlist; play its YouTube Mix. |

Examples:

```
http://<OSMC-IP>:5000/?url=https://www.youtube.com/playlist?list=...
http://<OSMC-IP>:5000/?url=https://www.youtube.com/watch?v=...
http://<OSMC-IP>:5000/?url=https://youtu.be/VIDEO_ID&action=add
http://<OSMC-IP>:5000/?url=https://www.youtube.com/watch?v=...&action=party
```

The server responds immediately with **202 Accepted** and a short message; playback and downloads run in the background. URL must be query-encoded if needed (e.g. `%3D` for `=`).

| Action | Request |
|--------|---------|
| **Stop background downloads** | `http://<OSMC-IP>:5000/stop-yt` or `http://<OSMC-IP>:5000/yt-stop` |
| **Stop after this song** | `http://<OSMC-IP>:5000/stop-after` — stops downloads/party mode, turns repeat off and removes everything queued after the current song, so Kodi stops when it ends |
| **View last 20 log lines** | `http://<OSMC-IP>:5000/log` or `http://<OSMC-IP>:5000/logs` |

## Stop downloads when you press Stop in Kodi

Polling “is something playing?” breaks playlists: when track 1 finishes before track 2 is downloaded, nothing is playing — same as after Stop — so don’t use that. Use **`onPlayBackStopped`** instead: it runs for **user Stop**, not when a track **ends** and the next one will play.

### Kodi service add-on **`script.stop.yt`**

The repo includes **`script.stop.yt/`** (`addon.xml` + `service.py`). It subclasses **`xbmc.Player`** and on **`onPlayBackStopped`** does **GET `http://127.0.0.1:5000/stop-yt`**.

**Install on OSMC**

1. Run **`setup.sh`** (copies the add-on to **`~/.kodi/addons/script.stop.yt`**), or zip from the repo root: **`zip -r script.stop.yt.zip script.stop.yt/`** and use *Add-ons → Install from zip file*.
2. **Restart Kodi** if you copied files by hand (so Kodi rescans add-ons).
3. **Enable**: *Settings → Add-ons → My add-ons → Services → Stop YT downloads on Stop → Enable*.

Logs: Kodi log, prefix **`[script.stop.yt]`** (grep `stop-yt`).

**Note:** Any **Stop** in Kodi triggers **`/stop-yt`** (all `yt_party_play.py` / background download processes), not only YouTube. Disable the add-on if you don’t want that.

You can also call **`/stop-yt`** or **`/yt-stop`** from a phone or automation.

## Logging

The listener writes to **yt-listener.log** in the same directory as the scripts (unless **`YT_LISTENER_LOG`** is set). To watch what the server and play script are doing:

```bash
tail -f /yt-service/yt-listener.log
```

(Use your actual install path if different.)

Over HTTP, **GET `/log`** or **`/logs`** returns the **last 20 lines** of that file as **text/plain** (or a short message if the file is missing or empty). Handy from a phone browser without SSH.

## Service

- **Unit**: `yt-listener.service`
- **Start on boot**: enabled by `setup.sh` (`systemctl enable`)
- **Restart**: `Restart=always` with 3s delay
- **User**: `osmc`; install path is set in `setup.sh` (default `/yt-service`).

## Troubleshooting

- **202 but nothing plays** – Check `yt-listener.log` for progress and errors. Ensure Kodi is running, HTTP control is on (port 8080), and EventClient is enabled so `kodi-send` works. Try running `kodi-send --action='PlayMedia("/tmp/some.mp3")'` manually.
- **Service may start before Kodi** – If you send a URL before Kodi is up, the stop/clear step will fail; send the URL again once Kodi is running.
- **Play vs Add** – Use `action=play` (default) to replace the queue and start playing. Use `action=add` to append to the current playlist without stopping.
- **Download failed** – If a track fails to download, Kodi shows an on-screen notification (via `kodi-send`). Details are in `yt-listener.log`.

## License

[GPL-3.0](LICENSE)
