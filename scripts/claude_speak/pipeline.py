"""Sentence-streaming TTS pipeline. Reads rewrite tokens, splits into sentences,
fetches TTS in parallel (synth is serialized inside each backend), plays in order."""
from __future__ import annotations

import subprocess
import threading
import time
from queue import Queue
from typing import Callable, Iterator

from .audio import ffplay_cmd
from .logging import log, log_v, section


def run_pipeline(
    cfg: dict,
    rewrite_stream: Callable[[str, dict], Iterator[str]],
    fetch_tts: Callable[[str], tuple[bytes, str]],
    text: str,
) -> None:
    """Streaming rewrite -> per-sentence TTS fetch in parallel -> serial playback."""
    from .transcript import split_sentence_stream

    t0 = time.monotonic()
    section("🔊 SPEAK")
    log_v(f"speak start input_chars={len(text)}")

    class Holder:
        __slots__ = ("idx", "sentence", "audio", "ext", "event")

        def __init__(self, idx: int, sentence: str) -> None:
            self.idx = idx
            self.sentence = sentence
            self.audio: bytes = b""
            self.ext: str = ""
            self.event = threading.Event()

    play_queue: Queue = Queue()
    first_audio_at: list[float | None] = [None]

    def player_loop() -> None:
        played = 0
        while True:
            item = play_queue.get()
            if item is None:
                log_v(f"player done played={played}")
                return
            wait_start = time.monotonic()
            item.event.wait()
            wait_ms = int((time.monotonic() - wait_start) * 1000)
            if not item.audio:
                log_v(f"sentence {item.idx} skipped (no audio)")
                continue
            if first_audio_at[0] is None:
                first_audio_at[0] = time.monotonic() - t0
            log_v(f"sentence {item.idx} play_start wait_for_audio={wait_ms}ms "
                  f"audio_bytes={len(item.audio)}")
            proc = subprocess.Popen(
                ffplay_cmd(cfg),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                assert proc.stdin is not None
                proc.stdin.write(item.audio)
                proc.stdin.close()
                proc.wait()
            except BrokenPipeError:
                pass
            log_v(f"sentence {item.idx} play_end duration="
                  f"{int((time.monotonic() - wait_start) * 1000)}ms")
            played += 1

    tts_total_ms = [0]  # mutable for closure

    def fetch_worker(holder: Holder) -> None:
        t_start = time.monotonic()
        try:
            audio, ext = fetch_tts(holder.sentence)
            holder.audio, holder.ext = audio, ext
            ms = int((time.monotonic() - t_start) * 1000)
            tts_total_ms[0] += ms
            log_v(f"sentence {holder.idx} tts_done ms={ms} bytes={len(audio)}")
        except Exception as e:
            log(f"sentence {holder.idx} tts_error {type(e).__name__}: {e}")
        finally:
            holder.event.set()

    player_thread = threading.Thread(target=player_loop, daemon=False)
    player_thread.start()

    first_token_logged = False
    token_count = 0

    rewrite_first_s: float | None = None

    def count_tokens(stream: Iterator[str]) -> Iterator[str]:
        nonlocal first_token_logged, token_count, rewrite_first_s
        for chunk in stream:
            if not first_token_logged:
                rewrite_first_s = time.monotonic() - t0
                log_v(f"rewrite first_token in {rewrite_first_s:.1f}s")
                first_token_logged = True
            token_count += 1
            yield chunk

    sentences_collected: list[str] = []
    rewrite_done_s = 0.0
    try:
        sentence_count = 0
        rewrite_chars = 0
        for sentence in split_sentence_stream(count_tokens(rewrite_stream(text, cfg))):
            sentence_count += 1
            rewrite_chars += len(sentence)
            sentences_collected.append(sentence)
            log_v(f"sentence {sentence_count}: {sentence[:80]!r} ({len(sentence)}c)")
            holder = Holder(sentence_count, sentence)
            play_queue.put(holder)
            threading.Thread(target=fetch_worker, args=(holder,), daemon=True).start()
        rewrite_done_s = time.monotonic() - t0
        full_speech = " ".join(sentences_collected)
        log(f"✍️  claude rewrite: {rewrite_done_s:.1f}s → "
            f"{sentence_count} sentences, {rewrite_chars}c")
        log(f"   speech: {full_speech[:400]!r}"
            + (f"…(+{len(full_speech) - 400}c)" if len(full_speech) > 400 else ""))
    except Exception as e:
        log(f"❌ rewrite error: {type(e).__name__}: {e}")
    finally:
        play_queue.put(None)
        player_thread.join()
        total_s = time.monotonic() - t0
        first_s = first_audio_at[0] if first_audio_at[0] is not None else 0.0
        first_synth_s = max(0.0, first_s - rewrite_done_s) if first_s else 0.0
        log(f"🔉 first audio at {first_s:.1f}s "
            f"(rewrite {rewrite_done_s:.1f}s + first synth {first_synth_s:.1f}s)")
        log(f"✅ done in {total_s:.1f}s "
            f"(total synth {tts_total_ms[0] / 1000:.1f}s, play ~{max(0, total_s - first_s):.1f}s)")
