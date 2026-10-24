"""Reads agents' final answers aloud: Kokoro (local, via mlx-audio) when installed, macOS `say` otherwise."""

import queue
import re
import subprocess
import threading
from pathlib import Path

RATE = 24000


class Speaker:
    def __init__(self, cfg: dict):
        self.cfg = cfg["speak"]
        self.model = None
        self.q: queue.Queue[tuple[int, str]] = queue.Queue()
        self.player: subprocess.Popen | None = None
        self.gen = 0  # bumped by speak() and stop() to cancel the job being read
        self.active = False
        self.lock = threading.Lock()
        if self.cfg["engine"] == "kokoro":
            threading.Thread(target=self._load, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

    def _load(self):
        try:
            import misaki.espeak  # noqa: F401  (sets bundled espeak-ng paths, which point nowhere on macOS)
            from mlx_audio.tts.utils import load_model
            from phonemizer.backend.espeak.wrapper import EspeakWrapper

            prefix = next((p for p in ("/opt/homebrew", "/usr/local") if Path(p, "lib/libespeak-ng.dylib").exists()), None)
            if prefix:
                EspeakWrapper.set_library(f"{prefix}/lib/libespeak-ng.dylib")
                EspeakWrapper.set_data_path(f"{prefix}/share/espeak-ng-data")
            model = load_model("mlx-community/Kokoro-82M-bf16")
            self._synth(model, "Ready.")  # warm up so the first real answer starts fast
            self.model = model
            print(f"kokoro loaded ({self.cfg['kokoro_voice']})", flush=True)
        except Exception as e:
            print(f"kokoro unavailable, using say: {e}", flush=True)

    def _synth(self, model, text: str):
        import numpy as np

        voice = self.cfg["kokoro_voice"]
        chunks = [np.array(r.audio) for r in model.generate(text=text, voice=voice, speed=self.cfg["speed"], lang_code=voice[0])]
        audio = np.concatenate(chunks)
        peak = float(np.max(np.abs(audio))) or 1.0
        return audio * (0.9 / peak)  # Kokoro renders quietly; normalize to near full scale

    def speak(self, text: str):
        """Read text in sentence-sized pieces; a new call or stop() cancels whatever is still being read."""
        with self.lock:
            self.gen += 1
            self.q.put((self.gen, text))

    def busy(self) -> bool:
        with self.lock:
            playing = self.player is not None and self.player.poll() is None
            return playing or self.active or not self.q.empty()

    def stop(self):
        with self.lock:
            self.gen += 1
            if self.player and self.player.poll() is None:
                self.player.terminate()

    def _say_cmd(self, text: str) -> list[str]:
        return (lambda self, text: ["say", *(["-v", self.cfg["say_voice"]] if self.cfg["say_voice"] else []), text])(self, text)

    def _worker(self):
        while True:
            gen, text = self.q.get()
            if gen != self.gen:
                continue
            self.active = True
            try:
                if self.model is not None:
                    self._stream(gen, pieces(text))
                else:
                    for part in pieces(text):
                        with self.lock:
                            if gen != self.gen:
                                break
                            self.player = subprocess.Popen(self._say_cmd(part))
                        self.player.wait()
            except Exception as e:
                print(f"speak failed: {e}", flush=True)
            finally:
                self.active = False

    def _stream(self, gen: int, parts: list[str]):
        """Play pieces back to back through one output stream while the next piece is synthesized."""
        import numpy as np
        import sounddevice as sd

        ready: queue.Queue = queue.Queue(maxsize=2)

        def produce():
            for part in parts:
                if gen != self.gen:
                    break
                ready.put(self._synth(self.model, part).astype(np.float32))
            ready.put(None)

        threading.Thread(target=produce, daemon=True).start()
        block = RATE // 10
        # Blocking writes from this thread only: no Python runs in the CoreAudio thread, so stopping can't deadlock.
        with sd.OutputStream(samplerate=RATE, channels=1, dtype="float32") as out:
            while gen == self.gen:
                audio = ready.get()
                if audio is None:
                    break
                for i in range(0, len(audio), block):
                    if gen != self.gen:
                        break
                    out.write(audio[i:i + block].reshape(-1, 1))
            if gen != self.gen:
                out.abort()


def kokoro_installed() -> bool:
    try:
        import mlx_audio  # noqa: F401
        import misaki  # noqa: F401
    except ImportError:
        return False
    return True


def pieces(text: str, max_words: int = 40) -> list[str]:
    """The first sentence alone (so speech starts right away), then chunks of whole sentences."""
    sentences = [s for s in re.split(r"(?<=[.!?:])\s+", text.strip()) if s]
    if not sentences:
        return []
    out, cur = [sentences[0]], ""
    for sentence in sentences[1:]:
        if cur and len((cur + " " + sentence).split()) > max_words:
            out.append(cur)
            cur = sentence
        else:
            cur = (cur + " " + sentence).strip()
    if cur:
        out.append(cur)
    return out


def _short_path(m: re.Match) -> str:
    """Read a file path as its file name (src/server/auth.ts -> auth.ts); leave short words like and/or alone."""
    path = m.group(0).rstrip("/")
    name = path.rsplit("/", 1)[-1]
    return name if "." in name or path.count("/") >= 2 else m.group(0)


def for_speech(text: str, max_words: int) -> str:
    """The answer without code, tables and markdown, with paths shortened, up to max_words."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"^\s*\|.*$", " ", text, flags=re.M)
    text = re.sub(r"^\s*(?:[-*>]|#+|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[~\w.@-]*(?:/[\w.@-]+)+/?", _short_path, text)
    text = re.sub(r"[*_`~]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    out = ""
    for s in re.split(r"(?<=[.!?])\s+", text):
        if out and len((out + " " + s).split()) > max_words:
            break
        out = (out + " " + s).strip()
    return out
