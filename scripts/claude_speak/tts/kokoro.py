"""Local TTS via Kokoro (kokoro-onnx + ONNX runtime).

On first use:
  1. pip install kokoro-onnx + onnxruntime (user site)
  2. Download kokoro-v1.0.onnx and voices-v1.0.bin into CLAUDE_PLUGIN_DATA/kokoro/

A load lock serializes the one-time install/download across pipeline threads.
A synth lock serializes CPU-bound ONNX inference (parallel calls thrash cores
on Intel and make every sentence N× slower). Cached `Kokoro` instance lives at
module scope so all sentences in the same hook invocation share it.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from ..audio import pcm_to_wav
from ..config import DATA_DIR
from ..logging import log, log_v, notify

_INSTANCE = None
_LOAD_LOCK = threading.Lock()
_SYNTH_LOCK = threading.Lock()


def _ensure(cfg: dict) -> tuple[Path, Path] | None:
    try:
        import kokoro_onnx  # noqa: F401
    except ImportError:
        log("installing kokoro-onnx + onnxruntime (one-time setup)")
        notify("claude-speak", "Installing Kokoro TTS (first-time setup)…")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "--quiet",
             "kokoro-onnx", "onnxruntime"],
            check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            log("kokoro-onnx install failed — run: pip3 install --user kokoro-onnx onnxruntime")
            return None

    model_dir = DATA_DIR / "kokoro"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / "kokoro-v1.0.onnx"
    voices_path = model_dir / "voices-v1.0.bin"

    for path, url, label, size in (
        (model_path, cfg.get("kokoro_model_url"), "Kokoro model", "~170MB"),
        (voices_path, cfg.get("kokoro_voices_url"), "Kokoro voices", "~27MB"),
    ):
        if path.exists() and path.stat().st_size > 1_000_000:
            continue
        log(f"downloading {label}: {url}")
        notify("claude-speak", f"Downloading {label} ({size}, one-time)…")
        tmp: Path | None = None
        try:
            tmp = path.with_suffix(path.suffix + ".partial")
            urllib.request.urlretrieve(url, tmp)
            tmp.rename(path)
            log(f"{label} downloaded bytes={path.stat().st_size}")
        except Exception as e:
            log(f"{label} download failed: {type(e).__name__}: {e}")
            if tmp is not None:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
            return None
    return model_path, voices_path


def _load(cfg: dict):
    global _INSTANCE
    with _LOAD_LOCK:
        if _INSTANCE is not None:
            return _INSTANCE
        paths = _ensure(cfg)
        if not paths:
            return None
        model_path, voices_path = paths
        try:
            from kokoro_onnx import Kokoro  # type: ignore
            t = time.monotonic()
            _INSTANCE = Kokoro(str(model_path), str(voices_path))
            log_v(f"kokoro loaded in {time.monotonic() - t:.2f}s voice={cfg.get('kokoro_voice')}")
            return _INSTANCE
        except Exception as e:
            log(f"kokoro load error: {type(e).__name__}: {e}")
            return None


def synthesize(text: str, cfg: dict) -> tuple[bytes, str]:
    """Synthesize the given text into a WAV. Returns (bytes, 'wav') or (b'', '') on failure."""
    kokoro = _load(cfg)
    if kokoro is None:
        return b"", ""
    voice = cfg.get("kokoro_voice", "af_sarah")
    speed = float(cfg.get("kokoro_speed", 1.0))
    lang = cfg.get("kokoro_lang", "en-us")
    try:
        import numpy as np  # type: ignore
        with _SYNTH_LOCK:
            t = time.monotonic()
            # Kokoro-onnx handles long text via internal chunking — we hand it
            # whatever we got. Callers that need a budget (rewrite input, for
            # example) enforce their own cap upstream.
            samples, sr = kokoro.create(text, voice=voice, speed=speed, lang=lang)
            log_v(f"kokoro synth {len(text)}c in {time.monotonic() - t:.2f}s")
        pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("int16").tobytes()
        return pcm_to_wav(pcm16, sample_rate=int(sr)), "wav"
    except Exception as e:
        log(f"kokoro synth error: {type(e).__name__}: {e}")
        return b"", ""


if __name__ == "__main__":
    from ..venv import ensure_venv_python
    ensure_venv_python()

    import argparse
    import shutil
    import sys as _sys
    from ..config import load_config
    from ..defaults import DEFAULTS
    from ..logging import enable_stderr_tee, log, section
    from ..transcript import split_sentence_stream
    enable_stderr_tee()

    ap = argparse.ArgumentParser(
        description="Synthesize text with Kokoro and (optionally) play back. "
        "Prints load/synth/audio timings per sentence for perf benchmarking.",
    )
    KOKORO_VOICES = (
        "af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova "
        "af_river af_sarah af_sky am_adam am_echo am_eric am_fenrir am_liam "
        "am_michael am_onyx am_puck am_santa bf_alice bf_emma bf_isabella bf_lily "
        "bm_daniel bm_fable bm_george bm_lewis"
    ).split()
    ap.add_argument("--voice", help="override kokoro_voice (see --list-voices)")
    ap.add_argument("--speed", type=float, help="synthesis speed (1.0 default; the "
                    "prosody/pace Kokoro itself produces)")
    ap.add_argument("--rate", type=float, help="playback rate via ffplay atempo "
                    "(0.25-4.0, pitch-preserving); independent of --speed")
    ap.add_argument("--lang", default="en-us", help="language code (en-us, en-gb, ...)")
    ap.add_argument("--list-voices", action="store_true",
                    help="print available voice names and exit")
    ap.add_argument("--text", help="inline text to speak")
    ap.add_argument("--file", type=Path, help="read text from a file instead of --text")
    ap.add_argument("--whole", action="store_true",
                    help="synthesize the entire text in one Kokoro call instead of the "
                    "default overlapped per-sentence pipeline (slower first audio, "
                    "smoother prosody)")
    ap.add_argument("--out", type=Path, default=Path("/tmp/kokoro-test.wav"))
    ap.add_argument("--no-play", action="store_true", help="skip playback after synth")
    args = ap.parse_args()

    if args.list_voices:
        for v in KOKORO_VOICES:
            print(v)
        raise SystemExit(0)

    if args.text and args.file:
        ap.error("--text and --file are mutually exclusive")
    if args.file:
        text = args.file.read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    elif not _sys.stdin.isatty():
        text = _sys.stdin.read()
    else:
        ap.error("provide --text, --file, or pipe text on stdin")
    text = text.strip()
    if not text:
        print("empty input")
        raise SystemExit(1)

    cfg = load_config(dict(DEFAULTS))
    if args.voice:
        cfg["kokoro_voice"] = args.voice
    if args.speed is not None:
        cfg["kokoro_speed"] = args.speed
    if args.lang:
        cfg["kokoro_lang"] = args.lang
    if args.rate is not None:
        cfg["playback_rate"] = args.rate

    mode = "whole" if args.whole else "per-sentence"
    section("🔊 SPEAK (cli)")
    log(f"voice={cfg.get('kokoro_voice')} synth_speed={cfg.get('kokoro_speed')} "
        f"lang={cfg.get('kokoro_lang')} playback_rate={cfg.get('playback_rate', 1.0)} "
        f"chars={len(text)} mode={mode}")

    def play(path: Path) -> None:
        if args.no_play:
            return
        # Use ffplay when we need atempo (non-1.0 playback rate); afplay otherwise.
        from ..audio import playback_rate, atempo_chain
        rate = playback_rate(cfg)
        if shutil.which("ffplay") and abs(rate - 1.0) > 1e-3:
            subprocess.run(
                ["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet",
                 "-af", atempo_chain(rate), str(path)],
                check=False,
            )
        elif shutil.which("afplay"):
            rate_args = []
            if abs(rate - 1.0) > 1e-3:
                rate_args = ["-r", f"{rate:.3f}"]
            subprocess.run(["afplay", *rate_args, str(path)], check=False)
        elif shutil.which("ffplay"):
            subprocess.run(
                ["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet", str(path)],
                check=False,
            )

    per_sentence = not args.whole
    if per_sentence and args.no_play:
        # Pure synthesis benchmark — no player subprocess, straightforward
        # sequential timing so the numbers are clean.
        sentences = list(split_sentence_stream(iter([text])))
        print(f"# benchmark mode: {len(sentences)} sentence(s), no playback")
        total_bytes = 0
        total_t = 0.0
        for i, sentence in enumerate(sentences, 1):
            t = time.monotonic()
            audio, _ = synthesize(sentence, cfg)
            dt = time.monotonic() - t
            total_t += dt
            if not audio:
                print(f"  [{i}] FAILED after {dt:.2f}s")
                continue
            total_bytes += len(audio)
            audio_s = max(0.0, (len(audio) - 44) / (24000 * 2))
            rtf = dt / audio_s if audio_s > 0 else float("inf")
            print(f"  [{i}] {dt:.2f}s  {len(audio)}B  ~{audio_s:.2f}s audio  "
                  f"RTF={rtf:.2f}x  \"{sentence[:60]}{'…' if len(sentence) > 60 else ''}\"")
        print(f"# total synth: {total_t:.2f}s  output: {total_bytes}B")
    elif per_sentence:
        # Use the same pipeline the hook uses: sentence N+1 renders in a
        # worker thread while sentence N plays. Synthesis is serialized by
        # Kokoro's internal lock (ONNX is CPU-bound), but playback overlap
        # gives that continuous flow.
        from ..pipeline import run_pipeline
        from ..logging import LOG_PATH

        def passthrough(txt: str, _cfg: dict):
            yield txt

        import sys as _sys

        def fetch(sentence: str):
            t_start = time.monotonic()
            audio, ext = synthesize(sentence, cfg)
            dt = time.monotonic() - t_start
            audio_s = max(0.0, (len(audio) - 44) / (24000 * 2)) if audio else 0.0
            rtf = dt / audio_s if audio_s > 0 else float("inf")
            preview = sentence[:60] + ("…" if len(sentence) > 60 else "")
            _sys.stderr.write(
                f"  synth={dt:.2f}s  {len(audio)}B  ~{audio_s:.2f}s audio  "
                f"RTF={rtf:.2f}x  \"{preview}\"\n"
            )
            _sys.stderr.flush()
            return audio, ext

        print("# pipeline mode: overlapped render/playback")
        print(f"# log: tail -f {LOG_PATH}")
        t0 = time.monotonic()
        run_pipeline(cfg, passthrough, fetch, text)
        print(f"# total wall time: {time.monotonic() - t0:.2f}s")
    else:
        t = time.monotonic()
        audio, ext = synthesize(text, cfg)
        dt = time.monotonic() - t
        if not audio:
            print(f"synth FAILED after {dt:.2f}s")
            raise SystemExit(1)
        args.out.write_bytes(audio)
        audio_s = max(0.0, (len(audio) - 44) / (24000 * 2))
        rtf = dt / audio_s if audio_s > 0 else float("inf")
        print(f"synth={dt:.2f}s  audio~{audio_s:.2f}s  RTF={rtf:.2f}x  "
              f"bytes={len(audio)}  out={args.out}")
        play(args.out)
