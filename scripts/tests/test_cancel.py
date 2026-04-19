from claude_speak.stt.cancel import is_cancel


CFG = {"cancel_phrases": ["cancel", "skip", "never mind", "nevermind", "stop"]}


def test_empty_is_cancel():
    assert is_cancel("", CFG) is True
    assert is_cancel("   ", CFG) is True


def test_bare_phrase():
    assert is_cancel("cancel", CFG) is True
    assert is_cancel("Cancel.", CFG) is True
    assert is_cancel("Never mind.", CFG) is True


def test_phrase_at_end_of_sentence():
    text = ("Actually recording? Can you hear me? "
            "Stop recording and you can continue. Cancel. Cancel. Cancel.")
    assert is_cancel(text, CFG) is True


def test_phrase_mid_sentence_is_not_cancel():
    assert is_cancel("Cancel this please", CFG) is False
    assert is_cancel("Please cancel my subscription tomorrow", CFG) is False


def test_last_word_is_phrase():
    assert is_cancel("I said cancel", CFG) is True


def test_unrelated_text():
    assert is_cancel("Write me a short poem about dogs.", CFG) is False
