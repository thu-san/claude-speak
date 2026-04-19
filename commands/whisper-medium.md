---
description: Switch to whisper.cpp medium.en quantized (~470MB, ~2x slower than small, better accuracy)
---

!python3 ${CLAUDE_PLUGIN_ROOT}/scripts/speak_config.py whisper-model ggml-medium.en-q5_0.bin

Print the stdout from the command above verbatim inside a fenced code block. No commentary, no summary, no extra lines.
