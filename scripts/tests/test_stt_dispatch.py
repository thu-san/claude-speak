"""stt.dictate() routes to the daemon when enabled, in-process otherwise."""
from claude_speak import stt as stt_pkg


def test_dictate_in_process_calls_whisper(monkeypatch):
    """With use_daemon=False, dictate() runs the in-process pipeline:
    record_mic → transcribe."""
    from claude_speak import recording
    from claude_speak.stt import whisper_cpp
    monkeypatch.setattr(recording, "record_mic", lambda cfg: "/tmp/fake.wav")
    monkeypatch.setattr(whisper_cpp, "transcribe", lambda wav, cfg: "ok")
    assert stt_pkg.dictate({"daemon": False}) == "ok"


def test_dictate_routes_to_daemon_when_enabled(monkeypatch):
    """With cfg.daemon=True (the default) and a reachable daemon, dictate()
    sends a 'dictate' op via send_request and returns its text."""
    from claude_speak import daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "ensure_daemon", lambda: True)
    captured = {}
    def fake_send(req, timeout=600.0):
        captured["req"] = req
        return {"ok": True, "text": "from-daemon"}
    monkeypatch.setattr(daemon_mod, "send_request", fake_send)

    cfg = {"daemon": True, "whisper_cpp_model": "ggml-tiny.en.bin"}
    assert stt_pkg.dictate(cfg) == "from-daemon"
    assert captured["req"]["op"] == "dictate"
    assert captured["req"]["overrides"]["whisper_cpp_model"] == "ggml-tiny.en.bin"


def test_dictate_falls_back_when_daemon_unavailable(monkeypatch):
    """If the daemon can't be started, dictate() falls back to in-process."""
    from claude_speak import daemon as daemon_mod, recording
    from claude_speak.stt import whisper_cpp
    monkeypatch.setattr(daemon_mod, "ensure_daemon", lambda: False)
    monkeypatch.setattr(recording, "record_mic", lambda cfg: "/tmp/fake.wav")
    monkeypatch.setattr(whisper_cpp, "transcribe", lambda wav, cfg: "fallback")
    assert stt_pkg.dictate({"daemon": True}) == "fallback"


def test_dictate_use_daemon_explicit_false(monkeypatch):
    """use_daemon=False overrides cfg.daemon=True (used by main.py to avoid
    recursing into the daemon while we're already serving a speak op)."""
    from claude_speak import daemon as daemon_mod, recording
    from claude_speak.stt import whisper_cpp
    def boom(*a, **kw):
        raise AssertionError("daemon path must not be taken")
    monkeypatch.setattr(daemon_mod, "ensure_daemon", boom)
    monkeypatch.setattr(recording, "record_mic", lambda cfg: "/tmp/fake.wav")
    monkeypatch.setattr(whisper_cpp, "transcribe", lambda wav, cfg: "in-proc")
    assert stt_pkg.dictate({"daemon": True}, use_daemon=False) == "in-proc"
