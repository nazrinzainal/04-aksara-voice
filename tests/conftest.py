"""Shared fixtures. Every test server runs on a free port with its own temporary data folder,
so the real data/history.json and data/audio are never touched."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Must be set before tts_engine is imported anywhere in this process.
os.environ.setdefault("VOX_DATA_DIR", tempfile.mkdtemp(prefix="vox-unit-"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base: str):
        self.base = base

    def request(self, method: str, path: str, body=None, headers=None, raw: bytes | None = None):
        data = raw
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as ex:
            return ex.code, dict(ex.headers), ex.read()

    def json(self, method: str, path: str, body=None, headers=None, raw=None):
        status, headers_out, payload = self.request(method, path, body, headers, raw)
        return status, json.loads(payload.decode("utf-8") or "{}")


def start_server(engines_off: str, data_dir: Path):
    port = free_port()
    env = dict(os.environ, VOX_DATA_DIR=str(data_dir), VOX_DISABLE_ENGINES=engines_off, PYTHONUNBUFFERED="1")
    log_path = Path(data_dir) / "server.log"
    log = open(log_path, "w", encoding="utf-8")  # a file, not a pipe: a full pipe would block the server
    proc = subprocess.Popen([sys.executable, "app.py", "--no-browser", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    client = Client(f"http://127.0.0.1:{port}")
    client.log_path = log_path
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited:\n" + log_path.read_text(encoding="utf-8", errors="replace"))
        try:
            if client.json("GET", "/api/health")[0] == 200:
                return proc, client
        except (OSError, ValueError):
            pass
        time.sleep(0.3)
    proc.kill()
    raise RuntimeError("server did not start")


def wait_for_engines(client: Client, timeout: float = 240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, cat = client.json("GET", "/api/catalog")
        if not any(e.get("loading") for e in cat["engines"].values()):
            return cat
        time.sleep(1)
    raise RuntimeError("engines still loading")


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """Fast server: Chatterbox off (it takes ~1 min to load); Kokoro on for real synthesis."""
    data = tmp_path_factory.mktemp("vox-data")
    proc, client = start_server("chatterbox", data)
    client.data_dir = data
    wait_for_engines(client)
    yield client
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
