"""Text-to-speech. Currently only one local backend: Kokoro."""
from __future__ import annotations


def synthesize(text: str, cfg: dict) -> tuple[bytes, str]:
    """Return (audio_bytes, extension) for the given text using the configured TTS backend.

    Always routes to Kokoro today; kept as a function so adding new backends later is easy.
    """
    from .kokoro import synthesize as _synth
    return _synth(text, cfg)
