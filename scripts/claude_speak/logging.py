"""Logging + user-facing notifications."""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import DATA_DIR

LOG_PATH: Path = DATA_DIR / "speak.log"

_TEE_STDERR = False


def _rotate_if_needed() -> None:
    """Daily rotation: when today's date differs from the file's mtime date,
    rename speak.log → speak.log.YYYY-MM-DD (the file's last-write date)
    and start a fresh speak.log. Keeps each calendar day's logs in its own
    file; older days remain on disk until you delete them."""
    try:
        st = LOG_PATH.stat()
    except FileNotFoundError:
        return
    file_date = datetime.date.fromtimestamp(st.st_mtime)
    today = datetime.date.today()
    if file_date >= today:
        return
    backup = LOG_PATH.with_suffix(f".log.{file_date.isoformat()}")
    try:
        # If a backup for that date already exists (daemon was restarted
        # mid-day before today rolled over), append a counter.
        i = 0
        target = backup
        while target.exists():
            i += 1
            target = LOG_PATH.with_suffix(f".log.{file_date.isoformat()}.{i}")
        LOG_PATH.rename(target)
    except OSError:
        pass


class tail_log_to_stderr:
    """Context manager. While the block runs, tail new bytes appended to
    speak.log and write them to stderr every 100ms. Used by CLIs that
    block on a long-running daemon op so the user sees the daemon's
    progress logs in their terminal instead of a silent wait.

    Handles mid-request log rotation by tracking the file's inode — when
    it changes, restarts reading from byte 0 of the new file."""

    def __init__(self) -> None:
        import threading
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "tail_log_to_stderr":
        import threading
        try:
            st = LOG_PATH.stat()
            start_offset = st.st_size
            start_inode: int | None = st.st_ino
        except FileNotFoundError:
            start_offset = 0
            start_inode = None
        stop = self._stop

        def _tail() -> None:
            offset = start_offset
            inode = start_inode
            while True:
                try:
                    st = LOG_PATH.stat()
                    if inode is None:
                        inode = st.st_ino
                    if st.st_ino != inode:
                        offset = 0
                        inode = st.st_ino
                    if st.st_size > offset:
                        with open(LOG_PATH, "rb") as f:
                            f.seek(offset)
                            chunk = f.read(st.st_size - offset)
                            offset = st.st_size
                        sys.stderr.write(chunk.decode("utf-8", errors="replace"))
                        sys.stderr.flush()
                except (FileNotFoundError, OSError):
                    pass
                if stop.is_set():
                    return  # one final read above caught the tail
                stop.wait(0.1)

        self._thread = threading.Thread(target=_tail, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)


def enable_stderr_tee(on: bool = True) -> None:
    """When enabled, log() and section() also write to stderr — useful for
    standalone CLI invocations so the user sees the same nice output that
    goes to speak.log."""
    global _TEE_STDERR
    _TEE_STDERR = on


def section(title: str) -> None:
    """Visual separator + heading. Use at the start of each major phase."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"\n──── [{stamp} pid={os.getpid()}] {title} ────\n"
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
        if _TEE_STDERR:
            sys.stderr.write(line)
            sys.stderr.flush()
    except Exception:
        pass


def log(msg: str) -> None:
    """Write a one-line entry. Format: '[HH:MM:SS pid=XXXX] msg'. The pid
    matters because the daemon, the Stop hook, the install hook, and CLI
    invocations all share speak.log — without pid you can't tell whose
    line is whose when they interleave."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp} pid={os.getpid()}] {msg}\n"
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
        if _TEE_STDERR:
            sys.stderr.write(line)
            sys.stderr.flush()
    except Exception:
        pass


def log_v(msg: str) -> None:
    """Verbose log — only written when log_verbose is enabled in config.
    Read lazily so we don't import config at module load."""
    try:
        from .config import load_config
        from .defaults import DEFAULTS
        if not load_config(DEFAULTS).get("log_verbose", False):
            return
    except Exception:
        return
    log(msg)


def notify(title: str, message: str) -> None:
    """Show a macOS notification banner. Fire-and-forget — osascript can take
    200-500ms which we don't want blocking mic open / playback start."""
    if not shutil.which("osascript"):
        return
    safe_title = title.replace('"', "'")
    safe_msg = message.replace('"', "'")
    subprocess.Popen(
        ["osascript", "-e", f'display notification "{safe_msg}" with title "{safe_title}"'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def beep(kind: str = "start") -> None:
    """Short sound cue. kind = 'start' (listening) or 'stop' (processing).

    Fire-and-forget — we don't block on afplay. Blocking here costs ~0.5-1s on
    each call, which shows up as fake 'reaction time' before the user starts
    speaking and a bogus tail delay after silence is detected.
    """
    mapping = {
        "start": ["/System/Library/Sounds/Ping.aiff", "/System/Library/Sounds/Pop.aiff"],
        "stop": ["/System/Library/Sounds/Tink.aiff", "/System/Library/Sounds/Morse.aiff"],
    }
    if not shutil.which("afplay"):
        return
    for candidate in mapping.get(kind, []):
        if Path(candidate).exists():
            subprocess.Popen(
                ["afplay", candidate],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
