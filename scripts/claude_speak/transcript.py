"""Transcript reading, rewrite-prompt assembly, sentence splitting."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

REWRITE_SYSTEM = (
    "You rewrite an assistant response into natural spoken English for a senior developer "
    "listening hands-free. You will receive the USER's most recent prompt for context, then "
    "the ASSISTANT's reply. Your job is to speak ONLY a summary of the ASSISTANT's reply — "
    "never narrate the user's question back. Rules: speak like a peer giving a quick update; "
    "skip code blocks, file paths, and markdown syntax; when the reply references something "
    "from the user's prompt, use the context to make the summary self-contained ('I updated "
    "the retry logic' instead of 'I updated it'); keep it under 40 seconds of speech (roughly "
    "100 words); no preamble like 'sure' or 'here is'."
)


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


def read_last_turn(transcript_path: str) -> tuple[str, str]:
    """Return (last_user_text, last_assistant_text) from a Claude Code transcript JSONL."""
    if not transcript_path or not Path(transcript_path).exists():
        return "", ""
    with open(transcript_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assistant_text = ""
    user_text = ""
    saw_assistant = False
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message") or entry
        role = msg.get("role") or entry.get("type")
        text = _extract_text(msg.get("content"))
        if not text:
            continue
        if role == "assistant" and not saw_assistant:
            assistant_text = text
            saw_assistant = True
        elif role == "user" and saw_assistant:
            user_text = text
            break
    return user_text.strip(), assistant_text.strip()


def build_rewrite_input(user_text: str, assistant_text: str, max_chars: int) -> str:
    """Package the user's last prompt + the assistant's reply for the rewrite model."""
    user_trimmed = user_text[:1000]
    assistant_trimmed = assistant_text[: max_chars * 4]
    if not user_trimmed:
        return assistant_trimmed
    return (
        f"USER prompt (for context only, do not narrate):\n{user_trimmed}\n\n"
        f"ASSISTANT reply (summarize this):\n{assistant_trimmed}"
    )


SENTENCE_RE = re.compile(r"[.!?](?=\s)")


def split_sentence_stream(tokens: Iterator[str]) -> Iterator[str]:
    """Yield complete sentences as tokens arrive. Trailing fragment emitted at stream end."""
    buf = ""
    for chunk in tokens:
        buf += chunk
        while True:
            m = SENTENCE_RE.search(buf)
            if not m:
                break
            end = m.end()
            sentence = buf[:end].strip()
            buf = buf[end:].lstrip()
            if sentence:
                yield sentence
    tail = buf.strip()
    if tail:
        yield tail
