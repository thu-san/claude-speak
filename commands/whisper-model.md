---
description: Switch + download a whisper.cpp STT model
argument-hint: "<ggml-small.en.bin | ggml-medium.en-q5_0.bin | ggml-large-v3-turbo-q5_0.bin>"
---

!CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA} python3 ${CLAUDE_PLUGIN_ROOT}/scripts/speak_config.py whisper-model $ARGUMENTS

Print the stdout from the command above verbatim inside a fenced code block. No commentary, no summary, no extra lines.
