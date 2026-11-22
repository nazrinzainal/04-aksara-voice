"""Download the Aksara Local (English) model files into models/kokoro.

    .venv\\Scripts\\python.exe tools\\download_kokoro.py
    .venv\\Scripts\\python.exe tools\\download_kokoro.py --force   # download again

Files that already exist with the expected size are skipped. Each file is written to a temporary
name and moved into place only after its size matches, so an interrupted download never leaves a
broken model behind.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "models" / "kokoro"
BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {  # name -> expected size in bytes (model-files-v1.0 release)
    "kokoro-v1.0.onnx": 325_532_387,
    "voices-v1.0.bin": 28_214_398,
}
ATTEMPTS = 5
CHUNK = 1024 * 512


def fetch(name: str, size: int, force: bool) -> None:
    target = OUT / name
    if target.exists() and target.stat().st_size == size and not force:
        print(f"[skip] {name} ({size / 1e6:,.1f} MB already present)", flush=True)
        return
    tmp = target.with_suffix(target.suffix + ".part")
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with requests.get(BASE + name, stream=True, timeout=(30, 120),
                              headers={"User-Agent": "aksara-voice-downloader/1.0"}) as r:
                r.raise_for_status()
                done = 0
                with open(tmp, "wb") as handle:
                    for chunk in r.iter_content(CHUNK):
                        if chunk:
                            handle.write(chunk)
                            done += len(chunk)
                print(f"[{name}] {done / 1e6:,.1f} MB", flush=True)
            if tmp.stat().st_size != size:
                raise RuntimeError(f"size {tmp.stat().st_size} != expected {size}")
            tmp.replace(target)
            print(f"[done] {name}", flush=True)
            return
        except (requests.RequestException, OSError, RuntimeError) as ex:
            last = ex
            wait = 2 ** attempt
            print(f"  attempt {attempt}/{ATTEMPTS} failed: {ex}; retrying in {wait}s", flush=True)
            time.sleep(wait)
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"{name}: download failed after {ATTEMPTS} attempts: {last}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Download the Aksara Local model files.")
    parser.add_argument("--force", action="store_true", help="download even if the files exist")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        for name, size in FILES.items():
            fetch(name, size, args.force)
    except RuntimeError as ex:
        print(f"[error] {ex}\nCheck the internet connection and run this script again.", flush=True)
        return 1
    print("Aksara Local model files are ready in", OUT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
