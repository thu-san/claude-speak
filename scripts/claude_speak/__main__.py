"""End-to-end pipeline CLI: `python -m claude_speak turn`.

Runs the full Stop-hook flow against a canned assistant reply so you can
benchmark / debug the whole pipeline without a live Claude Code session:

    [input text] → rewrite (claude -p)
                 → Kokoro TTS
                 → ffplay (you hear the speech)
                 → record mic (Silero VAD)
                 → whisper.cpp
                 → prints the transcribed reply on stdout

Exactly what the Stop hook triggers — just sourced from a file or --text
instead of a real transcript. Routes through the warm daemon by default
so the timing matches production; pass --no-daemon to run in-process.
"""
from __future__ import annotations

from .venv import ensure_venv_python
ensure_venv_python()

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from .config import load_config
from .defaults import DEFAULTS
from .logging import enable_stderr_tee, log, section, tail_log_to_stderr


def _write_fake_transcript(assistant_text: str, user_text: str) -> Path:
    """Synthesize a minimal JSONL transcript that read_last_turn understands."""
    tmp = Path(tempfile.mkstemp(prefix="cs-turn-", suffix=".jsonl")[1])
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": user_text},
        }) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": assistant_text},
        }) + "\n")
    return tmp


def _cmd_turn(args: argparse.Namespace) -> int:
    enable_stderr_tee()

    if args.file:
        assistant_text = open(args.file, encoding="utf-8").read().strip()
    elif args.text:
        assistant_text = args.text
    elif not sys.stdin.isatty():
        assistant_text = sys.stdin.read().strip()
    else:
        print("error: provide --text, --file, or pipe text on stdin", file=sys.stderr)
        return 1

    user_context = args.user or "(debug: canned user prompt for end-to-end test)"
    transcript_path = _write_fake_transcript(assistant_text, user_context)

    section("🔁 TURN (cli)")
    log(f"assistant={len(assistant_text)}c user={len(user_context)}c "
        f"transcript={transcript_path}")

    cfg = load_config(dict(DEFAULTS))
    use_daemon = cfg.get("daemon", True) and not args.no_daemon

    t0 = time.monotonic()
    try:
        if use_daemon:
            from .daemon import ensure_daemon, send_request
            if not ensure_daemon():
                log("daemon unreachable — falling back to in-process")
                use_daemon = False
            else:
                # Tail speak.log to stderr while the daemon works — otherwise
                # we block silently for 15-30s and the user sees nothing.
                with tail_log_to_stderr():
                    resp = send_request(
                        {"op": "turn", "transcript_path": str(transcript_path)},
                        timeout=600,
                    )
                if not resp or not resp.get("ok"):
                    log(f"daemon turn failed: {resp}")
                    return 1
                out = resp.get("stdout") or ""
                log(f"turn completed in {time.monotonic() - t0:.1f}s")
                if out:
                    print(out)
                return 0

        from .main import run_turn
        out = run_turn({"transcript_path": str(transcript_path)})
        log(f"turn completed in {time.monotonic() - t0:.1f}s")
        if out:
            print(out)
        return 0
    finally:
        try:
            transcript_path.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="claude_speak",
        description="End-to-end pipeline CLI. Today: only the 'turn' subcommand.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_turn = sub.add_parser(
        "turn",
        help="Run the full Stop-hook pipeline (rewrite → TTS → speak → listen → STT → print).",
        description="Run the full Stop-hook pipeline against a canned assistant reply.",
    )
    p_turn.add_argument("--text", help="inline assistant text to feed the pipeline")
    p_turn.add_argument("--file", help="read assistant text from a file")
    p_turn.add_argument("--user", help="canned user-prompt context (optional)")
    p_turn.add_argument("--no-daemon", action="store_true",
                        help="bypass the warm daemon and run in-process")
    p_turn.set_defaults(func=_cmd_turn)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
