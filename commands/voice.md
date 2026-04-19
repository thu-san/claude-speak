---
description: Set Kokoro TTS voice (run /claude-speak:voices to list available)
argument-hint: "<af_sarah | af_nova | am_onyx | bf_emma | bm_george | ...>"
---

!python3 ${CLAUDE_PLUGIN_ROOT}/scripts/speak_config.py voice $ARGUMENTS

Print the stdout from the command above verbatim inside a fenced code block. No commentary, no summary, no extra lines.
