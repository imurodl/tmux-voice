"""Browser page that sends each recording to every configured provider and scores it against the script."""

import asyncio
import json
import re
import subprocess
import webbrowser
from datetime import date
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import stt, vocab
from .config import CONFIG_DIR, STATE_DIR, load

HERE = Path(__file__).parent
cfg = load()
user_prompts = CONFIG_DIR / "bench_prompts.json"
PROMPTS = {p["id"]: p for p in json.loads((user_prompts if user_prompts.exists() else HERE / "bench_prompts.json").read_text())}
RUN_DIR = STATE_DIR / "bench" / date.today().isoformat()
RUN_DIR.mkdir(parents=True, exist_ok=True)
TERMS = vocab.terms_for(None, cfg)
VARIANTS = [(f"{n}{'_terms' if t else ''}", f"{n}{' + key terms' if t else ''}", n, t)
            for n in stt.PROVIDERS if stt.available(n) for t in (False, True)]

app = FastAPI()
app.mount("/runs", StaticFiles(directory=STATE_DIR / "bench"), name="runs")
http = httpx.AsyncClient(timeout=120)
APOS = re.compile(r"[‘’ʻʼ`´]")


def words(s: str) -> list[str]:
    s = re.sub(r"[^\w'\s]", " ", APOS.sub("'", s.lower()))
    return [w.strip("'") for w in s.split() if w.strip("'")]


def wer(ref: str, hyp: str) -> float:
    r, h = words(ref), words(hyp)
    if not r:
        return 0.0
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return round(d[len(h)] / len(r), 3)


async def run_variant(vid: str, name: str, with_terms: bool, wav: bytes) -> dict:
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    try:
        text = await stt.PROVIDERS[name](wav, TERMS if with_terms else [], cfg, http)
        err = None
    except httpx.HTTPStatusError as e:
        text, err = "", f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        text, err = "", f"{type(e).__name__}: {str(e)[:300]}"
    return {"id": vid, "text": text, "error": err, "ms": round((loop.time() - t0) * 1000)}


@app.get("/")
def index():
    return FileResponse(HERE / "bench.html")


@app.get("/api/config")
def config():
    return {"prompts": list(PROMPTS.values()), "providers": [{"id": v[0], "label": v[1]} for v in VARIANTS], "disabled": []}


@app.get("/api/results")
def results():
    return [json.loads(f.read_text()) for f in sorted(RUN_DIR.glob("*.json"))]


@app.post("/api/clip")
async def clip(clip_id: str = Form(...), audio: UploadFile = Form(...)):
    prompt = PROMPTS[clip_id]
    raw, wav_path = RUN_DIR / f"{clip_id}.webm", RUN_DIR / f"{clip_id}.wav"
    raw.write_bytes(await audio.read())
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", raw, "-ar", "16000", "-ac", "1", wav_path], check=True)
    wav = wav_path.read_bytes()
    free = prompt.get("free", False)

    async def stream():
        out = {"clip_id": clip_id, "cat": prompt["cat"], "ref": prompt["text"], "free": free,
               "duration_s": round(len(wav) / 32000, 2), "audio": f"/runs/{RUN_DIR.name}/{wav_path.name}", "results": []}
        tasks = [asyncio.create_task(run_variant(vid, n, t, wav)) for vid, _, n, t in VARIANTS]
        for task in asyncio.as_completed(tasks):
            r = await task
            if not free and not r["error"]:
                r["wer"] = wer(prompt["text"], r["text"])
                h = APOS.sub("'", r["text"].lower())
                r["terms"] = [t.lower() in h for t in prompt.get("terms", [])]
            out["results"].append(r)
            yield json.dumps({"type": "result", **r}) + "\n"
        (RUN_DIR / f"{clip_id}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
        yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def run(port: int) -> int:
    import uvicorn

    if not VARIANTS:
        print("no STT provider configured; add keys to", CONFIG_DIR / "env")
        return 1
    print(f"bench on http://localhost:{port}  (results in {RUN_DIR})")
    webbrowser.open(f"http://localhost:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
