#!/usr/bin/env bash
# tmux-voice installer for macOS. Safe to re-run: every edit sits between "tmux-voice begin/end"
# markers and each changed file is backed up first.
#
#   ./install.sh [--yes] [--port 8766] [--key rightalt|rightcmd|rightctrl|rightshift|fn]
#                [--no-tmux] [--no-claude-hook] [--no-instructions] [--no-start]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="${TMUX_VOICE_CONFIG:-$HOME/.config/tmux-voice}"
PORT=8766 KEY=rightalt YES=0 DO_TMUX=1 DO_HOOK=1 DO_INSTR=1 DO_START=1

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --port) PORT="$2"; shift ;;
    --key) KEY="$2"; shift ;;
    --no-tmux) DO_TMUX=0 ;;
    --no-claude-hook) DO_HOOK=0 ;;
    --no-instructions) DO_INSTR=0 ;;
    --no-start) DO_START=0 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ask() { [ "$YES" = 1 ] && return 0; read -r -p "$1 [Y/n] " a; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }
backup() { [ -f "$1" ] && cp "$1" "$1.bak.tmux-voice.$(date +%Y%m%d%H%M%S)" || true; }
strip_block() { [ -f "$1" ] && sed -i '' '/tmux-voice begin/,/tmux-voice end/d' "$1" || true; }

[ "$(uname)" = Darwin ] || { echo "tmux-voice currently supports macOS only." >&2; exit 1; }

step "Checking dependencies"
missing=()
command -v tmux >/dev/null || missing+=(tmux)
command -v uv >/dev/null || missing+=(uv)
command -v ffmpeg >/dev/null || missing+=(ffmpeg)
if [ ${#missing[@]} -gt 0 ]; then
  command -v brew >/dev/null || { echo "Missing ${missing[*]} and Homebrew is not installed: https://brew.sh" >&2; exit 1; }
  ask "Install ${missing[*]} with Homebrew?" && brew install "${missing[@]}"
fi
if ! open -Ra Hammerspoon 2>/dev/null; then
  ask "Install Hammerspoon (global hotkey) with Homebrew?" && brew install --cask hammerspoon
fi

step "Installing Python dependencies"
(cd "$ROOT" && uv sync --quiet)

step "Voice for reading answers aloud"
if [ "$(uname -m)" = arm64 ] && ask "Install the local Kokoro voice for reading answers aloud (about 1 GB, runs offline)? Otherwise the macOS voice is used."; then
  command -v brew >/dev/null && { brew list espeak-ng >/dev/null 2>&1 || brew install espeak-ng; }
  (cd "$ROOT" && uv sync --quiet --extra kokoro)
  ENGINE=kokoro
else
  ENGINE=say
fi

step "Config in $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
  sed -e "s/^port = 8766/port = $PORT/" -e "s/^engine = \"kokoro\"/engine = \"$ENGINE\"/" "$ROOT/config.example.toml" > "$CONFIG_DIR/config.toml"
  echo "created config.toml"
fi
if [ ! -f "$CONFIG_DIR/env" ]; then
  cp "$ROOT/env.example" "$CONFIG_DIR/env"
  chmod 600 "$CONFIG_DIR/env"
  echo "created env"
fi
set_key() {  # set_key NAME VALUE
  (cd "$ROOT" && KEY_NAME="$1" KEY_VALUE="$2" ENV_FILE="$CONFIG_DIR/env" uv run --quiet python -c '
import os, re
p, k, v = os.environ["ENV_FILE"], os.environ["KEY_NAME"], os.environ["KEY_VALUE"]
s = open(p).read()
s = re.sub(rf"^{k}=.*$", lambda _: f"{k}={v}", s, flags=re.M) if re.search(rf"^{k}=", s, re.M) else s + f"\n{k}={v}\n"
open(p, "w").write(s)')
}
if [ "$YES" = 0 ]; then
  for name in ELEVENLABS_API_KEY GEMINI_API_KEY; do
    if ! grep -qE "^$name=.+" "$CONFIG_DIR/env"; then
      read -r -s -p "$name (Enter to skip): " value; echo
      [ -n "$value" ] && set_key "$name" "$value"
    fi
  done
fi
grep -qE '^(ELEVENLABS_API_KEY|GEMINI_API_KEY|GOOGLE_CLOUD_PROJECT)=.+' "$CONFIG_DIR/env" ||
  echo "No API key yet: add one to $CONFIG_DIR/env before using it."

step "Hammerspoon (~/.hammerspoon/init.lua)"
HS_INIT="$HOME/.hammerspoon/init.lua"
mkdir -p "$HOME/.hammerspoon"
backup "$HS_INIT"
strip_block "$HS_INIT"
cat >> "$HS_INIT" <<EOF
-- tmux-voice begin
tmux_voice = dofile("$ROOT/hammerspoon/tmux_voice.lua").start({ root = "$ROOT", port = $PORT, key = "$KEY" })
-- tmux-voice end
EOF
echo "hold key: $KEY"

if [ "$DO_TMUX" = 1 ]; then
  step "tmux status line"
  if [ -f "$HOME/.config/tmux/tmux.conf" ]; then TMUX_CONF="$HOME/.config/tmux/tmux.conf"
  elif [ -f "$HOME/.tmux.conf" ]; then TMUX_CONF="$HOME/.tmux.conf"
  else
    TMUX_CONF="$HOME/.tmux.conf"
    if ask "No tmux config found. Install the example config (mouse, Option+arrow panes, agent bell) to ~/.tmux.conf?"; then
      cp "$ROOT/examples/tmux.conf" "$TMUX_CONF"
    fi
  fi
  backup "$TMUX_CONF"
  strip_block "$TMUX_CONF"
  cat >> "$TMUX_CONF" <<'EOF'
# tmux-voice begin
set -g focus-events on
if -F '#{m:*@voice*,#{status-right}}' '' 'set -ga status-right "#{@voice}"'
# tmux-voice end
EOF
  echo "updated $TMUX_CONF"
  tmux source-file "$TMUX_CONF" 2>/dev/null || true
fi

if [ "$DO_HOOK" = 1 ]; then
  step "Claude Code hooks (final answers read aloud)"
  mkdir -p "$HOME/.claude"
  backup "$HOME/.claude/settings.json"
  (cd "$ROOT" && ROOT="$ROOT" PORT="$PORT" uv run --quiet python -c '
import json, os
p = os.path.expanduser("~/.claude/settings.json")
s = json.load(open(p)) if os.path.exists(p) else {}
hooks = s.setdefault("hooks", {})
for event, script in (("Stop", "claude-stop.sh"), ("UserPromptSubmit", "claude-prompt.sh")):
    path = os.environ["ROOT"] + "/hooks/" + script
    groups = hooks.setdefault(event, [])
    for group in groups:
        group["hooks"] = [h for h in group.get("hooks", []) if not h.get("command", "").startswith(path)]
    groups[:] = [g for g in groups if g["hooks"]]
    groups.append({"matcher": "", "hooks": [{"type": "command", "command": path + " " + os.environ["PORT"]}]})
json.dump(s, open(p, "w"), indent=2)
open(p, "a").write("\n")')
  echo "added Stop and UserPromptSubmit hooks to ~/.claude/settings.json"
fi

if [ "$DO_INSTR" = 1 ] && ask "Tell Claude Code and Codex that prompts may be dictated (adds 2 lines to ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md)?"; then
  for f in "$HOME/.claude/CLAUDE.md" "$HOME/.codex/AGENTS.md"; do
    mkdir -p "$(dirname "$f")"
    backup "$f"
    strip_block "$f"
    [ -s "$f" ] && [ -n "$(tail -c1 "$f")" ] && echo >> "$f"
    { echo "<!-- tmux-voice begin -->"; cat "$ROOT/examples/agent-instructions.md"; echo "<!-- tmux-voice end -->"; } >> "$f"
    echo "updated $f"
  done
fi

if [ "$DO_START" = 1 ]; then
  step "Starting Hammerspoon"
  if pgrep -xq Hammerspoon; then
    osascript -e 'quit app "Hammerspoon"' 2>/dev/null || true
    sleep 1
  fi
  open -a Hammerspoon
fi

cat <<EOF

Almost done:
  1. System Settings > Privacy & Security > Accessibility: turn on Hammerspoon,
     then quit and reopen Hammerspoon (the menu bar shows "mic" when it is running).
  2. Hold the $KEY key, say something, release. Allow the microphone prompt the first time.
  3. Check everything with:  cd "$ROOT" && uv run tmux-voice doctor
EOF
