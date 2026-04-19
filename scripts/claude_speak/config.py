"""Config resolution: paths, defaults, key lookup."""
from __future__ import annotations

import json
import os
from pathlib import Path


PLUGIN_NAME = "claude-speak"


def data_dir() -> Path:
    """Resolve the plugin's persistent data dir from CLAUDE_PLUGIN_DATA.

    Claude Code sets this env var for every context where the plugin
    actually runs:
      * Hooks (Stop, Notification, SessionStart) — set directly.
      * Slash commands — our command templates prepend
        `CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA}` before invoking python,
        which Claude Code substitutes at template time so the env var
        reaches the subprocess.
      * Raw terminal — set it yourself: `export CLAUDE_PLUGIN_DATA=~/.claude/plugins/data/claude-speak-<marketplace>`.

    No canonical fallback — guessing the marketplace name when the env var
    is missing silently lands data in the wrong install (burn-scar from
    earlier). Better to raise and make the caller fix their invocation."""
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if d:
        return Path(d)
    raise RuntimeError(
        "CLAUDE_PLUGIN_DATA is not set.\n"
        "  * Hooks + slash commands: Claude Code sets this automatically; "
        "if you see this error from one of those, reinstall the plugin and "
        "file a bug.\n"
        "  * Terminal: export CLAUDE_PLUGIN_DATA=~/.claude/plugins/data/"
        "claude-speak-<marketplace> before running the command."
    )


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


