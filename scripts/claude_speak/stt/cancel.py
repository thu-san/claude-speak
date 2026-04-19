"""Cancel-phrase detection. Pure logic, trivially unit-testable."""
from __future__ import annotations


def is_cancel(text: str, cfg: dict) -> bool:
    """True if the transcript signals the user wants to skip this turn.

    Empty transcript = cancel. Otherwise we look for cancel phrases at the
    tail of the utterance: exact match, as the last word, or the entire trailing
    clause. So "... Cancel. Cancel. Cancel." counts as cancel, but
    "cancel this please" does not.
    """
    cleaned = text.strip().lower()
    if not cleaned:
        return True
    tail = cleaned
    while tail and tail[-1] in " .!?,":
        tail = tail[:-1]
    phrases = [p.lower() for p in cfg.get("cancel_phrases", [])]
    for phrase in phrases:
        if tail == phrase or tail.endswith(" " + phrase) or tail.endswith("." + phrase):
            return True
        last_word = tail.rsplit(None, 1)[-1] if tail else ""
        if last_word == phrase:
            return True
    return False
