import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import date, timedelta

from .config import CONFIG_DIR, STATE_DIR, load

# ElevenLabs Scribe v2 pay-as-you-go: $0.22/h plus $0.05/h for key terms.
SCRIBE_USD_PER_HOUR = 0.27


def doctor() -> int:
    cfg = load()
    ok = True

    def check(label: str, passed: bool, hint: str = ""):
        nonlocal ok
        ok &= passed
        print(f"  [{'ok' if passed else '!!'}] {label}" + ("" if passed else f"  -> {hint}"))

    from . import stt

    print("tmux-voice doctor")
    check("config file", (CONFIG_DIR / "config.toml").exists(), f"run ./install.sh (expects {CONFIG_DIR}/config.toml)")
    check("STT provider key", bool(stt.order(cfg)), f"add ELEVENLABS_API_KEY or GEMINI_API_KEY to {CONFIG_DIR}/env")
    print(f"       providers in order: {', '.join(stt.order(cfg)) or 'none'}")
    check("tmux installed", bool(shutil.which("tmux")), "brew install tmux")
    from . import tts

    engine = cfg["speak"]["engine"]
    if engine == "kokoro":
        check("Kokoro voice installed", tts.kokoro_installed(),
              "re-run ./install.sh and accept the Kokoro voice, or set engine = \"say\" in config.toml")
    else:
        check("say available", bool(shutil.which("say")), "reading answers aloud needs macOS `say`")
    try:
        import sounddevice as sd

        dev = sd.query_devices(kind="input")["name"]
        check(f"microphone: {dev}", True)
    except Exception as e:
        check("microphone", False, str(e))
    check("Hammerspoon installed", subprocess.run(["open", "-Ra", "Hammerspoon"], capture_output=True).returncode == 0,
          "brew install --cask hammerspoon")
    try:
        import httpx

        r = httpx.get(f"http://127.0.0.1:{cfg['port']}/status", timeout=2).json()
        check(f"daemon running on port {cfg['port']}", True)
        print(f"       recording={r['recording']} providers={r['providers']} voice={r.get('tts')} auto_read={r.get('auto_read')}")
    except Exception:
        check(f"daemon running on port {cfg['port']}", False, "open Hammerspoon and use Reload Config")
    return 0 if ok else 1


def usage(days: int) -> int:
    path = STATE_DIR / "history.jsonl"
    if not path.exists():
        print("no history yet")
        return 0
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    per_day: dict[str, list[float]] = defaultdict(list)
    providers: dict[str, int] = defaultdict(int)
    for line in path.read_text().splitlines():
        e = json.loads(line)
        if e["ts"][:10] >= since:
            per_day[e["ts"][:10]].append(e["seconds"])
            providers[e["provider"]] += 1
    total = sum(sum(v) for v in per_day.values())
    for d in sorted(per_day):
        print(f"  {d}  {len(per_day[d]):4d} clips  {sum(per_day[d]) / 60:6.1f} min")
    print(f"  total {total / 60:.1f} min of audio, about ${total / 3600 * SCRIBE_USD_PER_HOUR:.2f} on Scribe v2"
          f" ({', '.join(f'{k}: {v}' for k, v in providers.items())})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="tmux-voice")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the daemon (Hammerspoon does this for you)")
    sub.add_parser("doctor", help="check keys, microphone, tmux, Hammerspoon and the daemon")
    u = sub.add_parser("usage", help="minutes of audio transcribed and estimated cost")
    u.add_argument("--days", type=int, default=7)
    b = sub.add_parser("bench", help="compare STT providers on your own voice in the browser")
    b.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    if a.cmd == "doctor":
        return doctor()
    if a.cmd == "usage":
        return usage(a.days)
    if a.cmd == "bench":
        from .bench import run as bench_run

        return bench_run(a.port)
    if a.cmd in (None, "run"):
        from .daemon import run

        run()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
