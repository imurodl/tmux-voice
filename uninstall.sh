#!/usr/bin/env bash
# Removes everything install.sh added. --purge also deletes config, keys and history.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

strip_block() { [ -f "$1" ] && sed -i '' '/tmux-voice begin/,/tmux-voice end/d' "$1" && echo "cleaned $1" || true; }

for f in "$HOME/.hammerspoon/init.lua" "$HOME/.config/tmux/tmux.conf" "$HOME/.tmux.conf" \
         "$HOME/.claude/CLAUDE.md" "$HOME/.codex/AGENTS.md"; do
  strip_block "$f"
done

if [ -f "$HOME/.claude/settings.json" ]; then
  (cd "$ROOT" && ROOT="$ROOT" uv run --quiet python -c '
import json, os
p = os.path.expanduser("~/.claude/settings.json")
s = json.load(open(p))
for event in ("Stop", "UserPromptSubmit"):
    groups = s.get("hooks", {}).get(event, [])
    for group in groups:
        group["hooks"] = [h for h in group.get("hooks", []) if not h.get("command", "").startswith(os.environ["ROOT"] + "/hooks/")]
    groups[:] = [g for g in groups if g["hooks"]]
json.dump(s, open(p, "w"), indent=2)
open(p, "a").write("\n")')
  echo "removed Claude Code hooks"
fi

PORT=$(sed -n 's/^port *= *\([0-9]*\).*/\1/p' "$HOME/.config/tmux-voice/config.toml" 2>/dev/null || true)
lsof -ti tcp:"${PORT:-8766}" | xargs kill 2>/dev/null || true
tmux set -gu @voice 2>/dev/null || true

if [ "$PURGE" = 1 ]; then
  rm -rf "$HOME/.config/tmux-voice" "$HOME/.local/state/tmux-voice"
  echo "deleted config, keys and history"
fi
echo "Done. Reload Hammerspoon (menu bar > Reload Config) and restart tmux to clear the status line."
