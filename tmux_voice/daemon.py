"""Push-to-talk daemon: record the mic, transcribe, and type into the active tmux pane or the frontmost app."""

import asyncio
import io
import json
import re
import subprocess
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd
from fastapi import FastAPI, Request

from . import stt, tts, vocab
from .config import STATE_DIR, load

RATE = 16000
ROUTE = re.compile(r"^\s*(claude|codex)\s*[,:.\-]\s*", re.I)
BUILTIN_FIXES = [
    (r"\b(?:cloud|claude)\s*\.?\s*md\b", "CLAUDE.md"),
    (r"\bagents?\s*\.?\s*md\b", "AGENTS.md"),
    (r"\bcloud\s+code\b", "Claude Code"),
    (r"^(\s*)cloud(?=\s*[,:])", r"\1Claude"),
    (r"\b(?:code ?x|codecs)\b", "Codex"),
]

cfg = load()
FIXES = [(re.compile(p, re.I), r) for p, r in BUILTIN_FIXES]
FIXES += [(re.compile(f["pattern"], re.I), f["replace"]) for f in cfg["fixes"]]
SEND = re.compile(r"[\s,.]*\b(" + "|".join(map(re.escape, cfg["routing"]["send_phrases"])) + r")[.!]?\s*$", re.I)
HISTORY = STATE_DIR / "history.jsonl"
AUTO_FLAG = STATE_DIR / "auto-read"
FINALS = STATE_DIR / "finals.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()
http = httpx.AsyncClient(timeout=30)
lock = threading.Lock()
state = {"rec": None}
voiced: dict[str, float] = {}  # pane_id -> time of last voice prompt
finals: dict[str, dict] = json.loads(FINALS.read_text()) if FINALS.exists() else {}  # pane_id -> {"text", "read"}
working: set[str] = set()  # panes whose agent got a prompt and hasn't finished
speaker = tts.Speaker(cfg)


def tmux(*args: str) -> str:
    try:
        r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip()


def set_status(text: str):
    tmux("set", "-g", "@voice", text)
    for c in tmux("list-clients", "-F", "#{client_name}").splitlines():
        tmux("refresh-client", "-S", "-t", c)


def panes() -> list[dict]:
    fmt = "#{pane_id}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_active}#{window_active}\t#{window_activity}"
    out = []
    for line in tmux("list-panes", "-a", "-F", fmt).splitlines():
        pid, cmd, path, active, activity = line.split("\t")
        # Native Claude Code builds show their version (e.g. 2.1.310) as the process name.
        kind = "claude" if re.fullmatch(r"claude|\d+\.\d+\.\d+", cmd) else "codex" if cmd == "codex" else None
        out.append({"id": pid, "path": path, "active": active == "11", "activity": int(activity or 0), "kind": kind})
    return out


def focused_client() -> str | None:
    """The tmux client whose terminal has keyboard focus (needs `focus-events on`), in any app: Ghostty, Zed, ..."""
    for line in tmux("list-clients", "-F", "#{client_name}\t#{client_flags}").splitlines():
        name, flags = line.split("\t")
        if "focused" in flags.split(","):
            return name
    return None


def active_pane(client: str | None = None) -> dict | None:
    pid = tmux("display-message", *(["-c", client] if client else []), "-p", "#{pane_id}")
    return next((p for p in panes() if p["id"] == pid), None)


def current_pane() -> str | None:
    """The pane you're looking at: the active pane of the tmux client with keyboard focus."""
    client = focused_client()
    return (active_pane(client) or {}).get("id") if client else None


def find_agent(kind: str) -> dict | None:
    cands = [p for p in panes() if p["kind"] == kind]
    return max(cands, key=lambda p: (p["active"], p["activity"]), default=None)


def type_into(pane: str, text: str):
    """Type text into a pane in small bursts. Agent TUIs treat one big burst as a paste: Claude Code then keeps
    only the last ~1 KB, and a bracketed paste would collapse into "[Pasted text]" that you can't review."""
    for i in range(0, len(text), 48):
        tmux("send-keys", "-t", pane, "-l", "--", text[i:i + 48])
        time.sleep(0.02)


def wav_bytes(frames: list[np.ndarray]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.concatenate(frames).tobytes())
    return buf.getvalue()


def auto_read() -> bool:
    if AUTO_FLAG.exists():
        return AUTO_FLAG.read_text().strip() == "1"
    return cfg["speak"]["auto"]


def save_finals():
    FINALS.write_text(json.dumps(finals, ensure_ascii=False))


def stop_speaking():
    speaker.stop()


def recording() -> bool:
    return state["rec"] is not None


def _open_input() -> sd.InputStream:
    try:
        return sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=1600)
    except sd.PortAudioError:
        # PortAudio lists devices once at startup; after a mic comes or goes (AirPods, iPhone) the cached default is stale.
        sd._terminate()
        sd._initialize()
        return sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=1600)


def _record(stop: threading.Event, frames: list[np.ndarray], ready: threading.Event):
    # Blocking reads keep Python out of the CoreAudio IO thread; a callback stream can deadlock on stop.
    try:
        with _open_input() as stream:
            ready.set()
            while not stop.is_set():
                data, _ = stream.read(1600)
                frames.append(data.copy())
            stream.abort()
    except Exception as e:
        print(f"recording failed: {e}", flush=True)
    finally:
        ready.set()


@app.post("/start")
def start():
    with lock:
        if state["rec"]:
            return {"ok": True}
        stop_speaking()
        stop, ready, frames = threading.Event(), threading.Event(), []
        thread = threading.Thread(target=_record, args=(stop, frames, ready), daemon=True)
        thread.start()
        state["rec"] = {"thread": thread, "stop": stop, "frames": frames}
    ready.wait(3)
    set_status("#[fg=red,bold]● Listening…#[default] ")
    return {"ok": True}


async def _close() -> list[np.ndarray]:
    with lock:
        rec, state["rec"] = state["rec"], None
    if not rec:
        return []
    rec["stop"].set()
    await asyncio.to_thread(rec["thread"].join, 3)
    if rec["thread"].is_alive():
        print("audio thread did not stop in 3s; abandoning it", flush=True)
    return list(rec["frames"])


@app.post("/cancel")
async def cancel():
    await _close()
    set_status("")
    return {"ok": True}


def apply_fixes(text: str) -> str:
    for pattern, fix in FIXES:
        text = pattern.sub(fix, text)
    return text


@app.post("/stop")
async def stop(req: Request):
    body = await req.json()
    frames = await _close()
    seconds = sum(len(f) for f in frames) / RATE
    if seconds < 0.4:
        set_status("")
        return {"text": "", "inserted": False}
    set_status("#[fg=yellow]Transcribing…#[default] ")
    client = focused_client()
    target = active_pane(client) if client or body.get("app") in cfg["routing"]["terminals"] else None
    try:
        text, provider, ms = await stt.transcribe(
            wav_bytes(frames), vocab.terms_for(target and target["path"], cfg), cfg, http)
    except Exception as e:
        set_status("#[fg=red]Transcription failed#[default] ")
        threading.Timer(3, lambda: recording() or set_status("")).start()
        print(f"transcription failed: {e}", flush=True)
        return {"text": "", "inserted": False, "error": str(e)}

    raw = text
    text = apply_fixes(text)
    m = ROUTE.match(text) if cfg["routing"]["agent_prefix"] else None
    if m:
        agent = find_agent(m.group(1).lower())
        if agent:
            target, text = agent, text[m.end():]
    submit = False
    if cfg["routing"]["send_word"] and SEND.search(text):
        text, submit = SEND.sub("", text), True

    inserted = False
    if target and text:
        await asyncio.to_thread(type_into, target["id"], text)
        if submit:
            tmux("send-keys", "-t", target["id"], "Enter")
        voiced[target["id"]] = time.time()
        inserted = True
    set_status("")
    if cfg["history"]["enabled"]:
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "seconds": round(seconds, 2),
                 "provider": provider, "ms": ms, "chars": len(text),
                 "target": target["id"] if target else body.get("app")}
        if cfg["history"]["store_text"]:
            entry["text"] = text
            if raw != text:
                entry["raw"] = raw
        with HISTORY.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"text": text, "inserted": inserted}


def read_final(pane: str) -> bool:
    f = finals.get(pane)
    line = tts.for_speech(f["text"], cfg["speak"]["max_words"]) if f else ""
    if f:
        f["read"] = True
        save_finals()
    if line:
        speaker.speak(line)
    return bool(line)


def set_final(pane: str, text: str):
    """An agent finished: keep its answer for this pane; read it now if you're looking at the pane."""
    if not text.strip():
        return
    finals[pane] = {"text": text, "read": False}
    working.discard(pane)
    save_finals()
    if current_pane() == pane and auto_read() and not recording():
        read_final(pane)


def last_assistant_text(transcript_path: str) -> str:
    last = ""
    with open(transcript_path, errors="ignore") as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("type") == "assistant" and not e.get("isSidechain"):
                parts = e.get("message", {}).get("content", []) or []
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
                if text.strip():
                    last = text
    return last


@app.post("/turn")
async def turn_hook(req: Request):
    """Claude Code UserPromptSubmit hook: a new turn started, so the old final answer is no longer current."""
    pane = req.query_params.get("pane") or ""
    finals.pop(pane, None)
    working.add(pane)
    save_finals()
    return {"ok": True}


@app.post("/speak")
async def speak_hook(req: Request):
    """Claude Code Stop hook: the turn finished; keep its final answer for this pane."""
    pane = req.query_params.get("pane") or ""
    body = await req.json()
    text = body.get("last_assistant_message") or ""
    if not text and body.get("transcript_path"):
        await asyncio.sleep(0.3)
        text = last_assistant_text(body["transcript_path"])
    if pane:
        set_final(pane, text)
    return {"ok": True}


@app.post("/speak-toggle")
def speak_toggle():
    """Hotkey: stop reading if it's talking, otherwise read the final answer of the pane you're on."""
    if speaker.busy():
        stop_speaking()
        return {"action": "stopped"}
    pane = current_pane()
    if pane in finals and read_final(pane):
        return {"action": "reading"}
    return {"action": "working" if pane in working else "nothing"}


@app.post("/auto-toggle")
def auto_toggle():
    """Menu bar click: turn automatic reading of final answers on or off."""
    on = not auto_read()
    AUTO_FLAG.write_text("1" if on else "0")
    if not on:
        stop_speaking()
    return {"enabled": on}


def watch_focus():
    """Read a waiting final answer when you go to its pane; stop talking when you leave; show Reading aloud."""
    last, was_busy = None, False
    while True:
        time.sleep(0.4)
        try:
            busy = speaker.busy()
            if busy != was_busy and not recording():
                set_status("#[fg=cyan]Reading aloud…#[default] " if busy else "")
            was_busy = busy
            pane = current_pane()
            if pane == last:
                continue
            last = pane
            if speaker.busy():
                speaker.stop()
            f = finals.get(pane)
            if f and not f["read"] and auto_read() and not recording():
                read_final(pane)
        except Exception as e:
            print(f"focus watcher: {e}", flush=True)


def watch_codex():
    """Keep Codex final answers for the panes running that session; a new prompt clears them."""
    base = Path.home() / ".codex" / "sessions"
    offsets: dict[Path, int] = {}
    cwds: dict[Path, str] = {}
    started = time.time()
    while True:
        time.sleep(1)
        try:
            day = base / datetime.now().strftime("%Y/%m/%d")
            if not day.is_dir():
                continue
            for f in day.glob("*.jsonl"):
                size = f.stat().st_size
                if f not in offsets:
                    offsets[f] = size if f.stat().st_mtime < started else 0
                    with f.open(errors="ignore") as fh:
                        meta = json.loads(fh.readline() or "{}")
                    cwds[f] = meta.get("payload", {}).get("cwd", "")
                if size <= offsets[f]:
                    continue
                with f.open(errors="ignore") as fh:
                    fh.seek(offsets[f])
                    chunk = fh.read()
                offsets[f] += len(chunk.encode())
                for raw in chunk.splitlines():
                    try:
                        e = json.loads(raw)
                    except ValueError:
                        continue
                    p = e.get("payload", {})
                    is_final = (e.get("type") == "response_item" and p.get("type") == "message"
                                and p.get("role") == "assistant" and p.get("phase") == "final_answer")
                    is_prompt = e.get("type") == "event_msg" and p.get("type") == "user_message"
                    if not (is_final or is_prompt):
                        continue
                    for x in panes():
                        if x["kind"] == "codex" and x["path"] == cwds[f]:
                            if is_prompt:
                                finals.pop(x["id"], None)
                                working.add(x["id"])
                                save_finals()
                            else:
                                text = "".join(c.get("text", "") for c in p.get("content", []) if isinstance(c, dict))
                                set_final(x["id"], text)
        except Exception as e:
            print(f"codex watcher: {e}", flush=True)


@app.get("/status")
def status():
    return {"recording": recording(), "providers": stt.order(cfg), "voiced": list(voiced), "auto_read": auto_read(),
            "tts": "kokoro" if speaker.model is not None else "say", "finals": {p: f["read"] for p, f in finals.items()}}


def run():
    import uvicorn

    threading.Thread(target=watch_codex, daemon=True).start()
    threading.Thread(target=watch_focus, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=cfg["port"], log_level="warning")
