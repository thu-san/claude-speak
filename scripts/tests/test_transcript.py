from claude_speak.transcript import (
    build_rewrite_input,
    split_sentence_stream,
)


def test_build_rewrite_input_with_context():
    out = build_rewrite_input("What time is it?", "It is three PM.", max_chars=100)
    assert "USER prompt" in out
    assert "ASSISTANT reply" in out
    assert "It is three PM." in out
    assert "What time is it?" in out


def test_build_rewrite_input_no_user_text():
    # No user context -> just pass the assistant reply through.
    out = build_rewrite_input("", "Done.", max_chars=100)
    assert out == "Done."


def test_build_rewrite_input_truncates_long_assistant_reply():
    long_reply = "x" * 10000
    out = build_rewrite_input("hi", long_reply, max_chars=100)
    # assistant portion capped at max_chars * 4 = 400
    assistant_section = out.split("ASSISTANT reply (summarize this):\n", 1)[1]
    assert len(assistant_section) == 400


def test_split_sentence_stream_basic():
    tokens = iter(["Hello world. How are you? I am fine! "])
    assert list(split_sentence_stream(tokens)) == [
        "Hello world.", "How are you?", "I am fine!"
    ]


def test_split_sentence_stream_preserves_fragment_at_stream_end():
    # No trailing punctuation — should still yield the remainder.
    tokens = iter(["First. ", "Second. Tail without period"])
    assert list(split_sentence_stream(tokens)) == [
        "First.", "Second.", "Tail without period"
    ]


def test_split_sentence_stream_does_not_break_decimals():
    # '0.' at the end of a chunk must wait for the next chunk; otherwise "0.25"
    # would be split as "0." + "25".
    tokens = iter(["The range is from 0.", "25 to 4.", "0. Done."])
    result = list(split_sentence_stream(tokens))
    assert result == ["The range is from 0.25 to 4.0.", "Done."]


def test_split_sentence_stream_handles_multiple_sentences_per_chunk():
    tokens = iter(["One. Two. Three."])
    assert list(split_sentence_stream(tokens)) == ["One.", "Two.", "Three."]
