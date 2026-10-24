#!/bin/sh
# Claude Code UserPromptSubmit hook: tells the tmux-voice daemon a new turn started in this pane,
# so it won't read the previous answer. Usage: claude-prompt.sh [port]
[ -n "$TMUX_PANE" ] || exit 0
curl -s -m 2 -X POST -H 'Content-Type: application/json' --data-binary @- \
  "http://127.0.0.1:${1:-8766}/turn?pane=$(printf %s "$TMUX_PANE" | sed 's/%/%25/')" >/dev/null 2>&1
exit 0
