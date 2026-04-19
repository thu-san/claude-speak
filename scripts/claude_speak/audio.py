"""Audio playback helpers: WAV framing, ffplay command building, synchronous play."""
from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

from .config import DATA_DIR


def pcm_to_wav(pcm: bytes, sample_rate: int = 24000, channels: int = 1, bits: int = 16) -> bytes:
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm)
    header = b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
    data = b"data" + struct.pack("<I", data_size) + pcm
    return header + fmt + data


def playback_rate(cfg: dict) -> float:
    try:
        r = float(cfg.get("playback_rate", 1.0))
    except (TypeError, ValueError):
        return 1.0
    return max(0.25, min(4.0, r))


def atempo_chain(rate: float) -> str:
    """Build an atempo filter chain; each atempo instance supports 0.5–2.0."""
    factors: list[float] = []
    r = rate
    while r < 0.5:
        factors.append(0.5)
        r /= 0.5
    while r > 2.0:
        factors.append(2.0)
        r /= 2.0
    factors.append(r)
    return ",".join(f"atempo={f:.4f}" for f in factors)


def ffplay_cmd(cfg: dict, extra: list[str] | None = None) -> list[str]:
    cmd = ["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet"]
    if extra:
        cmd += extra
    rate = playback_rate(cfg)
    if abs(rate - 1.0) > 1e-3:
        cmd += ["-af", atempo_chain(rate)]
    cmd.append("-")
    return cmd


def play_synchronous(audio: bytes, ext: str, cfg: dict) -> None:
    """Write audio to a temp file and block until playback completes."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / f"last.{ext}"
    tmp.write_bytes(audio)
    rate = playback_rate(cfg)
    if shutil.which("ffplay"):
        args = ["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet"]
        if abs(rate - 1.0) > 1e-3:
            args += ["-af", atempo_chain(rate)]
        args += [str(tmp)]
    else:
        args = ["afplay"]
        if abs(rate - 1.0) > 1e-3:
            args += ["-r", f"{rate:.3f}"]
        args += [str(tmp)]
    subprocess.run(args, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def play_async(audio: bytes, ext: str, cfg: dict) -> int:
    """Write audio then fire-and-forget via afplay. Returns the player PID."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / f"last.{ext}"
    tmp.write_bytes(audio)
    args = ["afplay"]
    rate = playback_rate(cfg)
    if abs(rate - 1.0) > 1e-3:
        args += ["-r", f"{rate:.3f}"]
    args.append(str(tmp))
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid
