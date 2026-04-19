"""CLI entry point: `python -m claude_speak.stt`.

Records from the mic (or transcribes a given WAV) and prints the result on
stdout. Honors --no-daemon to bypass the warm daemon and run in-process.
"""
from __future__ import annotations

from ..venv import ensure_venv_python
ensure_venv_python()

import argparse
import signal
import sys
from pathlib import Path

from . import dictate as stt_dictate
from .whisper_cpp import transcribe
from ..config import load_config
from ..defaults import DEFAULTS
from ..logging import enable_stderr_tee, log, section
from ..recording import record_mic


def _set_whisper_model(model_name: str) -> int:
    """Persist a new whisper_cpp_model in config (downloading if needed) and
    restart the daemon so it picks up the new model on the next request."""
    import json
    from ..config import CONFIG_PATH, DATA_DIR
    from ..install import _download

    if not model_name.startswith("ggml-") or not model_name.endswith(".bin"):
        print(f"invalid model name: {model_name!r}", file=sys.stderr)
        print("expected like: ggml-small.en.bin, ggml-medium.en-q5_0.bin, "
              "ggml-large-v3-turbo-q5_0.bin", file=sys.stderr)
        return 1

    url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{model_name}"
    dest = DATA_DIR / "models" / model_name
    if not _download(url, dest, f"whisper model ({model_name})", quiet=False):
        print(f"download failed; config unchanged", file=sys.stderr)
        return 1

    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    cfg["whisper_cpp_model"] = model_name
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print(f"whisper_cpp_model = {model_name}")

    from ..daemon import main as daemon_main
    return daemon_main(["restart"])


def _install_kill_handler() -> None:
    """On SIGINT / SIGTERM, tell the daemon to abort any in-flight recording
    so the mic is released, system unmuted, and the daemon goes idle."""
    def _on_signal(signum, _frame):
        try:
            from ..daemon import _socket_alive, send_request
            if _socket_alive():
                send_request({"op": "kill"}, timeout=2)
        except Exception:
            pass
        raise SystemExit(130 if signum == signal.SIGINT else 143)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)


def main() -> int:
    enable_stderr_tee()
    _install_kill_handler()

    ap = argparse.ArgumentParser(
        prog="claude_speak.stt",
        description="Record from the mic and transcribe with whisper.cpp. "
        "If a WAV path is given, transcribe that file instead.",
    )
    ap.add_argument("wav", type=Path, nargs="?",
                    help="path to a 16kHz mono WAV. Omit to record + transcribe.")
    ap.add_argument("--model", help="override whisper_cpp_model "
                    "(e.g. ggml-tiny.en.bin, ggml-base.en.bin, ggml-small.en.bin)")
    ap.add_argument("--threads", type=int, help="override whisper_cpp_threads")
    ap.add_argument("--lang", help="override language (e.g. en, ja, fr)")
    ap.add_argument("--silence", type=float, help="(record mode) silence seconds before stop")
    ap.add_argument("--max", type=int, help="(record mode) max recording seconds")
    ap.add_argument("--no-daemon", action="store_true",
                    help="bypass the warm daemon and run in-process")
    ap.add_argument("--restart-daemon", action="store_true",
                    help="restart the warm daemon (pick up new code / reload models) and exit")
    ap.add_argument("--set-model", metavar="NAME",
                    help="persist a new whisper_cpp_model in config (downloads if missing) "
                    "and restart the daemon, then exit. e.g. ggml-large-v3-turbo-q5_0.bin")
    args = ap.parse_args()

    if args.restart_daemon:
        from ..daemon import main as daemon_main
        return daemon_main(["restart"])

    if args.set_model:
        return _set_whisper_model(args.set_model)

    cfg = load_config(dict(DEFAULTS))
    if args.model:
        cfg["whisper_cpp_model"] = args.model
    if args.threads:
        cfg["whisper_cpp_threads"] = args.threads
    if args.lang:
        cfg["whisper_cpp_language"] = args.lang
    if args.silence is not None:
        cfg["record_silence_seconds"] = args.silence
    if args.max is not None:
        cfg["record_max_seconds"] = args.max

    section("🎙️  DICTATION (cli)")
    log(f"model={cfg.get('whisper_cpp_model')} lang={cfg.get('whisper_cpp_language')} "
        f"threads={cfg.get('whisper_cpp_threads')}")

    if args.wav is not None:
        # Direct file transcription — no recording, no daemon.
        text = transcribe(args.wav, cfg)
    else:
        text = stt_dictate(cfg, use_daemon=not args.no_daemon)

    log(f"📤 heard ({len(text)}c)")
    print(text)
    return 0 if text else 1


if __name__ == "__main__":
    raise SystemExit(main())
