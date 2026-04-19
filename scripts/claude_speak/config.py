"""Config resolution: paths, defaults, key lookup."""
from __future__ import annotations

import json
import os
from pathlib import Path


CANONICAL_DIR_NAME = "claude-speak-thu-san"


def data_dir() -> Path:
    """Resolve the plugin's persistent data dir.

    Order of preference:
      1. CLAUDE_PLUGIN_DATA — set by Claude Code for hook subprocesses.
         Always wins. This is the only source of truth for "which marketplace
         is actually active in the current session."
      2. The most-recently-modified `~/.claude/plugins/data/claude-speak-*`
         directory. Terminal invocations don't get CLAUDE_PLUGIN_DATA, so we
         use recency as a proxy for "whichever install the user was just
         using." That tracks correctly when the user has multiple
         side-by-side installs (e.g. an @claude-speak-local dev install next
         to a stale @thu-san from an earlier experiment) — the one Claude
         Code just wrote to via the hook wins.
      3. Fall back to the canonical path for the published marketplace.
    """
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if d:
        return Path(d)
    base = Path.home() / ".claude" / "plugins" / "data"
    if base.is_dir():
        candidates = [
            e for e in base.iterdir()
            if e.is_dir() and e.name.startswith("claude-speak")
        ]
        if candidates:
            # Newest wins. Ties broken alphabetically for stability.
            candidates.sort(key=lambda p: (-p.stat().st_mtime, p.name))
            return candidates[0]
    return base / CANONICAL_DIR_NAME


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


