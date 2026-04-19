#!/usr/bin/env python3
"""CLI for the /speak slash command. Reads/writes the plugin's config.json."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from claude_speak.config import CONFIG_PATH, data_dir  # noqa: E402
from claude_speak.defaults import DEFAULTS  # noqa: E402

DATA_DIR = data_dir()


def load() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}


def save(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def merged() -> dict:
    return {**DEFAULTS, **load()}


def show() -> None:
    cfg = merged()
    # Only print the knobs users actually interact with.
    visible_keys = [
        "enabled", "kokoro_voice", "kokoro_speed", "playback_rate",
        "mode", "voice_loop", "speak_notifications", "record_mute_system",
        "claude_model", "record_silence_seconds",
    ]
    print(json.dumps({k: cfg.get(k) for k in visible_keys}, indent=2))


def usage() -> None:
    d = DEFAULTS
    print(
        "Usage: /speak <command>\n"
        f"  on | off                  — enable / disable playback "
        f"(default: {'on' if d['enabled'] else 'off'})\n"
        "  stop                      — kill current playback\n"
        f"  voice <name>              — set Kokoro voice "
        f"(default: {d['kokoro_voice']}; run 'voices' to list)\n"
        "  voices                    — list all Kokoro voices\n"
        f"  rate <0.25-4.0>           — playback speed, pitch-preserving "
        f"(default: {d['playback_rate']})\n"
        f"  silence <0.3-10>          — seconds of silence that ends a dictation "
        f"(default: {d['record_silence_seconds']})\n"
        f"  mode stream|whole         — stream: play sentence-by-sentence as each finishes\n"
        f"                              (fast first audio); whole: one big synthesis then play\n"
        f"                              (slower start, smoother prosody) "
        f"(default: {d['mode']})\n"
        f"  dictate on|off            — auto-record after each response and feed voice back "
        f"(default: {'on' if d['auto_dictation'] else 'off'})\n"
        f"  mute on|off               — mute macOS system output while recording, so "
        f"background audio doesn't leak into the mic "
        f"(default: {'on' if d['record_mute_system'] else 'off'})\n"
        f"  model <name>              — Claude model for the rewrite step "
        f"(default: {d['claude_model']})\n"
        f"  whisper-model <name>      — switch + download a whisper.cpp model "
        f"(default: {d['whisper_cpp_model']})\n"
        f"                              examples: ggml-small.en.bin, ggml-medium.en-q5_0.bin,\n"
        f"                                        ggml-large-v3-turbo-q5_0.bin\n"
        f"  verbose on|off            — log per-sentence/per-step detail "
        f"(default: {'on' if d['log_verbose'] else 'off'})\n"
        "  install [--force]         — pre-download all models/deps (runs automatically\n"
        "                              on first session; --force re-runs)\n"
        "  uninstall [--wipe-logs]   — delete venv, models, config; requires --force\n"
        "  progress [--follow]       — show install log lines (snapshot or live stream)\n"
        "  status                    — show current config + tooling availability\n"
        "\n"
        "Debug individual backends from a terminal:\n"
        "  python3 -m claude_speak.tts.kokoro --text 'hi' --voice af_nova\n"
        "  python3 -m claude_speak.recording --silence 1.0 --out /tmp/v.wav\n"
        "  python3 -m claude_speak.stt.whisper_cpp      # record + transcribe in one shot"
    )


KOKORO_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica",
    "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir",
    "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]


def voices() -> None:
    cfg = merged()
    current = cfg.get("kokoro_voice", DEFAULTS["kokoro_voice"])
    print("Kokoro voices (local; prefix — af: US female, am: US male, bf: UK female, bm: UK male)")
    per_row = 4
    rows = [KOKORO_VOICES[i:i + per_row] for i in range(0, len(KOKORO_VOICES), per_row)]
    for row in rows:
        line = "  "
        for v in row:
            mark = "*" if v == current else " "
            line += f"{mark}{v:<13}"
        print(line.rstrip())
    print()
    print("(* = currently selected)")
    print("Set with: /speak voice <name>")


def _installer_running() -> bool:
    return subprocess.run(
        ["pgrep", "-f", "claude_speak.install"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def _print_progress_snapshot() -> None:
    from claude_speak.logging import LOG_PATH
    if not LOG_PATH.exists():
        print(f"(no log yet at {LOG_PATH})")
        return
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        lines = [ln.rstrip() for ln in f if "install:" in ln]
    for ln in lines[-30:]:
        print(ln)
    print()
    print(f"installer running: {'yes' if _installer_running() else 'no'}")
    print(f"log file: {LOG_PATH}")
    print(f"live follow: tail -f {LOG_PATH} | grep install:")


def _follow_install_log() -> None:
    """Stream install: log lines until the installer exits. Ctrl+C to stop."""
    from claude_speak.logging import LOG_PATH
    import time as _time
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.touch(exist_ok=True)
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        # Start from end so we only show new lines.
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                if "install:" in line:
                    print(line.rstrip(), flush=True)
            else:
                if not _installer_running():
                    break
                _time.sleep(0.2)


def status() -> None:
    show()
    print()
    print(f"claude CLI:  {'yes' if shutil.which('claude') else 'NO'}")
    print(f"ffplay:      {'yes' if shutil.which('ffplay') else 'no (install ffmpeg for atempo playback)'}")
    print(f"afplay:      {'yes' if shutil.which('afplay') else 'no'}")
    print(f"whisper-cli: {'yes' if shutil.which('whisper-cli') else 'not yet (first use will brew install)'}")
    print(f"data dir:    {DATA_DIR}")
    log_path = DATA_DIR / "speak.log"
    log_hint = f"{log_path} ({log_path.stat().st_size // 1024}KB)" if log_path.exists() else f"{log_path} (not yet written)"
    print(f"log file:    {log_hint}")


def main(argv: list[str]) -> int:
    if not argv:
        usage()
        return 0
    cmd, *rest = argv
    cfg = load()

    if cmd == "on":
        cfg["enabled"] = True
    elif cmd == "off":
        cfg["enabled"] = False
    elif cmd == "stop":
        # 1. Tell the daemon to kill any in-flight playback/recording.
        #    Uses its kill op (SIGTERMs ffplay via PID_FILE, sets
        #    RECORD_CANCEL to abort recording) — scoped to OUR processes.
        try:
            from claude_speak.daemon import _socket_alive, send_request
            if _socket_alive():
                send_request({"op": "kill"}, timeout=2)
        except Exception:
            pass
        # 2. Write a one-shot marker so speak.py silences the VERY NEXT
        #    Stop hook firing. Without this, Claude's reply to /speak stop
        #    immediately triggers a new turn and we'd speak 'stopped' back.
        try:
            (DATA_DIR / ".skip_next_turn").touch()
        except OSError:
            pass
        print("stopped")
        return 0
    elif cmd == "voice" and rest:
        cfg["kokoro_voice"] = rest[0]
    elif cmd == "rate" and rest:
        try:
            r = float(rest[0])
        except ValueError:
            print(f"invalid rate: {rest[0]!r}")
            return 1
        if not 0.25 <= r <= 4.0:
            print(f"rate {r} out of range (0.25–4.0)")
            return 1
        cfg["playback_rate"] = r
    elif cmd == "silence" and rest:
        try:
            s = float(rest[0])
        except ValueError:
            print(f"invalid silence seconds: {rest[0]!r}")
            return 1
        if not 0.3 <= s <= 10:
            print(f"silence {s} out of range (0.3–10)")
            return 1
        cfg["record_silence_seconds"] = s
    elif cmd == "mode" and rest and rest[0] in ("stream", "whole"):
        cfg["mode"] = rest[0]
        # drop legacy key if present so the two don't disagree
        cfg.pop("pipeline_sentences", None)
    elif cmd == "dictate" and rest and rest[0] in ("on", "off"):
        # Renamed to voice_loop; set both for back-compat while older code
        # (or running daemon) is still around.
        on = rest[0] == "on"
        cfg["voice_loop"] = on
        cfg["auto_dictation"] = on
    elif cmd == "notifications" and rest and rest[0] in ("on", "off"):
        cfg["speak_notifications"] = rest[0] == "on"
    elif cmd == "mute" and rest and rest[0] in ("on", "off"):
        cfg["record_mute_system"] = rest[0] == "on"
    elif cmd == "model" and rest:
        cfg["claude_model"] = rest[0]
    elif cmd == "whisper-model" and rest:
        from claude_speak.install import _download, REQUIREMENTS  # noqa: F401
        from claude_speak.config import DATA_DIR as _DD
        model_name = rest[0]
        if not model_name.startswith("ggml-") or not model_name.endswith(".bin"):
            print(f"invalid model name: {model_name!r}")
            print("expected like: ggml-small.en.bin, ggml-medium.en-q5_0.bin, "
                  "ggml-large-v3-turbo-q5_0.bin")
            return 1
        url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{model_name}"
        dest = _DD / "models" / model_name
        ok = _download(url, dest, f"whisper model ({model_name})", quiet=False)
        if not ok:
            print(f"download failed; keeping current model {cfg.get('whisper_cpp_model')!r}")
            return 1
        cfg["whisper_cpp_model"] = model_name
    elif cmd == "verbose" and rest and rest[0] in ("on", "off"):
        cfg["log_verbose"] = rest[0] == "on"
    elif cmd == "status":
        status()
        return 0
    elif cmd == "log":
        log_path = DATA_DIR / "speak.log"
        if not log_path.exists():
            print(f"log not yet written: {log_path}")
            return 0
        # Default to last 40 lines; user can pass an int to override.
        n = 40
        if rest and rest[0].lstrip("-").isdigit():
            n = int(rest[0].lstrip("-"))
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-n:]:
            print(line, end="")
        return 0
    elif cmd == "install":
        from claude_speak.install import install_all, MARKER
        force = "--force" in rest
        if force:
            try:
                MARKER.unlink()
            except FileNotFoundError:
                pass
        # If a background installer is already running, attach to it by
        # streaming its progress instead of starting a duplicate process.
        if _installer_running():
            print("# installer is already running in the background — following its log")
            _follow_install_log()
            return 0
        ok = install_all(cfg=merged(), quiet=False)
        return 0 if ok else 1
    elif cmd == "uninstall":
        from claude_speak.install import uninstall_all
        from claude_speak.config import DATA_DIR as _DD
        if "--force" not in rest:
            print(f"This will wipe {_DD}")
            print("  - .venv/ (plugin-private Python env, ~1GB)")
            print("  - kokoro/ and models/ (downloaded TTS and STT models)")
            print("  - config.json, markers, lockfile")
            print()
            print("Pass --force to confirm (add --wipe-logs to also delete speak.log).")
            return 1
        wipe_logs = "--wipe-logs" in rest
        ok = uninstall_all(keep_log=not wipe_logs, quiet=False)
        return 0 if ok else 1
    elif cmd == "progress":
        follow = "--follow" in rest
        if follow:
            _follow_install_log()
            return 0
        _print_progress_snapshot()
        return 0
    elif cmd == "voices":
        voices()
        return 0
    elif cmd == "daemon" and rest:
        sub = rest[0]
        from claude_speak.daemon import (
            ensure_daemon, send_request, _socket_alive, SOCKET_PATH,
        )
        if sub == "status":
            if not _socket_alive():
                print("daemon: not running")
                return 0
            resp = send_request({"op": "status"}, timeout=2)
            print(json.dumps(resp, indent=2) if resp else "daemon: unresponsive")
            return 0
        if sub == "stop":
            if not _socket_alive():
                print("daemon: not running")
                return 0
            send_request({"op": "shutdown"}, timeout=2)
            import time as _t
            for _ in range(20):
                if not SOCKET_PATH.exists():
                    print("daemon: stopped")
                    return 0
                _t.sleep(0.1)
            print("daemon: didn't exit cleanly")
            return 1
        if sub == "restart":
            if _socket_alive():
                send_request({"op": "shutdown"}, timeout=2)
                import time as _t
                for _ in range(20):
                    if not SOCKET_PATH.exists():
                        break
                    _t.sleep(0.1)
            print("daemon: restarted" if ensure_daemon() else "daemon: failed to start")
            return 0
        if sub == "on":
            cfg["daemon"] = True
        elif sub == "off":
            cfg["daemon"] = False
            if _socket_alive():
                send_request({"op": "shutdown"}, timeout=2)
        else:
            print(f"unknown daemon subcommand: {sub}")
            return 1
    else:
        usage()
        return 1

    save(cfg)
    show()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
