"""Single-instance file /stop-yt coordination (/tmp/yt-play-instances, /tmp/yt-play-cancel)."""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import os
import signal
import sys
import time
from typing import Callable, Iterator, List, Optional

YT_INSTANCES = "/tmp/yt-play-instances"
YT_CANCEL = "/tmp/yt-play-cancel"


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Exclusive lock for read-modify-write of YT_INSTANCES (listener + play processes)."""
    with open(YT_INSTANCES + ".lock", "w", encoding="ascii") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _read_pids() -> List[int]:
    """PIDs in registration order (oldest first)."""
    try:
        with open(YT_INSTANCES, encoding="ascii") as f:
            return [int(x) for x in f if x.strip().isdigit()]
    except FileNotFoundError:
        return []


def _write_pids(pids: List[int]) -> None:
    try:
        if pids:
            with open(YT_INSTANCES, "w", encoding="ascii") as f:
                f.write("".join(f"{p}\n" for p in pids))
        else:
            os.remove(YT_INSTANCES)
    except OSError:
        pass


def _kill_process_group(pid: int) -> bool:
    """SIGTERM pid's process group (or pid). True if it was running."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
    return True


def kill_other_instances(my_pid: int) -> None:
    """
    SIGTERM instances registered before my_pid. Newer ones are kept, so when two
    requests arrive close together the older one cannot kill the newer one.
    """
    with _locked():
        pids = _read_pids()
        older = pids[: pids.index(my_pid)] if my_pid in pids else [p for p in pids if p != my_pid]
        for pid in older:
            _kill_process_group(pid)
        _write_pids([p for p in pids if p not in older])


def stop_all_instances() -> List[int]:
    """SIGTERM every registered instance and clear the file. Returns PIDs that were running."""
    with _locked():
        killed = [pid for pid in _read_pids() if _kill_process_group(pid)]
        _write_pids([])
    return killed


def register_instance(my_pid: int) -> None:
    """Append PID so /stop-yt can signal this session (listener may have appended already)."""
    with _locked():
        pids = _read_pids()
        if my_pid not in pids:
            _write_pids(pids + [my_pid])


def unregister_instance(my_pid: int) -> None:
    with _locked():
        _write_pids([p for p in _read_pids() if p != my_pid])


def clear_cancel_flag() -> None:
    try:
        os.remove(YT_CANCEL)
    except OSError:
        pass


def cancel_requested() -> bool:
    return os.path.isfile(YT_CANCEL)


class InstanceSession:
    """
    Registers this process in YT_INSTANCES and removes the line on exit.

    on_terminate kills tracked child Popen trees (yt-dlp); atexit then unregisters.
    """

    def __init__(self, my_pid: int, kill_children: Callable[[], None]):
        self._pid = my_pid
        self._kill_children = kill_children
        self._registered = False

    def start(self, kill_others_first: bool) -> None:
        register_instance(self._pid)
        if kill_others_first:
            kill_other_instances(self._pid)
        self._registered = True
        atexit.register(self._atexit)

        def _handle(sig, _frame):
            # No unregister here: the signal may arrive while this process holds the lock.
            # sys.exit unwinds (releasing it) and atexit unregisters.
            self._kill_children()
            sys.exit(143)

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

    def _atexit(self) -> None:
        if self._registered:
            unregister_instance(self._pid)
            self._registered = False


def wait_until_cancel_or(
    predicate: Callable[[], bool],
    *,
    cancel_path: str = YT_CANCEL,
    interval_s: float = 1.0,
    timeout_s: Optional[float] = None,
) -> bool:
    """
    Sleep until predicate() is True. Returns False if cancel_path appears first or timeout.

    If timeout_s is None, wait forever.
    """
    t0 = time.monotonic()
    while True:
        if os.path.isfile(cancel_path):
            return False
        if predicate():
            return True
        if timeout_s is not None and (time.monotonic() - t0) >= timeout_s:
            return False
        time.sleep(interval_s)
