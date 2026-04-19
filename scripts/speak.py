#!/usr/bin/env python3
"""Stop-hook shim. Tries the long-running daemon first; falls back to
in-process execution if the daemon isn't available or is disabled."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from claude_speak.venv import ensure_venv_python  # noqa: E402
ensure_venv_python()

from claude_speak.config import load_config  # noqa: E402
from claude_speak.defaults import DEFAULTS  # noqa: E402


def _try_daemon(payload: dict) -> bool:
    """Send the speak request to the daemon. Return True if it handled it."""
    from claude_speak.daemon import ensure_daemon, send_request
    if not ensure_daemon():
        return False
    resp = send_request({"op": "speak", "transcript_path": payload.get("transcript_path", "")})
    if not resp or not resp.get("ok"):
        return False
    out = resp.get("stdout") or ""
    if out:
        sys.stdout.write(out)
        if not out.endswith("\n"):
            sys.stdout.write("\n")
    return True


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {}

    cfg = load_config(DEFAULTS)
    if cfg.get("daemon", True):
        try:
            if _try_daemon(payload):
                return 0
        except Exception:
            pass  # fall through to in-process

    # Fallback: in-process. Restore stdin for main().
    import io
    sys.stdin = io.StringIO(raw)
    from claude_speak.main import main as _main
    return _main()


if __name__ == "__main__":
    sys.exit(main())
