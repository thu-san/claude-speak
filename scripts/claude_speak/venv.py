"""Plugin-private virtualenv management.

Keeps torch / onnxruntime / numpy / kokoro-onnx / silero-vad / sounddevice
isolated from the user's system Python. The canonical location is
$CLAUDE_PLUGIN_DATA/.venv.

Usage patterns:
  - Hook entrypoints (scripts/speak.py) call ensure_venv_python() at the top;
    if the venv exists but we're running from system Python, it re-execs.
  - install.py creates the venv itself (running from system Python) and then
    installs packages via VENV_PIP.
  - Module __main__ blocks (kokoro, recording, whisper_cpp, …) call
    ensure_venv_python() before touching ONNX / torch / sounddevice.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import DATA_DIR

VENV_DIR = DATA_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python3"
VENV_PIP = VENV_DIR / "bin" / "pip"


def venv_ready() -> bool:
    return VENV_PYTHON.exists()


def running_in_venv() -> bool:
    """True if the current interpreter is the plugin venv's python."""
    try:
        return Path(sys.prefix).resolve() == VENV_DIR.resolve()
    except OSError:
        return False


def ensure_venv_python() -> None:
    """If the venv exists and we're not in it, re-exec ourselves via venv python.

    Does nothing when the venv hasn't been created yet — install.py is the
    bootstrap, and it must run from whatever interpreter invoked it.

    Detects whether the parent invocation was `python -m foo.bar` (preserves
    the -m form, otherwise relative imports break) vs `python script.py`.
    """
    if not venv_ready():
        return
    if running_in_venv():
        return

    main_mod = sys.modules.get("__main__")
    main_spec = getattr(main_mod, "__spec__", None) if main_mod else None
    if main_spec is not None and getattr(main_spec, "name", None) and main_spec.name != "__main__":
        # Was launched as `python -m <module.name>`; preserve that.
        os.execv(
            str(VENV_PYTHON),
            [str(VENV_PYTHON), "-m", main_spec.name, *sys.argv[1:]],
        )
    else:
        # Was launched as `python <script.py> ...`.
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])
