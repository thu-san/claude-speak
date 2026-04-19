"""Whisper sometimes returns bracketed silence/noise tags for non-speech audio.
These must not be fed to Claude as a real prompt."""
from claude_speak.stt.whisper_cpp import _strip_noise_markers


def test_blank_audio_alone_strips_to_empty():
    assert _strip_noise_markers("[BLANK_AUDIO]") == ""
    assert _strip_noise_markers("[blank_audio]") == ""
    assert _strip_noise_markers("  [BLANK_AUDIO]  ") == ""


def test_silence_marker_strips_to_empty():
    assert _strip_noise_markers("[SILENCE]") == ""
    assert _strip_noise_markers("(silence)") == ""


def test_music_marker_strips_to_empty():
    assert _strip_noise_markers("[Music]") == ""
    assert _strip_noise_markers("[MUSIC]") == ""


def test_real_speech_passes_through():
    assert _strip_noise_markers("Hello world.") == "Hello world."
    assert _strip_noise_markers("Let's commit this.") == "Let's commit this."


def test_speech_with_trailing_marker_keeps_speech():
    # Whisper occasionally appends a marker at the end of a real utterance.
    assert _strip_noise_markers("Yes, exactly. [BLANK_AUDIO]") == "Yes, exactly."


def test_empty_input_stays_empty():
    assert _strip_noise_markers("") == ""
    assert _strip_noise_markers("   ") == ""
