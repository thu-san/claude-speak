"""Local whisper.cpp (Homebrew package). Pure provider adapter — exposes
ensure(cfg) and transcribe(wav, cfg). The daemon-vs-in-process routing
and CLI live in stt/__init__.py and stt/__main__.py respectively."""
from __future__ import annotations

import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from ..config import DATA_DIR
from ..logging import log, notify

WHISPER_MODEL_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"


def _find_cli() -> str | None:
    for candidate in ("whisper-cli", "whisper-cpp", "main"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def ensure(cfg: dict) -> tuple[str, Path] | None:
    cli = _find_cli()
    if not cli:
        if shutil.which("brew"):
            log("whisper-cli not found — running `brew install whisper-cpp` (one-time setup)")
            notify("claude-speak", "Installing whisper.cpp (first-time setup)…")
            subprocess.run(
                ["brew", "install", "whisper-cpp"],
                check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            cli = _find_cli()
        if not cli:
            log("whisper-cli install failed — install manually: brew install whisper-cpp")
            return None

    model_name = cfg.get("whisper_cpp_model", "ggml-small.en.bin")
    models_dir = DATA_DIR / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    model_path = models_dir / model_name
    if not model_path.exists() or model_path.stat().st_size < 1_000_000:
        url = f"{WHISPER_MODEL_BASE_URL}/{model_name}"
        log(f"downloading whisper model: {url}")
        notify("claude-speak", f"Downloading {model_name} (one-time, ~30–150MB)…")
        tmp = model_path.with_suffix(model_path.suffix + ".partial")
        last_pct = [0]

        def _progress(block_num, block_size, total_size):
            if total_size <= 0:
                return
            pct = min(100, int(block_num * block_size * 100 / total_size))
            if pct >= last_pct[0] + 10 or pct == 100:
                last_pct[0] = pct
                log(f"downloading {model_name}: {pct}% of {total_size // (1024*1024)}MB")

        try:
            urllib.request.urlretrieve(url, tmp, reporthook=_progress)
            tmp.rename(model_path)
            log(f"model downloaded bytes={model_path.stat().st_size}")
        except Exception as e:
            log(f"model download failed: {type(e).__name__}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
            return None
    return cli, model_path


# Whisper sometimes returns these markers when the audio has no speech
# (silence, noise, music) — they aren't real prompts and shouldn't be fed
# back to Claude as if the user said something.
_NOISE_MARKERS = {
    "[BLANK_AUDIO]", "[blank_audio]",
    "[SILENCE]", "[silence]",
    "[NO_SPEECH]", "[no_speech]",
    "(silence)", "(noise)", "(music)",
    "[Music]", "[MUSIC]", "[music]",
}


_TIMING_RE_CACHE: dict[str, "re.Pattern"] = {}


def _parse_timing(stderr: str, label: str) -> float | None:
    """Pull a `whisper_print_timings: <label> = NNN.NN ms` value out of stderr.
    Returns milliseconds as float, or None if not found."""
    import re
    pat = _TIMING_RE_CACHE.get(label)
    if pat is None:
        pat = re.compile(rf"{re.escape(label)}\s*=\s*([0-9.]+)\s*ms")
        _TIMING_RE_CACHE[label] = pat
    m = pat.search(stderr)
    return float(m.group(1)) if m else None


def _strip_noise_markers(text: str) -> str:
    """Remove whisper's bracketed silence/noise tags. If the entire transcript
    is just markers, returns ''. Otherwise just removes the markers."""
    cleaned = text
    for marker in _NOISE_MARKERS:
        cleaned = cleaned.replace(marker, "")
    cleaned = cleaned.strip()
    if not cleaned:
        return ""
    return cleaned


def transcribe(wav_path: Path, cfg: dict) -> str:
    if not Path(wav_path).is_file():
        log(f"❌ wav not found: {wav_path}")
        return ""
    ready = ensure(cfg)
    if not ready:
        return ""
    cli, model = ready
    threads = str(int(cfg.get("whisper_cpp_threads", 4)))
    lang = cfg.get("whisper_cpp_language", "en")
    t = time.monotonic()
    # Drop -np so whisper-cli prints its `whisper_print_timings:` block to
    # stderr; we parse the load + total times out of it for the log line.
    result = subprocess.run(
        [cli, "-m", str(model), "-f", str(wav_path),
         "-t", threads, "-l", lang, "-nt"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        log(f"❌ whisper-cli exit={result.returncode} stderr={result.stderr.strip()!r}")
        return ""
    elapsed = time.monotonic() - t
    load_ms = _parse_timing(result.stderr, "load time")
    total_ms = _parse_timing(result.stderr, "total time")
    encode_ms = _parse_timing(result.stderr, "encode time")
    parts = [f"📝 transcribed in {elapsed:.1f}s"]
    if load_ms is not None:
        parts.append(f"load {load_ms / 1000:.2f}s")
    if encode_ms is not None:
        parts.append(f"encode {encode_ms / 1000:.2f}s")
    if total_ms is not None:
        parts.append(f"whisper {total_ms / 1000:.2f}s")
    log(" | ".join(parts))
    raw = result.stdout.strip()
    cleaned = _strip_noise_markers(raw)
    if raw and not cleaned:
        log(f"⏭️  whisper returned only silence markers ({raw[:80]!r}) — treated as no speech")
    return cleaned


