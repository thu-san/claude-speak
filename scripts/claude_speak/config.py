"""Config resolution: paths, defaults, key lookup."""
from __future__ import annotations

import json
import os
from pathlib import Path


CANONICAL_DIR_NAME = "claude-speak-thu-san"
PLUGIN_NAME = "claude-speak"


def data_dir() -> Path:
    """Resolve the plugin's persistent data dir.

    Order of preference:
      1. CLAUDE_PLUGIN_DATA — env var set by Claude Code for hook
         subprocesses. Always wins. Works for 99% of users: both production
         (single @thu-san install) and dev (single @claude-speak-local
         install) flow through the Stop / SessionStart / Notification hooks,
         and Claude Code sets this env var to the right path every time.
      2. Fall back to the canonical dir `claude-speak-thu-san` (the path
         users of the published plugin get on a clean install).

    Users running terminal commands against a NON-canonical install (e.g.
    a dev machine with @claude-speak-local alongside production @thu-san)
    should export CLAUDE_PLUGIN_DATA in their shell rc pointing at the
    dev dir. See README "Developing against a local marketplace" for
    details.
    """
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if d:
        return Path(d)
    return Path.home() / ".claude" / "plugins" / "data" / CANONICAL_DIR_NAME


DATA_DIR = data_dir()
CONFIG_PATH = DATA_DIR / "config.json"


def load_config(defaults: dict) -> dict:
    """Merge persisted config over the given defaults."""
    cfg = dict(defaults)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    return cfg


