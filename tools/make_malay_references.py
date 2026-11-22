"""Prepare the two Malaysian reference clips used by Chatterbox.

The clips are public Malay demo outputs of an open Malaysian TTS model
(`Malaysian-TTS-1.7B-v0.1`). Aksara HD uses them as speaker references so its
Malay keeps a Malaysian accent and pronunciation. They are synthetic demo audio,
not recordings of a real person.

    .venv\\Scripts\\python.exe tools\\make_malay_references.py           # skip clips that exist
    .venv\\Scripts\\python.exe tools\\make_malay_references.py --force   # rebuild both
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "models" / "chatterbox"

BASE = "https://huggingface.co/mesolitica/Malaysian-TTS-1.7B-v0.1/resolve/main/"
CLIPS = {
    "ref_ms_malaysia_f.wav": "output-idayu.mp3",
    "ref_ms_malaysia_m.wav": "output-husein.mp3",
}
ATTEMPTS = 5
MIN_SECONDS = 2.0


def fetch(url: str) -> bytes:
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = requests.get(url, timeout=(20, 120),
                                    headers={"User-Agent": "aksara-voice-references/1.1"})
            if response.status_code == 404:
                raise RuntimeError(f"404 Not Found: {url} (the file was moved or renamed upstream)")
            response.raise_for_status()
            if len(response.content) < 1000:
                raise RuntimeError(f"response too small ({len(response.content)} bytes)")
            return response.content
        except RuntimeError as ex:
            if "404" in str(ex):
                raise
            last = ex
        except requests.RequestException as ex:
            last = ex
        wait = 2 ** attempt
        print(f"  attempt {attempt}/{ATTEMPTS} failed: {last}; retrying in {wait}s", flush=True)
        time.sleep(wait)
    raise RuntimeError(f"download failed after {ATTEMPTS} attempts: {last}")


def build(name: str, remote: str, force: bool) -> None:
    import librosa  # imported lazily: slow import, and only needed when we actually convert
    import soundfile as sf

    target = OUT / name
    if target.exists() and target.stat().st_size > 1000 and not force:
        print("skip", name, "(exists; use --force to rebuild)", flush=True)
        return
    print("downloading", remote, flush=True)
    data = fetch(BASE + remote)
    try:
        samples, sr = librosa.load(io.BytesIO(data), sr=24000, mono=True)
    except Exception as ex:
        raise RuntimeError(f"cannot decode {remote} as audio: {ex}") from ex
    samples = np.asarray(samples, dtype=np.float32)
    seconds = len(samples) / sr
    if seconds < MIN_SECONDS:
        raise RuntimeError(f"{remote} is only {seconds:.1f}s; a reference needs at least {MIN_SECONDS}s")
    peak = float(np.max(np.abs(samples))) or 1.0
    samples = samples / peak * 0.9
    tmp = target.with_suffix(".tmp.wav")
    sf.write(tmp, samples, sr)
    tmp.replace(target)  # atomic: never leaves a half-written reference behind
    print("done", name, round(seconds, 2), "seconds", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the Malaysian Chatterbox reference clips.")
    parser.add_argument("--force", action="store_true", help="rebuild even if the clips exist")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    failed = []
    for name, remote in CLIPS.items():
        try:
            build(name, remote, args.force)
        except Exception as ex:  # report clearly, keep going with the other clip
            print(f"[error] {name}: {ex}", flush=True)
            failed.append(name)
    if failed:
        print("Some references were not built:", ", ".join(failed),
              "\nCheck the internet connection and run this script again.", flush=True)
        return 1
    print("Malaysian reference clips are ready in", OUT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
