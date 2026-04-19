"""Runtime config defaults. All-local: Kokoro TTS, whisper.cpp STT, claude -p rewrite."""

DEFAULTS = {
    "enabled": True,
    # ---- Kokoro (local TTS) ----
    "kokoro_voice": "af_sarah",
    "kokoro_speed": 1.0,
    "kokoro_lang": "en-us",
    "kokoro_model_url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "kokoro_voices_url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
    # ---- Rewrite (claude -p only) ----
    "claude_cli": "claude",
    "claude_model": "sonnet",
    "claude_rewrite_timeout": 60,
    # ---- STT (whisper.cpp, local) ----
    # small.en is ~500MB but materially more accurate than base.en on dev terms,
    # short commands, and soft speech. On Intel it transcribes a 5s clip in
    # ~4-6s; drop to ggml-base.en.bin if that's too slow.
    "whisper_cpp_model": "ggml-small.en.bin",
    "whisper_cpp_language": "en",
    "whisper_cpp_threads": 4,
    # ---- Recording ----
    "record_use_vad": True,               # Silero neural VAD (preferred)
    "record_vad_threshold": 0.5,          # 0-1; higher = more conservative (ignore soft speech)
    "record_use_sounddevice": True,       # fallback path if Silero unavailable
    "record_sample_rate": 16000,
    "record_block_ms": 50,
    "record_max_seconds": 60,
    "record_silence_seconds": 4.0,
    "record_silence_db": -40,
    "record_start_grace": 8.0,
    "record_mic_input": ":0",
    "record_beep": True,
    "record_mute_system": True,           # mute macOS output during recording so
                                           # background audio doesn't leak into the mic
    "cancel_phrases": ["cancel", "skip", "never mind", "nevermind", "stop"],
    # ---- Playback mode ----
    # "stream": synthesize sentence-by-sentence, play each as it's ready (low
    #           latency; tiny gaps between sentences)
    # "whole":  synthesize the entire rewritten response in one Kokoro call,
    #           then play it (slower first audio, smoother prosody)
    "mode": "stream",
    "playback_rate": 1.0,
    "max_chars": 1200,
    # ---- Conversational turn ----
    # When true, after speaking Claude's reply the hook records the user's
    # voice reply and feeds it back as Claude's next prompt (full voice loop).
    # When false, the hook only speaks — no listening afterward.
    # Back-compat: falls back to "auto_dictation" if present in old configs.
    "voice_loop": True,

    # ---- Logging ----
    "log_verbose": False,                 # if true, also log per-sentence/per-step detail

    # ---- Daemon ----
    # When true, the Stop hook talks to a long-running background daemon that
    # keeps Silero+Kokoro warm in memory (~2-3s per-turn cold-start savings).
    "daemon": True,

    # ---- Notifications ----
    # When true, Claude Code's Notification hook (permission prompts, MCP
    # elicitation dialogs, idle reminders, auth success) speaks the message
    # aloud so you can tell the UI is waiting for you without looking.
    # Message is spoken verbatim (no rewrite); speak-only (no voice reply).
    "speak_notifications": True,
    "speak_notification_types": {
        "permission_prompt": True,
        "elicitation_dialog": True,
        "idle_prompt": True,
        "auth_success": False,
    },
}
