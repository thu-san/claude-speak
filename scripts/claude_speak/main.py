"""Stop-hook entrypoint. One conversational turn per invocation:

    claude raw reply → claude -p rewrite → Kokoro TTS → ffplay
      → (if voice_loop) record mic → whisper.cpp → decision:block JSON

Two entry paths:

- `run_turn(req)`: called by the daemon for each "turn" op. Runs the full
  flow in-process (the daemon is the long-running process — no forking).
  Returns the decision:block JSON string (empty if nothing to feed back).

- `main()`: in-process fallback, used only when the daemon is unreachable.
  Reads request payload from stdin, calls run_turn, prints stdout.
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
    ffplay` which would also nuke unrelated user processes. Now scoped to
    the pids we actually wrote to PID_FILE / DAEMON_PID_FILE."""
    killed = False
    if DAEMON_PID_FILE.exists():
        try:
            pid = int(DAEMON_PID_FILE.read_text().strip())
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


def _listen_and_emit(cfg: dict) -> str:
    """Listen for the user's voice reply, return decision:block JSON (or '')."""
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


def _resolve_mode(cfg: dict) -> str:
    """Stream vs whole. Honors legacy 'pipeline_sentences' bool."""
    if "mode" not in cfg and "pipeline_sentences" in cfg:
        return "stream" if cfg["pipeline_sentences"] else "whole"
    return cfg.get("mode", "stream")


def _resolve_voice_loop(cfg: dict) -> bool:
    """voice_loop is the new name; auto_dictation is the legacy key we still
    read from existing configs."""
    if "voice_loop" in cfg:
        return bool(cfg["voice_loop"])
    return bool(cfg.get("auto_dictation", True))


def _passthrough_stream(text: str, _cfg: dict):
    """A no-op 'rewriter' that yields the input verbatim as one chunk.
    Lets run_pipeline produce the full timing/section logs
    (first audio at, total synth, play ~, done in) even when we skip
    the claude -p rewrite step."""
    yield text


def _run_pipeline(cfg: dict, text: str, *, do_rewrite: bool = True) -> None:
    from .pipeline import run_pipeline
    def fetch(sentence: str) -> tuple[bytes, str]:
        return synthesize(sentence, cfg)
    stream = rewrite_stream if do_rewrite else _passthrough_stream
    run_pipeline(cfg, stream, fetch, text)


def run_turn(req: dict, *, forked_fallback: bool = False) -> str:
    """One full conversational turn. Returns the decision:block JSON string
    for the shim to print (or '' when there's nothing to feed back).

    Request schema:
      - transcript_path: read last user/assistant turn from this JSONL, go
        through rewrite → synth → play. This is the Stop-hook path.
      - text: pre-built text to speak; skips the transcript read. Used by
        the Notification hook so short messages are spoken verbatim without
        the 3-5s claude -p floor.
      - rewrite (default True): if False and `text` given, skip the rewrite
        step and speak `text` verbatim.
      - voice_loop (default = cfg.voice_loop): if False, speak-only; no mic
        recording afterward. Explicit override wins over the config value.

    `forked_fallback=False` (default, daemon path): everything runs in this
    process; no subprocess forking. Playback is streamed synchronously so
    the listen step can start right after audio ends.

    `forked_fallback=True` (speak-only path when daemon unreachable AND
    voice_loop=False): fork+daemonize so the Stop hook returns quickly
    and playback continues in the background.
    """
    cfg = load_config(DEFAULTS)
    if not cfg.get("enabled", True):
        return ""

    direct_text = req.get("text")
    do_rewrite = bool(req.get("rewrite", True))
    if "voice_loop" in req:
        voice_loop = bool(req["voice_loop"])
    else:
        voice_loop = _resolve_voice_loop(cfg)

    if direct_text is not None:
        # Notification-style path: pre-built text, no transcript read.
        user_text = ""
        assistant_text = direct_text
        text = direct_text
    else:
        transcript_path = req.get("transcript_path") or ""
        user_text, assistant_text = read_last_turn(transcript_path)
        if not assistant_text or len(assistant_text) < 4:
            return ""
        max_chars = int(cfg.get("max_chars", 1200))
        text = build_rewrite_input(user_text, assistant_text, max_chars)
        log_v(f"transcript={transcript_path}")

    voice = cfg.get("kokoro_voice")
    rate = float(cfg.get("playback_rate", 1.0))
    mode = _resolve_mode(cfg)
    # The pipeline works even when rewrite is skipped — we pass a
    # passthrough "rewriter" that yields the text unchanged. That way the
    # caller gets the same timing section logs (🔊 SPEAK, 🔉 first audio at,
    # ✅ done in, total synth, play ~) whether or not claude -p ran.
    use_pipeline = (mode == "stream")

    section("🎤 TURN")
    log(f"📥 in: user={len(user_text)}c assistant={len(assistant_text)}c "
        f"voice={voice} rate={rate}x mode={mode} voice_loop={voice_loop} "
        f"rewrite={do_rewrite} direct_text={direct_text is not None}")
    if user_text:
        log(f"   user: {user_text[:200]!r}")

    if use_pipeline:
        # In voice-loop mode we must run playback synchronously so the listen
        # step can start right after audio ends. In speak-only mode on the
        # in-process fallback, fork so the hook returns quickly.
        if forked_fallback and not voice_loop:
            pid = os.fork()
            if pid > 0:
                log_v(f"pipeline forking first_child={pid}")
                return ""
            _daemonize()
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                DAEMON_PID_FILE.write_text(str(os.getpid()))
                _run_pipeline(cfg, text, do_rewrite=do_rewrite)
            except Exception as e:
                log(f"❌ pipeline error: {type(e).__name__}: {e}")
            finally:
                try:
                    DAEMON_PID_FILE.unlink()
                except FileNotFoundError:
                    pass
            os._exit(0)
        try:
            _run_pipeline(cfg, text, do_rewrite=do_rewrite)
        except Exception as e:
            log(f"❌ pipeline error: {type(e).__name__}: {e}")
            return ""
        return _listen_and_emit(cfg) if voice_loop else ""

    # Buffered path: synthesize the whole text as one audio chunk.
    try:
        if do_rewrite:
            spoken = rewrite(text, cfg)
            if not spoken:
                log("❌ rewrite returned empty")
                return ""
            log(f"✍️  rewrite done → {len(spoken)}c")
            log(f"   speech: {spoken[:400]!r}")
        else:
            # Verbatim path — used for notifications / any caller that passes
            # text directly and has no use for the rewrite step.
            spoken = text
            log(f"   speech verbatim: {spoken[:400]!r}")
        audio, ext = synthesize(spoken, cfg)
        if not audio:
            log("tts synthesis returned no audio")
            return ""
        if voice_loop:
            play_synchronous(audio, ext, cfg)
            log("✅ buffered playback done")
            return _listen_and_emit(cfg)
        pid = play_async(audio, ext, cfg)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(pid))
        log_v(f"buffered playback started pid={pid}")
    except Exception as e:
        log(f"❌ error: {type(e).__name__}: {e}")
    return ""


# Legacy alias. Kept so daemon.py's older op handler still resolves if a
# user hasn't restarted the daemon after pulling. Remove once we're past v1.
run_speak_via_daemon = run_turn


def main() -> int:
    """In-process fallback. Only used when the daemon is unreachable."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    cfg = load_config(DEFAULTS)
    if not cfg.get("enabled", True):
        return 0
    if os.environ.get("CLAUDE_SPEAK") == "0":
        return 0

    _kill_previous()
    out = run_turn(payload, forked_fallback=True)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
