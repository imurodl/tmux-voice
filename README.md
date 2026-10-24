# tmux-voice

**Website:** https://imurodl.me/tmux-voice/

Talk to Claude Code and Codex instead of typing, and hear their answers back. Hold a key, speak, release: the text lands in the prompt of the active tmux pane, ready for you to check and press Enter. When the agent finishes, its final answer is read aloud with a local voice when you're on that pane.

Built for people who run coding agents in tmux on macOS, and for people who don't speak only English. Speech is transcribed with ElevenLabs Scribe v2 or Gemini 3.5 Transcribe, which detect the language on every recording, so English, Uzbek, Korean or a mix of them in one sentence all work without switching anything.

## What it does

- **Hold to talk.** Hold Right Option (configurable), speak, release. Any other key while holding cancels, so Option shortcuts keep working.
- **Goes where you are.** When a tmux window has keyboard focus (in Ghostty, iTerm, Zed's terminal or any other app), text goes into its active pane without pressing Enter. Otherwise it is typed into the app in front, so dictating into an editor or Slack works too.
- **Talk to a specific agent.** Start with "Claude, ..." or "Codex, ..." to send the rest to that agent's pane, even if you are looking at something else.
- **Knows your project's words.** Each recording is sent with key terms from the pane's project: repo name, branch, recently changed files, dependencies, recent project folders and SSH hosts. File names and library names come out spelled right.
- **Fixes the usual mishearings.** "cloud MD" becomes CLAUDE.md, "Cloud, run the tests" becomes "Claude, run the tests". Add your own rules in the config.
- **Reads the answer back.** When Claude Code or Codex finishes, its final answer is read aloud: right away if you're on that pane, or when you switch to it later. Still working means nothing is read. Code blocks and tables are skipped, and file paths are read as file names. Switching away stops it.
- **A local voice.** Answers are read with [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M), an open model that runs on your Mac (about 1 second to the first word), or with the macOS voice if you skip it.
- **You control it.** Ctrl+Option+Cmd+S starts or stops reading the answer of the pane you're on. Clicking the menu bar item turns automatic reading on or off.
- **Status everywhere.** `● Listening…`, `Transcribing…` and `Reading aloud` in the tmux status line, and the same in the menu bar.
- **Falls back automatically.** If the primary provider fails, the other one is used.

## Requirements

- macOS, tmux, [uv](https://docs.astral.sh/uv/), ffmpeg (only for `bench`), and [Hammerspoon](https://www.hammerspoon.org/) for the global hotkey. The installer offers to install missing ones with Homebrew.
- For the Kokoro voice: Apple Silicon, `espeak-ng` (installed by the installer) and about 1 GB of Python packages. Without it, answers are read with `say`.
- An API key for at least one provider:
  - **ElevenLabs** (Scribe v2, recommended): [get a key](https://elevenlabs.io/app/settings/api-keys).
  - **Gemini** (Gemini 3.5 Transcribe): an [AI Studio key](https://aistudio.google.com/apikey), or a Google Cloud project with application default credentials.

## Install

```sh
git clone https://github.com/imurodl/tmux-voice.git
cd tmux-voice
./install.sh
```

The installer:

1. installs missing dependencies (asks first),
2. offers the local Kokoro voice for reading answers aloud,
3. creates `~/.config/tmux-voice/config.toml` and `~/.config/tmux-voice/env` and asks for your keys,
4. adds a block to `~/.hammerspoon/init.lua` that starts the hotkeys and the daemon,
5. turns on tmux `focus-events` (so it knows which pane you're looking at) and adds the voice status to your tmux status line, or offers `examples/tmux.conf` if you have no tmux config,
6. adds Stop and UserPromptSubmit hooks to `~/.claude/settings.json`, so it knows when a Claude Code turn starts and finishes,
7. optionally adds two lines to `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` telling the agents that prompts may be dictated.

Every change sits between `tmux-voice begin` / `tmux-voice end` markers, each edited file is backed up first, and re-running is safe. Options: `--key rightcmd`, `--port 8770`, `--yes`, `--no-tmux`, `--no-claude-hook`, `--no-instructions`, `--no-start`.

Then:

1. **System Settings → Privacy & Security → Accessibility:** turn on Hammerspoon, then quit and reopen it. Hammerspoon only picks up the permission after a restart.
2. Hold Right Option, say something, release. Allow the microphone prompt the first time.
3. Check the setup: `uv run tmux-voice doctor`.

## Usage

| Action | What happens |
|---|---|
| Hold the key, speak, release | Text goes into the prompt, no Enter |
| Press any other key while holding | Cancels |
| Tap the key quickly | Ignored |
| "Claude, ..." / "Codex, ..." | Sent to that agent's pane |
| End with "send it" (if `send_word = true`) | Also presses Enter |
| An agent finishes while you're on its pane | Its final answer is read aloud |
| An agent finished while you were elsewhere | The answer is read when you go to that pane |
| Ctrl+Option+Cmd+S | Stop reading, or read (replay) the focused pane's answer |
| Click the menu bar item | Automatic reading on or off (`mic (quiet)` when off) |

Commands (run inside the checkout):

```sh
uv run tmux-voice doctor        # check keys, microphone, tmux, Hammerspoon, daemon
uv run tmux-voice usage         # minutes transcribed per day and estimated cost
uv run tmux-voice bench         # compare providers on your own voice in the browser
```

## Choosing a provider

Accuracy depends a lot on your language and accent, so test with your own voice: `uv run tmux-voice bench` opens a page with scripts to read; each recording goes to every configured provider, with and without key terms, and you get word error rate, how many code terms came out right, and latency. Edit `~/.config/tmux-voice/bench_prompts.json` (copy it from `tmux_voice/bench_prompts.json`) to use scripts in your own language.

In the author's test (mixed Uzbek and English, 17 clips on a MacBook mic), Scribe v2 with key terms had the lowest error rate (14%) and got 94% of code terms right; Gemini 3.5 Transcribe with key terms was second (17%) and fastest, but sometimes dropped whole sentences of mixed-language speech. Without key terms both were much worse on code words.

## Cost

Scribe v2 costs about $0.27 per hour of audio with key terms (pay-as-you-go), and ElevenLabs plans include monthly hours (Starter covers about 20+ hours). Replacing all typing for a heavy coding day is roughly 30–60 minutes of speech, so a month of daily use is in the range of $3–6. Check `tmux-voice usage` for your real numbers.

On your Claude or Codex subscription the extra words barely matter: a prompt is a few dozen tokens inside turns that process tens of thousands.

## Configuration

`~/.config/tmux-voice/config.toml` (see `config.example.toml` for every option):

```toml
[stt]
primary = "scribe"         # or "gemini"
fallback = "gemini"
language = ""              # "" = auto-detect each recording

[vocab]
extra = ["Supabase", "my-api"]

[speak]
auto = true                # read finished answers when you're on (or go to) the pane
kokoro_voice = "bf_emma"   # af_heart, am_michael, bm_george, ...

[[fixes]]
pattern = '\bsuper base\b'
replace = "Supabase"
```

After editing, restart the daemon: Hammerspoon menu bar → Reload Config. To change the shortcut, pass `read_key = { { "ctrl", "alt", "cmd" }, "S" }` in the `start({...})` call in `~/.hammerspoon/init.lua`.

## How it works

```
Hammerspoon (hotkey, menu bar, watchdog)
   │ POST /start, /stop
   ▼
tmux-voice daemon (localhost)
   ├─ records the mic (16 kHz mono)
   ├─ key terms from the active pane's project
   ├─ Scribe v2 → Gemini fallback
   ├─ fixes + "Claude, / Codex," routing
   └─ tmux send-keys -l  (or returns text for Hammerspoon to type)

Claude Code hooks / Codex session files → daemon keeps each pane's final answer
   → read aloud (Kokoro or `say`) when that pane has your focus
```

Logs: `~/.local/state/tmux-voice/daemon.log`. History: `~/.local/state/tmux-voice/history.jsonl` (set `store_text = false` to keep only durations).

## Privacy

Audio is sent to the provider you configure (ElevenLabs or Google) for transcription, together with the key terms (project, file and host names). Reading answers aloud happens on your Mac. Nothing else leaves your machine. The daemon listens on 127.0.0.1 only.

## Uninstall

```sh
./uninstall.sh            # removes all marked blocks and the hooks
./uninstall.sh --purge    # also deletes config, keys and history
```

## Troubleshooting

- **Nothing happens when holding the key:** Hammerspoon needs Accessibility, and must be restarted after you grant it. `hs.accessibilityState()` in the Hammerspoon console should return true.
- **Empty transcripts:** check the input device in System Settings → Sound → Input, then Reload Config.
- **Claude panes not detected for "Claude, ...":** the daemon looks for panes running `claude` (or a version number, which native Claude Code builds show as their process name).
- **Daemon stuck:** the Hammerspoon watchdog restarts it automatically within about 30 seconds; details in `daemon.log`.
- **Answers not read for a Claude session:** Claude Code loads hooks when a session starts, so sessions opened before installing need a restart. Check that `focus-events` is on (`tmux show -g focus-events`).
- **Text typed into the wrong place:** routing follows tmux focus. If your terminal doesn't report focus, add its bundle id to `routing.terminals` in the config.

## License

MIT
