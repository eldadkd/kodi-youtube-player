"""Shared logging setup for yt-listener and related scripts."""

import logging
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_LOG_FMT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _log_path() -> str:
    default = os.path.join(SCRIPT_DIR, "yt-listener.log")
    return os.path.abspath(os.environ.get("YT_LISTENER_LOG", default))


LOG_FILE = _log_path()


def _ensure_root_logging() -> None:
    """
    Attach file + stream handlers to the root logger.

    logging.basicConfig is skipped when the root logger already has handlers
    (common in embedded Python builds). Child processes spawned with
    stdout/stderr to DEVNULL would then lose all logs unless we always add
    our FileHandler.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    path = _log_path()
    has_our_file = False
    for h in root.handlers:
        if isinstance(h, logging.FileHandler):
            try:
                if os.path.abspath(h.baseFilename) == path:
                    has_our_file = True
                    break
            except (AttributeError, OSError):
                pass
    if not has_our_file:
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(_LOG_FMT)
        root.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(_LOG_FMT)
        root.addHandler(sh)


_ensure_root_logging()


def get_logger(name):
    """Return a child of the root logger (handlers configured above)."""
    return logging.getLogger(name)
