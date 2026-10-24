#!/bin/sh
# Claude Code Stop hook: asks the tmux-voice daemon to read a short summary aloud
# when this pane's turn was started by voice. Usage: claude-stop.sh [port]
[ -n "$TMUX_PANE" ] || exit 0
curl -s -m 2 -X POST -H 'Content-Type: application/json' --data-binary @- \
  "http://127.0.0.1:${1:-8766}/speak?pane=$(printf %s "$TMUX_PANE" | sed 's/%/%25/')" >/dev/null 2>&1
exit 0
