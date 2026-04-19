---
description: Pre-download all models and pip install plugin deps into the venv
argument-hint: "[--force]  (clears the marker and reinstalls)"
---

!CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA} python3 ${CLAUDE_PLUGIN_ROOT}/scripts/speak_config.py install $ARGUMENTS

Print the stdout from the command above verbatim inside a fenced code block. No commentary, no summary, no extra lines.
