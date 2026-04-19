"""Speech-to-text. Single public entry point: `dictate(cfg)`.

Internally routes through the warm daemon when available (cfg.daemon=True
and the daemon is reachable), and falls back to in-process record+transcribe
otherwise. Callers — Stop hook, CLI, daemon op handler — should never need
to know which path ran.

Backend: local whisper.cpp via stt.whisper_cpp (pure provider adapter —
transcribe() + ensure(), no CLI, no daemon awareness).
"""
from __future__ import annotations

import sys
import threading

from ..logging import log


def dictate(cfg: dict, *, use_daemon: bool | None = None) -> str:
    """Record from the mic and transcribe. Returns transcript or ''.

    use_daemon=None  → honor cfg.daemon (default True)
    use_daemon=True  → require daemon path (still falls back if it fails)
    use_daemon=False → always run in-process (used by the daemon's own
                       dictate op handler to avoid recursing into itself)
    """
    if use_daemon is None:
        use_daemon = bool(cfg.get("daemon", True))
    if use_daemon:
        text = _dictate_via_daemon(cfg)
        if text is not None:
            return text
        log("daemon dictate unavailable — falling back to in-process")
    return _dictate_in_process(cfg)


def _dictate_via_daemon(cfg: dict) -> str | None:
    """Returns the transcript on success, or None if the daemon is
    unreachable / errored (so the caller can fall back).

    While the daemon does the work, we tail speak.log from our pre-request
    offset and print any new lines to stderr — without this, recording /
    transcribe timing logs would only land in the file (the daemon process
    writes them) and the CLI user would see nothing for the wait."""
    try:
        from ..daemon import ensure_daemon, send_request
        from ..logging import LOG_PATH
        if not ensure_daemon():
            return None
        overrides = {k: cfg[k] for k in (
            "whisper_cpp_model", "whisper_cpp_threads", "whisper_cpp_language",
            "record_silence_seconds", "record_max_seconds",
        ) if k in cfg}

        # Track inode + offset; if the file is rotated mid-request the
        # inode changes and we restart from the new file's start.
        try:
            st = LOG_PATH.stat()
            start_offset = st.st_size
            start_inode = st.st_ino
        except FileNotFoundError:
            start_offset = 0
            start_inode = None
        stop = threading.Event()

        def _tail() -> None:
            offset = start_offset
            inode = start_inode
            while True:
                try:
                    st = LOG_PATH.stat()
                    if inode is None:
                        inode = st.st_ino
                    if st.st_ino != inode:
                        # Rotation happened — track the new file from byte 0.
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
                    return  # one final read above already caught any tail
                stop.wait(0.1)

        t = threading.Thread(target=_tail, daemon=True)
        t.start()
        try:
            resp = send_request({"op": "dictate", "overrides": overrides})
        finally:
            # One last poll to catch lines written between the previous tail
            # tick and the response arriving, then signal the thread to exit.
            stop.set()
            t.join(timeout=0.5)

        if not resp or not resp.get("ok"):
            return None
        return resp.get("text", "")
    except Exception as e:
        log(f"daemon dictate error: {type(e).__name__}: {e}")
        return None


def _dictate_in_process(cfg: dict) -> str:
    """In-process record + transcribe. Used as the daemon's own worker and
    as the fallback when no daemon is running."""
    log("stt dispatch provider=whisper-cpp")
    from .whisper_cpp import transcribe
    from ..recording import record_mic
    wav = record_mic(cfg)
    if not wav:
        return ""
    try:
        return transcribe(wav, cfg)
    except Exception as e:
        log(f"whisper-cpp error: {type(e).__name__}: {e}")
        return ""


def is_cancel(text: str, cfg: dict) -> bool:
    from .cancel import is_cancel as _c
    return _c(text, cfg)
