---
description: Control the claude-speak plugin
argument-hint: "on | off | stop | voice <name> | voices | rate <0.25-4.0> | silence <sec> | mode stream|whole | dictate on|off | mute on|off | model <name> | whisper-model <name> | verbose on|off | install [--force] | uninstall --force [--wipe-logs] | progress [--follow] | status"
---

!CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA} python3 ${CLAUDE_PLUGIN_ROOT}/scripts/speak_config.py $ARGUMENTS

Print the stdout from the command above verbatim inside a fenced code block. No commentary, no summary, no extra lines.
