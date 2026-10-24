import asyncio
import os
import time

import httpx

_gemini = None


def gemini_client():
    global _gemini
    if _gemini is None:
        from google import genai

        if os.environ.get("GEMINI_API_KEY"):
            _gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        else:
            _gemini = genai.Client(
                enterprise=True,
                project=os.environ["GOOGLE_CLOUD_PROJECT"],
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
            )
    return _gemini


async def scribe(wav: bytes, terms: list[str], cfg: dict, http: httpx.AsyncClient) -> str:
    fields = [("model_id", "scribe_v2"), ("tag_audio_events", "false")]
    if cfg["stt"]["language"]:
        fields.append(("language_code", cfg["stt"]["language"]))
    fields += [("keyterms", t) for t in terms]
    r = await http.post(
        "https://api.elevenlabs.io/v1/speech-to-text",
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
        files=[(k, (None, v)) for k, v in fields] + [("file", ("clip.wav", wav, "audio/wav"))],
    )
    r.raise_for_status()
    return r.json()["text"].strip()


async def gemini(wav: bytes, terms: list[str], cfg: dict, http: httpx.AsyncClient) -> str:
    from google.genai import types

    languages = cfg["stt"]["gemini_languages"]
    tc = types.AudioTranscriptionConfig(
        language_codes=languages or None,
        custom_vocabulary=terms[:100] or None,
    )
    resp = await gemini_client().aio.models.generate_content(
        model="gemini-3.5-transcribe",
        contents=[types.Part.from_bytes(data=wav, mime_type="audio/wav")],
        config=types.GenerateContentConfig(audio_transcription_config=tc),
    )
    return "".join(p.text or "" for p in resp.candidates[0].content.parts).strip()


PROVIDERS = {"scribe": scribe, "gemini": gemini}


def available(name: str) -> bool:
    if name == "scribe":
        return bool(os.environ.get("ELEVENLABS_API_KEY"))
    if name == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_CLOUD_PROJECT"))
    return False


def order(cfg: dict) -> list[str]:
    names = [cfg["stt"]["primary"], cfg["stt"]["fallback"]]
    return [n for i, n in enumerate(names) if n in PROVIDERS and n not in names[:i] and available(n)]


async def transcribe(wav: bytes, terms: list[str], cfg: dict, http: httpx.AsyncClient) -> tuple[str, str, int]:
    names = order(cfg)
    if not names:
        raise RuntimeError("no STT provider configured: set ELEVENLABS_API_KEY or GEMINI_API_KEY")
    t0 = time.perf_counter()
    for i, name in enumerate(names):
        try:
            text = await asyncio.wait_for(PROVIDERS[name](wav, terms, cfg, http), cfg["stt"]["timeout"])
            return text, name, round((time.perf_counter() - t0) * 1000)
        except Exception as e:
            if i == len(names) - 1:
                raise
            print(f"{name} failed, trying {names[i + 1]}: {e}", flush=True)
    raise RuntimeError("unreachable")
