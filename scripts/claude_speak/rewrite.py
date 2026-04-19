"""Rewrite the assistant response into natural spoken English via the claude CLI.

This is the only rewrite provider — previously OpenAI/Gemini were supported but
were removed in favor of a local-only (claude subscription) default.
"""
from __future__ import annotations

import os
import subprocess
from typing import Iterator

from .logging import log
from .transcript import REWRITE_SYSTEM


def rewrite(text: str, cfg: dict) -> str:
    """Synchronously invoke `claude -p` and return the rewritten text.

    The CLAUDE_SPEAK=0 env var is set so the nested session's Stop hook
    bails out instead of recursing into another rewrite.
    """
    prompt = (
        f"{REWRITE_SYSTEM}\n\n"
        "Rewrite for spoken delivery. Output only the rewritten text, no preamble, no quotes.\n\n"
        "---\n"
        f"{text}"
    )
    cli = cfg.get("claude_cli", "claude")
    timeout = int(cfg.get("claude_rewrite_timeout", 60))
    model = cfg.get("claude_model")
    env = {**os.environ, "CLAUDE_SPEAK": "0"}
    args = [cli, "-p", prompt]
    if model:
        args += ["--model", model]
    log(f"rewrite → cli={cli} model={model} prompt_chars={len(prompt)} timeout={timeout}s")
    # Log a preview of the END of the prompt — that's where the actual
    # assistant text lives; the head is our (constant, boring) system prompt.
    log(f"rewrite in tail: ...{text[-400:]!r}")
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        # Surface whatever partial stderr was captured before the timeout —
        # that usually names the real cause (auth prompt, MCP server hung,
        # rate limit, etc.).
        partial_stderr = (e.stderr or b"")
        if isinstance(partial_stderr, bytes):
            partial_stderr = partial_stderr.decode("utf-8", errors="replace")
        log(f"claude rewrite timed out after {timeout}s stderr={partial_stderr.strip()[:400]!r}")
        return ""
    if result.returncode != 0:
        log(f"claude rewrite exit={result.returncode} stderr={result.stderr.strip()[:400]!r}")
        return ""
    out = result.stdout.strip()
    log(f"rewrite out: {len(out)}c head={out[:200]!r}")
    return out


def rewrite_stream(text: str, cfg: dict) -> Iterator[str]:
    """Buffered rewrite exposed as a single-chunk iterator so the sentence
    splitter upstream works unchanged. Claude CLI has no practical streaming
    mode for this use case."""
    full = rewrite(text, cfg)
    if full:
        yield full


if __name__ == "__main__":
    from .venv import ensure_venv_python
    ensure_venv_python()

    import argparse
    import sys
    import time

    from .config import load_config
    from .defaults import DEFAULTS
    from .logging import enable_stderr_tee, log, section
    enable_stderr_tee()

    ap = argparse.ArgumentParser(
        description="Rewrite assistant text as spoken English via `claude -p`. "
        "Reads from --text, --file, or stdin; prints the rewritten text to stdout.",
    )
    ap.add_argument("--text", help="inline text to rewrite")
    ap.add_argument("--file", help="read text from a file instead of --text")
    ap.add_argument("--model", help="override claude_model (e.g. sonnet, opus)")
    ap.add_argument("--timeout", type=int, help="override rewrite timeout in seconds")
    args = ap.parse_args()

    if args.text and args.file:
        ap.error("--text and --file are mutually exclusive")
    if args.file:
        input_text = open(args.file, encoding="utf-8").read()
    elif args.text:
        input_text = args.text
    elif not sys.stdin.isatty():
        input_text = sys.stdin.read()
    else:
        ap.error("provide --text, --file, or pipe text on stdin")

    cfg = load_config(dict(DEFAULTS))
    if args.model:
        cfg["claude_model"] = args.model
    if args.timeout:
        cfg["claude_rewrite_timeout"] = args.timeout

    section("✍️  REWRITE (cli)")
    log(f"model={cfg.get('claude_model')} timeout={cfg.get('claude_rewrite_timeout')}s "
        f"input={len(input_text)}c")
    t = time.monotonic()
    out = rewrite(input_text.strip(), cfg)
    dt = time.monotonic() - t
    log(f"✅ rewrite done in {dt:.1f}s → {len(out)}c")
    log(f"   speech: {out[:400]!r}")
    print(out)  # rewrite text on stdout for piping
