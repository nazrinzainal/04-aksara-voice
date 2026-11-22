"""Aksara Voice engines.

Two engines:
  * Aksara Cloud - online neural voices (Malay + English; needs internet).
  * Kokoro Local - the Kokoro-82M ONNX voices (English; runs on CPU).

The public surface is TTSEngine.catalog() and TTSEngine.synth().
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import re
import threading
import time
import uuid
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
# VOX_DATA_DIR lets tests (or a second instance) use a separate history/audio folder.
DATA_DIR = Path(os.environ.get("VOX_DATA_DIR") or (ROOT / "data")).resolve()
AUDIO_DIR = DATA_DIR / "audio"
WEB_DIR = ROOT / "web"
# Only these names are ever served from / deleted in AUDIO_DIR.
AUDIO_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(mp3|wav)")


def safe_audio_path(name: str) -> Path | None:
    """Return AUDIO_DIR/name only for a plain, well-formed audio file name."""
    name = str(name or "")
    if not AUDIO_NAME_RE.fullmatch(name) or ".." in name:
        return None
    path = (AUDIO_DIR / name).resolve()
    if path.parent != AUDIO_DIR.resolve():
        return None
    return path


class EngineError(Exception):
    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


class Cancelled(EngineError):
    """The client went away; stop between chunks and do not save."""

    def __init__(self):
        super().__init__("Dibatalkan.", 499)


def _check(cancelled) -> None:
    if cancelled is not None and cancelled():
        raise Cancelled()


# --------------------------------------------------------------------------- voices
VOICES = [
    # Malay - Chatterbox Multilingual V2 (local, most natural)
    {"id": "chatterbox:ms-malaysia-f", "engine": "chatterbox", "lang": "ms", "name": "Melayu Perempuan",
     "gender": "female", "accent": "Malaysia", "note": "Chatterbox · Malaysian reference", "color": "#ffb86b",
     "reference": "ref_ms_malaysia_f.wav"},
    {"id": "chatterbox:ms-malaysia-m", "engine": "chatterbox", "lang": "ms", "name": "Melayu Lelaki",
     "gender": "male", "accent": "Malaysia", "note": "Chatterbox · Malaysian reference", "color": "#7ef0c0",
     "reference": "ref_ms_malaysia_m.wav"},
    # Malay - Edge Neural
    {"id": "edge:ms-MY-YasminNeural", "engine": "edge", "lang": "ms", "name": "Yasmin",
     "gender": "female", "accent": "Malaysia", "note": "Warm, natural Malay", "color": "#ffb86b"},
    {"id": "edge:ms-MY-OsmanNeural", "engine": "edge", "lang": "ms", "name": "Osman",
     "gender": "male", "accent": "Malaysia", "note": "Calm, clear Malay", "color": "#7ef0c0"},
    # English - Kokoro local (ONNX)
    {"id": "kokoro:af_heart", "engine": "kokoro", "lang": "en", "name": "Heart",
     "gender": "female", "accent": "American", "note": "Signature warm voice", "color": "#ffb86b"},
    {"id": "kokoro:af_bella", "engine": "kokoro", "lang": "en", "name": "Bella",
     "gender": "female", "accent": "American", "note": "Expressive, bright", "color": "#ff9fb0"},
    {"id": "kokoro:af_nicole", "engine": "kokoro", "lang": "en", "name": "Nicole",
     "gender": "female", "accent": "American", "note": "Soft, intimate", "color": "#b9a7ff"},
    {"id": "kokoro:bf_emma", "engine": "kokoro", "lang": "en", "name": "Emma",
     "gender": "female", "accent": "British", "note": "Polished, precise", "color": "#7ef0c0"},
    {"id": "kokoro:am_michael", "engine": "kokoro", "lang": "en", "name": "Michael",
     "gender": "male", "accent": "American", "note": "Calm male", "color": "#6bd5ff"},
    {"id": "kokoro:am_onyx", "engine": "kokoro", "lang": "en", "name": "Onyx",
     "gender": "male", "accent": "American", "note": "Deep male", "color": "#9fb7ff"},
    {"id": "kokoro:bm_george", "engine": "kokoro", "lang": "en", "name": "George",
     "gender": "male", "accent": "British", "note": "Classic butler", "color": "#ffc46b"},
    {"id": "kokoro:bm_lewis", "engine": "kokoro", "lang": "en", "name": "Lewis",
     "gender": "male", "accent": "British", "note": "Warm British", "color": "#7ef0c0"},
    # English - Edge Neural
    {"id": "edge:en-US-AriaNeural", "engine": "edge", "lang": "en", "name": "Aria",
     "gender": "female", "accent": "American", "note": "Confident, clear", "color": "#ff9f6b"},
    {"id": "edge:en-US-JennyNeural", "engine": "edge", "lang": "en", "name": "Jenny",
     "gender": "female", "accent": "American", "note": "Friendly, considerate", "color": "#ffd36b"},
    {"id": "edge:en-GB-SoniaNeural", "engine": "edge", "lang": "en", "name": "Sonia",
     "gender": "female", "accent": "British", "note": "Elegant, composed", "color": "#c9a7ff"},
    {"id": "edge:en-AU-NatashaNeural", "engine": "edge", "lang": "en", "name": "Natasha",
     "gender": "female", "accent": "Australian", "note": "Warm Australian", "color": "#ff8fb1"},
    {"id": "edge:en-US-GuyNeural", "engine": "edge", "lang": "en", "name": "Guy",
     "gender": "male", "accent": "American", "note": "Deep, steady", "color": "#6bd5ff"},
    {"id": "edge:en-US-ChristopherNeural", "engine": "edge", "lang": "en", "name": "Christopher",
     "gender": "male", "accent": "American", "note": "Authoritative", "color": "#9fb7ff"},
    {"id": "edge:en-GB-RyanNeural", "engine": "edge", "lang": "en", "name": "Ryan",
     "gender": "male", "accent": "British", "note": "Warm British", "color": "#7ef0c0"},
    {"id": "edge:en-IN-NeerjaNeural", "engine": "edge", "lang": "en", "name": "Neerja",
     "gender": "female", "accent": "Indian", "note": "Clear Indian English", "color": "#ffd36b"},
]

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 7788,
    "max_chars": 3000,
    "history_limit": 60,
    "kokoro_model": "models/kokoro/kokoro-v1.0.onnx",
    "kokoro_voices": "models/kokoro/voices-v1.0.bin",
    "chatterbox_device": "auto",
    "chatterbox_dir": "models/chatterbox",
    "default_voice_ms": "chatterbox:ms-malaysia-f",
    "default_voice_en": "kokoro:af_heart",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = ROOT / "config.json"
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            pass
    for key in ("kokoro_model", "kokoro_voices", "chatterbox_dir"):
        p = Path(str(cfg[key]))
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        cfg[key] = str(p)
    cfg["max_chars"] = max(100, min(8000, int(cfg.get("max_chars", 3000))))
    cfg["history_limit"] = max(10, min(300, int(cfg.get("history_limit", 60))))
    return cfg


def normalize_text(text: str, max_chars: int) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise EngineError("Teks kosong. Taip sesuatu dahulu.", 400)
    if len(text) > max_chars:
        raise EngineError(f"Teks terlalu panjang ({len(text)} / {max_chars} aksara).", 400)
    return text


def split_sentences(text: str, limit: int = 280) -> list[str]:
    """Split text into safe chunks for Kokoro (its phoneme window is small)."""
    text = re.sub(r"\s+", " ", text).strip()
    pieces = re.split(r"(?<=[.!?;:])\s+", text)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not piece:
            continue
        if len(piece) > limit:
            for word in piece.split():
                if len(current) + len(word) + 1 > limit and current:
                    chunks.append(current.strip())
                    current = ""
                current += (" " if current else "") + word
            continue
        if len(current) + len(piece) + 1 > limit and current:
            chunks.append(current.strip())
            current = piece
        else:
            current += (" " if current else "") + piece
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c] or [text[:limit]]


def wav_bytes(samples, sample_rate: int) -> bytes:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def match_loudness(reference, audio, ceiling: float = 0.99):
    """Scale `audio` to the RMS level of `reference` without clipping (peak <= ceiling)."""
    audio = np.asarray(audio, dtype=np.float32)
    ref_rms = float(np.sqrt(np.mean(np.square(np.asarray(reference, dtype=np.float32))))) if len(reference) else 0.0
    cur_rms = float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0
    if ref_rms <= 0 or cur_rms <= 0:
        return audio
    gain = ref_rms / cur_rms
    peak = float(np.max(np.abs(audio))) * gain
    if peak > ceiling:
        gain *= ceiling / peak
    return audio * gain


def audio_duration(data: bytes, ext: str) -> float:
    try:
        if ext == "wav":
            with wave.open(io.BytesIO(data)) as w:
                return w.getnframes() / float(w.getframerate() or 24000)
        from mutagen.mp3 import MP3
        return float(MP3(io.BytesIO(data)).info.length)
    except Exception:
        return 0.0


# --------------------------------------------------------------------- Kokoro local
class KokoroEngine:
    def __init__(self, cfg: dict):
        self.model = Path(cfg["kokoro_model"])
        self.voices_file = Path(cfg["kokoro_voices"])
        self.available = self.model.exists() and self.voices_file.exists()
        self.ok = False
        self.loading = False
        self.error = "" if self.available else "Kokoro model files not found"
        self._engine = None
        self._voice_set: set[str] = set()
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def start(self):
        if not self.available:
            self._ready.set()
            return
        self.loading = True
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            from kokoro_onnx import Kokoro
            engine = Kokoro(str(self.model), str(self.voices_file))
            self._voice_set = set(engine.get_voices())
            self._engine = engine
            self.ok = True
        except Exception as ex:  # pragma: no cover - depends on local files
            self.error = str(ex)
        finally:
            self.loading = False
            self._ready.set()

    def has_voice(self, name: str) -> bool:
        return not self.available or not self._voice_set or name in self._voice_set

    def status(self) -> dict:
        return {"ok": self.ok, "loading": self.loading, "available": self.available, "error": self.error}

    def synth(self, text: str, voice: str, speed: float, cancelled=None) -> bytes:
        if not self.ok:
            if self.loading:
                if not self._ready.wait(90):
                    raise EngineError("Kokoro masih dimuatkan. Cuba sebentar lagi.")
            if not self.ok:
                raise EngineError("Kokoro tidak tersedia: " + (self.error or "model tidak dijumpai"))
        lang = "en-gb" if voice.startswith(("bf", "bm")) else "en-us"
        chunks = split_sentences(text, 280)
        parts = []
        sample_rate = 24000
        with self._lock:
            for i, chunk in enumerate(chunks):
                _check(cancelled)
                samples, sample_rate = self._engine.create(chunk, voice=voice, speed=speed, lang=lang)
                parts.append(np.asarray(samples, dtype=np.float32).reshape(-1))
                if i < len(chunks) - 1:
                    parts.append(np.zeros(int(sample_rate * 0.12), dtype=np.float32))
        if not parts:
            raise EngineError("Kokoro tidak menghasilkan audio.")
        return wav_bytes(np.concatenate(parts), sample_rate)


# ---------------------------------------------------------------------- Edge Neural
class EdgeEngine:
    def __init__(self):
        self.available = importlib.util.find_spec("edge_tts") is not None

    def status(self) -> dict:
        return {"ok": self.available, "loading": False, "available": self.available,
                "error": "" if self.available else "edge-tts not installed"}

    def synth(self, text: str, voice: str, rate: int, pitch: int) -> bytes:
        if not self.available:
            raise EngineError("Edge Neural tidak tersedia (edge-tts tidak dipasang).")
        rate = max(-50, min(50, int(rate)))
        pitch = max(-50, min(50, int(pitch)))
        try:
            return asyncio.run(asyncio.wait_for(self._run(text, voice, rate, pitch), timeout=150))
        except asyncio.TimeoutError:
            raise EngineError("Edge Neural mengambil masa terlalu lama. Semak sambungan internet.")
        except EngineError:
            raise
        except Exception as ex:
            raise EngineError("Edge Neural gagal: " + str(ex))

    async def _run(self, text: str, voice: str, rate: int, pitch: int) -> bytes:
        import edge_tts
        communicate = edge_tts.Communicate(
            text, voice, rate=f"{rate:+d}%", pitch=f"{pitch:+d}Hz"
        )
        out = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                out.extend(chunk["data"])
        if not out:
            raise RuntimeError("Edge tidak memulangkan audio")
        return bytes(out)


# ------------------------------------------------------- Chatterbox (Malay, local)
class ChatterboxEngine:
    """Chatterbox Multilingual — the natural Malay voice, running on this PC."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.available = importlib.util.find_spec("chatterbox") is not None
        self.ok = False
        self.loading = False
        self.error = ""
        self.device = "cpu"
        self.preferred = str(cfg.get("chatterbox_device", "auto")).lower()
        self._model = None
        self._sr = 24000
        self._lock = threading.Lock()
        self._ready = threading.Event()

    def start(self):
        if not self.available:
            self.error = "chatterbox-tts not installed"
            self._ready.set()
            return
        self.loading = True
        threading.Thread(target=self._load, daemon=True).start()

    def _pick_device(self) -> str:
        if self.preferred == "cpu":
            return "cpu"
        try:
            import torch
            if not torch.cuda.is_available():
                return "cpu"
            if self.preferred == "cuda":
                return "cuda"
            free, _ = torch.cuda.mem_get_info()
            return "cuda" if free >= 3_300_000_000 else "cpu"
        except Exception:
            return "cpu"

    def _load(self):
        try:
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS
            local_dir = Path(self.cfg.get("chatterbox_dir", ROOT / "models" / "chatterbox"))
            required = ["t3_mtl23ls_v2.safetensors", "ve.pt", "s3gen.pt", "conds.pt",
                        "grapheme_mtl_merged_expanded_v1.json", "Cangjie5_TC.json"]
            use_local = local_dir.exists() and all(
                (local_dir / name).exists() and (local_dir / name).stat().st_size > 0
                for name in required)
            device = self._pick_device()
            self.device = device
            try:
                if use_local:
                    self._model = ChatterboxMultilingualTTS.from_local(local_dir, device)
                else:
                    import os
                    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
                    self._model = ChatterboxMultilingualTTS.from_pretrained(device=device)
            except Exception as ex:
                if device != "cuda":
                    raise
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                self.device = "cpu"
                if use_local:
                    self._model = ChatterboxMultilingualTTS.from_local(local_dir, "cpu")
                else:
                    self._model = ChatterboxMultilingualTTS.from_pretrained(device="cpu")
            self._sr = int(getattr(self._model, "sr", 24000))
            self.ok = True
        except Exception as ex:  # pragma: no cover - depends on local install/model
            self.error = str(ex)
        finally:
            self.loading = False
            self._ready.set()

    def status(self) -> dict:
        return {"ok": self.ok, "loading": self.loading, "available": self.available,
                "error": self.error, "device": self.device}

    def _one(self, text: str, exaggeration: float, reference: str | None = None,
             temperature: float = 0.8):
        import inspect
        kwargs = {"language_id": "ms"}
        try:
            params = inspect.signature(self._model.generate).parameters
        except (TypeError, ValueError):
            params = {}
        if "exaggeration" in params:
            kwargs["exaggeration"] = float(exaggeration)
        if "cfg_weight" in params:
            kwargs["cfg_weight"] = 0.5
        elif "cfg" in params:
            kwargs["cfg"] = 0.5
        if "temperature" in params:
            kwargs["temperature"] = float(temperature)
        if reference:
            kwargs["audio_prompt_path"] = reference
        wav = self._model.generate(text, **kwargs)
        if hasattr(wav, "detach"):
            wav = wav.detach().float().cpu().numpy()
        arr = np.asarray(wav, dtype=np.float32).squeeze()
        if arr.ndim > 1:
            arr = arr.mean(axis=0)
        return arr.reshape(-1)

    def reference_path(self, name: str | None) -> str | None:
        if not name:
            return None
        base = Path(self.cfg.get("chatterbox_dir", ROOT / "models" / "chatterbox"))
        path = base / name
        return str(path) if path.exists() and path.stat().st_size > 1000 else None

    def synth(self, text: str, exaggeration: float = 0.5, speed: float = 1.0,
              reference: str | None = None, stability: float = 0.5, cancelled=None) -> bytes:
        if not self.ok:
            if self.loading:
                if not self._ready.wait(180):
                    raise EngineError("Chatterbox masih dimuatkan. Cuba sebentar lagi.")
            if not self.ok:
                raise EngineError("Chatterbox tidak tersedia: " + (self.error or "model tidak dijumpai"))
        exaggeration = max(0.2, min(0.9, float(0.5 if exaggeration is None else exaggeration)))
        speed = max(0.7, min(1.3, float(1.0 if speed is None else speed)))
        stability = max(0.0, min(1.0, float(0.5 if stability is None else stability)))
        # High stability = lower sampling temperature (fewer wild takes).
        temperature = max(0.45, min(1.0, 0.92 - (stability - 0.5) * 0.45))
        ref_path = self.reference_path(reference)
        chunks = split_sentences(text, 240)
        parts = []
        sample_rate = self._sr
        with self._lock:
            for i, chunk in enumerate(chunks):
                _check(cancelled)
                audio = self._one(chunk, exaggeration, ref_path, temperature)
                if len(audio):
                    parts.append(audio)
                if i < len(chunks) - 1:
                    parts.append(np.zeros(int(sample_rate * 0.14), dtype=np.float32))
        if not parts:
            raise EngineError("Chatterbox tidak menghasilkan audio.")
        samples = np.concatenate(parts)
        if abs(speed - 1.0) > 0.01:
            try:
                import librosa
                stretched = librosa.effects.time_stretch(samples.astype(np.float32), rate=speed)
                # the phase vocoder drops loudness ~25-35 %; bring it back to the original level
                samples = match_loudness(samples, stretched)
            except Exception:
                pass
        return wav_bytes(samples, sample_rate)


# ---------------------------------------------------------------------- facade
class TTSEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        # VOX_DISABLE_ENGINES=chatterbox,kokoro skips loading heavy engines (used by tests).
        disabled = {x.strip().lower() for x in os.environ.get("VOX_DISABLE_ENGINES", "").split(",") if x.strip()}
        self.edge = EdgeEngine()
        if "edge" in disabled:
            self.edge.available = False
        self.kokoro = KokoroEngine(cfg)
        if "kokoro" in disabled:
            self.kokoro.available = False
            self.kokoro.error = "disabled"
        self.kokoro.start()
        self.chatterbox = ChatterboxEngine(cfg)
        if "chatterbox" in disabled:
            self.chatterbox.available = False
        self.chatterbox.start()

    def _kokoro_voices(self) -> list[dict]:
        if not self.kokoro.available:
            return []
        if self.kokoro.error and not self.kokoro.ok and not self.kokoro.loading:
            return []
        return [v for v in VOICES
                if v["engine"] == "kokoro" and self.kokoro.has_voice(v["id"].split(":", 1)[1])]

    def _chatterbox_voices(self) -> list[dict]:
        if not self.chatterbox.available:
            return []
        if self.chatterbox.error and not self.chatterbox.ok and not self.chatterbox.loading:
            return []
        voices = []
        for v in VOICES:
            if v["engine"] != "chatterbox":
                continue
            ref = v.get("reference")
            if ref and not self.chatterbox.reference_path(ref):
                continue
            voices.append(v)
        return voices

    def catalog(self) -> dict:
        kokoro_voices = self._kokoro_voices()
        chatter_voices = self._chatterbox_voices()
        voices = [v for v in VOICES
                  if (v["engine"] != "kokoro" or v in kokoro_voices)
                  and (v["engine"] != "chatterbox" or v in chatter_voices)
                  and (v["engine"] != "edge" or self.edge.available)]
        return {
            "voices": voices,
            "engines": {"edge": self.edge.status(), "kokoro": self.kokoro.status(),
                        "chatterbox": self.chatterbox.status()},
            "defaults": {"ms": self.cfg["default_voice_ms"], "en": self.cfg["default_voice_en"]},
            "max_chars": self.cfg["max_chars"],
            "languages": [{"id": "ms", "label": "Bahasa Melayu"}, {"id": "en", "label": "English"}],
        }

    def voice(self, voice_id: str) -> dict:
        for v in VOICES:
            if v["id"] == voice_id:
                return v
        raise EngineError("Voice tidak dikenali.", 400)

    def synth(self, voice_id: str, text: str, speed: float = 1.0, pitch: int = 0,
              exaggeration: float = 0.5, stability: float = 0.5, cancelled=None):
        voice = self.voice(voice_id)
        text = normalize_text(text, self.cfg["max_chars"])
        speed = max(0.7, min(1.3, float(1.0 if speed is None else speed)))
        pitch = max(-20, min(20, int(round(float(pitch or 0)))))
        exaggeration = max(0.2, min(0.9, float(0.5 if exaggeration is None else exaggeration)))
        stability = max(0.0, min(1.0, float(0.5 if stability is None else stability)))
        started = time.time()
        if voice["engine"] == "edge":
            rate = int(round((speed - 1.0) * 100))
            data = self.edge.synth(text, voice_id.split(":", 1)[1], rate, pitch)
            ext, ctype = "mp3", "audio/mpeg"
        elif voice["engine"] == "chatterbox":
            data = self.chatterbox.synth(text, exaggeration, speed, voice.get("reference"), stability,
                                         cancelled=cancelled)
            ext, ctype = "wav", "audio/wav"
        else:
            if voice_id.split(":", 1)[1] not in self.kokoro._voice_set and self.kokoro.ok:
                raise EngineError("Voice Kokoro itu tidak ada dalam model.", 400)
            data = self.kokoro.synth(text, voice_id.split(":", 1)[1], speed, cancelled=cancelled)
            ext, ctype = "wav", "audio/wav"
        duration = audio_duration(data, ext)
        return {"data": data, "ext": ext, "mime": ctype, "duration": duration,
                "voice": voice, "seconds": round(time.time() - started, 2),
                "chars": len(text), "text": text, "exaggeration": exaggeration,
                "stability": stability}


# ---------------------------------------------------------------------- history
class History:
    def __init__(self, limit: int = 60):
        self.dir = AUDIO_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = DATA_DIR / "history.json"
        self.limit = limit
        self._lock = threading.Lock()
        self.items = self._load()

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [x for x in raw if isinstance(x, dict) and x.get("id")][: self.limit]
        except (OSError, ValueError):
            pass
        return []

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        payload = json.dumps(self.items, ensure_ascii=False, indent=2)
        for attempt in range(3):
            try:
                tmp.write_text(payload, encoding="utf-8")
                tmp.replace(self.path)
                return
            except OSError as ex:  # e.g. antivirus briefly locking the file on Windows
                if attempt == 2:
                    print("history save failed:", ex, flush=True)
                time.sleep(0.05)

    @staticmethod
    def _unlink(item: dict) -> None:
        path = safe_audio_path(item.get("file", ""))
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def add(self, result: dict, text: str, speed: float, pitch: int,
            exaggeration: float = 0.5, stability: float = 0.5) -> dict:
        voice = result["voice"]
        generation_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:5]
        filename = f"{generation_id}.{result['ext']}"
        with self._lock:
            (self.dir / filename).write_bytes(result["data"])
            item = {
                "id": generation_id,
                "url": f"/audio/{filename}",
                "file": filename,
                "text": text,
                "voice_id": voice["id"],
                "voice": voice["name"],
                "engine": voice["engine"],
                "lang": voice["lang"],
                "accent": voice["accent"],
                "gender": voice["gender"],
                "speed": round(float(speed), 2),
                "pitch": int(pitch),
                "exaggeration": round(float(exaggeration), 2),
                "stability": round(float(stability), 2),
                "duration": round(float(result["duration"]), 2),
                "chars": result["chars"],
                "created": time.time(),
            }
            self.items.insert(0, item)
            while len(self.items) > self.limit:
                self._unlink(self.items.pop())
            self._save()
            return item

    def list(self, limit: int = 30) -> list[dict]:
        with self._lock:
            return [dict(x) for x in self.items[:limit]]

    def delete(self, generation_id: str) -> bool:
        with self._lock:
            for i, item in enumerate(self.items):
                if item.get("id") == generation_id:
                    self.items.pop(i)
                    self._unlink(item)
                    self._save()
                    return True
        return False

    def clear(self):
        with self._lock:
            for item in self.items:
                self._unlink(item)
            self.items = []
            self._save()
