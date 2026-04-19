---
description: Claude model used for the rewrite step (passed to `claude -p --model ...`)
argument-hint: "<sonnet | opus | haiku | claude-sonnet-4-6 | ...>"
---

!python3 ${CLAUDE_PLUGIN_ROOT}/scripts/speak_config.py model $ARGUMENTS

Print the stdout from the command above verbatim inside a fenced code block. No commentary, no summary, no extra lines.
