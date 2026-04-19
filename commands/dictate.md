---
description: Auto-record after each response and feed transcript back as the next prompt
argument-hint: "<on | off>"
---

!CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA} python3 ${CLAUDE_PLUGIN_ROOT}/scripts/speak_config.py dictate $ARGUMENTS

Print the stdout from the command above verbatim inside a fenced code block. No commentary, no summary, no extra lines.
