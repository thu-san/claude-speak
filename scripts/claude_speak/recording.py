"""Microphone capture. Prefers sounddevice (PortAudio) with ffmpeg as fallback."""
from __future__ import annotations

import math
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

from .config import DATA_DIR
from .logging import beep, log, log_v, notify

_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")

# Set by the daemon's "kill" op (or any external caller) to abort an
# in-flight recording. Each capture path checks this in its inner loop.
RECORD_CANCEL = threading.Event()


def _get_system_muted() -> bool | None:
    """Return True/False if we can read the macOS output mute state; None otherwise."""
    if not shutil.which("osascript"):
        return None
    try:
        result = subprocess.run(
            ["osascript", "-e", "output muted of (get volume settings)"],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except subprocess.TimeoutExpired:
        return None
    out = result.stdout.strip().lower()
    if out == "true":
        return True
    if out == "false":
        return False
    return None


def _set_system_muted(muted: bool) -> None:
    if not shutil.which("osascript"):
        return
    cmd = "true" if muted else "false"
    subprocess.run(
        ["osascript", "-e", f"set volume output muted {cmd}"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
    )


class _SystemMute:
    """Context manager that mutes macOS system output for the duration of the
    `with` block and restores whatever the previous mute state was on exit.
    No-op on non-macOS or when the feature is disabled in config."""

    def __init__(self, cfg: dict) -> None:
        self.enabled = bool(cfg.get("record_mute_system", True))
        self.prev: bool | None = None

    def __enter__(self) -> "_SystemMute":
        if not self.enabled:
            return self
        self.prev = _get_system_muted()
        if self.prev is False:
            _set_system_muted(True)
            log_v("system output muted for recording")
        return self

    def __exit__(self, *exc_info) -> None:
        if not self.enabled:
            return
        if self.prev is False:
            _set_system_muted(False)
            log_v("system output unmuted")


def _ensure_silero() -> bool:
    """Lazy-install silero-vad. Returns True if available."""
    try:
        import silero_vad  # noqa: F401
        return True
    except ImportError:
        pass
    log("installing silero-vad (one-time, ~10MB including ONNX model)")
    notify("claude-speak", "Installing Silero VAD (first-time)…")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--user", "--quiet", "silero-vad"],
        check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        import silero_vad  # noqa: F401
        return True
    except ImportError:
        log("silero-vad install failed — falling back to dB threshold. "
            "Try manually: pip3 install --user silero-vad")
        return False


def _ensure_sounddevice() -> bool:
    """Lazy-install sounddevice + numpy on first use. Returns True if available."""
    try:
        import numpy  # noqa: F401
        import sounddevice  # noqa: F401
        return True
    except ImportError:
        pass
    log("installing sounddevice + numpy (one-time setup, cuts mic cold-start ~1s)")
    notify("claude-speak", "Installing mic capture library (one-time)…")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--user", "--quiet",
         "sounddevice", "numpy"],
        check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        import numpy  # noqa: F401
        import sounddevice  # noqa: F401
        return True
    except ImportError:
        log("sounddevice install failed — falling back to ffmpeg. "
            "Try manually: pip3 install --user sounddevice numpy")
        return False


def record_mic_silero(cfg: dict) -> Path | None:
    """Capture via sounddevice using Silero VAD for speech/silence detection.

    Silero's neural VAD emits per-window speech probabilities. It ignores
    breathing, keyboard clicks, and low-level ambient noise that a dB
    threshold would catch, so "am I hearing speech?" is much more accurate.
    """
    if not _ensure_sounddevice() or not _ensure_silero():
        return None
    import numpy as np  # type: ignore
    import sounddevice as sd  # type: ignore
    import silero_vad  # type: ignore

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "input.wav"
    if out.exists():
        try:
            out.unlink()
        except OSError:
            pass

    sr = 16000  # Silero's native rate
    window_samples = 512  # Silero VAD expects 512 samples @ 16kHz (32ms)
    stop_secs = float(cfg.get("record_silence_seconds", 4.0))
    max_secs = int(cfg.get("record_max_seconds", 60))
    start_grace = float(cfg.get("record_start_grace", 8.0))
    threshold = float(cfg.get("record_vad_threshold", 0.5))
    windows_for_stop = max(1, int(stop_secs / (window_samples / sr)))

    if cfg.get("record_beep", True):
        beep("start")
    log_v(f"recording (silero-vad) start max={max_secs}s stop_silence={stop_secs}s "
        f"threshold={threshold} sr={sr}")

    # Defer the system mute + notification banner to a background thread so
    # the mic opens immediately. osascript takes ~200-500ms for each call,
    # which otherwise shows up as "I started speaking but it didn't hear me."
    _mute = _SystemMute(cfg)
    threading.Thread(
        target=lambda: (_mute.__enter__(),
                        notify("claude-speak 🎤",
                               "Listening… (pause to end, say 'cancel' to skip)")),
        daemon=True,
    ).start()

    model = silero_vad.load_silero_vad(onnx=True)

    audio_q: "queue.Queue" = queue.Queue()

    def callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
        audio_q.put(indata.copy())

    collected: list = []
    heard = False
    silent_windows = 0
    start = time.monotonic()
    error: str | None = None
    peak_prob = 0.0

    try:
        t_open = time.monotonic()
        stream = sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                blocksize=window_samples, callback=callback)
        stream.start()
        stream_open_ms = int((time.monotonic() - t_open) * 1000)
        log_v(f"sounddevice stream open in {stream_open_ms}ms "
              f"default_input={sd.default.device[0]}")
        speech_at: float | None = None
        try:
            while True:
                if RECORD_CANCEL.is_set():
                    log_v("recording cancelled by external signal")
                    break
                elapsed = time.monotonic() - start
                if elapsed > max_secs:
                    log_v("recording hit max duration")
                    break
                if not heard and elapsed > start_grace:
                    log_v(f"no speech within grace={start_grace}s "
                          f"peak_prob={peak_prob:.2f} threshold={threshold} — stopping")
                    break
                try:
                    block = audio_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                collected.append(block)
                # Silero wants a 1-D tensor of exactly window_samples samples.
                import torch  # imported lazily; silero-vad pulls it in
                prob = model(torch.from_numpy(block.flatten()), sr).item()
                if prob > peak_prob:
                    peak_prob = prob
                if prob >= threshold:
                    if not heard:
                        speech_at = elapsed
                        log_v(f"speech detected at {elapsed:.2f}s prob={prob:.2f}")
                    heard = True
                    silent_windows = 0
                elif heard:
                    silent_windows += 1
                    if silent_windows >= windows_for_stop:
                        log_v(f"silence after speech at {elapsed:.2f}s — stopping")
                        break
        finally:
            stream.stop()
            stream.close()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log_v(f"silero error: {error}")
    finally:
        _mute.__exit__(None, None, None)

    if cfg.get("record_beep", True):
        beep("stop")

    if not heard:
        notify("claude-speak", "No speech detected")
        return None
    if error:
        return None

    audio = np.concatenate(collected, axis=0).flatten()
    audio = np.clip(audio, -1.0, 1.0)
    pcm16 = (audio * 32767.0).astype("int16")
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())
    notify("claude-speak", "Transcribing…")
    elapsed_total = time.monotonic() - start
    speech_dur = (elapsed_total - speech_at) if speech_at is not None else 0
    log(f"🎤 recorded {elapsed_total:.1f}s "
        f"(speech ~{speech_dur:.1f}s, {out.stat().st_size // 1024}KB) → {out}")
    return out


def record_mic_sounddevice(cfg: dict) -> Path | None:
    """Capture via PortAudio. Returns WAV path, or None on no speech."""
    if not _ensure_sounddevice():
        return None
    import numpy as np  # type: ignore
    import sounddevice as sd  # type: ignore

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "input.wav"
    if out.exists():
        try:
            out.unlink()
        except OSError:
            pass

    sr = int(cfg.get("record_sample_rate", 16000))
    block_ms = int(cfg.get("record_block_ms", 50))
    block_samples = sr * block_ms // 1000
    stop_db = float(cfg.get("record_silence_db", -30))
    stop_secs = float(cfg.get("record_silence_seconds", 1.2))
    max_secs = int(cfg.get("record_max_seconds", 60))
    start_grace = float(cfg.get("record_start_grace", 8.0))
    blocks_for_stop = max(1, int(stop_secs * 1000 / block_ms))

    if cfg.get("record_beep", True):
        beep("start")
    notify("claude-speak 🎤", "Listening… (silence ends it, say 'cancel' to skip)")
    log_v(f"recording (sounddevice) start max={max_secs}s stop_silence={stop_secs}s "
        f"threshold={stop_db}dB sr={sr}")

    _mute = _SystemMute(cfg)
    _mute.__enter__()

    audio_q: "queue.Queue" = queue.Queue()

    def callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
        audio_q.put(indata.copy())

    collected: list = []
    heard = False
    silent_blocks = 0
    start = time.monotonic()
    error: str | None = None
    peak_db = -200.0          # loudest block seen
    stream_open_ms = 0

    try:
        t_open = time.monotonic()
        stream = sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                blocksize=block_samples, callback=callback)
        stream.start()
        stream_open_ms = int((time.monotonic() - t_open) * 1000)
        log_v(f"sounddevice stream open in {stream_open_ms}ms "
            f"default_input={sd.default.device[0]}")
        try:
            while True:
                elapsed = time.monotonic() - start
                if elapsed > max_secs:
                    log("recording hit max duration")
                    break
                if not heard and elapsed > start_grace:
                    log_v(f"no speech within grace={start_grace}s "
                        f"peak_db={peak_db:.1f} threshold={stop_db}dB — stopping")
                    break
                try:
                    block = audio_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                collected.append(block)
                rms = float(np.sqrt(np.mean(block.astype("float32") ** 2)) + 1e-12)
                db = 20.0 * math.log10(rms)
                if db > peak_db:
                    peak_db = db
                if db > stop_db:
                    if not heard:
                        log_v(f"speech detected at {elapsed:.2f}s db={db:.1f}")
                    heard = True
                    silent_blocks = 0
                elif heard:
                    silent_blocks += 1
                    if silent_blocks >= blocks_for_stop:
                        log_v(f"silence after speech at {elapsed:.2f}s — stopping")
                        break
        finally:
            stream.stop()
            stream.close()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log_v(f"sounddevice error: {error}")
    finally:
        _mute.__exit__(None, None, None)

    if cfg.get("record_beep", True):
        beep("stop")

    if not heard:
        notify("claude-speak", "No speech detected")
        return None
    if error:
        return None

    audio = np.concatenate(collected, axis=0).flatten()
    audio = np.clip(audio, -1.0, 1.0)
    pcm16 = (audio * 32767.0).astype("int16")
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())
    notify("claude-speak", "Transcribing…")
    log(f"🎤 recorded ({out.stat().st_size // 1024}KB) → {out}")
    return out


def record_mic_ffmpeg(cfg: dict) -> Path | None:
    """Fallback: record via ffmpeg + silencedetect. Kills ffmpeg on first silence after speech."""
    if not shutil.which("ffmpeg"):
        log("ffmpeg missing — cannot record")
        return None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "input.wav"
    if out.exists():
        try:
            out.unlink()
        except OSError:
            pass
    stop_db = cfg.get("record_silence_db", -30)
    stop_secs = float(cfg.get("record_silence_seconds", 2.0))
    max_secs = int(cfg.get("record_max_seconds", 60))
    start_grace = float(cfg.get("record_start_grace", 8.0))
    mic = cfg.get("record_mic_input", ":0")

    args = [
        "ffmpeg", "-y", "-hide_banner",
        "-f", "avfoundation", "-i", mic,
        "-af", f"silencedetect=noise={stop_db}dB:d={stop_secs}",
        "-t", str(max_secs),
        "-ac", "1", "-ar", "16000",
        str(out),
    ]
    if cfg.get("record_beep", True):
        beep("start")
    notify("claude-speak 🎤", "Listening… (silence ends it, say 'cancel' to skip)")
    log_v(f"recording (ffmpeg) start max={max_secs}s stop_silence={stop_secs}s threshold={stop_db}dB")

    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    start = time.monotonic()
    heard = False
    stderr_buf: list[str] = []

    def reader() -> None:
        nonlocal heard
        assert proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="ignore")
            stderr_buf.append(line)
            if _SILENCE_END_RE.search(line):
                if not heard:
                    log_v(f"speech detected at {time.monotonic() - start:.2f}s")
                heard = True
            elif _SILENCE_START_RE.search(line) and heard:
                log_v(f"silence after speech at {time.monotonic() - start:.2f}s — stopping")
                try:
                    proc.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
                return

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    try:
        while True:
            if proc.poll() is not None:
                break
            elapsed = time.monotonic() - start
            if not heard and elapsed > start_grace:
                log_v(f"no speech within grace={start_grace}s — stopping")
                try:
                    proc.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
                break
            if elapsed > max_secs + 2:
                log("hard max exceeded — killing")
                proc.kill()
                break
            time.sleep(0.2)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    if cfg.get("record_beep", True):
        beep("stop")
    notify("claude-speak", "Transcribing…" if heard else "No speech detected")

    if not out.exists() or out.stat().st_size < 4000:
        size = out.stat().st_size if out.exists() else 0
        tail = "".join(stderr_buf[-10:])[-400:]
        log_v(f"recording too short bytes={size} stderr_tail={tail!r}")
        return None
    log(f"🎤 recorded ({out.stat().st_size // 1024}KB) → {out}")
    if not heard:
        return None
    return out


def record_mic(cfg: dict) -> Path | None:
    """Dispatcher: Silero VAD → sounddevice dB → ffmpeg.

    Silero is the preferred path: neural VAD ignores breathing / keyboard
    noise / AC hum, so false-starts and false-stops are rare. If the package
    can't be installed, falls back to the simpler dB-threshold path.
    """
    # Reset the cancel flag here so every path gets a fresh state — a
    # leftover SET from a prior killed request would otherwise abort the
    # next recording before it starts.
    RECORD_CANCEL.clear()
    if cfg.get("record_use_vad", True):
        result = record_mic_silero(cfg)
        if result is not None or "silero_vad" in sys.modules:
            return result
    if cfg.get("record_use_sounddevice", True):
        result = record_mic_sounddevice(cfg)
        if result is not None or "sounddevice" in sys.modules:
            return result
    return record_mic_ffmpeg(cfg)
