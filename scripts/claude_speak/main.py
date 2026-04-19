"""Stop-hook entrypoint. Reads the transcript JSONL from stdin, rewrites the
last assistant turn via `claude -p`, synthesizes via Kokoro, plays it back.

With auto_dictation=on, playback runs synchronously and then a mic recording +
whisper.cpp transcription is fed back to Claude via a decision:block payload.
"""
from __future__ import annotations

import json
import os
import signal
import sys

from .audio import play_async, play_synchronous
from .config import DATA_DIR, load_config
from .defaults import DEFAULTS
from .logging import log, log_v, notify, section
from .rewrite import rewrite, rewrite_stream
from .stt import dictate as stt_dictate
from .stt.cancel import is_cancel
from .transcript import build_rewrite_input, read_last_turn
from .tts import synthesize

PID_FILE = DATA_DIR / "player.pid"
DAEMON_PID_FILE = DATA_DIR / "daemon.pid"


def _kill_previous() -> None:
    """Kill OUR last playback / pipeline daemon. Used to be a `pkill -f
    ffplay` which would also nuke unrelated user processes (e.g. someone
    else using ffplay outside this plugin). Now scoped to the pids we
    actually wrote to PID_FILE / DAEMON_PID_FILE."""
    killed = False
    if DAEMON_PID_FILE.exists():
        try:
            pid = int(DAEMON_PID_FILE.read_text().strip())
            # Negative pid = process group → kills the daemonized pipeline
            # along with any ffplay it spawned.
            os.killpg(pid, signal.SIGTERM)
            killed = True
        except (ProcessLookupError, ValueError, PermissionError):
            pass
        try:
            DAEMON_PID_FILE.unlink()
        except FileNotFoundError:
            pass
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            killed = True
        except (ProcessLookupError, ValueError, PermissionError):
            pass
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
    if killed:
        log_v("killed previous playback")


def _daemonize() -> None:
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    try:
        os.setpgid(0, 0)
    except OSError:
        pass
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass


def _dictate_and_emit(cfg: dict) -> str:
    """Run dictation; return the decision:block JSON to print, or empty string.

    Dictation always runs in the same process as the speak that invoked it
    (use_daemon=False) — either we ARE the daemon, or speak fell back to
    in-process and the daemon path would fail anyway.
    """
    section("🎙️  DICTATION")
    try:
        dictated = stt_dictate(cfg, use_daemon=False)
    except Exception as e:
        log(f"❌ dictation error: {type(e).__name__}: {e}")
        return ""
    if not dictated:
        log("⏭️  dictation empty (user skipped or no speech)")
        return ""
    if is_cancel(dictated, cfg):
        log(f"🛑 cancel phrase: {dictated[:80]!r}")
        notify("claude-speak", "Cancelled")
        return ""
    log(f"📤 heard ({len(dictated)}c): {dictated[:200]!r}")
    reason = (f"The user responded by voice: {dictated}\n\n"
              "Treat this as their next prompt.")
    return json.dumps({"decision": "block", "reason": reason})


def _run_pipeline_daemon(cfg: dict, text: str) -> None:
    from .pipeline import run_pipeline
    def fetch(sentence: str) -> tuple[bytes, str]:
        return synthesize(sentence, cfg)
    run_pipeline(cfg, rewrite_stream, fetch, text)


def run_speak_via_daemon(req: dict) -> str:
    """Daemon-side speak handler. Same flow as main() but no forking — the
    daemon is the long-running process — and stdout (the decision:block JSON
    for auto-dictation) is returned as a string for the daemon to relay to
    the hook client."""
    cfg = load_config(DEFAULTS)
    if not cfg.get("enabled", True):
        return ""
    transcript_path = req.get("transcript_path") or ""
    user_text, assistant_text = read_last_turn(transcript_path)
    if not assistant_text or len(assistant_text) < 4:
        return ""
    max_chars = int(cfg.get("max_chars", 1200))
    text = build_rewrite_input(user_text, assistant_text, max_chars)

    voice = cfg.get("kokoro_voice")
    rate = float(cfg.get("playback_rate", 1.0))
    auto_dictation = bool(cfg.get("auto_dictation", False))
    if "mode" not in cfg and "pipeline_sentences" in cfg:
        mode = "stream" if cfg["pipeline_sentences"] else "whole"
    else:
        mode = cfg.get("mode", "stream")
    use_pipeline = (mode == "stream")
    section("🎤 STOP HOOK (daemon)")
    log(f"📥 in: user={len(user_text)}c assistant={len(assistant_text)}c "
        f"voice={voice} rate={rate}x mode={mode} dictate={auto_dictation}")
    if user_text:
        log(f"   user: {user_text[:200]!r}")
    log_v(f"transcript={transcript_path}")

    if use_pipeline:
        try:
            _run_pipeline_daemon(cfg, text)
        except Exception as e:
            log(f"❌ pipeline error: {type(e).__name__}: {e}")
            return ""
        if auto_dictation:
            return _dictate_and_emit(cfg)
        return ""

    # Buffered path.
    try:
        spoken = rewrite(text, cfg)
        if not spoken:
            log("❌ rewrite returned empty")
            return ""
        log(f"✍️  rewrite done → {len(spoken)}c")
        log(f"   speech: {spoken[:400]!r}")
        audio, ext = synthesize(spoken, cfg)
        if not audio:
            log("tts synthesis returned no audio")
            return ""
        if auto_dictation:
            play_synchronous(audio, ext, cfg)
            log("✅ buffered playback done (auto_dictation)")
            return _dictate_and_emit(cfg)
        pid = play_async(audio, ext, cfg)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(pid))
        log_v(f"buffered playback started pid={pid}")
    except Exception as e:
        log(f"❌ error: {type(e).__name__}: {e}")
    return ""


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    cfg = load_config(DEFAULTS)
    if not cfg.get("enabled", True):
        return 0
    if os.environ.get("CLAUDE_SPEAK") == "0":
        return 0

    transcript_path = payload.get("transcript_path") or ""
    user_text, assistant_text = read_last_turn(transcript_path)
    if not assistant_text or len(assistant_text) < 4:
        return 0
    max_chars = int(cfg.get("max_chars", 1200))
    text = build_rewrite_input(user_text, assistant_text, max_chars)

    _kill_previous()

    voice = cfg.get("kokoro_voice")
    rate = float(cfg.get("playback_rate", 1.0))
    auto_dictation = bool(cfg.get("auto_dictation", False))
    # Back-compat: old configs used "pipeline_sentences": bool. Honor it when
    # the new "mode" key isn't explicitly set.
    if "mode" not in cfg and "pipeline_sentences" in cfg:
        mode = "stream" if cfg["pipeline_sentences"] else "whole"
    else:
        mode = cfg.get("mode", "stream")
    use_pipeline = (mode == "stream")
    section("🎤 STOP HOOK")
    log(f"📥 in: user={len(user_text)}c assistant={len(assistant_text)}c "
        f"voice={voice} rate={rate}x mode={mode} dictate={auto_dictation}")
    if user_text:
        log(f"   user: {user_text[:200]!r}")
    log_v(f"transcript={transcript_path}")

    if use_pipeline:
        if auto_dictation:
            # Synchronous pipeline — the hook needs to know when audio ends
            # before triggering STT.
            try:
                _run_pipeline_daemon(cfg, text)
            except Exception as e:
                log(f"❌ pipeline error: {type(e).__name__}: {e}")
            out = _dictate_and_emit(cfg)
            if out:
                print(out)
            return 0

        # Background: fork + daemonize so the hook returns immediately.
        pid = os.fork()
        if pid > 0:
            log_v(f"pipeline forking first_child={pid}")
            return 0
        _daemonize()
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            DAEMON_PID_FILE.write_text(str(os.getpid()))
            _run_pipeline_daemon(cfg, text)
        except Exception as e:
            log(f"❌ pipeline error: {type(e).__name__}: {e}")
        finally:
            try:
                DAEMON_PID_FILE.unlink()
            except FileNotFoundError:
                pass
        os._exit(0)

    # Buffered path: synthesize the whole rewrite as one audio chunk.
    try:
        spoken = rewrite(text, cfg)
        if not spoken:
            log("❌ rewrite returned empty")
            return 0
        log(f"✍️  rewrite done → {len(spoken)}c")
        log(f"   speech: {spoken[:400]!r}")
        audio, ext = synthesize(spoken, cfg)
        if not audio:
            log("tts synthesis returned no audio")
            return 0
        if auto_dictation:
            play_synchronous(audio, ext, cfg)
            log("✅ buffered playback done (auto_dictation)")
            out = _dictate_and_emit(cfg)
            if out:
                print(out)
            return 0
        pid = play_async(audio, ext, cfg)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(pid))
        log_v(f"buffered playback started pid={pid}")
    except Exception as e:
        log(f"❌ error: {type(e).__name__}: {e}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
