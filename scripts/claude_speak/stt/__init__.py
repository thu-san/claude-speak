"""Speech-to-text. Single public entry point: `dictate(cfg)`.

Internally routes through the warm daemon when available (cfg.daemon=True
and the daemon is reachable), and falls back to in-process record+transcribe
otherwise. Callers — Stop hook, CLI, daemon op handler — should never need
to know which path ran.

Backend: local whisper.cpp via stt.whisper_cpp (pure provider adapter —
transcribe() + ensure(), no CLI, no daemon awareness).
"""
from __future__ import annotations

from ..logging import log, tail_log_to_stderr


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

    Uses tail_log_to_stderr so daemon-side recording/transcribe logs
    stream to the user's terminal during the wait."""
    try:
        from ..daemon import ensure_daemon, send_request
        if not ensure_daemon():
            return None
        overrides = {k: cfg[k] for k in (
            "whisper_cpp_model", "whisper_cpp_threads", "whisper_cpp_language",
            "record_silence_seconds", "record_max_seconds",
        ) if k in cfg}
        with tail_log_to_stderr():
            resp = send_request({"op": "dictate", "overrides": overrides})
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
