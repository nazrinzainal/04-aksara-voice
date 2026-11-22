"""Pure unit tests (no server). VOX_DATA_DIR points at a temp folder (see conftest)."""
from __future__ import annotations

import json
import threading

import pytest

import app
import tts_engine


@pytest.mark.parametrize("header, size, expected", [
    (None, 100, None),
    ("bytes=0-9", 100, (0, 9)),
    ("bytes=90-", 100, (90, 99)),
    ("bytes=-10", 100, (90, 99)),
    ("bytes=-500", 100, (0, 99)),
    ("bytes=0-999", 100, (0, 99)),
    ("bytes=100-", 100, "unsatisfiable"),
    ("bytes=-0", 100, "unsatisfiable"),
    ("bytes=5-2", 100, "unsatisfiable"),
    ("bytes=0-1,4-5", 100, None),
    ("items=0-1", 100, None),
    ("bytes=-", 100, None),
])
def test_parse_range(header, size, expected):
    assert app.parse_range(header, size) == expected


def test_parse_tts_body_defaults_and_rounding():
    out = app.parse_tts_body({"text": "x", "voice": " kokoro:af_heart ", "pitch": 3.6})
    assert out == {"text": "x", "voice": "kokoro:af_heart", "speed": 1.0, "pitch": 4,
                   "exaggeration": 0.5, "stability": 0.5, "save": True}


@pytest.mark.parametrize("name", ["../x.wav", "..\\x.wav", "a/b.wav", "x.py", ".wav", "", "C:\\x.wav", "x..wav"])
def test_safe_audio_path_rejects(name):
    assert tts_engine.safe_audio_path(name) is None


def test_safe_audio_path_accepts():
    p = tts_engine.safe_audio_path("20261121-004633-81e1b.wav")
    assert p is not None and p.parent == tts_engine.AUDIO_DIR.resolve()


def _result(i):
    return {"voice": tts_engine.VOICES[4], "ext": "wav", "data": b"RIFF" + bytes(2000), "duration": 1.0,
            "chars": 5, "text": f"t{i}"}


def test_history_threaded_add_trim_and_tampered_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_engine, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(tts_engine, "DATA_DIR", tmp_path)
    outside = tmp_path / "keep.wav"
    outside.write_bytes(b"x")
    hist = tts_engine.History(limit=10)
    hist.items.append({"id": "evil", "file": "../keep.wav"})  # tampered history.json entry

    threads = [threading.Thread(target=hist.add, args=(_result(i), f"t{i}", 1.0, 0)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(hist.items) == 10
    assert len(list((tmp_path / "audio").glob("*.wav"))) == 10
    assert outside.exists(), "a tampered entry must never delete files outside data/audio"
    saved = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert [x["id"] for x in saved] == [x["id"] for x in hist.items]

    hist.clear()
    assert hist.items == [] and not list((tmp_path / "audio").glob("*.wav"))


def test_match_loudness_restores_level_after_time_stretch():
    import librosa
    import numpy as np

    sr = 24000
    t = np.arange(sr * 2) / sr
    voice = (0.3 * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)
    rms = lambda x: float(np.sqrt(np.mean(np.square(x))))  # noqa: E731
    for rate in (0.7, 1.3):
        stretched = librosa.effects.time_stretch(voice, rate=rate)
        fixed = tts_engine.match_loudness(voice, stretched)
        assert abs(rms(fixed) - rms(voice)) / rms(voice) < 0.02
        assert float(np.max(np.abs(fixed))) <= 0.99


def test_match_loudness_never_clips_and_handles_silence():
    import numpy as np

    loud = np.full(1000, 0.9, dtype=np.float32)
    quiet = np.full(1000, 0.05, dtype=np.float32)
    quiet[0] = 0.5  # a peak that would clip if scaled to the reference RMS
    out = tts_engine.match_loudness(loud, quiet)
    assert float(np.max(np.abs(out))) <= 0.99 + 1e-6
    silent = np.zeros(10, dtype=np.float32)
    assert np.array_equal(tts_engine.match_loudness(loud, silent), silent)
