"""Config resolution: paths, defaults, key lookup."""
from __future__ import annotations

import json
import os
from pathlib import Path


def data_dir() -> Path:
    """Resolve the plugin's persistent data dir.

    Order of preference:
      1. CLAUDE_PLUGIN_DATA — set by Claude Code for hook subprocesses.
      2. Any existing ~/.claude/plugins/data/claude-speak-* directory (matches
         whatever marketplace the plugin was installed from).
      3. Pin to the canonical path for the published marketplace so terminal
         invocations land in the SAME place the hook will use.
    """
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if d:
        return Path(d)
    base = Path.home() / ".claude" / "plugins" / "data"
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if entry.is_dir() and entry.name.startswith("claude-speak"):
                return entry
    # Canonical path used by `/plugin install claude-speak@thu-san`.
    return base / "claude-speak-thu-san"


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


