import copy
import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv

CONFIG_DIR = Path(os.environ.get("TMUX_VOICE_CONFIG", Path.home() / ".config" / "tmux-voice"))
STATE_DIR = Path(os.environ.get("TMUX_VOICE_STATE", Path.home() / ".local" / "state" / "tmux-voice"))

DEFAULTS = {
    "port": 8766,
    "stt": {
        "primary": "scribe",
        "fallback": "gemini",
        "language": "",
        "gemini_languages": [],
        "timeout": 15,
    },
    "vocab": {
        "builtin": True,
        "extra": [],
        "projects_dir": "~/projects",
        "recent_projects": 25,
        "ssh_hosts": True,
        "project_files": True,
        "dependencies": True,
        "max_terms": 150,
    },
    "routing": {
        "terminals": [
            "com.mitchellh.ghostty",
            "com.googlecode.iterm2",
            "com.apple.Terminal",
            "net.kovidgoyal.kitty",
            "com.github.wez.wezterm",
            "org.alacritty",
            "dev.warp.Warp-Stable",
        ],
        "agent_prefix": True,
        "send_word": False,
        "send_phrases": ["send it"],
    },
    "speak": {
        "enabled": True,
        "voice": "",
        "max_words": 30,
    },
    "history": {
        "enabled": True,
        "store_text": True,
    },
    "fixes": [],
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load() -> dict:
    load_dotenv(CONFIG_DIR / "env")
    path = CONFIG_DIR / "config.toml"
    user = tomllib.loads(path.read_text()) if path.exists() else {}
    return _merge(DEFAULTS, user)
