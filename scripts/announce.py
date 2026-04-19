#!/usr/bin/env python3
"""Notification-hook shim. Speaks Claude Code's Notification messages
(permission prompts, elicitation dialogs, idle reminders, auth-success)
aloud by handing them to the existing daemon's `turn` op with
rewrite=False + voice_loop=False. Falls back to in-process playback when
the daemon is unreachable, same pattern as speak.py."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from claude_speak.venv import ensure_venv_python  # noqa: E402
ensure_venv_python()

from claude_speak.config import load_config  # noqa: E402
from claude_speak.defaults import DEFAULTS  # noqa: E402
from claude_speak.logging import log, log_v  # noqa: E402


def _should_speak(payload: dict, cfg: dict) -> tuple[bool, str]:
    """Return (speak, reason). reason is only set when we're NOT speaking."""
    if os.environ.get("CLAUDE_SPEAK") == "0":
        return False, "CLAUDE_SPEAK=0 in env"
    if not cfg.get("enabled", True):
        return False, "plugin disabled"
    if not cfg.get("speak_notifications", True):
        return False, "speak_notifications=false"
    ntype = payload.get("notification_type") or ""
    type_map = cfg.get("speak_notification_types") or {}
    # Per-type toggle; unknown types default to True (speak).
    if ntype and not type_map.get(ntype, True):
        return False, f"type '{ntype}' disabled"
    return True, ""


def _extract_message(payload: dict) -> str:
    """Pull the spoken text out of the notification payload.

    Docs list `message` + `title`. We prefer `message`; fall back to `title`
    so we don't go silent on an odd payload shape. Short title prefix for
    the common 'permission needed' case so the listener has context."""
    title = (payload.get("title") or "").strip()
    message = (payload.get("message") or "").strip()
    if message and title and title.lower() not in message.lower():
        return f"{title}. {message}"
    return message or title


def _try_daemon(text: str) -> bool:
    """Send to the warm daemon's `turn` op. Returns True on success."""
    from claude_speak.daemon import ensure_daemon, send_request
    if not ensure_daemon():
        return False
    resp = send_request({
        "op": "turn",
        "text": text,
        "rewrite": False,
        "voice_loop": False,
    })
    return bool(resp and resp.get("ok"))


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {}

    cfg = load_config(DEFAULTS)
    ok, reason = _should_speak(payload, cfg)
    if not ok:
        log_v(f"notification suppressed ({reason})")
        return 0

    text = _extract_message(payload)
    if not text:
        log_v("notification had no message text — ignoring")
        return 0

    ntype = payload.get("notification_type") or "?"
    log(f"🔔 notification ({ntype}): {text[:200]!r}")

    if cfg.get("daemon", True):
        try:
            if _try_daemon(text):
                return 0
        except Exception as e:
            log(f"daemon announce error: {type(e).__name__}: {e}")

    # Fallback: speak in-process. Short messages → Kokoro cold-start
    # dominates (~4s) but still completes.
    from claude_speak.main import run_turn
    run_turn({"text": text, "rewrite": False, "voice_loop": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
