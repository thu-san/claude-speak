from claude_speak.audio import atempo_chain, pcm_to_wav, playback_rate


def test_atempo_chain_identity():
    # At 1.0× we still emit atempo=1.0 string; callers skip the -af flag, but
    # the chain itself should be well-formed.
    assert atempo_chain(1.0) == "atempo=1.0000"


def test_atempo_chain_simple_range():
    assert atempo_chain(1.5) == "atempo=1.5000"
    assert atempo_chain(0.8) == "atempo=0.8000"


def test_atempo_chain_chains_for_extremes():
    # 4.0 decomposes as atempo=2.0,atempo=2.0
    assert atempo_chain(4.0) == "atempo=2.0000,atempo=2.0000"
    # 0.25 decomposes as atempo=0.5,atempo=0.5
    assert atempo_chain(0.25) == "atempo=0.5000,atempo=0.5000"


def test_atempo_chain_chains_for_midpoint_extremes():
    # 3.0 needs one atempo=2.0 step + 1.5
    chain = atempo_chain(3.0)
    assert chain == "atempo=2.0000,atempo=1.5000"


def test_playback_rate_clamps_to_range():
    assert playback_rate({"playback_rate": 0.1}) == 0.25
    assert playback_rate({"playback_rate": 100}) == 4.0
    assert playback_rate({"playback_rate": 1.5}) == 1.5


def test_playback_rate_default_one():
    assert playback_rate({}) == 1.0
    assert playback_rate({"playback_rate": "not-a-number"}) == 1.0


def test_pcm_to_wav_roundtrip():
    # A tiny synthetic PCM buffer. We just care the WAV header reports the
    # right byte counts and the data section matches.
    pcm = b"\x01\x00\x02\x00\x03\x00"  # 3 samples of 16-bit mono
    wav = pcm_to_wav(pcm, sample_rate=16000, channels=1, bits=16)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    # WAV ends with the raw PCM bytes.
    assert wav.endswith(pcm)
    # Total length = 44 bytes header + data
    assert len(wav) == 44 + len(pcm)
