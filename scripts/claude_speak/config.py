"""Config resolution: paths, defaults, key lookup."""
from __future__ import annotations

import json
import os
from pathlib import Path


CANONICAL_DIR_NAME = "claude-speak-thu-san"
PLUGIN_NAME = "claude-speak"


def _enabled_marketplace() -> str | None:
    """Read Claude Code's settings files to find the marketplace that the
    currently-enabled claude-speak plugin came from.

    Plugin keys in `enabledPlugins` look like `claude-speak@<marketplace>`,
    e.g. `claude-speak@thu-san` (from GitHub) or
    `claude-speak@claude-speak-local` (a local dev install). The data dir
    name matches: `~/.claude/plugins/data/claude-speak-<marketplace>`.

    Search order (highest priority first):
      1. Project-scope: walk up from cwd looking for
         `.claude/settings.local.json` then `.claude/settings.json`.
      2. User-scope: `~/.claude/settings.local.json`, then
         `~/.claude/settings.json`.
    First enabled claude-speak@X wins. Returns the marketplace name or
    None if claude-speak isn't enabled in any settings file.
    """
    prefix = f"{PLUGIN_NAME}@"
    candidates = []
    cwd = Path.cwd().resolve()
    for d in [cwd] + list(cwd.parents):
        for name in ("settings.local.json", "settings.json"):
            candidates.append(d / ".claude" / name)
    home = Path.home() / ".claude"
    candidates.extend([home / "settings.local.json", home / "settings.json"])
    for p in candidates:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        enabled = data.get("enabledPlugins", {})
        for key, on in enabled.items():
            if on and isinstance(key, str) and key.startswith(prefix):
                return key[len(prefix):]
    return None


def data_dir() -> Path:
    """Resolve the plugin's persistent data dir.

    Order of preference:
      1. CLAUDE_PLUGIN_DATA — set by Claude Code for hook subprocesses.
         Always wins.
      2. Read ~/.claude/settings*.json for the currently-enabled
         claude-speak@<marketplace> and use the matching data dir. This
         makes terminal invocations follow whichever install the user has
         active — without depending on file mtimes or env vars.
      3. Fall back to the canonical dir for a fresh production install.
    """
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if d:
        return Path(d)
    base = Path.home() / ".claude" / "plugins" / "data"
    marketplace = _enabled_marketplace()
    if marketplace:
        candidate = base / f"{PLUGIN_NAME}-{marketplace}"
        if candidate.is_dir():
            return candidate
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


