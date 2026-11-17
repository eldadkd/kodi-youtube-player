"""Unit tests for helpers and yt_party_play CLI parsing (no Kodi / yt-dlp network)."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

# Keep test runs out of the real yt-listener.log (logger_helper opens it on import).
os.environ.setdefault(
    "YT_LISTENER_LOG", os.path.join(tempfile.gettempdir(), "yt-listener-test.log")
)

# Repo root on path for imports
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from party_cache import (  # noqa: E402
    normalize_playback_path,
    paths_mean_different_track,
    prune_cache,
)
from instance_control import wait_until_cancel_or  # noqa: E402
import kodi_jsonrpc  # noqa: E402
import ytdlp_helper  # noqa: E402


class TestPathNormalize(unittest.TestCase):
    def test_file_url_decode(self):
        p = normalize_playback_path("file:///tmp/foo%20bar.mp3")
        self.assertTrue(p.endswith("foo bar.mp3") or "foo bar" in p)

    def test_paths_differ(self):
        self.assertFalse(paths_mean_different_track("", "/tmp/a.mp3"))
        a = __file__
        b = __file__
        self.assertFalse(paths_mean_different_track(f"file://{a}", b))


def make_mp3s(d: str, count: int, size: int = 500_000) -> list[str]:
    """ytm-songN.mp3 files with increasing mtime (song0 is oldest)."""
    paths = []
    for i in range(count):
        fp = os.path.join(d, f"ytm-song{i}.mp3")
        with open(fp, "wb") as f:
            f.write(b"x" * size)
        os.utime(fp, (1_700_000_000 + i, 1_700_000_000 + i))
        paths.append(fp)
    return paths


class TestPruneCache(unittest.TestCase):
    def test_deletes_oldest_until_under_cap(self):
        with tempfile.TemporaryDirectory() as d:
            paths = make_mp3s(d, 3)
            total = prune_cache(d, max_bytes=1_200_000, keep_paths=[])
            self.assertEqual(sorted(glob_mp3(d)), paths[1:])
            self.assertEqual(total, 1_000_000)

    def test_never_deletes_kept_files(self):
        with tempfile.TemporaryDirectory() as d:
            paths = make_mp3s(d, 3)
            total = prune_cache(d, max_bytes=600_000, keep_paths=[f"file://{paths[0]}", paths[1]])
            self.assertEqual(sorted(glob_mp3(d)), paths[:2])
            self.assertEqual(total, 1_000_000)


def glob_mp3(d: str) -> list[str]:
    return [
        os.path.join(d, x)
        for x in os.listdir(d)
        if x.startswith("ytm-") and x.endswith(".mp3")
    ]


class TestWaitUntilCancelOr(unittest.TestCase):
    def test_predicate_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancel = os.path.join(tmp, "cancel")
            ok = wait_until_cancel_or(lambda: True, cancel_path=cancel, interval_s=0.05)
            self.assertTrue(ok)

    def test_cancel_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancel = os.path.join(tmp, "cancel")

            def never():
                return False

            def touch_cancel():
                time.sleep(0.15)
                open(cancel, "w", encoding="utf-8").close()

            threading.Thread(target=touch_cancel, daemon=True).start()
            ok = wait_until_cancel_or(never, cancel_path=cancel, interval_s=0.05)
            self.assertFalse(ok)


class TestLoadKodiEnv(unittest.TestCase):
    def test_parses_quoted_values(self):
        with tempfile.TemporaryDirectory() as d:
            envp = os.path.join(d, "kodi-jsonrpc.env")
            with open(envp, "w", encoding="utf-8") as f:
                f.write('KODI_JSONRPC_USER="my user"\n')
                f.write("KODI_JSONRPC_PASS=secret\n")
            user, pw, url = kodi_jsonrpc.load_kodi_env(d)
            self.assertEqual(user, "my user")
            self.assertEqual(pw, "secret")
            self.assertTrue(url.endswith("/jsonrpc"))

    def test_exports_js_runtimes(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "kodi-jsonrpc.env"), "w", encoding="utf-8") as f:
                f.write("KODI_JSONRPC_USER=u\nYT_DLP_JS_RUNTIMES=node:/opt/node22/bin/node\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                kodi_jsonrpc.load_kodi_env(d)
                self.assertEqual(os.environ["YT_DLP_JS_RUNTIMES"], "node:/opt/node22/bin/node")


class TestJsRuntimesArgv(unittest.TestCase):
    def test_env_override(self):
        with mock.patch.dict(os.environ, {"YT_DLP_JS_RUNTIMES": "node:/bin/node"}):
            self.assertEqual(ytdlp_helper.js_runtimes_argv(), ["--js-runtimes", "node:/bin/node"])

    def test_prefers_first_on_path(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("ytdlp_helper.shutil.which", side_effect=lambda n: f"/x/{n}" if n == "deno" else None):
                self.assertEqual(ytdlp_helper.js_runtimes_argv(), ["--js-runtimes", "deno"])


class TestPlaylistContainsFile(unittest.TestCase):
    def test_detects_normalized_match(self):
        from unittest.mock import MagicMock

        import yt_party_play  # noqa: E402

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            path = tmp.name
        try:
            rpc = MagicMock()
            rpc.playlist_item_files.return_value = [f"file://{path}"]
            self.assertTrue(yt_party_play.playlist_contains_file(rpc, path))
        finally:
            os.unlink(path)


class TestYtPartyPlayArgs(unittest.TestCase):
    def test_party_default_action(self):
        import yt_party_play  # noqa: E402

        ns = yt_party_play.parse_args(["https://youtu.be/dQw4w9WgXcQ"])
        self.assertEqual(ns.action, "party")

    def test_explicit_play(self):
        import yt_party_play  # noqa: E402

        ns = yt_party_play.parse_args(["https://youtu.be/dQw4w9WgXcQ", "play"])
        self.assertEqual(ns.action, "play")


class TestPartyMix(unittest.TestCase):
    def _pick(self, mix, seen, entries):
        import yt_party_play  # noqa: E402

        fetch = mock.MagicMock(return_value=[(v, v) for v in entries])
        with mock.patch.multiple(
            yt_party_play,
            fetch_flat_entries=fetch,
            fetch_video_duration_seconds=mock.MagicMock(return_value=200.0),
            cancel_requested=mock.MagicMock(return_value=False),
        ):
            nxt = yt_party_play.pick_next_party_track(mix, "s" * 11, seen, "/x")
        return nxt, fetch

    def test_fetches_seed_mix_and_skips_seed(self):
        mix: list[str] = []
        nxt, fetch = self._pick(mix, {"s" * 11}, ["s" * 11, "a" * 11, "b" * 11])
        self.assertEqual(nxt, "a" * 11)
        self.assertEqual(mix, ["b" * 11])
        self.assertIn("list=RD" + "s" * 11, fetch.call_args.args[0])

    def test_uses_pending_mix_without_fetching(self):
        nxt, fetch = self._pick(["b" * 11], set(), [])
        self.assertEqual(nxt, "b" * 11)
        fetch.assert_not_called()

    def test_none_when_refill_has_only_seen(self):
        nxt, fetch = self._pick([], {"s" * 11}, ["s" * 11])
        self.assertIsNone(nxt)
        fetch.assert_called_once()


class TestReplaceQueueOnlyAfterDownload(unittest.TestCase):
    """Regression: play/party must not touch current playback before the new file is ready."""

    def _run(self, action):
        import yt_party_play  # noqa: E402

        events = []
        rpc = mock.MagicMock()
        rpc.playlist_clear_music.side_effect = lambda: events.append("clear")
        rpc.upcoming_playlist_files.return_value = []
        rpc.is_playback_idle.return_value = False

        def download(*_a, **_k):
            events.append("download")
            return "/tmp/ytm-new.mp3"

        with mock.patch.multiple(
            yt_party_play,
            download_mp3=mock.MagicMock(side_effect=download),
            fetch_flat_entries=mock.MagicMock(return_value=[("aaaaaaaaaaa", "A")]),
            fetch_first_video_id=mock.MagicMock(return_value="aaaaaaaaaaa"),
            pick_next_party_track=mock.MagicMock(return_value=None),
            play_media=mock.MagicMock(side_effect=lambda p: events.append("play")),
            send_notification=mock.MagicMock(),
            remove_ytm_mp3s=mock.MagicMock(),
            clear_cancel_flag=mock.MagicMock(),
            cancel_requested=mock.MagicMock(return_value=False),
        ):
            if action == "party":
                yt_party_play.run_party("u", rpc, "/tmp", 10**9)
            else:
                yt_party_play.run_standard(action, "u", rpc, "/tmp", 10**9)
        return events

    def test_play(self):
        self.assertEqual(self._run("play"), ["download", "clear", "play"])

    def test_party(self):
        self.assertEqual(self._run("party")[:3], ["download", "clear", "play"])


class TestMakeRoom(unittest.TestCase):
    def test_waits_for_track_change_when_only_queued_files_remain(self):
        import yt_party_play  # noqa: E402

        with tempfile.TemporaryDirectory() as d:
            paths = make_mp3s(d, 2)
            rpc = mock.MagicMock()
            # First check: both files still needed; after the track changes only the second.
            rpc.upcoming_playlist_files.side_effect = [paths, paths[1:]]
            rpc.get_current_playing_file.side_effect = [paths[0], paths[1]]
            with mock.patch.object(yt_party_play, "YT_CANCEL", os.path.join(d, "cancel")):
                self.assertTrue(yt_party_play.make_room(rpc, d, max_bytes=600_000))
            self.assertEqual(glob_mp3(d), paths[1:])


class TestKillOtherInstances(unittest.TestCase):
    def test_kills_only_older_instances(self):
        import instance_control  # noqa: E402

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "instances")
            with open(path, "w", encoding="ascii") as f:
                f.write("101\n202\n303\n")
            with mock.patch.object(instance_control, "YT_INSTANCES", path), mock.patch.object(
                instance_control, "_kill_process_group"
            ) as kill:
                instance_control.kill_other_instances(202)
                self.assertEqual([c.args[0] for c in kill.call_args_list], [101])
                self.assertEqual(instance_control._read_pids(), [202, 303])


class TestInsertPlayNext(unittest.TestCase):
    def test_after_previous_play_next_track(self):
        import yt_party_play  # noqa: E402

        rpc = mock.MagicMock()
        rpc.playlist_item_files.return_value = ["/tmp/a.mp3", "/tmp/b.mp3", "/tmp/c.mp3"]
        yt_party_play.insert_play_next(rpc, "/tmp/new.mp3", after="/tmp/b.mp3")
        rpc.playlist_insert_file.assert_called_once_with(2, "/tmp/new.mp3")

    def test_after_current_track_first_time(self):
        import yt_party_play  # noqa: E402

        rpc = mock.MagicMock()
        rpc.playlist_item_files.return_value = ["/tmp/a.mp3", "/tmp/b.mp3"]
        rpc.current_playlist_position.return_value = 0
        yt_party_play.insert_play_next(rpc, "/tmp/new.mp3", after="")
        rpc.playlist_insert_file.assert_called_once_with(1, "/tmp/new.mp3")


class TestStopAfterCurrent(unittest.TestCase):
    def test_removes_items_after_current_from_the_end(self):
        rpc = kodi_jsonrpc.KodiJsonRpc("http://x/jsonrpc", "u", "p")
        with mock.patch.object(rpc, "call") as call, mock.patch.object(
            rpc, "get_active_player_id_for_audio", return_value=0
        ), mock.patch.object(rpc, "current_playlist_position", return_value=1), mock.patch.object(
            rpc, "playlist_item_files", return_value=["a", "b", "c", "d"]
        ):
            self.assertEqual(rpc.stop_after_current(), 2)
        self.assertEqual(
            call.call_args_list,
            [
                mock.call("Player.SetRepeat", {"playerid": 0, "repeat": "off"}),
                mock.call("Playlist.Remove", {"playlistid": 0, "position": 3}),
                mock.call("Playlist.Remove", {"playlistid": 0, "position": 2}),
            ],
        )

    def test_nothing_playing(self):
        rpc = kodi_jsonrpc.KodiJsonRpc("http://x/jsonrpc", "u", "p")
        with mock.patch.object(rpc, "call") as call, mock.patch.object(
            rpc, "get_active_player_id_for_audio", return_value=None
        ), mock.patch.object(rpc, "current_playlist_position", return_value=-1):
            self.assertEqual(rpc.stop_after_current(), 0)
        call.assert_not_called()


class TestSkipFailedDownload(unittest.TestCase):
    def test_failed_video_is_skipped(self):
        import yt_party_play  # noqa: E402

        rpc = mock.MagicMock()
        rpc.upcoming_playlist_files.return_value = []
        rpc.is_playback_idle.return_value = False
        with mock.patch.multiple(
            yt_party_play,
            download_mp3=mock.MagicMock(side_effect=[None, "/tmp/ytm-b.mp3"]),
            fetch_flat_entries=mock.MagicMock(return_value=[("a" * 11, "A"), ("b" * 11, "B")]),
            send_notification=mock.MagicMock(),
            cancel_requested=mock.MagicMock(return_value=False),
        ):
            rc = yt_party_play.run_standard("add", "u", rpc, "/tmp", 10**9)
        self.assertEqual(rc, 0)
        rpc.playlist_add_file.assert_called_once_with("/tmp/ytm-b.mp3")


class TestStreamCache(unittest.TestCase):
    def test_stream_url_quotes_title(self):
        import stream_cache  # noqa: E402

        self.assertEqual(
            stream_cache.stream_url("abcdefghijk", "AC/DC - T.N.T"),
            "http://127.0.0.1:5000/stream/abcdefghijk/AC-DC%20-%20T.N.T",
        )

    def test_cached_url_respects_expiry(self):
        import stream_cache  # noqa: E402

        with tempfile.TemporaryDirectory() as d, mock.patch.object(stream_cache, "URL_CACHE_DIR", d):
            for vid, expire in (("fresh", time.time() + 3 * 3600), ("stale", time.time() + 600)):
                with open(os.path.join(d, vid), "w", encoding="utf-8") as f:
                    f.write(f"https://x.googlevideo.com/videoplayback?expire={int(expire)}&id=1")
            self.assertIsNotNone(stream_cache.cached_audio_url("fresh"))
            self.assertIsNone(stream_cache.cached_audio_url("stale"))
            self.assertIsNone(stream_cache.cached_audio_url("missing"))

    def test_resolve_caches_lookup(self):
        import stream_cache  # noqa: E402

        url = f"https://x.googlevideo.com/videoplayback?expire={int(time.time()) + 6 * 3600}"
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            stream_cache, "URL_CACHE_DIR", d
        ), mock.patch.object(stream_cache, "fetch_audio_url", return_value=url) as fetch, mock.patch.object(
            stream_cache, "_url_works", return_value=True
        ):
            self.assertEqual(stream_cache.resolve_audio_url("abcdefghijk", log=mock.MagicMock()), url)
            self.assertEqual(stream_cache.resolve_audio_url("abcdefghijk", log=mock.MagicMock()), url)
            fetch.assert_called_once()

    def test_resolve_replaces_rejected_url(self):
        import stream_cache  # noqa: E402

        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            stream_cache, "URL_CACHE_DIR", d
        ), mock.patch.object(
            stream_cache, "fetch_audio_url", side_effect=["https://g/bad", "https://g/good"]
        ), mock.patch.object(stream_cache, "_url_works", side_effect=lambda u: u.endswith("good")):
            got = stream_cache.resolve_audio_url("abcdefghijk", log=mock.MagicMock())
        self.assertEqual(got, "https://g/good")


class TestRunStream(unittest.TestCase):
    def _run(self, action, resolvable, idle=False, playlist=("x", "y", "z")):
        import yt_party_play  # noqa: E402

        events = []
        rpc = mock.MagicMock()
        rpc.is_playback_idle.return_value = idle
        rpc.current_playlist_position.return_value = 2
        rpc.playlist_item_files.return_value = list(playlist)
        rpc.playlist_clear_music.side_effect = lambda: events.append("clear")
        rpc.play_playlist_position.side_effect = lambda p: events.append(f"play@{p}")

        def resolve(vid, log):
            events.append(f"resolve:{vid}")
            return f"https://g/{vid}" if vid in resolvable else None

        entries = [("a" * 11, "A"), ("b" * 11, "B"), ("c" * 11, "C")]
        with mock.patch.multiple(
            yt_party_play,
            fetch_flat_entries=mock.MagicMock(return_value=entries),
            resolve_audio_url=mock.MagicMock(side_effect=resolve),
            stream_url=lambda vid, title: f"s/{title}",
            send_notification=mock.MagicMock(),
            remove_ytm_mp3s=mock.MagicMock(),
            cancel_requested=mock.MagicMock(return_value=False),
        ):
            rc = yt_party_play.run_stream(action, "u", rpc, "/tmp")
        return rc, events, rpc

    def test_play_resolves_first_before_touching_kodi_then_prefetches(self):
        rc, events, rpc = self._run("play", {"a" * 11, "b" * 11, "c" * 11})
        self.assertEqual(rc, 0)
        self.assertEqual(
            events,
            ["resolve:" + "a" * 11, "clear", "play@0", "resolve:" + "b" * 11, "resolve:" + "c" * 11],
        )
        rpc.playlist_add_files.assert_called_once_with(["s/A", "s/B", "s/C"])

    def test_shuffled_playlist_starts_on_resolved_track(self):
        _, events, rpc = self._run("play", {"a" * 11, "b" * 11, "c" * 11}, playlist=("s/C", "s/B", "s/A"))
        rpc.playlist_swap.assert_called_once_with(2, 0)
        self.assertIn("play@0", events)

    def test_in_play_order_follows_kodi_from_current_track(self):
        import stream_cache  # noqa: E402
        import yt_party_play  # noqa: E402

        rpc = mock.MagicMock()
        ids = ["a" * 11, "b" * 11, "c" * 11, "d" * 11]
        # Kodi (shuffled): d, b, a, c; currently playing b (position 1).
        rpc.playlist_item_files.return_value = [stream_cache.stream_url(v, "T") for v in "dbac" for v in [v * 11]]
        rpc.current_playlist_position.return_value = 1
        got = yt_party_play.in_play_order(rpc, [(v, v) for v in ids])
        self.assertEqual([v[0] for v, _ in got], ["b", "a", "c", "d"])

    def test_unplayable_first_video_is_dropped(self):
        _, _, rpc = self._run("play", {"b" * 11, "c" * 11})
        rpc.playlist_add_files.assert_called_once_with(["s/B", "s/C"])

    def test_play_next_inserts_after_current(self):
        _, events, rpc = self._run("play-next", {"a" * 11, "b" * 11, "c" * 11})
        rpc.playlist_insert_files.assert_called_once_with(3, ["s/A", "s/B", "s/C"])
        self.assertNotIn("clear", events)

    def test_add_when_idle_starts_at_first_added(self):
        _, events, rpc = self._run("add", {"a" * 11, "b" * 11, "c" * 11}, idle=True)
        rpc.playlist_add_files.assert_called_once_with(["s/A", "s/B", "s/C"])
        self.assertIn("play@3", events)


def _load_listener():
    import importlib.util  # noqa: E402

    spec = importlib.util.spec_from_file_location("yt_listener", os.path.join(ROOT, "yt-listener.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestStreamRelay(unittest.TestCase):
    """Listener /stream against a fake googlevideo that, like YouTube, 403s open-ended ranges."""

    DATA = bytes(range(256)) * 10_000  # 2.56 MB, spans several relay chunks

    def setUp(self):
        import http.server  # noqa: E402
        import urllib.request  # noqa: E402

        data = self.DATA

        class FakeYouTube(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                m = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
                # A "/bad" URL works for the first chunk only, like YouTube's mid-song 403s.
                if not m or (self.path.startswith("/bad") and int(m.group(1)) > 0):
                    self.send_response(403)
                    self.end_headers()
                    return
                start, end = int(m.group(1)), min(int(m.group(2)), len(data) - 1)
                self.send_response(206)
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                self.wfile.write(data[start : end + 1])

        self.listener = _load_listener()
        # HTTPServer does a reverse-DNS lookup of the bind address, which can take seconds.
        with mock.patch("socket.getfqdn", return_value="localhost"):
            self.servers = self._start_servers(FakeYouTube)

        upstream = (
            f"http://127.0.0.1:{self.servers[0].server_port}/videoplayback"
            f"?clen={len(data)}&mime=audio%2Fwebm"
        )
        patches = [
            mock.patch.object(self.listener, "resolve_audio_url", return_value=upstream),
            mock.patch.object(self.listener, "cached_audio_url", return_value=upstream),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.base = f"http://127.0.0.1:{self.servers[1].server_port}/stream/abcdefghijk/Song"
        self.urlopen = urllib.request.urlopen
        self.Request = urllib.request.Request

    def _start_servers(self, fake_youtube):
        import http.server  # noqa: E402

        servers = [
            http.server.ThreadingHTTPServer(("127.0.0.1", 0), fake_youtube),
            http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.listener.RequestHandler),
        ]
        for srv in servers:
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        return servers

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def _get(self, rng=None, method="GET"):
        req = self.Request(self.base, method=method, headers={"Range": rng} if rng else {})
        with self.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()

    def test_open_range_is_relayed_whole(self):
        status, headers, body = self._get("bytes=0-")
        self.assertEqual(status, 206)
        self.assertEqual(body, self.DATA)
        self.assertEqual(headers["Content-Range"], f"bytes 0-{len(self.DATA) - 1}/{len(self.DATA)}")

    def test_seek_range(self):
        status, _, body = self._get("bytes=1500000-")
        self.assertEqual(status, 206)
        self.assertEqual(body, self.DATA[1500000:])

    def test_url_rejected_mid_song_is_replaced(self):
        port = self.servers[0].server_port
        bad, good = (
            f"http://127.0.0.1:{port}/{name}?clen={len(self.DATA)}&mime=audio%2Fwebm"
            for name in ("bad", "good")
        )
        with mock.patch.object(self.listener, "resolve_audio_url", side_effect=[bad, good]):
            status, _, body = self._get("bytes=0-")
        self.assertEqual((status, body), (206, self.DATA))

    def test_no_range_and_head(self):
        status, headers, body = self._get()
        self.assertEqual((status, body), (200, self.DATA))
        status, headers, body = self._get(method="HEAD")
        self.assertEqual((status, headers["Content-Length"], body), (200, str(len(self.DATA)), b""))


if __name__ == "__main__":
    unittest.main()
