---
description: Wipe venv, models, config, markers (does NOT remove the plugin itself)
argument-hint: "--force [--wipe-logs]"
---

!CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA} python3 ${CLAUDE_PLUGIN_ROOT}/scripts/speak_config.py uninstall $ARGUMENTS

Print the stdout from the command above verbatim inside a fenced code block. No commentary, no summary, no extra lines.
