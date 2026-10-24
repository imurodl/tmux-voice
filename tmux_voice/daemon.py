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

from . import stt, vocab
from .config import STATE_DIR, load

RATE = 16000
VOICED_TTL = 30 * 60
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
STATE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()
http = httpx.AsyncClient(timeout=30)
lock = threading.Lock()
state = {"rec": None, "say": None}
voiced: dict[str, float] = {}  # pane_id -> time of last voice prompt


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


def active_pane() -> dict | None:
    pid = tmux("display-message", "-p", "#{pane_id}")
    return next((p for p in panes() if p["id"] == pid), None)


def find_agent(kind: str) -> dict | None:
    cands = [p for p in panes() if p["kind"] == kind]
    return max(cands, key=lambda p: (p["active"], p["activity"]), default=None)


def wav_bytes(frames: list[np.ndarray]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.concatenate(frames).tobytes())
    return buf.getvalue()


def stop_speaking():
    p = state["say"]
    if p and p.poll() is None:
        p.terminate()


def recording() -> bool:
    return state["rec"] is not None


def _record(stop: threading.Event, frames: list[np.ndarray], ready: threading.Event):
    # Blocking reads keep Python out of the CoreAudio IO thread; a callback stream can deadlock on stop.
    try:
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=1600) as stream:
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
    set_status("#[fg=red,bold]● REC#[default] ")
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
    set_status("#[fg=yellow]… STT#[default] ")
    target = active_pane() if body.get("app") in cfg["routing"]["terminals"] else None
    try:
        text, provider, ms = await stt.transcribe(
            wav_bytes(frames), vocab.terms_for(target and target["path"], cfg), cfg, http)
    except Exception as e:
        set_status("#[fg=red]STT failed#[default] ")
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
        tmux("send-keys", "-t", target["id"], "-l", "--", text)
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


def summarize(text: str, max_words: int) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"^\s*[|#>-].*$", " ", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`~]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    out = ""
    for s in re.split(r"(?<=[.!?])\s+", text):
        if out and len((out + " " + s).split()) > max_words:
            break
        out = (out + " " + s).strip()
    return " ".join(out.split()[: max_words + 10])


def speak(pane: str, message: str):
    if not cfg["speak"]["enabled"] or not message or recording():
        return
    if time.time() - voiced.get(pane, 0) > VOICED_TTL:
        return
    line = summarize(message, cfg["speak"]["max_words"])
    if not line:
        return
    stop_speaking()
    voice = cfg["speak"]["voice"]
    state["say"] = subprocess.Popen(["say", *(["-v", voice] if voice else []), line])


def last_assistant_text(transcript_path: str) -> str:
    last = ""
    with open(transcript_path, errors="ignore") as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("type") == "assistant" and not e.get("isSidechain"):
                parts = e.get("message", {}).get("content", [])
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
                if text.strip():
                    last = text
    return last


@app.post("/speak")
async def speak_hook(req: Request):
    """Claude Code Stop hook payload; the pane id comes as ?pane=$TMUX_PANE."""
    pane = req.query_params.get("pane") or ""
    if pane not in voiced:
        return {"spoken": False}
    body = await req.json()
    message = body.get("last_assistant_message") or ""
    if not message and body.get("transcript_path"):
        await asyncio.sleep(0.3)
        message = last_assistant_text(body["transcript_path"])
    speak(pane, message)
    return {"spoken": True}


def watch_codex():
    """Speak Codex turn completions for codex panes that received a voice prompt."""
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
                    if e.get("type") == "event_msg" and p.get("type") == "task_complete":
                        pane = next((x["id"] for x in panes() if x["kind"] == "codex" and x["path"] == cwds[f]
                                     and x["id"] in voiced), None)
                        if pane:
                            speak(pane, p.get("last_agent_message") or "")
        except Exception as e:
            print(f"codex watcher: {e}", flush=True)


@app.get("/status")
def status():
    return {"recording": recording(), "providers": stt.order(cfg), "voiced": list(voiced)}


def run():
    import uvicorn

    threading.Thread(target=watch_codex, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=cfg["port"], log_level="warning")
