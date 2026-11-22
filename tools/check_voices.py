"""Check every voice of a running Aksara Voice studio: generate, decode and analyse the audio.

    .venv\\Scripts\\python.exe tools\\check_voices.py                 # all voices, default settings
    .venv\\Scripts\\python.exe tools\\check_voices.py --settings      # + min/max settings per engine
    .venv\\Scripts\\python.exe tools\\check_voices.py --url http://127.0.0.1:7790

Uses save=false, so nothing is added to the studio's history. Exit code 1 if any check fails.
A voice passes when: HTTP 200, the audio decodes, duration is plausible for the text, and the
audio is not silent/clipped garbage.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request

import numpy as np
import soundfile as sf

TEXT = {
    "ms": "Selamat pagi semua. Hari ini kita akan menguji suara ini dengan ayat yang ringkas.",
    "en": "Good morning everyone. Today we are testing this voice with a short sentence.",
}
WORDS_PER_SECOND = (1.2, 5.5)  # plausible speech rate range (slow..fast)


def post(url: str, body: dict, timeout: float = 600):
    req = urllib.request.Request(url + "/api/tts", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(), time.time() - t
    except urllib.error.HTTPError as ex:
        return ex.code, dict(ex.headers), ex.read(), time.time() - t


def analyse(data: bytes) -> dict:
    samples, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    duration = len(mono) / sr
    rms = float(np.sqrt(np.mean(mono ** 2))) if len(mono) else 0.0
    peak = float(np.max(np.abs(mono))) if len(mono) else 0.0
    # share of 20 ms frames that carry sound (speech should have plenty)
    frame = max(1, int(sr * 0.02))
    frames = mono[: len(mono) // frame * frame].reshape(-1, frame) if len(mono) >= frame else mono.reshape(1, -1)
    active = float(np.mean(np.sqrt(np.mean(frames ** 2, axis=1)) > 0.01))
    return {"sr": sr, "duration": duration, "rms": rms, "peak": peak, "active": active}


def check(url: str, voice: dict, extra: dict | None = None, label: str = "") -> tuple[bool, str]:
    text = TEXT.get(voice["lang"], TEXT["en"])
    body = {"text": text, "voice": voice["id"], "save": False}
    body.update(extra or {})
    status, headers, data, took = post(url, body)
    name = f'{voice["name"]:<17} {label:<22}'
    if status != 200:
        try:
            msg = json.loads(data.decode()).get("error", "")
        except Exception:
            msg = data[:80]
        return False, f"FAIL {name} HTTP {status}: {msg}"
    try:
        a = analyse(data)
    except Exception as ex:
        return False, f"FAIL {name} audio does not decode: {ex}"
    words = len(text.split())
    speed = float(body.get("speed", 1.0))
    lo, hi = words / (WORDS_PER_SECOND[1] * speed), words / (WORDS_PER_SECOND[0] * speed)
    problems = []
    if not lo <= a["duration"] <= hi * 1.3:
        problems.append(f"duration {a['duration']:.1f}s outside {lo:.1f}-{hi * 1.3:.1f}s")
    if a["rms"] < 0.01 or a["peak"] < 0.05:
        problems.append(f"too quiet (rms {a['rms']:.3f}, peak {a['peak']:.2f})")
    if a["active"] < 0.35:
        problems.append(f"mostly silence ({a['active']:.0%} active)")
    ok = not problems
    line = (f"{'ok  ' if ok else 'FAIL'} {name} {headers.get('X-Ext', '?'):>3} {a['duration']:5.1f}s audio "
            f"in {took:5.1f}s  rms {a['rms']:.3f}  active {a['active']:.0%}")
    if problems:
        line += "  <- " + "; ".join(problems)
    return ok, line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:7788")
    ap.add_argument("--settings", action="store_true", help="also test min/max settings per engine")
    args = ap.parse_args()
    url = args.url.rstrip("/")
    with urllib.request.urlopen(url + "/api/catalog", timeout=30) as r:
        catalog = json.loads(r.read().decode())
    voices = catalog["voices"]
    print(f"{len(voices)} voices ({sum(v['lang'] == 'ms' for v in voices)} Malay, "
          f"{sum(v['lang'] == 'en' for v in voices)} English); engines: "
          + ", ".join(f"{k}={'ok' if e.get('ok') else 'DOWN'}{'/' + e['device'] if e.get('device') else ''}"
                      for k, e in catalog["engines"].items()))
    failures = 0
    for v in voices:
        ok, line = check(url, v)
        failures += not ok
        print(line, flush=True)
    if args.settings:
        print("--- settings (min / max) ---")
        first = {}
        for v in voices:
            first.setdefault(v["engine"], v)
        cases = {
            "edge": [({"speed": 0.7, "pitch": -20}, "speed 0.7, pitch -20"), ({"speed": 1.3, "pitch": 20}, "speed 1.3, pitch +20")],
            "kokoro": [({"speed": 0.7}, "speed 0.7"), ({"speed": 1.3}, "speed 1.3")],
            "chatterbox": [({"speed": 0.7, "exaggeration": 0.2, "stability": 0.0}, "slow, expr 0.2, stab 0"),
                           ({"speed": 1.3, "exaggeration": 0.9, "stability": 1.0}, "fast, expr 0.9, stab 1")],
        }
        for engine, voice in first.items():
            for extra, label in cases.get(engine, []):
                ok, line = check(url, voice, extra, label)
                failures += not ok
                print(line, flush=True)
    print(f"RESULT: {'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
