"""API tests against a real Aksara Voice server (temporary data folder, Chatterbox disabled)."""
from __future__ import annotations

import json
import socket
import threading
import time
from urllib.parse import urlparse

import pytest

HEART = "kokoro:af_heart"


def tts(server, **body):
    payload = {"text": "Hello from the test suite.", "voice": HEART, "save": True}
    payload.update(body)
    return server.request("POST", "/api/tts", payload)


# ------------------------------------------------------------------ basics
def test_index_and_static(server):
    status, headers, body = server.request("GET", "/")
    assert status == 200 and b"Aksara Voice" in body
    assert "Content-Security-Policy" in headers
    for path in ("/app.js", "/style.css", "/favicon.svg"):
        assert server.request("GET", path)[0] == 200


def test_head_static_and_unknown_method(server):
    status, headers, body = server.request("HEAD", "/")
    assert status == 200 and body == b"" and int(headers["Content-Length"]) > 0
    assert server.json("PUT", "/api/tts", raw=b"{}")[0] == 405


def test_catalog_shape(server):
    status, cat = server.json("GET", "/api/catalog")
    assert status == 200
    ids = {v["id"] for v in cat["voices"]}
    assert HEART in ids
    assert cat["defaults"] == {"ms": "chatterbox:ms-malaysia-f", "en": "kokoro:af_heart"}
    assert cat["max_chars"] >= 100
    assert set(cat["engines"]) == {"edge", "kokoro", "chatterbox"}
    # Chatterbox is disabled in this fixture, so its voices must be hidden, not broken.
    assert not any(v["engine"] == "chatterbox" for v in cat["voices"])
    assert sum(v["lang"] == "en" for v in cat["voices"]) == 16


# -------------------------------------------------------------- validation
@pytest.mark.parametrize("raw, ctype, code, fragment", [
    (b"{bad", "application/json", 400, "JSON tidak sah"),
    (b'{"text":"hi","voice":"kokoro:af_heart","speed":NaN}', "application/json", 400, "JSON tidak sah"),
    (b"[1,2]", "application/json", 400, "objek JSON"),
    (b'{"text":"hi","voice":"kokoro:af_heart"}', "text/plain", 415, "Content-Type"),
    (b"", "application/json", 400, "Body JSON"),
])
def test_tts_rejects_bad_bodies(server, raw, ctype, code, fragment):
    status, body = server.json("POST", "/api/tts", raw=raw, headers={"Content-Type": ctype})
    assert status == code and fragment in body["error"]


@pytest.mark.parametrize("override, fragment", [
    ({"voice": "nope:none"}, "Voice tidak dikenali"),
    ({"voice": ""}, "'voice'"),
    ({"text": "   "}, "Teks kosong"),
    ({"text": 123}, "'text'"),
    ({"text": "a" * 3001}, "terlalu panjang"),
    ({"speed": "abc"}, "'speed' mesti nombor"),
    ({"speed": True}, "'speed' mesti nombor"),
    ({"speed": 5}, "'speed' mesti antara"),
    ({"pitch": -21}, "'pitch' mesti antara"),
    ({"stability": 1.5}, "'stability' mesti antara"),
    ({"exaggeration": 0}, "'exaggeration' mesti antara"),
    ({"save": "no"}, "'save'"),
])
def test_tts_field_validation(server, override, fragment):
    payload = {"text": "hi", "voice": HEART}
    payload.update(override)
    status, body = server.json("POST", "/api/tts", payload)
    assert status == 400, body
    assert fragment in body["error"]
    assert "Traceback" not in body["error"]


def test_body_size_limit(server):
    # Only the headers are sent: the server must refuse from Content-Length alone, without reading
    # the body (sending 400 KB would race with the early 413 and get the socket reset on Windows).
    host, port = urlparse(server.base).hostname, urlparse(server.base).port
    with socket.create_connection((host, port), timeout=10) as s:
        s.sendall(b"POST /api/tts HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                  b"Content-Length: 400000\r\n\r\n")
        chunks = []
        while chunk := s.recv(4096):  # HTTP/1.0: the server closes after the response
            chunks.append(chunk)
        reply = b"".join(chunks).decode("utf-8", "replace")
    assert " 413 " in reply.splitlines()[0]
    assert "terlalu besar" in reply


def test_missing_content_length_is_rejected(server):
    host, port = urlparse(server.base).hostname, urlparse(server.base).port
    with socket.create_connection((host, port), timeout=10) as s:
        s.sendall(b"POST /api/history/delete HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n\r\n")
        reply = s.recv(4096).decode("latin-1")
    assert " 411 " in reply.splitlines()[0]


# ---------------------------------------------------------------- security
def test_cross_site_post_is_blocked(server):
    status, body = server.json("POST", "/api/history/clear", headers={"Origin": "https://evil.example"})
    assert status == 403
    status, _ = server.json("POST", "/api/history/clear", headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403


def test_dns_rebinding_host_is_blocked(server):
    status, _ = server.json("GET", "/api/catalog", headers={"Host": "evil.example"})
    assert status == 403


@pytest.mark.parametrize("path", [
    "/audio/../app.py", "/audio/..%2f..%2fapp.py", "/audio/..%5capp.py", "/audio/%2e%2e%2fconfig.json",
    "/audio/C:%5cWindows%5cwin.ini", "/audio/history.json", "/audio/.wav", "/audio/x.py",
])
def test_audio_path_traversal(server, path):
    host, port = urlparse(server.base).hostname, urlparse(server.base).port
    with socket.create_connection((host, port), timeout=10) as s:  # raw socket: no client-side normalising
        s.sendall(f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
        reply = s.recv(4096).decode("latin-1")
    assert " 404 " in reply.splitlines()[0], reply


def test_history_limit_param(server):
    assert server.json("GET", "/api/history?limit=abc")[0] == 400
    assert server.json("GET", "/api/history?limit=5")[0] == 200


# ------------------------------------------------- synthesis + history + range
def test_generate_history_range_delete(server):
    status, headers, audio = tts(server)
    assert status == 200 and headers["Content-Type"] == "audio/wav" and audio[:4] == b"RIFF"
    assert float(headers["X-Duration"]) > 0.3
    hid, url = headers["X-History-Id"], headers["X-Audio-Url"]
    _, hist = server.json("GET", "/api/history")
    assert hist["items"][0]["id"] == hid

    status, h, body = server.request("GET", url)
    assert status == 200 and body == audio and h["Accept-Ranges"] == "bytes"
    size = len(audio)
    status, h, body = server.request("HEAD", url)
    assert status == 200 and body == b"" and int(h["Content-Length"]) == size
    status, h, body = server.request("GET", url, headers={"Range": "bytes=0-99"})
    assert status == 206 and body == audio[:100] and h["Content-Range"] == f"bytes 0-99/{size}"
    status, h, body = server.request("GET", url, headers={"Range": "bytes=-50"})
    assert status == 206 and body == audio[-50:] and h["Content-Range"] == f"bytes {size - 50}-{size - 1}/{size}"
    status, h, body = server.request("GET", url, headers={"Range": "bytes=100-"})
    assert status == 206 and body == audio[100:]
    status, h, _ = server.request("GET", url, headers={"Range": f"bytes={size}-"})
    assert status == 416 and h["Content-Range"] == f"bytes */{size}"
    status, _, body = server.request("GET", url, headers={"Range": "bytes=0-1,5-6"})
    assert status == 200 and body == audio  # multi-range ignored -> full file

    status, body = server.json("POST", "/api/history/delete", {"id": hid})
    assert status == 200 and body["ok"]
    assert server.request("GET", url)[0] == 404
    assert server.json("POST", "/api/history/delete", {"id": hid})[0] == 404


def test_preview_is_not_saved(server):
    before = len(server.json("GET", "/api/history?limit=100")[1]["items"])
    status, headers, _ = tts(server, save=False)
    assert status == 200 and "X-History-Id" not in headers
    assert len(server.json("GET", "/api/history?limit=100")[1]["items"]) == before


def test_concurrent_generations_and_clear(server):
    results = []

    def worker(i):
        results.append(tts(server, text=f"Concurrent request number {i}.")[0])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 4
    items = server.json("GET", "/api/history?limit=100")[1]["items"]
    assert len({i["id"] for i in items}) == len(items) >= 4
    history_file = server.data_dir / "history.json"
    assert len(json.loads(history_file.read_text(encoding="utf-8"))) == len(items)

    assert server.json("POST", "/api/history/clear")[0] == 200
    assert server.json("GET", "/api/history")[1]["items"] == []
    assert not list((server.data_dir / "audio").glob("*.wav"))


def test_client_abort_is_not_saved_and_server_survives(server):
    before = len(server.json("GET", "/api/history?limit=100")[1]["items"])
    text = " ".join(f"This is sentence number {i} of a long cancel test." for i in range(55))[:2900]
    raw = json.dumps({"text": text, "voice": "kokoro:am_michael"}).encode()
    host, port = urlparse(server.base).hostname, urlparse(server.base).port
    s = socket.create_connection((host, port), timeout=10)
    s.sendall(b"POST /api/tts HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
              + f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
    time.sleep(1.0)
    s.close()  # the browser's AbortController does the same
    deadline = time.time() + 30
    while time.time() < deadline and "cancelled" not in server.log_path.read_text(encoding="utf-8", errors="replace"):
        time.sleep(0.5)
    assert "tts cancelled by client" in server.log_path.read_text(encoding="utf-8", errors="replace")
    assert len(server.json("GET", "/api/history?limit=100")[1]["items"]) == before
    assert tts(server, save=False)[0] == 200
